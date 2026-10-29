#!/usr/bin/env bash
# Build a PyInstaller one-folder backend bundle for the native Linux Tauri package.
# Native worker compilation and model download are separate, pinned steps.
set -euo pipefail

if [[ "$(uname -s)" != "Linux" ]]; then
  echo "STOP: prepare-linux-bundle.sh must run on native Linux, not macOS/UTM cross-build assumptions." >&2
  exit 2
fi

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/../.." && pwd -P)"
PYTHON_BIN="${RDT_PYTHON:-python3}"
WORKER="${REPO_ROOT}/backend/whisper_cpp/bin/rdt_whisper_worker"
RESOURCE_ROOT="${REPO_ROOT}/rdt_connect/src-tauri/resources"
TARGET="${RESOURCE_ROOT}/rdt_voice_backend"

if ! command -v "${PYTHON_BIN}" >/dev/null 2>&1; then
  echo "STOP: RDT_PYTHON is not available: ${PYTHON_BIN}" >&2
  exit 2
fi
if [[ ! -x "${WORKER}" ]]; then
  echo "STOP: native CPU worker is missing/not executable: ${WORKER}" >&2
  echo "Build it first with backend/whisper_cpp/scripts/build-linux-worker.sh cpu <pinned-clean-whisper.cpp-checkout>" >&2
  exit 2
fi
if [[ ! -f "${REPO_ROOT}/backend/linux/server.py" || ! -f "${REPO_ROOT}/backend/whisper_cpp/model_registry.py" || ! -f "${REPO_ROOT}/backend/whisper_cpp/engine.py" ]]; then
  echo "STOP: Linux server or shared worker/model registry source is missing." >&2
  exit 2
fi
if [[ ! -f "${RESOURCE_ROOT}/rdt_voice_web/index.html" || ! -f "${RESOURCE_ROOT}/rdt_voice_web/worker.js" || ! -f "${RESOURCE_ROOT}/rdt_voice_web/pcm-worklet.js" || ! -f "${RESOURCE_ROOT}/rdt_voice_web/models.manifest.json" ]]; then
  echo "STOP: embedded RDT Voice web assets are incomplete under ${RESOURCE_ROOT}/rdt_voice_web." >&2
  exit 2
fi
if [[ ! -d "${RESOURCE_ROOT}/third_party_notices" ]]; then
  echo "STOP: third_party_notices resource directory is missing." >&2
  exit 2
fi
if [[ -e "${TARGET}" ]]; then
  echo "STOP: refusing to overwrite an existing backend bundle: ${TARGET}" >&2
  echo "Move it to a deliberate backup first or use a clean qualification checkout." >&2
  exit 2
fi

"${PYTHON_BIN}" -m PyInstaller --version >/dev/null 2>&1 || {
  echo "STOP: PyInstaller is missing from RDT_PYTHON." >&2
  exit 2
}

STAGE_ROOT="$(mktemp -d "${RESOURCE_ROOT}/.rdt_voice_backend.stage.XXXXXX")"
WORK_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/rdt-linux-pyinstaller.XXXXXX")"
cleanup() { rm -rf -- "${STAGE_ROOT}" "${WORK_ROOT}"; }
trap cleanup EXIT

"${PYTHON_BIN}" -m PyInstaller \
  --noconfirm --clean --onedir --name rdt_voice_backend \
  --distpath "${WORK_ROOT}/dist" \
  --workpath "${WORK_ROOT}/work" \
  --specpath "${WORK_ROOT}" \
  --paths "${REPO_ROOT}/backend" \
  --paths "${REPO_ROOT}/backend/linux" \
  --paths "${REPO_ROOT}/backend/whisper_cpp" \
  --hidden-import model_registry \
  --hidden-import engine \
  --hidden-import qrcode.image.svg \
  --hidden-import python_multipart \
  --hidden-import backports.tarfile \
  --collect-all fastapi \
  --collect-all starlette \
  --collect-all uvicorn \
  --collect-all numpy \
  --collect-all qrcode \
  --collect-all multipart \
  --collect-all httpx \
  --exclude-module mlx \
  --exclude-module mlx_whisper \
  "${REPO_ROOT}/backend/linux/server.py"

BUILT="${WORK_ROOT}/dist/rdt_voice_backend"
if [[ ! -x "${BUILT}/rdt_voice_backend" ]]; then
  echo "STOP: PyInstaller did not create an executable backend." >&2
  exit 1
fi

cp -a -- "${BUILT}/." "${STAGE_ROOT}/"
install -m 0755 "${WORKER}" "${STAGE_ROOT}/rdt_whisper_worker"
AVX2_WORKER="${REPO_ROOT}/backend/whisper_cpp/bin/rdt_whisper_worker_avx2"
if [[ -x "${AVX2_WORKER}" ]]; then
  if [[ "$(uname -m)" != "x86_64" && "$(uname -m)" != "amd64" ]]; then
    echo "STOP: refusing to package an x64 AVX2 worker on $(uname -m)." >&2
    exit 1
  fi
  install -m 0755 "${AVX2_WORKER}" "${STAGE_ROOT}/rdt_whisper_worker_avx2"
fi

# Refuse bundles with unresolved shared-library dependencies. A successful copy
# alone is not evidence the executable will run on a supported Linux system.
for binary in "${STAGE_ROOT}/rdt_voice_backend" "${STAGE_ROOT}"/rdt_whisper_worker*; do
  deps="$(ldd "${binary}" 2>&1 || true)"
  if grep -q 'not found' <<<"${deps}"; then
    echo "STOP: missing shared library for ${binary}:" >&2
    printf '%s\n' "${deps}" >&2
    exit 1
  fi
done

"${PYTHON_BIN}" - "${STAGE_ROOT}" "${REPO_ROOT}" <<'PY'
import hashlib, json, pathlib, platform, sys, time
stage = pathlib.Path(sys.argv[1])
repo = pathlib.Path(sys.argv[2])
def sha(p):
    h=hashlib.sha256()
    with p.open('rb') as f:
        for chunk in iter(lambda:f.read(1024*1024), b''):
            h.update(chunk)
    return h.hexdigest()
files = {}
names = ['rdt_voice_backend', 'rdt_whisper_worker']
if (stage/'rdt_whisper_worker_avx2').is_file():
    names.append('rdt_whisper_worker_avx2')
for name in names:
    p=stage/name
    if not p.is_file() or p.is_symlink():
        raise SystemExit(f'expected regular bundled binary missing: {p}')
    files[name]={'bytes':p.stat().st_size,'sha256':sha(p)}
manifest={
  'schema':'rdt-linux-backend-bundle-v1',
  'built_at_utc':time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
  'platform':platform.platform(),
  'machine':platform.machine(),
  'worker_pinned_commit':'d1be6fde11ac6e0407606b4e42fe72d34add8037',
  'provider':'CPU build; successful inference qualification still required',
  'source_revision':__import__('subprocess').run(['git','-C',str(repo),'rev-parse','HEAD'],capture_output=True,text=True,check=False).stdout.strip() or 'unknown',
  'files':files,
}
(stage/'rdt-linux-backend-bundle.json').write_text(json.dumps(manifest,indent=2)+'\n',encoding='utf-8')
PY

# Promote only after the build and dependency checks passed.
mv -- "${STAGE_ROOT}" "${TARGET}"
# Leave trap harmless; the moved path no longer exists.
echo "LINUX_BACKEND_BUNDLE_READY=${TARGET}"
cat "${TARGET}/rdt-linux-backend-bundle.json"
echo "Note: this confirms bundle construction and dependency checks only, not actual model inference."
