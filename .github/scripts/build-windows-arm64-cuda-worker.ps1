[CmdletBinding()]
param(
  [Parameter(Mandatory = $true)][string]$OutputDirectory
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

$hostArchitecture = [System.Runtime.InteropServices.RuntimeInformation]::ProcessArchitecture.ToString()
if ($hostArchitecture -ne 'X64') {
  throw "The ARM64 CUDA cross-worker must be built on an x64 Windows host; got $hostArchitecture."
}

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '../..')).Path
$pinFile = Join-Path $repoRoot 'backend/whisper_cpp/engine-pins.json'
$pins = Get-Content -LiteralPath $pinFile -Raw | ConvertFrom-Json
$sourceCommit = [string]$pins.source_commit
$sourceRepository = [string]$pins.source_repository
if ($sourceCommit -notmatch '^[0-9a-f]{40}$' -or
    $sourceRepository -notmatch '^https://') {
  throw 'The pinned whisper.cpp repository or commit is invalid.'
}

$programFilesX86 = [Environment]::GetEnvironmentVariable('ProgramFiles(x86)')
$vswhere = Join-Path $programFilesX86 'Microsoft Visual Studio/Installer/vswhere.exe'
if (-not (Test-Path -LiteralPath $vswhere -PathType Leaf)) {
  throw "Visual Studio locator is missing: $vswhere"
}
$vsInstall = & $vswhere -latest -products '*' `
  -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 Microsoft.VisualStudio.Component.VC.Tools.ARM64 `
  -property installationPath | Select-Object -First 1
if (-not $vsInstall -or -not (Test-Path -LiteralPath $vsInstall)) {
  throw 'Visual Studio x64-hosted ARM64 MSVC tools are not installed.'
}
$devShell = Join-Path $vsInstall 'Common7/Tools/Microsoft.VisualStudio.DevShell.dll'
if (-not (Test-Path -LiteralPath $devShell -PathType Leaf)) {
  throw "Visual Studio developer shell is missing: $devShell"
}
Import-Module $devShell
Enter-VsDevShell -VsInstallPath $vsInstall -SkipAutomaticLocation `
  -DevCmdArguments '-arch=arm64 -host_arch=x64 -no_logo'

$cl = Get-Command cl.exe -ErrorAction SilentlyContinue | Select-Object -First 1
if (-not $cl -or $cl.Source -notmatch '[\\/]Hostx64[\\/]arm64[\\/]cl\.exe$') {
  throw 'The active MSVC compiler is not the x64-hosted ARM64-target compiler.'
}
foreach ($tool in @('cmake', 'ninja', 'git', 'python')) {
  if (-not (Get-Command $tool -ErrorAction SilentlyContinue)) {
    throw "Required cross-build tool is missing: $tool"
  }
}
$env:CMAKE_GENERATOR = 'Ninja'

$cudaBase = 'https://developer.download.nvidia.com/compute/cuda/13.5.0'
$checksumFile = Join-Path $env:RUNNER_TEMP 'cuda-13.5-md5sum.txt'
curl.exe --fail --location --retry 5 --retry-all-errors --retry-delay 10 `
  --output $checksumFile "$cudaBase/docs/sidebar/md5sum.txt"
if ($LASTEXITCODE -ne 0) { throw "NVIDIA checksum manifest download failed: $LASTEXITCODE" }

function Get-OfficialNvidiaMd5([string]$FileName) {
  $rows = @(Select-String -LiteralPath $checksumFile -SimpleMatch $FileName)
  if ($rows.Count -ne 1) {
    throw "Expected exactly one NVIDIA MD5 entry for $FileName; found $($rows.Count)."
  }
  $match = [regex]::Match($rows[0].Line, '(?i)\b[0-9a-f]{32}\b')
  if (-not $match.Success) { throw "Could not parse NVIDIA MD5 entry for $FileName." }
  return $match.Value.ToLowerInvariant()
}

function Install-VerifiedCudaComponents(
  [string]$FileName,
  [string[]]$Components
) {
  $installer = Join-Path $env:RUNNER_TEMP $FileName
  $url = "$cudaBase/local_installers/$FileName"
  Write-Host "Downloading official NVIDIA installer: $FileName"
  curl.exe --fail --location --retry 5 --retry-all-errors --retry-delay 10 `
    --output $installer $url
  if ($LASTEXITCODE -ne 0) { throw "NVIDIA installer download failed: $LASTEXITCODE ($FileName)" }
  $expected = Get-OfficialNvidiaMd5 $FileName
  $actual = (Get-FileHash -LiteralPath $installer -Algorithm MD5).Hash.ToLowerInvariant()
  if ($actual -ne $expected) {
    throw "NVIDIA installer MD5 mismatch for $FileName. Expected $expected; got $actual."
  }
  Write-Host "Verified NVIDIA MD5: $actual"
  $process = Start-Process -FilePath $installer `
    -ArgumentList (@('-s') + $Components) -NoNewWindow -Wait -PassThru
  if ($process.ExitCode -notin @(0, 3010)) {
    throw "NVIDIA component installation failed for $FileName: $($process.ExitCode)"
  }
  Remove-Item -LiteralPath $installer -Force
}

Install-VerifiedCudaComponents `
  'cuda_13.5.0_windows_x86_64.exe' `
  @('cudart_13.5', 'nvcc_13.5', 'cublas_dev_13.5', 'crt_13.5', 'nvvm_13.5', 'nvptxcompiler_13.5')
Install-VerifiedCudaComponents `
  'cuda_cross_arm64_13.5.0_windows.exe' `
  @('cudart_cross_13.5', 'nvcc_cross_13.5', 'cublas_cross_13.5')

$cudaRoot = Join-Path $env:ProgramFiles 'NVIDIA GPU Computing Toolkit/CUDA/v13.5'
$nvcc = Join-Path $cudaRoot 'bin/nvcc.exe'
$arm64Lib = Join-Path $cudaRoot 'lib/arm64'
if (-not (Test-Path -LiteralPath $nvcc -PathType Leaf)) {
  throw "CUDA 13.5 x64 nvcc compiler is missing: $nvcc"
}
if (-not (Test-Path -LiteralPath $arm64Lib -PathType Container)) {
  throw "CUDA 13.5 ARM64 import-library directory is missing: $arm64Lib"
}
foreach ($library in @('cudart.lib', 'cudart_static.lib', 'cuda.lib', 'cublas.lib', 'cublasLt.lib')) {
  if (-not (Test-Path -LiteralPath (Join-Path $arm64Lib $library) -PathType Leaf)) {
    throw "Required CUDA ARM64 import library is missing: $library"
  }
}
$env:CUDA_PATH = $cudaRoot
$env:CUDAToolkit_ROOT = $cudaRoot
$env:CUDACXX = $nvcc
$env:PATH = "$(Join-Path $cudaRoot 'bin');$env:PATH"
& $nvcc --version
if ($LASTEXITCODE -ne 0) { throw 'CUDA nvcc version check failed.' }

$whisperRoot = Join-Path $env:RUNNER_TEMP 'rdt-pinned-whisper-cpp-arm64'
if (Test-Path -LiteralPath $whisperRoot) {
  throw "Refusing to reuse an existing whisper.cpp checkout: $whisperRoot"
}
git clone --no-checkout $sourceRepository $whisperRoot
if ($LASTEXITCODE -ne 0) { throw 'Could not clone the pinned whisper.cpp repository.' }
git -C $whisperRoot fetch --depth 1 origin $sourceCommit
if ($LASTEXITCODE -ne 0) { throw 'Could not fetch the pinned whisper.cpp commit.' }
git -C $whisperRoot checkout --detach $sourceCommit
if ($LASTEXITCODE -ne 0) { throw 'Could not check out the pinned whisper.cpp commit.' }
git -C $whisperRoot submodule update --init --recursive
if ($LASTEXITCODE -ne 0) { throw 'Could not initialize pinned whisper.cpp submodules.' }
$pinReport = Join-Path $env:RUNNER_TEMP 'rdt-arm64-cuda-engine-pin-report.json'
python (Join-Path $repoRoot 'backend/whisper_cpp/scripts/verify_engine_checkout.py') `
  --source $whisperRoot --pins $pinFile --output $pinReport
if ($LASTEXITCODE -ne 0) { throw 'Pinned whisper.cpp source/submodule verification failed.' }

$buildDir = Join-Path $env:RUNNER_TEMP 'rdt-arm64-cuda-worker-build'
$workerSource = Join-Path $repoRoot 'backend/whisper_cpp/worker'
$cudaLib = $arm64Lib.Replace('\', '/')
$cudaRootCmake = $cudaRoot.Replace('\', '/')
$clPath = $cl.Source.Replace('\', '/')
$nvccPath = $nvcc.Replace('\', '/')
$whisperPath = $whisperRoot.Replace('\', '/')

$configureArgs = @(
  '-S', $workerSource,
  '-B', $buildDir,
  '-G', 'Ninja',
  "-DWHISPER_CPP_SOURCE=$whisperPath",
  "-DRDT_WHISPER_CPP_COMMIT=$sourceCommit",
  '-DRDT_ENABLE_CUDA=ON',
  '-DRDT_ENABLE_VULKAN=OFF',
  '-DRDT_ENABLE_ARM_DOTPROD=OFF',
  '-DCMAKE_BUILD_TYPE=Release',
  "-DCMAKE_C_COMPILER:FILEPATH=$clPath",
  "-DCMAKE_CXX_COMPILER:FILEPATH=$clPath",
  "-DCMAKE_CUDA_COMPILER:FILEPATH=$nvccPath",
  "-DCMAKE_CUDA_HOST_COMPILER:FILEPATH=$clPath",
  "-DCUDAToolkit_ROOT=$cudaRootCmake",
  "-DCUDAToolkit_LIBRARY_DIR:PATH=$cudaLib",
  "-DCUDA_CUDART:FILEPATH=$cudaLib/cudart.lib",
  "-DCUDA_cudart_LIBRARY:FILEPATH=$cudaLib/cudart.lib",
  "-DCUDA_cudart_static_LIBRARY:FILEPATH=$cudaLib/cudart_static.lib",
  "-DCUDA_cuda_driver_LIBRARY:FILEPATH=$cudaLib/cuda.lib",
  "-DCUDA_cublas_LIBRARY:FILEPATH=$cudaLib/cublas.lib",
  "-DCUDA_cublasLt_LIBRARY:FILEPATH=$cudaLib/cublasLt.lib"
)
Write-Host '===== CONFIGURE WINDOWS ARM64 CUDA WORKER (X64 HOST) ====='
cmake @configureArgs
if ($LASTEXITCODE -ne 0) { throw "ARM64 CUDA worker CMake configuration failed: $LASTEXITCODE" }

Write-Host '===== BUILD WINDOWS ARM64 CUDA WORKER ====='
cmake --build $buildDir --target rdt_whisper_worker --config Release --parallel 2 --verbose
if ($LASTEXITCODE -ne 0) { throw "ARM64 CUDA worker compilation failed: $LASTEXITCODE" }

$builtWorker = Join-Path $buildDir 'bin/rdt_whisper_worker.exe'
if (-not (Test-Path -LiteralPath $builtWorker -PathType Leaf)) {
  throw "CMake did not produce the expected CUDA worker: $builtWorker"
}

function Get-PeMachine([string]$Path) {
  $stream = [IO.File]::OpenRead($Path)
  $reader = [IO.BinaryReader]::new($stream)
  try {
    if ($reader.ReadUInt16() -ne 0x5A4D) { throw "Invalid PE/DOS header: $Path" }
    $stream.Position = 0x3C
    $offset = $reader.ReadInt32()
    $stream.Position = $offset
    if ($reader.ReadUInt32() -ne 0x4550) { throw "Invalid PE signature: $Path" }
    return [int]$reader.ReadUInt16()
  } finally {
    $reader.Dispose()
    $stream.Dispose()
  }
}
$machine = Get-PeMachine $builtWorker
if ($machine -ne 0xAA64) {
  throw ('Cross-built CUDA worker has PE machine 0x{0:X4}; expected ARM64 0xAA64.' -f $machine)
}

$output = [IO.Path]::GetFullPath($OutputDirectory)
if (Test-Path -LiteralPath $output) {
  throw "Refusing to reuse an existing output directory: $output"
}
New-Item -ItemType Directory -Force -Path $output | Out-Null
$workerName = 'rdt_whisper_worker_cuda.exe'
$workerPath = Join-Path $output $workerName
Copy-Item -LiteralPath $builtWorker -Destination $workerPath
$workerInfo = Get-Item -LiteralPath $workerPath
$workerHash = (Get-FileHash -LiteralPath $workerPath -Algorithm SHA256).Hash.ToLowerInvariant()
$nvccVersion = (& $nvcc --version 2>&1 | Out-String).Trim()
$manifest = [ordered]@{
  schema = 'rdt-windows-arm64-cuda-worker-v1'
  architecture = 'ARM64'
  machine = '0xAA64'
  filename = $workerName
  size_bytes = [long]$workerInfo.Length
  sha256 = $workerHash
  whisper_cpp_commit = $sourceCommit
  cuda_version = '13.5'
  nvcc_version = $nvccVersion
}
$manifestPath = Join-Path $output 'rdt_whisper_worker_cuda.manifest.json'
$manifest | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $manifestPath -Encoding utf8
Write-Host "ARM64_CUDA_WORKER=$workerPath"
Write-Host "ARM64_CUDA_WORKER_SHA256=$workerHash"
Write-Host 'ARM64_CUDA_WORKER_PE_MACHINE=0xAA64'
