#!/usr/bin/env bash
# Build a PyInstaller one-folder backend bundle for the native Linux Tauri package.
# Native worker compilation and model download are separate, pinned steps.
set -euo pipefail

# RDT_CONNECT_EMBEDDED_VOICE_PREFLIGHT
RDT_CONNECT_SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
(cd "$RDT_CONNECT_SCRIPT_DIR/.." && node scripts/prepare-embedded-rdt-voice.mjs)


if [[ "$(uname -s)" != "Linux" ]]; then
  echo "STOP: prepare-linux-bundle.sh must run on native Linux, not macOS/UTM cross-build assumptions." >&2
  exit 2
fi

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/../.." && pwd -P)"
PYTHON_BIN="${RDT_PYTHON:-python3}"
WORKER="${REPO_ROOT}/backend/whisper_cpp/bin/rdt_whisper_worker"
FIXTURE_SOURCE="${REPO_ROOT}/backend/whisper_cpp/tests/fixtures/smoke-multilingual-16k-mono.wav"
FIXTURE_SHA256="296b1feaacbcc9e75ef6ea7b326000e290ce38efb734841691193d26bb1dc017"
VARIANT_DIR="${RDT_LINUX_WORKER_VARIANTS_DIR:-${REPO_ROOT}/rdt_connect/build/linux-worker/variants}"
RESOURCE_ROOT="${REPO_ROOT}/rdt_connect/src-tauri/resources"
LINUX_NOTICES_ROOT="${RESOURCE_ROOT}/linux-third-party-notices"
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
if [[ ! -f "${FIXTURE_SOURCE}" ]]; then
  echo "STOP: deterministic first-start benchmark fixture is missing: ${FIXTURE_SOURCE}" >&2
  exit 2
fi
actual_fixture_sha="$(sha256sum "${FIXTURE_SOURCE}" | awk '{print $1}')"
if [[ "${actual_fixture_sha}" != "${FIXTURE_SHA256}" ]]; then
  echo "STOP: benchmark fixture SHA-256 mismatch: ${actual_fixture_sha}" >&2
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
"${PYTHON_BIN}" "${REPO_ROOT}/backend/linux/scripts/collect_third_party_notices.py" \
  --repo-root "${REPO_ROOT}" \
  --destination "${LINUX_NOTICES_ROOT}"

if [[ ! -s "${LINUX_NOTICES_ROOT}/NOTICE-MANIFEST.json" ]]; then
  echo "STOP: Linux third-party notice manifest is missing or empty." >&2
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
  --hidden-import lifetime_statistics \
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
BUNDLED_WORKERS=("${STAGE_ROOT}/rdt_whisper_worker")
for variant in cuda vulkan dotprod; do
  source_worker="${VARIANT_DIR}/rdt_whisper_worker_${variant}"
  if [[ -e "${source_worker}" ]]; then
    if [[ ! -f "${source_worker}" || ! -x "${source_worker}" ]]; then
      echo "STOP: optional ${variant} worker exists but is not a regular executable: ${source_worker}" >&2
      exit 2
    fi
    install -m 0755 "${source_worker}" "${STAGE_ROOT}/rdt_whisper_worker_${variant}"
    BUNDLED_WORKERS+=("${STAGE_ROOT}/rdt_whisper_worker_${variant}")
  fi
done

if [[ "${RDT_REQUIRE_CUDA:-0}" == "1" || "${RDT_REQUIRE_CUDA:-0}" == "ON" ]]; then
  test -x "${STAGE_ROOT}/rdt_whisper_worker_cuda" || {
    echo "STOP: required CUDA worker is missing from the Linux bundle." >&2
    exit 2
  }
fi
if [[ "${RDT_REQUIRE_VULKAN:-0}" == "1" || "${RDT_REQUIRE_VULKAN:-0}" == "ON" ]]; then
  test -x "${STAGE_ROOT}/rdt_whisper_worker_vulkan" || {
    echo "STOP: required Vulkan worker is missing from the Linux bundle." >&2
    exit 2
  }
fi
case "$(uname -m)" in
  aarch64|arm64)
    test -x "${STAGE_ROOT}/rdt_whisper_worker_dotprod" || {
      echo "STOP: required ARM64 dot-product worker is missing from the Linux bundle." >&2
      exit 2
    }
    ;;
esac

install -m 0644 "${FIXTURE_SOURCE}" "${STAGE_ROOT}/rdt_benchmark_fixture.wav"

FFMPEG_PREFIX="${STAGE_ROOT}/ffmpeg"
FFMPEG_NOTICE_ROOT="${LINUX_NOTICES_ROOT}/ffmpeg"
"${REPO_ROOT}/rdt_connect/scripts/build-linux-ffmpeg.sh" \
  --prefix "${FFMPEG_PREFIX}" \
  --notices "${FFMPEG_NOTICE_ROOT}"

FFMPEG_SMOKE="${STAGE_ROOT}/.ffmpeg-smoke.wav"
"${FFMPEG_PREFIX}/bin/ffmpeg" -hide_banner -loglevel error -y \
  -i "${FIXTURE_SOURCE}" -t 1 -ar 16000 -ac 1 -c:a pcm_s16le \
  -f wav "${FFMPEG_SMOKE}"
"${FFMPEG_PREFIX}/bin/ffprobe" -v error \
  -show_entries format=duration -of default=noprint_wrappers=1:nokey=1 \
  "${FFMPEG_SMOKE}"
rm -f -- "${FFMPEG_SMOKE}"
echo "LINUX_BUNDLED_FFMPEG_SMOKE=PASS"

if [[ -x "${STAGE_ROOT}/rdt_whisper_worker_cuda" ]]; then
  CUDA_ROOT="${CUDAToolkit_ROOT:-${CUDA_HOME:-/usr/local/cuda}}"
  if [[ ! -d "${CUDA_ROOT}" ]]; then
    echo "STOP: CUDA worker exists but CUDA Toolkit root is missing: ${CUDA_ROOT}" >&2
    exit 1
  fi
  "${PYTHON_BIN}" "${REPO_ROOT}/backend/whisper_cpp/stage_cuda_runtime.py" stage \
    --platform linux --cuda-root "${CUDA_ROOT}" \
    --destination "${STAGE_ROOT}/cuda-runtime" \
    --binary "${STAGE_ROOT}/rdt_whisper_worker_cuda"
  "${PYTHON_BIN}" "${REPO_ROOT}/backend/whisper_cpp/stage_cuda_runtime.py" attach-notices \
    --runtime-dir "${STAGE_ROOT}/cuda-runtime" \
    --notices-root "${LINUX_NOTICES_ROOT}" \
    --bundle-root "${STAGE_ROOT}"
fi

# Finalize the full notice closure after FFmpeg and optional CUDA runtime staging.
if [[ -z "${WHISPER_CPP_SOURCE:-}" || ! -d "${WHISPER_CPP_SOURCE}" ]]; then
  echo "STOP: WHISPER_CPP_SOURCE must point to the pinned whisper.cpp checkout." >&2
  exit 2
fi
"${PYTHON_BIN}" "${REPO_ROOT}/backend/linux/scripts/collect_third_party_notices.py" \
  --repo-root "${REPO_ROOT}" \
  --destination "${LINUX_NOTICES_ROOT}" \
  --whisper-source "${WHISPER_CPP_SOURCE}" \
  --ffmpeg-notices "${FFMPEG_NOTICE_ROOT}" \
  --include-python-runtime \
  --runtime-roots-file "${REPO_ROOT}/backend/linux/runtime-package-roots.txt"

# Refuse bundles with unresolved shared-library dependencies. A successful copy
# alone is not evidence the executable will run on a supported Linux system.
for binary in "${STAGE_ROOT}/rdt_voice_backend" "${BUNDLED_WORKERS[@]}"; do
  if [[ "${binary}" == "${STAGE_ROOT}/rdt_whisper_worker_cuda" ]]; then
    deps="$(env -u LD_LIBRARY_PATH LD_LIBRARY_PATH="${STAGE_ROOT}/cuda-runtime" ldd "${binary}" 2>&1 || true)"
    missing="$(grep 'not found' <<<"${deps}" | grep -vE '^[[:space:]]*libcuda\.so(\.1)?[[:space:]]+=>' || true)"
  else
    deps="$(ldd "${binary}" 2>&1 || true)"
    missing="$(grep 'not found' <<<"${deps}" || true)"
  fi
  if [[ -n "${missing}" ]]; then
    echo "STOP: missing non-driver shared library for ${binary}:" >&2
    printf '%s\n' "${deps}" >&2
    exit 1
  fi
done

"${PYTHON_BIN}" - "${STAGE_ROOT}" "${REPO_ROOT}" "${FFMPEG_NOTICE_ROOT}" <<'PY'
import hashlib, json, os, pathlib, platform, sys, time
stage = pathlib.Path(sys.argv[1])
repo = pathlib.Path(sys.argv[2])
def sha(p):
    h=hashlib.sha256()
    with p.open('rb') as f:
        for chunk in iter(lambda:f.read(1024*1024), b''):
            h.update(chunk)
    return h.hexdigest()
worker_paths = sorted(
    p for p in stage.glob("rdt_whisper_worker*")
    if p.is_file() and not p.is_symlink()
)
if not worker_paths or not (stage/"rdt_whisper_worker").is_file():
    raise SystemExit("mandatory CPU worker or worker inventory missing")
fixture = stage/"rdt_benchmark_fixture.wav"
if not fixture.is_file() or sha(fixture) != "296b1feaacbcc9e75ef6ea7b326000e290ce38efb734841691193d26bb1dc017":
    raise SystemExit("bundled benchmark fixture missing or SHA-256 mismatch")
runtime_files = (
    sorted(p for p in (stage/"cuda-runtime").rglob("*") if p.is_file() and not p.is_symlink())
    if (stage/"cuda-runtime").is_dir() else []
)
runtime_notices = (
    sorted(p for p in (stage/"cuda-runtime-notices").rglob("*") if p.is_file() and not p.is_symlink())
    if (stage/"cuda-runtime-notices").is_dir() else []
)
ffmpeg_stage_root = stage/"ffmpeg"
if not ffmpeg_stage_root.is_dir() or ffmpeg_stage_root.is_symlink():
    raise SystemExit("bundled Linux FFmpeg directory missing or unsafe")
ffmpeg_root = ffmpeg_stage_root.resolve(strict=True)

ffmpeg_paths = sorted(ffmpeg_root.rglob("*"))
ffmpeg_files = [p for p in ffmpeg_paths if p.is_file() and not p.is_symlink()]
ffmpeg_symlinks = [p for p in ffmpeg_paths if p.is_symlink()]

for executable_name in ("ffmpeg", "ffprobe"):
    executable = ffmpeg_root/"bin"/executable_name
    if executable.is_symlink() or not executable.is_file() or not os.access(executable, os.X_OK):
        raise SystemExit(f"bundled Linux executable missing or unsafe: {executable}")

ffmpeg_entries = {}
actual_regular = {}
for path in ffmpeg_files:
    relative = path.relative_to(ffmpeg_root).as_posix()
    record = {"bytes": path.stat().st_size, "sha256": sha(path)}
    actual_regular[relative] = {
        "size_bytes": record["bytes"], "sha256": record["sha256"]
    }
    ffmpeg_entries["ffmpeg/" + relative] = {
        "type": "file", **record
    }

actual_symlinks = {}
for path in ffmpeg_symlinks:
    relative = path.relative_to(ffmpeg_root).as_posix()
    link_target = os.readlink(path)
    if not link_target or pathlib.PurePosixPath(link_target).is_absolute() or "\\" in link_target:
        raise SystemExit(f"FFmpeg symlink target is not relocatable: {relative} -> {link_target}")
    try:
        resolved = path.resolve(strict=True)
        resolved_relative = resolved.relative_to(ffmpeg_root).as_posix()
    except (OSError, RuntimeError, ValueError) as exc:
        raise SystemExit(f"unsafe bundled FFmpeg symlink {relative}: {exc}") from exc

    if not resolved.is_file():
        raise SystemExit(f"FFmpeg symlink target is not a regular file: {relative}")

    record = {
        "link_target": link_target,
        "resolved_path": resolved_relative,
        "resolved_size_bytes": resolved.stat().st_size,
        "resolved_sha256": sha(resolved),
    }
    actual_symlinks[relative] = record
    ffmpeg_entries["ffmpeg/" + relative] = {
        "type": "symlink", **record
    }

ffmpeg_build_manifest_path = pathlib.Path(sys.argv[3])/"FFMPEG-BUILD-MANIFEST.json"
if ffmpeg_build_manifest_path.is_symlink() or not ffmpeg_build_manifest_path.is_file():
    raise SystemExit("FFmpeg build manifest missing or unsafe")

ffmpeg_build_manifest = json.loads(
    ffmpeg_build_manifest_path.read_text(encoding="utf-8")
)
if not isinstance(ffmpeg_build_manifest, dict):
    raise SystemExit("FFmpeg build manifest root must be a JSON object")
if (
    ffmpeg_build_manifest.get("schema") != "rdt-linux-ffmpeg-bundle-v1"
    or ffmpeg_build_manifest.get("version") != "9.1"
):
    raise SystemExit("FFmpeg build manifest schema or version mismatch")

runtime_file_records = ffmpeg_build_manifest.get("runtime_files")
runtime_symlink_records = ffmpeg_build_manifest.get("runtime_symlinks")
if not isinstance(runtime_file_records, list) or not isinstance(runtime_symlink_records, list):
    raise SystemExit("FFmpeg build manifest runtime inventories are missing or invalid")

def safe_relative_path(value):
    if not isinstance(value, str) or not value or "\\" in value:
        return False
    candidate = pathlib.PurePosixPath(value)
    return (
        not candidate.is_absolute()
        and ".." not in candidate.parts
        and candidate.as_posix() == value
    )

def valid_sha256(value):
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )

expected_regular = {}
for item in runtime_file_records:
    if not isinstance(item, dict):
        raise SystemExit("FFmpeg build manifest contains an invalid file record")
    relative = item.get("path")
    size = item.get("size_bytes")
    digest_value = item.get("sha256")
    if (
        not safe_relative_path(relative)
        or relative in expected_regular
        or type(size) is not int
        or size < 0
        or not valid_sha256(digest_value)
    ):
        raise SystemExit("FFmpeg build manifest contains a duplicate or malformed file record")
    expected_regular[relative] = {
        "size_bytes": size,
        "sha256": digest_value,
    }

expected_symlinks = {}
for item in runtime_symlink_records:
    if not isinstance(item, dict):
        raise SystemExit("FFmpeg build manifest contains an invalid symlink record")
    relative = item.get("path")
    target = item.get("link_target")
    resolved = item.get("resolved_path")
    size = item.get("resolved_size_bytes")
    digest_value = item.get("resolved_sha256")
    if (
        not safe_relative_path(relative)
        or relative in expected_regular
        or relative in expected_symlinks
        or not isinstance(target, str)
        or not target
        or "\\" in target
        or pathlib.PurePosixPath(target).is_absolute()
        or not safe_relative_path(resolved)
        or type(size) is not int
        or size < 0
        or not valid_sha256(digest_value)
    ):
        raise SystemExit("FFmpeg build manifest contains a duplicate or malformed symlink record")
    expected_symlinks[relative] = {
        "link_target": target,
        "resolved_path": resolved,
        "resolved_size_bytes": size,
        "resolved_sha256": digest_value,
    }

if (
    type(ffmpeg_build_manifest.get("runtime_file_count")) is not int
    or ffmpeg_build_manifest["runtime_file_count"] != len(runtime_file_records)
):
    raise SystemExit("FFmpeg build manifest regular-file count is inconsistent")
if (
    type(ffmpeg_build_manifest.get("runtime_symlink_count")) is not int
    or ffmpeg_build_manifest["runtime_symlink_count"] != len(runtime_symlink_records)
):
    raise SystemExit("FFmpeg build manifest symlink count is inconsistent")

if expected_regular != actual_regular:
    raise SystemExit("FFmpeg regular-file package inventory differs from build manifest")
if expected_symlinks != actual_symlinks:
    raise SystemExit("FFmpeg symlink package inventory differs from build manifest")
if (
    type(ffmpeg_build_manifest.get("runtime_size_bytes")) is not int
    or ffmpeg_build_manifest["runtime_size_bytes"] != sum(
        item["size_bytes"] for item in expected_regular.values()
    )
):
    raise SystemExit("FFmpeg build-manifest runtime size is inconsistent")

required_paths = [stage/"rdt_voice_backend", *worker_paths, fixture, *runtime_files, *runtime_notices, *ffmpeg_files]
files = {}
for p in required_paths:
    if not p.is_file() or p.is_symlink():
        raise SystemExit(f'expected regular bundled input missing: {p}')
    files[p.name]={'bytes':p.stat().st_size,'sha256':sha(p)}
manifest={
  'schema':'rdt-linux-backend-bundle-v2',
  'worker_variants':[p.name for p in worker_paths],
  'benchmark_fixture':'rdt_benchmark_fixture.wav',
  'cuda_runtime_manifest':('cuda-runtime/cuda-runtime-manifest.json' if (stage/'cuda-runtime/cuda-runtime-manifest.json').is_file() else None),
  'ffmpeg': {
      'version': ffmpeg_build_manifest['version'],
      'build_manifest_sha256': sha(ffmpeg_build_manifest_path),
      'files': ffmpeg_entries,
      'regular_file_count': len(ffmpeg_files),
      'symlink_count': len(ffmpeg_symlinks),
      'runtime_size_bytes': sum(p.stat().st_size for p in ffmpeg_files),
  },
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
