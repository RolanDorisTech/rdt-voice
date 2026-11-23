[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("x64", "arm64")]
    [string]$Architecture,

    [Parameter(Mandatory = $true)]
    [string]$Destination
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.Net.Http
Add-Type -AssemblyName System.IO.Compression.FileSystem

$pinPath = Join-Path $PSScriptRoot "../config/windows-ffmpeg-pins.json"
$pins = Get-Content -LiteralPath $pinPath -Raw | ConvertFrom-Json
if ($pins.schema -ne "rdt-windows-ffmpeg-pin-v1") {
    throw "Unsupported FFmpeg pin manifest schema."
}

$release = $pins.release
$ffmpeg = $pins.ffmpeg
$archPin = $pins.architectures.$Architecture
if (-not $archPin) { throw "No FFmpeg pin exists for $Architecture." }

$allowedHosts = @(
    "github.com",
    "release-assets.githubusercontent.com",
    "objects.githubusercontent.com",
    "github-releases.githubusercontent.com"
)

function Assert-TrustedUri([Uri]$Uri) {
    if (-not $Uri.IsAbsoluteUri -or $Uri.Scheme -ne "https" -or
        $Uri.Port -ne 443 -or $Uri.UserInfo -or $Uri.Fragment) {
        throw "Rejected non-HTTPS or malformed download URI."
    }
    if ($allowedHosts -notcontains $Uri.DnsSafeHost.ToLowerInvariant()) {
        throw "Rejected unapproved HTTPS redirect host: $($Uri.DnsSafeHost)"
    }
    if ($Uri.DnsSafeHost -eq "github.com" -and $Uri.Query) {
        throw "Initial GitHub release URL must not contain a query string."
    }
}

function Invoke-TrustedHttpsDownload {
    param(
        [Uri]$Uri,
        [string]$Path,
        [long]$MaximumBytes
    )

    Assert-TrustedUri $Uri
    $handler = [System.Net.Http.HttpClientHandler]::new()
    $handler.AllowAutoRedirect = $false
    $handler.AutomaticDecompression = [System.Net.DecompressionMethods]::None
    $client = [System.Net.Http.HttpClient]::new($handler)
    $client.Timeout = [TimeSpan]::FromMinutes(10)
    $current = $Uri
    $redirectHosts = @()
    $partial = "$Path.partial"

    try {
        for ($hop = 0; $hop -le 5; $hop++) {
            Assert-TrustedUri $current
            $request = [System.Net.Http.HttpRequestMessage]::new(
                [System.Net.Http.HttpMethod]::Get, $current
            )
            $request.Headers.UserAgent.ParseAdd("RDT-Connect-CI/1.0")
            $response = $null

            try {
                $response = $client.SendAsync(
                    $request,
                    [System.Net.Http.HttpCompletionOption]::ResponseHeadersRead
                ).GetAwaiter().GetResult()

                $status = [int]$response.StatusCode
                if ($status -in @(301, 302, 303, 307, 308)) {
                    if ($hop -eq 5 -or -not $response.Headers.Location) {
                        throw "FFmpeg download exceeded redirect limit or omitted Location."
                    }
                    $next = [Uri]::new($current, $response.Headers.Location)
                    Assert-TrustedUri $next
                    $redirectHosts += $next.DnsSafeHost
                    Write-Host "FFMPEG_REDIRECT_HOP=$($hop + 1) HOST=$($next.DnsSafeHost)"
                    $current = $next
                    continue
                }

                if ($status -ne 200) {
                    throw "Pinned FFmpeg download returned HTTP $status from $($current.DnsSafeHost)."
                }

                $declaredLength = $response.Content.Headers.ContentLength
                if ($null -ne $declaredLength -and $declaredLength -gt $MaximumBytes) {
                    throw "Download Content-Length exceeds the configured size limit."
                }

                $inputStream = $response.Content.ReadAsStreamAsync().GetAwaiter().GetResult()
                $outputStream = [System.IO.File]::Open(
                    $partial,
                    [System.IO.FileMode]::Create,
                    [System.IO.FileAccess]::Write,
                    [System.IO.FileShare]::None
                )
                $total = 0L
                $buffer = New-Object byte[] (1024 * 1024)
                try {
                    while (($read = $inputStream.Read($buffer, 0, $buffer.Length)) -gt 0) {
                        $total += $read
                        if ($total -gt $MaximumBytes) {
                            throw "Downloaded response exceeds the configured size limit."
                        }
                        $outputStream.Write($buffer, 0, $read)
                    }
                    $outputStream.Flush($true)
                }
                finally {
                    $outputStream.Dispose()
                    $inputStream.Dispose()
                }

                if ($total -le 0) { throw "Downloaded file is empty." }
                Move-Item -LiteralPath $partial -Destination $Path -Force
                return [pscustomobject]@{
                    Bytes = $total
                    FinalHost = $current.DnsSafeHost
                    RedirectHosts = @($redirectHosts)
                }
            }
            finally {
                if ($response) { $response.Dispose() }
                $request.Dispose()
            }
        }
        throw "FFmpeg download redirect loop."
    }
    catch {
        Remove-Item -LiteralPath $partial -Force -ErrorAction SilentlyContinue
        throw
    }
    finally {
        $client.Dispose()
        $handler.Dispose()
    }
}

function Get-PeMachine([string]$Path) {
    $stream = [System.IO.File]::OpenRead($Path)
    $reader = [System.IO.BinaryReader]::new($stream)
    try {
        if ($reader.ReadUInt16() -ne 0x5A4D) { throw "Invalid PE/DOS header: $Path" }
        $stream.Position = 0x3C
        $offset = $reader.ReadInt32()
        if ($offset -lt 64 -or $offset -gt ($stream.Length - 6)) {
            throw "Invalid PE header offset: $Path"
        }
        $stream.Position = $offset
        if ($reader.ReadUInt32() -ne 0x00004550) { throw "Invalid PE signature: $Path" }
        return [int]$reader.ReadUInt16()
    }
    finally {
        $reader.Dispose()
        $stream.Dispose()
    }
}

$repo = [string]$release.repository
$tag = [string]$release.tag
$asset = [string]$archPin.asset
if ($repo -ne "FFmpeg-Builds/FFmpeg-Builds" -or
    $tag -ne "autobuild-2026-11-23-12-43" -or
    $asset -notmatch '^ffmpeg-N-129241-g05c674c913-win(64|arm64)-lgpl-shared\.zip$') {
    throw "FFmpeg release identity is not the reviewed immutable pin."
}
if ($Architecture -eq "x64" -and $asset -notmatch "win64-") {
    throw "x64 asset name does not match target architecture."
}
if ($Architecture -eq "arm64" -and $asset -notmatch "winarm64-") {
    throw "ARM64 asset name does not match target architecture."
}

$baseUrl = "https://github.com/$repo/releases/download/$tag"
$work = Join-Path $env:RUNNER_TEMP "rdt-ffmpeg-$Architecture-verified"
$archive = Join-Path $work $asset
$checksumFile = Join-Path $work ([string]$release.checksum_asset)
$extract = Join-Path $work "extracted"
$minimumBytes = [long]$ffmpeg.minimum_asset_bytes
$maximumBytes = [long]$ffmpeg.maximum_asset_bytes

Remove-Item -LiteralPath $work -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force -Path $work | Out-Null

try {
    echo "FFMPEG_RELEASE_TAG=$tag"
    echo "FFMPEG_RELEASE_COMMIT=$($release.release_commit)"
    echo "FFMPEG_SOURCE_COMMIT=$($ffmpeg.source_commit)"
    echo "FFMPEG_VARIANT=$($ffmpeg.build_variant)"

    $checksumUrl = [Uri]("$baseUrl/$($release.checksum_asset)")
    $checksumResult = Invoke-TrustedHttpsDownload `
        -Uri $checksumUrl -Path $checksumFile -MaximumBytes 2000000
    $checksumHash = (Get-FileHash -LiteralPath $checksumFile -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($checksumHash -ne ([string]$release.checksum_sha256).ToLowerInvariant()) {
        throw "Official FFmpeg checksum manifest SHA-256 mismatch."
    }
    Write-Host "FFMPEG_CHECKSUM_MANIFEST_SHA256=$checksumHash"

    $checksumRows = @()
    foreach ($line in Get-Content -LiteralPath $checksumFile) {
        if ($line -match '^\s*([0-9a-fA-F]{64})\s+\*?(.+?)\s*$' -and $Matches[2] -ceq $asset) {
            $checksumRows += $Matches[1].ToLowerInvariant()
        }
    }
    if ($checksumRows.Count -ne 1) {
        throw "Official checksum manifest must contain exactly one entry for $asset."
    }
    $expectedHash = $checksumRows[0]

    $assetUrl = [Uri]("$baseUrl/$asset")
    $download = Invoke-TrustedHttpsDownload `
        -Uri $assetUrl -Path $archive -MaximumBytes $maximumBytes
    if ($download.Bytes -lt $minimumBytes) {
        throw "FFmpeg shared archive is smaller than the reviewed minimum size."
    }
    $actualHash = (Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($actualHash -ne $expectedHash) {
        throw "FFmpeg archive SHA-256 mismatch; refusing to extract."
    }
    Write-Host "FFMPEG_ARCHIVE_SHA256_VERIFIED=$actualHash"
    Write-Host "FFMPEG_ARCHIVE_BYTES=$($download.Bytes)"
    Write-Host "FFMPEG_FINAL_DOWNLOAD_HOST=$($download.FinalHost)"

    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $zip = [System.IO.Compression.ZipFile]::OpenRead($archive)
    $expandedBytes = 0L
    try {
        if ($zip.Entries.Count -gt 30000) { throw "FFmpeg archive contains too many entries." }
        foreach ($entry in $zip.Entries) {
            $name = $entry.FullName.Replace('\', '/')
            if ([System.IO.Path]::IsPathRooted($name) -or $name.StartsWith("/") -or
                $name -match '(^|/)\.\.(/|$)' -or $name -match '^[A-Za-z]:') {
                throw "Unsafe path in FFmpeg archive: $name"
            }
            $unixType = (($entry.ExternalAttributes -shr 16) -band 0xF000)
            if ($unixType -eq 0xA000) { throw "Symlink entry rejected in FFmpeg archive: $name" }
            $expandedBytes += [long]$entry.Length
            if ($expandedBytes -gt 1000000000) { throw "FFmpeg archive expands beyond 1 GB." }
        }
    }
    finally { $zip.Dispose() }

    Expand-Archive -LiteralPath $archive -DestinationPath $extract -Force
    $ffmpegExe = Get-ChildItem -LiteralPath $extract -Filter ffmpeg.exe -File -Recurse | Select-Object -First 1
    $ffprobeExe = Get-ChildItem -LiteralPath $extract -Filter ffprobe.exe -File -Recurse | Select-Object -First 1
    if (-not $ffmpegExe -or -not $ffprobeExe -or
        $ffmpegExe.Directory.FullName -ne $ffprobeExe.Directory.FullName) {
        throw "FFmpeg archive is missing matching ffmpeg.exe and ffprobe.exe."
    }

    $expectedMachine = [int]$archPin.pe_machine
    foreach ($binary in @($ffmpegExe, $ffprobeExe)) {
        $machine = Get-PeMachine $binary.FullName
        if ($machine -ne $expectedMachine) {
            throw "$($binary.Name) PE machine mismatch: expected $expectedMachine, got $machine."
        }
    }

    $dlls = @(Get-ChildItem -LiteralPath $ffmpegExe.Directory.FullName -Filter "*.dll" -File)
    foreach ($required in @("avcodec", "avformat", "avutil")) {
        if (-not ($dlls | Where-Object { $_.Name -match "^$required(-\d+)?\.dll$" })) {
            throw "LGPL shared FFmpeg dependency DLL missing: $required."
        }
    }

    $versionText = (& $ffmpegExe.FullName -version 2>&1 | Out-String)
    if ($LASTEXITCODE -ne 0 -or $versionText -notmatch [regex]::Escape([string]$ffmpeg.version)) {
        throw "FFmpeg binary version does not match the pinned source revision."
    }
    $buildConf = (& $ffmpegExe.FullName -buildconf 2>&1 | Out-String)
    if ($LASTEXITCODE -ne 0 -or $buildConf -notmatch '--enable-shared') {
        throw "FFmpeg shared build configuration could not be verified."
    }
    if ($buildConf -match '--enable-gpl|--enable-nonfree') {
        throw "GPL/nonfree FFmpeg build flags are forbidden by the approved LGPL pin."
    }
    $probeVersion = (& $ffprobeExe.FullName -version 2>&1 | Out-String)
    if ($LASTEXITCODE -ne 0 -or $probeVersion -notmatch [regex]::Escape([string]$ffmpeg.version)) {
        throw "FFprobe binary version does not match the pinned source revision."
    }

    New-Item -ItemType Directory -Force -Path $Destination | Out-Null
    Copy-Item -LiteralPath $ffmpegExe.FullName -Destination (Join-Path $Destination "ffmpeg.exe") -Force
    Copy-Item -LiteralPath $ffprobeExe.FullName -Destination (Join-Path $Destination "ffprobe.exe") -Force
    foreach ($dll in $dlls) {
        Copy-Item -LiteralPath $dll.FullName -Destination (Join-Path $Destination $dll.Name) -Force
    }

    $licenseFiles = @(Get-ChildItem -LiteralPath $extract -File -Recurse |
        Where-Object { $_.Name -match '^(LICENSE|COPYING|NOTICE)(\..*)?$' })
    if (-not ($licenseFiles | Where-Object { $_.Name -match '^LICENSE(\.txt)?$' })) {
        throw "FFmpeg LGPL archive license file is missing."
    }
    foreach ($file in $licenseFiles) {
        $target = Join-Path $Destination $file.Name
        if (-not (Test-Path -LiteralPath $target)) {
            Copy-Item -LiteralPath $file.FullName -Destination $target -Force
        }
    }

    $provenance = [ordered]@{
        schema = "rdt-ffmpeg-binary-provenance-v1"
        architecture = $Architecture
        binary_version = $ffmpeg.version
        variant = $ffmpeg.build_variant
        spdx_license = $ffmpeg.spdx_license
        release_repository = $repo
        release_tag = $tag
        release_commit = $release.release_commit
        asset = $asset
        asset_sha256 = $actualHash
        asset_bytes = $download.Bytes
        checksum_manifest_sha256 = $checksumHash
        ffmpeg_source = "https://github.com/FFmpeg/FFmpeg/tree/$($ffmpeg.source_commit)"
        ffmpeg_source_commit = $ffmpeg.source_commit
        builder_source = "https://github.com/FFmpeg-Builds/FFmpeg-Builds/tree/$($release.release_commit)"
        license_reference = $ffmpeg.license_url
        download_final_host = $download.FinalHost
        pe_machine = $expectedMachine
        dll_count = $dlls.Count
    }
    $provenance | ConvertTo-Json -Depth 6 |
        Set-Content -LiteralPath (Join-Path $Destination "RDT-FFmpeg-PROVENANCE.json") -Encoding utf8

    @"
RDT Connect Windows FFmpeg distribution
Variant: LGPL shared build
SPDX: LGPL-2.1-or-later
FFmpeg version: $($ffmpeg.version)
FFmpeg source commit: $($ffmpeg.source_commit)
Source archive: https://github.com/FFmpeg/FFmpeg/archive/$($ffmpeg.source_commit).zip
Builder repository commit: $($release.release_commit)
Builder source archive: https://github.com/FFmpeg-Builds/FFmpeg-Builds/archive/$($release.release_commit).zip
Official binary release: $baseUrl/$asset
Official checksum manifest SHA-256: $checksumHash
Binary SHA-256: $actualHash

The FFmpeg tools use their separately distributed shared FFmpeg DLLs.
The corresponding FFmpeg source revision and build scripts are identified above.
See the included LICENSE/COPYING files and the FFmpeg legal guidance:
$($ffmpeg.license_url)
"@ | Set-Content -LiteralPath (Join-Path $Destination "RDT-FFmpeg-SOURCE-AND-LICENSE.txt") -Encoding utf8

    Write-Host "FFMPEG_PE_MACHINE_VERIFIED=$expectedMachine"
    Write-Host "FFMPEG_SHARED_DLL_COUNT=$($dlls.Count)"
    Write-Host "FFMPEG_BUILD_CONFIGURATION=LGPL_SHARED_NO_GPL_NO_NONFREE"
    Write-Host "FFMPEG_LICENSE_FILES=$($licenseFiles.Count)"
    Write-Host "FFMPEG_PROVENANCE=RDT-FFmpeg-PROVENANCE.json"
    Write-Host "PASS: pinned FFmpeg $Architecture payload verified before packaging."
}
finally {
    Remove-Item -LiteralPath $work -Recurse -Force -ErrorAction SilentlyContinue
}
