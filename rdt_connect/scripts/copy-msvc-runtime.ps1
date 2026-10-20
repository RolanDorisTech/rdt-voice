[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [ValidateSet('x64', 'ARM64')]
    [string]$TargetArch,

    [Parameter(Mandatory)]
    [string]$DestinationDir
)

$ErrorActionPreference = 'Stop'
$arch = if ($TargetArch -eq 'ARM64') { 'arm64' } else { 'x64' }

# Search all standard Visual Studio installation locations.
# Do not depend on vswhere product/component filters: hosted images vary.
$programFilesDirs = @(
    $env:ProgramFiles
    ${env:ProgramFiles(x86)}
) | Where-Object { $_ } | Select-Object -Unique

$redistPatterns = @(
    foreach ($base in $programFilesDirs) {
        Join-Path $base 'Microsoft Visual Studio\*\*\VC\Redist\MSVC'
    }
)

$redistRoots = @(
    Get-ChildItem -Path $redistPatterns -Directory -ErrorAction SilentlyContinue |
        Sort-Object FullName -Descending
)

if (-not $redistRoots) {
    throw "No Visual Studio MSVC redistributable folders found. Searched: $($redistPatterns -join '; ')"
}

$archRoot = $null
$ompDll = $null
$crtDll = $null

foreach ($root in $redistRoots) {
    $versions = Get-ChildItem -LiteralPath $root.FullName -Directory |
        Where-Object { $_.Name -match '^\d+\.\d+\.\d+$' } |
        Sort-Object { [version]$_.Name } -Descending

    foreach ($version in $versions) {
        $candidateRoot = Join-Path $version.FullName $arch
        if (-not (Test-Path $candidateRoot)) { continue }

        $candidateOmp = Get-ChildItem -LiteralPath $candidateRoot `
            -Recurse -File -Filter 'vcomp140.dll' |
            Select-Object -First 1

        $candidateCrt = Get-ChildItem -LiteralPath $candidateRoot `
            -Recurse -File -Filter 'vcruntime140.dll' |
            Where-Object { $_.Directory.Name -match '^Microsoft\.VC\d+\.CRT$' } |
            Select-Object -First 1

        if ($candidateOmp -and $candidateCrt) {
            $archRoot = $candidateRoot
            $ompDll = $candidateOmp
            $crtDll = $candidateCrt
            break
        }
    }

    if ($ompDll -and $crtDll) { break }
}

if (-not $ompDll -or -not $crtDll) {
    throw "Could not locate matching $TargetArch vcruntime140.dll and vcomp140.dll in the discovered redistributable folders."
}

New-Item -ItemType Directory -Force -Path $DestinationDir | Out-Null

$runtimeDirs = @(
    $crtDll.Directory.FullName
    $ompDll.Directory.FullName
) | Select-Object -Unique

foreach ($runtimeDir in $runtimeDirs) {
    Get-ChildItem -LiteralPath $runtimeDir -File -Filter '*.dll' |
        ForEach-Object {
            Copy-Item -LiteralPath $_.FullName -Destination $DestinationDir -Force
        }
}

foreach ($required in @('vcruntime140.dll', 'vcomp140.dll')) {
    if (-not (Test-Path (Join-Path $DestinationDir $required))) {
        throw "Runtime staging failed: $required was not copied."
    }
}

Write-Host "Staged $TargetArch MSVC CRT and OpenMP runtime."
Write-Host "Source: $archRoot"
