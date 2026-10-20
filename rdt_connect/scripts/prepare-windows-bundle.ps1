[CmdletBinding()]
param(
  [ValidateSet('x64','ARM64')][string]$TargetArch = '',
  [string]$Python = '',
  [string]$FfmpegDir = $env:RDT_FFMPEG_DIR,
  [string]$WhisperCppSource = $env:WHISPER_CPP_SOURCE
)
$ErrorActionPreference = 'Stop'
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
$buildRoot = Join-Path $ProjectDir "build/windows-$TargetArch"
$venv = Join-Path $buildRoot 'venv'
$pythonExe = Join-Path $venv 'Scripts/python.exe'
if (-not (Test-Path $pythonExe)) { & $Python -m venv $venv; if ($LASTEXITCODE -ne 0) { throw 'Could not create isolated native build venv.' } }
$pythonVersion = & $pythonExe -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])'
if ($pythonVersion -ne '3.13.16') { throw "Build venv has Python $pythonVersion, expected 3.13.16." }
& $pythonExe -m pip install --disable-pip-version-check -r (Join-Path $RepoRoot 'backend/windows/requirements-windows.txt')
if ($LASTEXITCODE -ne 0) { throw 'Pinned build dependency install failed.' }

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
$workerBuild = Join-Path $buildRoot 'worker-build'
cmake -S (Join-Path $RepoRoot 'backend/whisper_cpp/worker') -B $workerBuild -A $TargetArch -DWHISPER_CPP_SOURCE=$WhisperCppSource -DCMAKE_BUILD_TYPE=Release
if ($LASTEXITCODE -ne 0) { throw 'CMake worker configuration failed.' }
cmake --build $workerBuild --config Release --parallel 2
if ($LASTEXITCODE -ne 0) { throw 'Native persistent worker build failed.' }
$worker = Join-Path $workerBuild 'bin/rdt_whisper_worker.exe'
if (-not (Test-Path $worker)) { throw "Expected native worker missing: $worker" }

$stage = Join-Path $buildRoot 'stage'
$dist = Join-Path $stage 'pyinstaller-dist'
$work = Join-Path $stage 'pyinstaller-work'
$spec = Join-Path $stage 'pyinstaller-spec'
$web = Join-Path $stage 'rdt_voice_web'
$notices = Join-Path $stage 'third_party_notices'
Remove-Item -Recurse -Force $stage -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force -Path $dist,$work,$spec,$web,$notices | Out-Null
$env:PYTHONPATH = Join-Path $RepoRoot 'backend'
$args = @('-m','PyInstaller','--noconfirm','--clean','--onedir','--name','rdt_voice_backend','--distpath',$dist,'--workpath',$work,'--specpath',$spec,'--paths',(Join-Path $RepoRoot 'backend'),'--hidden-import','model_registry','--hidden-import','engine','--hidden-import','whisper_cpp.model_registry','--hidden-import','whisper_cpp.engine','--hidden-import','qrcode.image.svg','--collect-all','qrcode')
& $pythonExe @args (Join-Path $RepoRoot 'backend/windows/server.py')
if ($LASTEXITCODE -ne 0) { throw 'PyInstaller onedir backend build failed.' }
$backendStage = Join-Path $dist 'rdt_voice_backend'
if (-not (Test-Path (Join-Path $backendStage 'rdt_voice_backend.exe'))) { throw 'PyInstaller did not produce rdt_voice_backend.exe.' }
& (Join-Path $RepoRoot 'rdt_connect/scripts/copy-msvc-runtime.ps1') -TargetArch $TargetArch -DestinationDir $backendStage
Copy-Item $worker (Join-Path $backendStage 'rdt_whisper_worker.exe') -Force
Copy-Item (Join-Path $FfmpegDir 'ffmpeg.exe') $backendStage -Force
Copy-Item (Join-Path $FfmpegDir 'ffprobe.exe') $backendStage -Force
Copy-Item (Join-Path $RepoRoot 'index.html') (Join-Path $web 'index.html') -Force
Copy-Item (Join-Path $RepoRoot 'worker.js') (Join-Path $web 'worker.js') -Force
Copy-Item (Join-Path $RepoRoot 'pcm-worklet.js') (Join-Path $web 'pcm-worklet.js') -Force
Copy-Item (Join-Path $RepoRoot 'models.manifest.json') (Join-Path $web 'models.manifest.json') -Force
Copy-Item (Join-Path $RepoRoot 'locales') $web -Recurse -Force
Copy-Item (Join-Path $RepoRoot 'assets') $web -Recurse -Force
Copy-Item (Join-Path $noticesRoot 'opencc-js') $notices -Recurse -Force
if (-not (Test-Path (Join-Path $FfmpegDir 'LICENSE.txt')) -and -not (Test-Path (Join-Path $FfmpegDir 'LICENSE'))) { throw 'FFmpeg license notice is required in RDT_FFMPEG_DIR (LICENSE.txt or LICENSE).' }
Copy-Item (Get-ChildItem $FfmpegDir -File | Where-Object { $_.Name -in @('LICENSE.txt','LICENSE') } | Select-Object -First 1).FullName $notices -Force
@"
# Build notices (Windows $TargetArch)

- RDT Voice frontend: see repository LICENSE and `licenses/third-party/opencc-js/NOTICE.md`.
- whisper.cpp / GGML: upstream MIT license from pinned source checkout; preserve upstream license files in release notices.
- FFmpeg: bundled architecture-specific distribution from RDT_FFMPEG_DIR; its license notice is copied here.
- Python and pip packages: see package licenses and package metadata for the locked requirements file.

This is an inventory, not a complete transitive license audit. Confirm upstream licenses and binary hashes before public release.
"@ | Set-Content -Encoding UTF8 (Join-Path $notices 'BUILD-NOTICES.md')

# Promote only after all required inputs and build outputs exist. Keep one rollback copy.
$promote = @('rdt_voice_backend','rdt_voice_web','third_party_notices')
$rollback = Join-Path $buildRoot 'rollback-resources'
Remove-Item -Recurse -Force $rollback -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force -Path $rollback | Out-Null
foreach ($name in $promote) {
  $destination = Join-Path $ResourceRoot $name
  if (Test-Path $destination) { Move-Item $destination (Join-Path $rollback $name) }
}
try {
  foreach ($name in $promote) {
    $source = if ($name -eq 'rdt_voice_backend') { $backendStage } elseif ($name -eq 'rdt_voice_web') { $web } else { $notices }
    Copy-Item $source (Join-Path $ResourceRoot $name) -Recurse -Force
  }
} catch {
  foreach ($name in $promote) { Remove-Item -Recurse -Force (Join-Path $ResourceRoot $name) -ErrorAction SilentlyContinue; if (Test-Path (Join-Path $rollback $name)) { Move-Item (Join-Path $rollback $name) (Join-Path $ResourceRoot $name) } }
  throw
}
Write-Host "Prepared native Windows $TargetArch resources at $ResourceRoot. This step does not build or qualify an NSIS installer, Windows TLS trust, or GPU execution."
