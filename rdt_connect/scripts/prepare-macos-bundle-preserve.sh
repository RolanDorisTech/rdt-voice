#!/bin/bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
REPO_ROOT_INPUT="${1:-}"
if [[ -z "$REPO_ROOT_INPUT" ]]; then
  echo "Usage: bash scripts/prepare-macos-bundle-preserve.sh /path/to/connect-git-root-or-rdt_connect" >&2
  exit 2
fi
if ! REPO_ROOT="$(cd "$REPO_ROOT_INPUT" 2>/dev/null && pwd)"; then
  echo "ERROR: supplied repository root does not exist: $REPO_ROOT_INPUT" >&2
  exit 2
fi

# Accept either the Git root (which contains rdt_connect/vendor) or the
# nested Connect checkout (which contains vendor). Normalize to the Git root
# because the offline-runtime preparer resolves rdt_connect/vendor from it.
if [[ -d "$REPO_ROOT/rdt_connect/vendor/rdt-offline-runtime" ]]; then
  :
elif [[ "$(basename "$REPO_ROOT")" == "rdt_connect" &&
        -d "$REPO_ROOT/vendor/rdt-offline-runtime" ]]; then
  CONNECT_CHECKOUT="$REPO_ROOT"
  REPO_ROOT="$(cd "$CONNECT_CHECKOUT/.." && pwd)"
  echo "INFO: normalized nested Connect checkout to Git root: $REPO_ROOT"
else
  echo "ERROR: expected either <git-root>/rdt_connect/vendor/rdt-offline-runtime or <rdt_connect>/vendor/rdt-offline-runtime." >&2
  echo "Supplied path: $REPO_ROOT_INPUT" >&2
  exit 2
fi

RUNTIME_VENDOR="$REPO_ROOT/rdt_connect/vendor/rdt-offline-runtime"
for required in \
  "$REPO_ROOT/backend/rdt_model_lock.json" \
  "$REPO_ROOT/assets/rdt_logo_BIG_yellow_halo_trans_bg.png" \
  "$RUNTIME_VENDOR/runtime-assets-v1/runtime-manifest.json" \
  "$RUNTIME_VENDOR/rdt-easywhisper-compat-sequential-r71.min.js"; do
  if [[ ! -s "$required" ]]; then
    echo "ERROR: required Connect packaging input is missing: $required" >&2
    exit 2
  fi
done
echo "PASS: normalized repository root and all pinned runtime inputs verified."
VOICE_WEB_SOURCE="${RDT_VOICE_WEB_SOURCE:-$HOME/dev/rdt-voice/alpha_testing}"
if [[ ! -d "$VOICE_WEB_SOURCE" ]]; then echo "ERROR: canonical RDT Voice Alpha Testing source missing: $VOICE_WEB_SOURCE" >&2; exit 2; fi
VOICE_WEB_SOURCE="$(cd "$VOICE_WEB_SOURCE" && pwd)"
CONNECT_BACKEND_SOURCE="${RDT_VOICE_BACKEND_SOURCE:-$HOME/dev/rdt-voice/backend/server.py}"
if [[ ! -s "$CONNECT_BACKEND_SOURCE" ]]; then echo "ERROR: canonical RDT Voice backend source missing: $CONNECT_BACKEND_SOURCE" >&2; exit 2; fi
echo "PASS: using canonical RDT Voice frontend and backend sources."
# Keep PyInstaller analysis inside this candidate; ignore paths inherited
# from the user's interactive shell and unrelated repository checkouts.
cd "$PROJECT_DIR"
if [[ -n "${PYTHONPATH:-}" ]]; then
  echo "NOTE: ignoring inherited PYTHONPATH for isolated packaging."
fi
unset PYTHONPATH
if [[ "$(uname -s)" != "Darwin" || "$(uname -m)" != "arm64" ]]; then echo "ERROR: Build this macOS MLX bundle on an Apple Silicon Mac using native arm64 Python." >&2; exit 2; fi
for required in index.html worker.js pcm-worklet.js models.manifest.json locales assets/opencc-js-1.4.2-full.js; do
  if [[ ! -e "$VOICE_WEB_SOURCE/$required" ]]; then
    echo "ERROR: Required canonical Voice item is missing: $VOICE_WEB_SOURCE/$required" >&2
    exit 2
  fi
done
for required in backend/rdt_model_lock.json assets/rdt_logo_BIG_yellow_halo_trans_bg.png; do
  if [[ ! -e "$REPO_ROOT/$required" ]]; then
    echo "ERROR: Required Connect build item is missing: $REPO_ROOT/$required" >&2
    exit 2
  fi
done
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
RESOURCES="$PROJECT_DIR/src-tauri/resources"

if [[ ! -s "$RESOURCES/rdt_voice_web_manifest.json" ]]; then
  echo "ERROR: required Voice web manifest is missing: $RESOURCES/rdt_voice_web_manifest.json" >&2
  exit 4
fi
if [[ ! -d "$RESOURCES/rdt_voice_web/assets" ]]; then
  echo "ERROR: required Voice web reference assets are missing: $RESOURCES/rdt_voice_web/assets" >&2
  exit 4
fi
RUN_ID="$(date -u +%Y%m%dT%H%M%SZ)-$$"
RUN_ROOT="$PROJECT_DIR/build/macos-bundle-preserve-$RUN_ID"
NEXT_RESOURCES="$RUN_ROOT/desktop-resources-new"
WEB="$NEXT_RESOURCES/rdt_voice_web"; BACKEND="$NEXT_RESOURCES/rdt_voice_backend"; NOTICES="$NEXT_RESOURCES/third_party_notices"
STAGE="$RUN_ROOT/desktop-backend-stage"; WORK="$RUN_ROOT/pyinstaller-work"; SPEC="$RUN_ROOT/pyinstaller-spec"
# Preservation-safe build: every run receives a unique directory, retained for inspection.
if [[ -e "$RUN_ROOT" ]]; then
  echo "ERROR: unique run directory already exists; preserving it: $RUN_ROOT" >&2
  exit 3
fi
mkdir -p "$WEB" "$BACKEND" "$NOTICES" "$STAGE" "$WORK" "$SPEC" "$PROJECT_DIR/build"
cp -p "$RESOURCES/rdt_voice_web_manifest.json" "$NEXT_RESOURCES/rdt_voice_web_manifest.json"
find "$VOICE_WEB_SOURCE" -maxdepth 1 -type f ! -name '.DS_Store' ! -name '*.bak' ! -name '*.bak.*' -exec cp -p {} "$WEB/" ';'
cp -R "$VOICE_WEB_SOURCE/locales" "$WEB/locales"
find "$WEB/locales" -type f \( -name "*.bak" -o -name "*.bak.*" \) -delete
mkdir -p "$WEB/assets"
cp -R "$REPO_ROOT/assets/." "$WEB/assets/"
"$PY" "$SCRIPT_DIR/prepare-offline-runtime.py" --repo-root "$REPO_ROOT" --web-root "$WEB" --notices "$NOTICES"
LOGO_SOURCE="$REPO_ROOT/assets/rdt_logo_BIG_yellow_halo_trans_bg.png"
if [[ ! -s "$PROJECT_DIR/public/rdt-brand-icon.png" ]] || ! cmp -s "$LOGO_SOURCE" "$PROJECT_DIR/public/rdt-brand-icon.png"; then
  echo "ERROR: existing public logo is missing or differs from the source; refusing to overwrite it." >&2
  exit 4
fi
TAURI_CLI="$PROJECT_DIR/node_modules/.bin/tauri"
if [[ ! -x "$TAURI_CLI" ]]; then echo "ERROR: Tauri CLI missing. Run npm install once from the desktop folder." >&2; exit 4; fi
ICON_DIR="$PROJECT_DIR/src-tauri/icons"
for icon_file in icon.png icon.icns 32x32.png 128x128.png 128x128@2x.png; do
  if [[ ! -s "$ICON_DIR/$icon_file" ]]; then
    echo "ERROR: existing app icon is missing; refusing to regenerate or overwrite icons." >&2
    exit 4
  fi
done
"$PY" "$SCRIPT_DIR/patch_desktop_backend.py" "$CONNECT_BACKEND_SOURCE" "$STAGE/server.py"
if ! grep -Fq '@app.get("/assets/{asset_path:path}")' "$STAGE/server.py"; then
  echo "ERROR: staged canonical backend is missing the Connect same-origin asset route." >&2
  exit 4
fi
echo "PASS: canonical Voice backend staged with Connect desktop adaptations."
ARGS=(--runtime-hook "$SCRIPT_DIR/pyi_rth_rdt_resource_tracker.py" --noconfirm --onedir --add-data "$REPO_ROOT/backend/rdt_model_lock.json:." --name rdt_voice_backend --distpath "$NEXT_RESOURCES" --workpath "$WORK" --specpath "$SPEC" --collect-all mlx --collect-data mlx_whisper --collect-all fastapi --collect-all uvicorn --collect-all huggingface_hub --collect-all qrcode --collect-all multipart --hidden-import qrcode.image.svg --exclude-module torch)
if "$PY" -c 'import importlib.util,sys; sys.exit(0 if importlib.util.find_spec("mlx_metal") else 1)'; then ARGS+=(--collect-all mlx_metal); fi
"$PY" -m PyInstaller "${ARGS[@]}" "$STAGE/server.py"
if [[ ! -x "$BACKEND/rdt_voice_backend" ]]; then echo "ERROR: PyInstaller output missing; preserving previous resources." >&2; exit 4; fi
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
FOUND_METALLIB="$(find "$BACKEND" -type f -name mlx.metallib -print -quit || true)"
if [[ -z "$FOUND_METALLIB" ]]; then echo "ERROR: frozen backend has no mlx.metallib; preserving previous resources." >&2; exit 4; fi
while IFS= read -r native; do target="$(dirname "$native")/mlx.metallib"; if [[ "$target" != "$FOUND_METALLIB" ]]; then cp -f "$FOUND_METALLIB" "$target"; fi; done < <(find "$BACKEND" -type f \( -name 'core*.so' -o -name 'libmlx.dylib' \) -print)
# RDT Connect MLX default-metallib path fix (packaging).
# PyInstaller may expose libmlx.dylib at _internal/libmlx.dylib. MLX looks for
# its default Metal shader beside that loaded library, so provide the sibling
# path in addition to the copies beside the nested MLX native libraries.
MLX_INTERNAL="$BACKEND/_internal"
if [[ -s "$MLX_INTERNAL/mlx/mlx.metallib" ]]; then
  ln -sfn "mlx/mlx.metallib" "$MLX_INTERNAL/mlx.metallib"
else
  echo "ERROR: Required MLX shader missing: $MLX_INTERNAL/mlx/mlx.metallib; preserving previous resources." >&2
  exit 4
fi

# Keep one physical MLX shader and relative aliases in preserve mode,
# matching the standard packager and the app-bundle size invariant.
if ! python3 "$SCRIPT_DIR/dedupe-macos-mlx-metallib.py" --internal-dir "$MLX_INTERNAL"; then
  echo "ERROR: could not normalize frozen-backend MLX shader aliases; preserving previous resources." >&2
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
# Validate staged outputs only. Live resources are deliberately not promoted.
if [[ ! -s "$WEB/index.html" || ! -s "$WEB/worker.js" || ! -x "$BACKEND/rdt_voice_backend" ]]; then
  echo "ERROR: staged frontend or backend is incomplete; all staged files are retained." >&2
  exit 5
fi
"$PY" - "$WEB" <<'RDT_RUNTIME_FINAL_CHECK'
import pathlib, sys
root = pathlib.Path(sys.argv[1])
required = [
    "assets/rdt-offline-runtime/modern/transformers.min.js",
    "assets/rdt-offline-runtime/modern/ort.bundle.min.mjs",
    "assets/rdt-offline-runtime/modern/ort-wasm-simd-threaded.jsep.wasm",
    "assets/rdt-offline-runtime/modern/rdt-easywhisper-compat-sequential-r71.min.js",
    "assets/rdt-offline-runtime/legacy/ort.wasm.min.js",
    "assets/rdt-offline-runtime/legacy/ort-wasm.wasm",
]
for name in required:
    p = root / name
    if not p.is_file() or p.stat().st_size == 0:
        raise SystemExit(f"Missing staged runtime asset: {name}")
scan = [root / "worker.js"] + list((root / "assets/rdt-offline-runtime").rglob("*.js")) + list((root / "assets/rdt-offline-runtime").rglob("*.mjs"))
for p in scan:
    text = p.read_text(encoding="utf-8", errors="replace").lower()
    for host in ("cdn.jsdelivr.net", "cdnjs.cloudflare.com", "unpkg.com"):
        if host in text:
            raise SystemExit(f"Runtime CDN reference remains in {p.relative_to(root)}: {host}")
print(f"PASS: all {len(required)} required runtime paths exist")
print(f"PASS: no CDN references in {len(scan)} staged worker/runtime JS files")
RDT_RUNTIME_FINAL_CHECK

python3 "$SCRIPT_DIR/verify-alpha-r82-staging.py" \
  "$VOICE_WEB_SOURCE" "$WEB" "$RESOURCES/rdt_voice_web/assets" \
  "${RDT_VOICE_BUNDLE_VERSION:-alpha-testing-current}" "$NEXT_RESOURCES/rdt_voice_web_manifest.json"

echo
echo "Preservation-safe macOS resources staged successfully."
echo "Frontend: $WEB"
echo "Backend: $BACKEND/rdt_voice_backend"
echo "Notices: $NOTICES"
echo "Retained build directory: $RUN_ROOT"
echo "Existing src-tauri/resources, app icons, and public logo were not replaced."
echo "No staging or backup files were deleted, and live resources were not promoted."
