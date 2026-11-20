#!/bin/bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
REPO_ROOT="${1:-}"
if [[ -z "$REPO_ROOT" ]]; then echo "Usage: bash scripts/prepare-macos-bundle.sh /path/to/rdt-voice-repository" >&2; exit 2; fi
REPO_ROOT="$(cd "$REPO_ROOT" && pwd)"
VOICE_WEB_SOURCE="${RDT_VOICE_WEB_SOURCE:-$HOME/dev/rdt-voice/alpha_testing}"
VOICE_WEB_SOURCE="$(cd "$VOICE_WEB_SOURCE" && pwd)"
OFFLINE_RUNTIME_TOOL="$SCRIPT_DIR/prepare-offline-runtime.py"
if [[ ! -s "$OFFLINE_RUNTIME_TOOL" ]]; then
  echo "ERROR: pinned offline runtime preparation tool is missing." >&2
  exit 4
fi
if [[ "$(uname -s)" != "Darwin" || "$(uname -m)" != "arm64" ]]; then echo "ERROR: Build this macOS MLX bundle on an Apple Silicon Mac using native arm64 Python." >&2; exit 2; fi
for required in index.html worker.js pcm-worklet.js models.manifest.json locales assets/opencc-js-1.4.2-full.js; do
  if [[ ! -e "$VOICE_WEB_SOURCE/$required" ]]; then echo "ERROR: current Alpha Testing item missing: $VOICE_WEB_SOURCE/$required" >&2; exit 2; fi
done
for required in backend/rdt_model_lock.json assets/rdt_logo_BIG_yellow_halo_trans_bg.png; do
  if [[ ! -e "$REPO_ROOT/$required" ]]; then echo "ERROR: Connect build source item missing: $REPO_ROOT/$required" >&2; exit 2; fi
done

CONNECT_BACKEND_SOURCE="${RDT_VOICE_BACKEND_SOURCE:-$HOME/dev/rdt-voice/backend/server.py}"
if [[ ! -s "$CONNECT_BACKEND_SOURCE" ]]; then
  echo "ERROR: canonical RDT Voice backend source missing: $CONNECT_BACKEND_SOURCE" >&2
  exit 2
fi
echo "PASS: using canonical RDT Voice backend source."
DEFAULT_PY="$HOME/dev/testing/RDT_Voice_Desktop_POC/.venv/bin/python"
if [[ ! -x "$DEFAULT_PY" ]]; then DEFAULT_PY="$HOME/dev/rdt-secure/servers/whisper/.venv/bin/python"; fi
if [[ ! -x "$DEFAULT_PY" ]]; then DEFAULT_PY="$HOME/mlx-env/bin/python"; fi
PY="${RDT_BUILD_PYTHON:-$DEFAULT_PY}"
if [[ ! -x "$PY" ]]; then echo "ERROR: No usable Python found. Set RDT_BUILD_PYTHON to the isolated native MLX build environment." >&2; exit 2; fi
"$PY" - <<'PY'
import importlib.util, importlib.metadata, platform, sys
print(f"Build Python: {sys.executable}")
print(f"Python: {sys.version.split()[0]} / machine={platform.machine()}")
missing = [name for name in ("mlx_whisper", "mlx", "fastapi", "uvicorn", "numpy", "multipart", "qrcode", "imageio_ffmpeg", "PyInstaller") if importlib.util.find_spec(name) is None]
if missing:
 print("Missing build modules: " + ", ".join(missing)); raise SystemExit("Install missing modules in the dedicated build environment before rebuilding.")
try:
 version = importlib.metadata.version("mlx-whisper")
except importlib.metadata.PackageNotFoundError:
 raise SystemExit("mlx-whisper distribution metadata is missing from the build environment.")
if version != "0.4.3":
 raise SystemExit(f"Expected locked mlx-whisper==0.4.3, found {version}; align the build venv before packaging.")
PY
BUILD_STAGING_ROOT="${RDT_MACOS_BUILD_STAGING_ROOT:-$PROJECT_DIR/build}"
RESOURCES="$PROJECT_DIR/src-tauri/resources"
NEXT_RESOURCES="$BUILD_STAGING_ROOT/desktop-resources-new"
OLD_RESOURCES="$BUILD_STAGING_ROOT/desktop-resources-old"
WEB="$NEXT_RESOURCES/rdt_voice_web"; BACKEND="$NEXT_RESOURCES/rdt_voice_backend"; NOTICES="$NEXT_RESOURCES/third_party_notices"

# Resume-only mode: promote an already verified staging tree without rerunning PyInstaller.
if [[ "${RDT_PROMOTE_STAGED_ONLY:-0}" == "1" ]]; then
  [[ -d "$WEB" && -d "$BACKEND" && -d "$NOTICES" ]] || {
    echo "ERROR: staged frontend, backend, or notices directory is missing." >&2
    exit 4
  }
  [[ -f "$NEXT_RESOURCES/rdt_voice_web_manifest.json" ]] || {
    echo "ERROR: staged R82 manifest is missing." >&2
    exit 4
  }

  python3 "$SCRIPT_DIR/verify-alpha-r82-staging.py" \
    "$VOICE_WEB_SOURCE" "$WEB" "$WEB/assets" \
    "${RDT_VOICE_BUNDLE_VERSION:-alpha-testing-current}" \
    "$NEXT_RESOURCES/rdt_voice_web_manifest.json"

  for item in \
    "$WEB/index.html" \
    "$WEB/worker.js" \
    "$BACKEND/rdt_voice_backend" \
    "$BACKEND/ffmpeg" \
    "$BACKEND/_internal/libomp.dylib" \
    "$BACKEND/_internal/mlx/lib/mlx.metallib" \
    "$NOTICES/BUILD-NOTICES.md" \
    "$NOTICES/libomp/LICENSE.TXT"; do
    [[ -s "$item" ]] || {
      echo "ERROR: staged resource missing or empty: $item" >&2
      exit 4
    }
  done

  [[ ! -e "$OLD_RESOURCES" ]] || {
    echo "ERROR: backup destination already exists; preserving it: $OLD_RESOURCES" >&2
    exit 4
  }

  mkdir -p "$RESOURCES" "$OLD_RESOURCES"
  PROMOTED=()
  BACKED_UP=()

  rollback_promotion() {
    local name
    for ((i=${#PROMOTED[@]}-1; i>=0; i--)); do
      name="${PROMOTED[$i]}"
      if [[ -e "$RESOURCES/$name" ]]; then
        mv "$RESOURCES/$name" "$NEXT_RESOURCES/$name"
      fi
    done
    for ((i=${#BACKED_UP[@]}-1; i>=0; i--)); do
      name="${BACKED_UP[$i]}"
      if [[ -e "$OLD_RESOURCES/$name" ]]; then
        mv "$OLD_RESOURCES/$name" "$RESOURCES/$name"
      fi
    done
  }

  for name in rdt_voice_web rdt_voice_backend third_party_notices rdt_voice_web_manifest.json; do
    if [[ -e "$RESOURCES/$name" ]]; then
      mv "$RESOURCES/$name" "$OLD_RESOURCES/$name"
      BACKED_UP+=("$name")
    fi
    if ! mv "$NEXT_RESOURCES/$name" "$RESOURCES/$name"; then
      echo "ERROR: promotion failed for $name; rolling back." >&2
      rollback_promotion
      exit 5
    fi
    PROMOTED+=("$name")
  done

  echo "PASS: R82 resources promoted using guarded resume path."
  echo "Previous resources backup retained at: $OLD_RESOURCES"
  exit 0
fi
STAGE="$BUILD_STAGING_ROOT/desktop-backend-stage"; WORK="$BUILD_STAGING_ROOT/pyinstaller-work"; SPEC="$BUILD_STAGING_ROOT/pyinstaller-spec"
# Build everything in staging. Do not destroy the current runnable resources until the replacement succeeds.
rm -rf "$NEXT_RESOURCES" "$OLD_RESOURCES" "$STAGE" "$WORK" "$SPEC"
mkdir -p "$WEB" "$BACKEND" "$NOTICES" "$STAGE" "$WORK" "$SPEC" "$BUILD_STAGING_ROOT" "$PROJECT_DIR/public"
mkdir -p "$WEB/assets"
cp -R "$REPO_ROOT/assets/." "$WEB/assets/"
find "$VOICE_WEB_SOURCE" -maxdepth 1 -type f ! -name '.DS_Store' ! -name '*.bak' ! -name '*.bak.*' -exec cp -p {} "$WEB/" \;
cp -R "$VOICE_WEB_SOURCE/locales" "$WEB/locales"
# Exclude Alpha editor backups from the packaged locale tree.
find "$WEB/locales" -type f \( -name "*.bak" -o -name "*.bak.*" \) -delete
cp -p "$RESOURCES/rdt_voice_web_manifest.json" "$NEXT_RESOURCES/rdt_voice_web_manifest.json"
# Retain Connect-specific offline help and UI images from the existing resource set.
for app_asset in RDT_Connect_Installation_Guide.png rdt-connect-server-running.png rdt-connect-server-stopped.png rdt-connect-windows-installation.png rdt-voice-EasySetup.png rdt-voice-ui.png rdt_logo_BIG_yellow_halo_trans_bg.png; do
  if [[ -f "$RESOURCES/rdt_voice_web/assets/$app_asset" ]]; then
    cp -p "$RESOURCES/rdt_voice_web/assets/$app_asset" "$WEB/assets/$app_asset"
  fi
done

LOGO_SOURCE="$REPO_ROOT/assets/rdt_logo_BIG_yellow_halo_trans_bg.png"
cp "$LOGO_SOURCE" "$PROJECT_DIR/public/rdt-brand-icon.png"
TAURI_CLI="$PROJECT_DIR/node_modules/.bin/tauri"
if [[ ! -x "$TAURI_CLI" ]]; then echo "ERROR: Tauri CLI missing. Run npm install once from the desktop folder." >&2; exit 4; fi
ICON_DIR="$PROJECT_DIR/src-tauri/icons"; mkdir -p "$ICON_DIR"
"$TAURI_CLI" icon "$LOGO_SOURCE" --output "$ICON_DIR"
for icon_file in icon.png icon.icns 32x32.png 128x128.png 128x128@2x.png; do if [[ ! -s "$ICON_DIR/$icon_file" ]]; then echo "ERROR: Tauri icon generation did not create $ICON_DIR/$icon_file." >&2; exit 4; fi; done
"$PY" "$SCRIPT_DIR/patch_desktop_backend.py" "$CONNECT_BACKEND_SOURCE" "$STAGE/server.py"
if ! grep -Fq '@app.get("/assets/{asset_path:path}")' "$STAGE/server.py"; then
  echo "ERROR: staged backend is missing the Connect same-origin asset route." >&2
  exit 4
fi
echo "PASS: canonical Voice backend staged with Connect desktop adaptations."
ARGS=(--runtime-hook "$SCRIPT_DIR/pyi_rth_rdt_resource_tracker.py" --noconfirm --clean --onedir --add-data "$REPO_ROOT/backend/rdt_model_lock.json:." --name rdt_voice_backend --distpath "$NEXT_RESOURCES" --workpath "$WORK" --specpath "$SPEC" --collect-all mlx --collect-data mlx_whisper --collect-all fastapi --collect-all uvicorn --collect-all huggingface_hub --collect-all qrcode --collect-all multipart --hidden-import qrcode.image.svg --exclude-module torch)
if "$PY" -c 'import importlib.util,sys; sys.exit(0 if importlib.util.find_spec("mlx_metal") else 1)'; then ARGS+=(--collect-all mlx_metal); fi
"$PY" -m PyInstaller "${ARGS[@]}" "$STAGE/server.py"
if [[ ! -x "$BACKEND/rdt_voice_backend" ]]; then echo "ERROR: PyInstaller output missing; preserving previous resources." >&2; exit 4; fi

# Keep both MLX dylib lookup paths while storing each identical library once.
"$PY" "$SCRIPT_DIR/dedupe-macos-mlx-dylibs.py" "$BACKEND/_internal"
FFMPEG_SOURCE="$("$PY" -c 'import imageio_ffmpeg; print(imageio_ffmpeg.get_ffmpeg_exe())')"
if [[ ! -x "$FFMPEG_SOURCE" ]]; then echo "ERROR: bundled FFmpeg not executable; preserving previous resources." >&2; exit 4; fi
cp -f "$FFMPEG_SOURCE" "$BACKEND/ffmpeg"; chmod 755 "$BACKEND/ffmpeg"
# RDT Connect libomp packaging fix: bundle Numba's OpenMP runtime.
# omppool's @rpath/../../.. resolves to this backend _internal directory.
LIBOMP_SOURCE="/opt/homebrew/opt/libomp/lib/libomp.dylib"
if [[ ! -s "$LIBOMP_SOURCE" ]]; then
  echo "ERROR: Homebrew libomp.dylib missing: $LIBOMP_SOURCE" >&2
  exit 4
fi
if ! /usr/bin/lipo -verify_arch arm64 "$LIBOMP_SOURCE" >/dev/null 2>&1; then
  echo "ERROR: libomp.dylib is not an arm64 library." >&2
  exit 4
fi
if [[ ! -d "$BACKEND/_internal/numba/np/ufunc" ]]; then
  echo "ERROR: expected Numba ufunc directory missing; refusing libomp packaging." >&2
  exit 4
fi
OMPPOOL="$(find "$BACKEND/_internal/numba/np/ufunc" -maxdepth 1 -type f -name 'omppool*.so' -print -quit)"
if [[ -z "$OMPPOOL" ]] || ! /usr/bin/otool -L "$OMPPOOL" | grep -Fq '@rpath/libomp.dylib'; then
  echo "ERROR: expected Numba @rpath/libomp.dylib dependency was not found." >&2
  exit 4
fi
install -m 644 "$LIBOMP_SOURCE" "$BACKEND/_internal/libomp.dylib"
if [[ ! -s "$BACKEND/_internal/libomp.dylib" ]]; then
  echo "ERROR: failed to bundle libomp.dylib." >&2
  exit 4
fi
OMPPOOL_RPATH_DIR="$(cd "$(dirname "$OMPPOOL")/../../.." && pwd)"
if ! /usr/bin/otool -l "$OMPPOOL" | grep -Fq 'path @loader_path/../../..'; then
  echo "ERROR: Numba omppool is missing its verified relative libomp runpath." >&2
  exit 4
fi
if [[ "$OMPPOOL_RPATH_DIR" != "$(cd "$BACKEND/_internal" && pwd)" ]] ||
   ! cmp -s "$LIBOMP_SOURCE" "$BACKEND/_internal/libomp.dylib"; then
  echo "ERROR: bundled libomp does not match Numba's resolved _internal runtime." >&2
  exit 4
fi
echo "PASS: Numba @rpath resolves to the bundled arm64 libomp.dylib."
# Normalize the frozen backend: one physical shader beside top-level libmlx.dylib.
MLX_INTERNAL="$BACKEND/_internal"
FOUND_METALLIB="$(find "$MLX_INTERNAL" -type f -name mlx.metallib -print -quit || true)"
if [[ -z "$FOUND_METALLIB" ]]; then
  echo "ERROR: frozen backend has no physical mlx.metallib; preserving previous resources." >&2
  exit 4
fi
if ! python3 "$SCRIPT_DIR/dedupe-macos-mlx-metallib.py" --internal-dir "$MLX_INTERNAL"; then
  echo "ERROR: could not normalize frozen-backend MLX shader copies." >&2
  exit 4
fi

if [[ -d "$REPO_ROOT/licenses/third-party/opencc-js" ]]; then mkdir -p "$NOTICES/opencc-js"; cp -R "$REPO_ROOT/licenses/third-party/opencc-js/." "$NOTICES/opencc-js/"; else echo "ERROR: OpenCC license notices missing; preserving previous resources." >&2; exit 2; fi
LIBOMP_CELLAR="$(brew --cellar libomp 2>/dev/null || true)"
LIBOMP_LICENSE_SOURCE=""
if [[ -n "$LIBOMP_CELLAR" && -d "$LIBOMP_CELLAR" ]]; then
  LIBOMP_LICENSE_SOURCE="$(find "$LIBOMP_CELLAR" -maxdepth 3 -type f -iname 'LICENSE.TXT' -print -quit)"
fi
if [[ -z "$LIBOMP_LICENSE_SOURCE" || ! -s "$LIBOMP_LICENSE_SOURCE" ]]; then
  echo "ERROR: Homebrew libomp license not found; preserving previous resources." >&2
  exit 2
fi
mkdir -p "$NOTICES/libomp"
cp -f "$LIBOMP_LICENSE_SOURCE" "$NOTICES/libomp/LICENSE.TXT"
cat > "$NOTICES/BUILD-NOTICES.md" <<'EOF'
# RDT Connect — bundled component notice index

This development bundle contains MLX Whisper, MLX native dependencies, the LLVM OpenMP runtime (libomp.dylib), FFmpeg from imageio-ffmpeg, OpenCC-JS and OpenCC dictionary data, and the RDT Voice frontend (including the original logo and local OpenCC bundle). The OpenMP runtime license is included at libomp/LICENSE.TXT.

Model weights are not bundled in the installer. The selected model is resolved only from the exact reviewed commit in backend/rdt_model_lock.json. RDT Connect model downloads use the official Hugging Face endpoint; inference in the managed desktop app is local-cache-only and refuses an unverified or incomplete snapshot. The desktop backend does not scan the full drive for models and skips artificial inference during server startup.

This is not a complete transitive license audit. Public distribution still requires license review, clean-Mac testing, code signing and notarization.
EOF
"$PY" -m py_compile "$STAGE/server.py"


# Bundle pinned local Transformers.js and ONNX Runtime assets for offline inference.
"$PY" "$OFFLINE_RUNTIME_TOOL" \
  --repo-root "$PROJECT_DIR/.." \
  --web-root "$WEB" \
  --notices "$NOTICES"

for runtime_file in \
  assets/rdt-offline-runtime/modern/transformers.min.js \
  assets/rdt-offline-runtime/modern/ort.bundle.min.mjs \
  assets/rdt-offline-runtime/modern/ort-wasm-simd-threaded.jsep.wasm \
  assets/rdt-offline-runtime/modern/rdt-easywhisper-compat-sequential-r71.min.js \
  assets/rdt-offline-runtime/legacy/ort.wasm.min.js \
  assets/rdt-offline-runtime/legacy/ort-wasm.wasm; do
  if [[ ! -s "$WEB/$runtime_file" ]]; then
    echo "ERROR: offline runtime asset missing after preparation: $runtime_file" >&2
    exit 4
  fi
done
echo "PASS: staged RDT Voice worker uses locally bundled inference runtime."

# Fail closed against the exact current Alpha Testing runtime and Connect support assets.
python3 "$SCRIPT_DIR/verify-alpha-r82-staging.py" \
  "$VOICE_WEB_SOURCE" "$WEB" "$RESOURCES/rdt_voice_web/assets" \
  "${RDT_VOICE_BUNDLE_VERSION:-alpha-testing-current}" "$NEXT_RESOURCES/rdt_voice_web_manifest.json"

mkdir -p "$RESOURCES" "$OLD_RESOURCES"
# Promote all staged resources. If any move fails, roll back every resource already promoted.
PROMOTED=""
for name in rdt_voice_web rdt_voice_backend third_party_notices rdt_voice_web_manifest.json; do
  if [[ -e "$RESOURCES/$name" ]]; then mv "$RESOURCES/$name" "$OLD_RESOURCES/$name"; fi
  if ! mv "$NEXT_RESOURCES/$name" "$RESOURCES/$name"; then
    echo "ERROR: Promotion failed for $name; restoring the previous resource set." >&2
    rm -rf "$RESOURCES/$name"
    if [[ -e "$OLD_RESOURCES/$name" ]]; then mv "$OLD_RESOURCES/$name" "$RESOURCES/$name"; fi
    for previous in $PROMOTED; do
      rm -rf "$RESOURCES/$previous"
      if [[ -e "$OLD_RESOURCES/$previous" ]]; then mv "$OLD_RESOURCES/$previous" "$RESOURCES/$previous"; fi
    done
    exit 5
  fi
  PROMOTED="$PROMOTED $name"
done
rm -rf "$NEXT_RESOURCES" "$OLD_RESOURCES" "$STAGE" "$WORK" "$SPEC"
echo; echo "RDT Connect resources prepared successfully:"; echo "  Frontend: $RESOURCES/rdt_voice_web"; echo "  Backend:  $RESOURCES/rdt_voice_backend/rdt_voice_backend"; echo "  Notices:  $RESOURCES/third_party_notices"; echo "  App icon: $LOGO_SOURCE"; echo "  MLX warmup skipped; model loads only for a real transcription request."
