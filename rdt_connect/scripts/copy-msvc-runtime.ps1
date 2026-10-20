[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [ValidateSet('x64', 'ARM64')]
    [string]$TargetArch,

    [Parameter(Mandatory)]
    [string]$DestinationDir
)

$ErrorActionPreference = 'Stop'

$vswhere = Join-Path ${env:ProgramFiles(x86)} `
    'Microsoft Visual Studio\Installer\vswhere.exe'
if (-not (Test-Path $vswhere)) {
    throw "Cannot find Visual Studio locator: $vswhere"
}

$component = if ($TargetArch -eq 'ARM64') {
    'Microsoft.VisualStudio.Component.VC.Tools.ARM64'
} else {
    'Microsoft.VisualStudio.Component.VC.Tools.x86.x64'
}

$vsInstall = (& $vswhere -latest -products '*' `
    -requires $component -property installationPath | Select-Object -First 1)
if ($LASTEXITCODE -ne 0 -or -not $vsInstall) {
    throw "Cannot locate the Visual Studio toolchain for $TargetArch."
}

$redistRoot = Join-Path $vsInstall 'VC\Redist\MSVC'
$versions = Get-ChildItem -LiteralPath $redistRoot -Directory |
    Where-Object { $_.Name -match '^\d+\.\d+\.\d+$' } |
    Sort-Object { [version]$_.Name } -Descending

$arch = if ($TargetArch -eq 'ARM64') { 'arm64' } else { 'x64' }
$archRoot = $null
$ompDll = $null
$crtDll = $null

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

if (-not $ompDll -or -not $crtDll) {
    throw "Could not locate matching $TargetArch MSVC CRT and OpenMP redistributable DLLs."
}

New-Item -ItemType Directory -Force -Path $DestinationDir | Out-Null

foreach ($dllDir in @($crtDll.Directory, $ompDll.Directory)) {
    Get-ChildItem -LiteralPath $dllDir.FullName -File -Filter '*.dll' |
        ForEach-Object {
            Copy-Item -LiteralPath $_.FullName `
                -Destination $DestinationDir -Force
        }
}

if (-not (Test-Path (Join-Path $DestinationDir 'vcomp140.dll'))) {
    throw 'vcomp140.dll was not staged successfully.'
}

Write-Host "Staged MSVC CRT and OpenMP runtime for $TargetArch from $archRoot"
