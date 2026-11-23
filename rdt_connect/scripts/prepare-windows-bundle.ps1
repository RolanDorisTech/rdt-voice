[CmdletBinding()]
param(
  [ValidateSet('x64','ARM64')][string]$TargetArch = '',
  [string]$Python = '',
  [string]$FfmpegDir = $env:RDT_FFMPEG_DIR,
  [string]$WhisperCppSource = $env:WHISPER_CPP_SOURCE,
  [switch]$EnableVulkan,
  [switch]$EnableCuda,
  [switch]$RequireGpuWorker
)
$ErrorActionPreference = 'Stop'

# RDT_CONNECT_EMBEDDED_VOICE_PREFLIGHT
$RdtConnectScriptDir = $PSScriptRoot
$RdtConnectRoot = Split-Path -Parent $RdtConnectScriptDir
Push-Location $RdtConnectRoot
try {
    & node (Join-Path $RdtConnectScriptDir "prepare-embedded-rdt-voice.mjs")
    if ($LASTEXITCODE -ne 0) { throw "Embedded RDT Voice preflight failed." }
} finally {
    Pop-Location
}

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot '../..')).Path
$ProjectDir = Join-Path $RepoRoot 'rdt_connect'
$ResourceRoot = Join-Path $ProjectDir 'src-tauri/resources'
$PinFile = Join-Path $RepoRoot 'backend/whisper_cpp/engine-pins.json'
$Pins = Get-Content $PinFile -Raw | ConvertFrom-Json
if (-not $TargetArch) {
  $machine = [System.Runtime.InteropServices.RuntimeInformation]::ProcessArchitecture.ToString()
  $TargetArch = if ($machine -eq 'Arm64') { 'ARM64' } elseif ($machine -eq 'X64') { 'x64' } else { throw "Unsupported native build architecture: $machine" }
}
$nativeMachine = [System.Runtime.InteropServices.RuntimeInformation]::ProcessArchitecture.ToString()
$expectedMachine = if ($TargetArch -eq 'ARM64') { 'Arm64' } else { 'X64' }
if ($nativeMachine -ne $expectedMachine) { throw "A native $TargetArch build requires a native $TargetArch process. Current Python/PowerShell architecture is $nativeMachine." }
if (-not $Python) { $Python = (Get-Command py -ErrorAction SilentlyContinue).Source }
if (-not $Python) { throw 'Python launcher/executable missing. Provide -Python with a native Python 3.13.16 executable.' }
$pyInfo = & $Python -c 'import platform,sys; print("%d.%d.%d|%s" % (*sys.version_info[:3], platform.machine()))'
if ($LASTEXITCODE -ne 0 -or -not $pyInfo.StartsWith('3.13.16|')) { throw "Requires Python 3.13.16 for reproducible qualification; found: $pyInfo" }
if (-not $FfmpegDir -or -not (Test-Path (Join-Path $FfmpegDir 'ffmpeg.exe')) -or -not (Test-Path (Join-Path $FfmpegDir 'ffprobe.exe'))) {
  throw 'Set RDT_FFMPEG_DIR to a verified native-architecture FFmpeg distribution containing ffmpeg.exe, ffprobe.exe, and its license notices. The script will not download or guess binaries.'
}
$noticesRoot = Join-Path $RepoRoot 'licenses/third-party'
if (-not (Test-Path (Join-Path $noticesRoot 'opencc-js/NOTICE.md'))) { throw 'Required OpenCC-JS notice is missing.' }
$directRequirements = Join-Path $RepoRoot 'backend/windows/requirements-windows.txt'
$RequirementsLock = if ($TargetArch -eq 'ARM64') {
  Join-Path $RepoRoot 'backend/windows/requirements-windows-arm64.txt'
} else {
  Join-Path $RepoRoot 'backend/windows/requirements-windows-x64.txt'
}
if (-not (Test-Path -LiteralPath $RequirementsLock -PathType Leaf)) {
  throw "Required architecture-specific hash lock is missing: $RequirementsLock. Generate and qualify it on the target architecture; direct requirements are not a release-install fallback."
}

$buildRoot = Join-Path $ProjectDir "build/windows-$TargetArch"
$venv = Join-Path $buildRoot 'venv'
$pythonExe = Join-Path $venv 'Scripts/python.exe'
if (-not (Test-Path $pythonExe)) { & $Python -m venv $venv; if ($LASTEXITCODE -ne 0) { throw 'Could not create isolated native build venv.' } }
$pythonVersion = & $pythonExe -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])'
if ($pythonVersion -ne '3.13.16') { throw "Build venv has Python $pythonVersion, expected 3.13.16." }

& $pythonExe (Join-Path $RepoRoot 'backend/windows/scripts/validate_windows_requirements_lock.py') --lock $RequirementsLock --target-arch $TargetArch --direct-requirements $directRequirements
if ($LASTEXITCODE -ne 0) { throw "Architecture-specific dependency lock validation failed for $TargetArch." }

& $pythonExe -m pip install --disable-pip-version-check --require-hashes --only-binary=:all: -r $RequirementsLock
if ($LASTEXITCODE -ne 0) { throw "Hash-verified binary-wheel dependency installation failed for $TargetArch." }

if (-not $WhisperCppSource) {
  $WhisperCppSource = Join-Path $buildRoot 'whisper.cpp'
  if (-not (Test-Path (Join-Path $WhisperCppSource '.git'))) {
    New-Item -ItemType Directory -Force -Path $buildRoot | Out-Null
    git clone --no-checkout $Pins.source_repository $WhisperCppSource
    if ($LASTEXITCODE -ne 0) { throw 'Could not clone whisper.cpp.' }
  }
  git -C $WhisperCppSource fetch --depth 1 origin $Pins.source_commit
  if ($LASTEXITCODE -ne 0) { throw 'Could not fetch the pinned whisper.cpp commit.' }
  git -C $WhisperCppSource checkout --detach $Pins.source_commit
  if ($LASTEXITCODE -ne 0) { throw 'Could not detach at the pinned whisper.cpp commit.' }
  git -C $WhisperCppSource submodule update --init --recursive
  if ($LASTEXITCODE -ne 0) { throw 'Could not initialize pinned whisper.cpp submodules.' }
}
& $pythonExe (Join-Path $RepoRoot 'backend/whisper_cpp/scripts/verify_engine_checkout.py') --source $WhisperCppSource --pins $PinFile --output (Join-Path $buildRoot 'engine-pin-report.json')
if ($LASTEXITCODE -ne 0) { throw 'whisper.cpp source/submodule pin verification failed.' }
$whisperCmakeLists = Join-Path $WhisperCppSource 'CMakeLists.txt'
if (-not (Test-Path -LiteralPath $whisperCmakeLists -PathType Leaf)) {
  throw "Pinned whisper.cpp CMakeLists.txt is missing at: $WhisperCppSource"
}
$WhisperCppSource = (Resolve-Path -LiteralPath $WhisperCppSource).Path
$cmakeWhisperSource = $WhisperCppSource.Replace('\', '/')
Write-Host "Verified whisper.cpp source root: $cmakeWhisperSource"

if ($EnableVulkan -and -not $env:VULKAN_SDK) {
  throw 'Vulkan was requested, but VULKAN_SDK is not set. Install the Vulkan SDK before building.'
}
if ($EnableCuda -and -not (Get-Command nvcc -ErrorAction SilentlyContinue)) {
  throw 'CUDA was requested, but nvcc is unavailable. Install a compatible CUDA Toolkit before building.'
}

function Build-WorkerVariant {
  param(
    [string]$Variant,
    [string]$Cuda,
    [string]$Vulkan,
    [string]$ArmDotprod = 'OFF'
  )
  $workerBuildBase = if ($env:RUNNER_TEMP) { $env:RUNNER_TEMP } else { [System.IO.Path]::GetTempPath() }
  $dir = Join-Path $workerBuildBase "rdt-$TargetArch-worker-$Variant"
  $buildLog = Join-Path $workerBuildBase "rdt-$TargetArch-worker-$Variant.log"
  cmake -S (Join-Path $RepoRoot 'backend/whisper_cpp/worker') `
    -B $dir -A $TargetArch `
    "-DWHISPER_CPP_SOURCE=$cmakeWhisperSource" `
    "-DRDT_ENABLE_CUDA=$Cuda" `
    "-DRDT_ENABLE_VULKAN=$Vulkan" `
    "-DRDT_ENABLE_ARM_DOTPROD=$ArmDotprod" `
    -DCMAKE_BUILD_TYPE=Release 2>&1 | Tee-Object -FilePath $buildLog | Out-Host
  if ($LASTEXITCODE -ne 0) { throw "CMake configuration failed for $Variant worker." }

  cmake --build $dir --config Release --parallel 2 --verbose 2>&1 | Tee-Object -FilePath $buildLog -Append | Out-Host
  $buildExitCode = $LASTEXITCODE
  if ($buildExitCode -ne 0) {
    Write-Host "::group::Last 160 lines from $buildLog"
    Get-Content -LiteralPath $buildLog -Tail 160 | ForEach-Object { Write-Host $_ }
    Write-Host "::endgroup::"
    throw "Native $Variant worker build failed (exit $buildExitCode). Full log: $buildLog"
  }

  $exe = Join-Path $dir 'bin/rdt_whisper_worker.exe'
  if (-not (Test-Path -LiteralPath $exe -PathType Leaf)) {
    throw "Expected $Variant worker missing: $exe"
  }
  return $exe
}

# The portable CPU worker is mandatory; optimized workers are additive.
$workers = @()
$workers += [pscustomobject]@{
  Path = (Build-WorkerVariant -Variant 'cpu' -Cuda 'OFF' -Vulkan 'OFF')
  Name = 'rdt_whisper_worker.exe'
}
if ($TargetArch -eq 'ARM64') {
  Write-Host 'Building required ARM64 dot-product worker.'
  $dotprodWorker = Build-WorkerVariant -Variant 'arm-dotprod' `
    -Cuda 'OFF' -Vulkan 'OFF' -ArmDotprod 'ON'
  $workers += [pscustomobject]@{
    Path = $dotprodWorker
    Name = 'rdt_whisper_worker_dotprod.exe'
  }
}
if ($EnableCuda) {
  $workers += [pscustomobject]@{
    Path = (Build-WorkerVariant -Variant 'cuda' -Cuda 'ON' -Vulkan 'OFF')
    Name = 'rdt_whisper_worker_cuda.exe'
  }
}
if ($EnableVulkan) {
  $workers += [pscustomobject]@{
    Path = (Build-WorkerVariant -Variant 'vulkan' -Cuda 'OFF' -Vulkan 'ON')
    Name = 'rdt_whisper_worker_vulkan.exe'
  }
}

# A release explicitly requiring GPU support must never silently package
# only the CPU worker. Requested GPU builds already fail on missing SDKs,
# CMake errors, link errors, or missing executables.
$workerNames = @($workers | ForEach-Object { $_.Name })

if ($RequireGpuWorker) {
  if (-not $EnableCuda -or -not $EnableVulkan) {
    throw 'This release requires both CUDA and Vulkan worker builds.'
  }
  foreach ($requiredWorker in @('rdt_whisper_worker_cuda.exe', 'rdt_whisper_worker_vulkan.exe')) {
    if ($workerNames -notcontains $requiredWorker) {
      throw "Required GPU worker is missing: $requiredWorker"
    }
  }
}

if ($TargetArch -eq 'ARM64' -and $workerNames -notcontains 'rdt_whisper_worker_dotprod.exe') {
  throw 'Required ARM64 dot-product worker is missing.'
}

$gpuWorkers = @($workers | Where-Object {
  $_.Name -in @('rdt_whisper_worker_cuda.exe', 'rdt_whisper_worker_vulkan.exe')
})
if ($RequireGpuWorker) {
  Write-Host ("REQUIRED_GPU_WORKERS=" + (($gpuWorkers | ForEach-Object { $_.Name }) -join ','))
}
$stage = Join-Path $buildRoot 'stage'
$dist = Join-Path $stage 'pyinstaller-dist'
$work = Join-Path $stage 'pyinstaller-work'
$spec = Join-Path $stage 'pyinstaller-spec'
$web = Join-Path $stage 'rdt_voice_web'
$notices = Join-Path $stage 'windows-third-party-notices'
$guideStage = Join-Path $stage 'windows_installation_guide'
$guideSource = Join-Path (Split-Path -Parent $PSScriptRoot) 'windows-package/guide'
Remove-Item -Recurse -Force $stage -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force -Path $dist,$work,$spec,$web,$notices,$guideStage | Out-Null

# Include the complete Windows installation guide in packaged resources.
foreach ($file in @('windows_installation_guide.html','rdt_connect_guide_icon.png','rdt_connect_guide.ico')) {
  $sourceFile = Join-Path $guideSource $file
  if (-not (Test-Path -LiteralPath $sourceFile -PathType Leaf)) {
    throw "Required Windows guide asset is missing: $sourceFile"
  }
  Copy-Item -LiteralPath $sourceFile -Destination (Join-Path $guideStage $file) -Force
}
$serverStaged = Join-Path $stage 'server.py'
& $pythonExe (Join-Path $RepoRoot 'rdt_connect/scripts/patch_windows_desktop_backend.py') (Join-Path $RepoRoot 'backend/windows/server.py') $serverStaged
if ($LASTEXITCODE -ne 0) { throw 'Could not prepare the staged Windows backend.' }
$env:PYTHONPATH = Join-Path $RepoRoot 'backend'
$args = @('-m','PyInstaller','--noconfirm','--clean','--onedir','--name','rdt_voice_backend','--distpath',$dist,'--workpath',$work,'--specpath',$spec,'--paths',(Join-Path $RepoRoot 'backend'),'--paths',(Join-Path $RepoRoot 'backend/windows'),'--paths',(Join-Path $RepoRoot 'backend/whisper_cpp'),'--hidden-import','model_registry','--hidden-import','lifetime_statistics','--hidden-import','engine','--hidden-import','whisper_cpp.model_registry','--hidden-import','whisper_cpp.engine','--hidden-import','qrcode.image.svg','--collect-all','qrcode')
& $pythonExe @args $serverStaged
if ($LASTEXITCODE -ne 0) { throw 'PyInstaller onedir backend build failed.' }
$backendStage = Join-Path $dist 'rdt_voice_backend'
if (-not (Test-Path (Join-Path $backendStage 'rdt_voice_backend.exe'))) { throw 'PyInstaller did not produce rdt_voice_backend.exe.' }
$fixtureSource = Join-Path $RepoRoot 'backend/whisper_cpp/tests/fixtures/smoke-multilingual-16k-mono.wav'
$fixtureExpectedSha256 = '296b1feaacbcc9e75ef6ea7b326000e290ce38efb734841691193d26bb1dc017'
if (-not (Test-Path -LiteralPath $fixtureSource -PathType Leaf)) { throw "Mandatory first-start benchmark fixture is missing: $fixtureSource" }
$fixtureActualSha256 = (Get-FileHash -LiteralPath $fixtureSource -Algorithm SHA256).Hash.ToLowerInvariant()
if ($fixtureActualSha256 -ne $fixtureExpectedSha256) { throw "Benchmark fixture SHA-256 mismatch: $fixtureActualSha256" }
Copy-Item -LiteralPath $fixtureSource -Destination (Join-Path $backendStage 'rdt_benchmark_fixture.wav') -Force
& (Join-Path $RepoRoot 'rdt_connect/scripts/copy-msvc-runtime.ps1') -TargetArch $TargetArch -DestinationDir $backendStage
foreach ($variant in $workers) {
  Copy-Item -LiteralPath $variant.Path -Destination (Join-Path $backendStage $variant.Name) -Force
  Write-Host "Staged worker: $($variant.Name)"
}
if ($EnableCuda) {
  if (-not $env:CUDA_PATH -or -not (Test-Path -LiteralPath $env:CUDA_PATH -PathType Container)) {
    throw 'CUDA worker is enabled but CUDA_PATH is not a valid toolkit directory.'
  }
  $cudaRuntimeStage = Join-Path $backendStage 'cuda-runtime'
  & $pythonExe (Join-Path $RepoRoot 'backend/whisper_cpp/stage_cuda_runtime.py') stage `
    --platform windows `
    --cuda-root $env:CUDA_PATH `
    --destination $cudaRuntimeStage `
    --binary (Join-Path $backendStage 'rdt_whisper_worker_cuda.exe')
  if ($LASTEXITCODE -ne 0) { throw 'CUDA runtime DLL dependency staging failed.' }
}
Copy-Item (Join-Path $FfmpegDir 'ffmpeg.exe') $backendStage -Force
Copy-Item (Join-Path $FfmpegDir 'ffprobe.exe') $backendStage -Force
Copy-Item (Join-Path $RepoRoot 'index.html') (Join-Path $web 'index.html') -Force
Copy-Item (Join-Path $RepoRoot 'worker.js') (Join-Path $web 'worker.js') -Force
Copy-Item (Join-Path $RepoRoot 'pcm-worklet.js') (Join-Path $web 'pcm-worklet.js') -Force
Copy-Item (Join-Path $RepoRoot 'models.manifest.json') (Join-Path $web 'models.manifest.json') -Force
Copy-Item (Join-Path $RepoRoot 'locales') $web -Recurse -Force
Copy-Item (Join-Path $RepoRoot 'assets') $web -Recurse -Force
# Collect notices from the actual staged binaries, pinned source checkout,
# and installed runtime dependency closure. The collector requires an empty
# destination and fails closed when license material or provenance is missing.
& $pythonExe (Join-Path $RepoRoot 'backend/windows/scripts/collect_third_party_notices.py') `
  --repo-root $RepoRoot `
  --whisper-source $WhisperCppSource `
  --ffmpeg-dir $FfmpegDir `
  --site-packages (Join-Path $venv 'Lib/site-packages') `
  --destination $notices `
  --runtime-roots-file (Join-Path $RepoRoot 'backend/windows/runtime-package-roots.txt')
if ($LASTEXITCODE -ne 0) { throw 'Third-party notice collection or verification failed.' }
if ($EnableCuda) {
  & $pythonExe (Join-Path $RepoRoot 'backend/whisper_cpp/stage_cuda_runtime.py') attach-notices `
    --runtime-dir (Join-Path $backendStage 'cuda-runtime') `
    --notices-root $notices
  if ($LASTEXITCODE -ne 0) { throw 'CUDA runtime license/notice attachment failed.' }
}

# Promote only after all required inputs and build outputs exist. Keep one rollback copy.
$promote = @('rdt_voice_backend','rdt_voice_web','windows-third-party-notices','windows_installation_guide')
$rollback = Join-Path $buildRoot 'rollback-resources'
Remove-Item -Recurse -Force $rollback -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force -Path $rollback | Out-Null
foreach ($name in $promote) {
  $destination = Join-Path $ResourceRoot $name
  if (Test-Path $destination) { Move-Item $destination (Join-Path $rollback $name) }
}
try {
  foreach ($name in $promote) {
    $source = if ($name -eq 'rdt_voice_backend') { $backendStage } elseif ($name -eq 'rdt_voice_web') { $web } elseif ($name -eq 'windows_installation_guide') { $guideStage } else { $notices }
    Copy-Item $source (Join-Path $ResourceRoot $name) -Recurse -Force
  }
} catch {
  foreach ($name in $promote) { Remove-Item -Recurse -Force (Join-Path $ResourceRoot $name) -ErrorAction SilentlyContinue; if (Test-Path (Join-Path $rollback $name)) { Move-Item (Join-Path $rollback $name) (Join-Path $ResourceRoot $name) } }
  throw
}
Write-Host "Prepared native Windows $TargetArch resources at $ResourceRoot. This step does not build or qualify an NSIS installer, Windows TLS trust, or GPU execution."
