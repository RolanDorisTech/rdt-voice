#!/bin/bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
REPO_ROOT="${1:-}"
if [[ -z "$REPO_ROOT" ]]; then echo "Usage: bash scripts/prepare-macos-bundle.sh /path/to/rdt-voice-repository" >&2; exit 2; fi
REPO_ROOT="$(cd "$REPO_ROOT" && pwd)"
# Keep PyInstaller analysis inside this candidate; ignore paths inherited
# from the user's interactive shell and unrelated repository checkouts.
cd "$PROJECT_DIR"
if [[ -n "${PYTHONPATH:-}" ]]; then
  echo "NOTE: ignoring inherited PYTHONPATH for isolated packaging."
fi
unset PYTHONPATH
if [[ "$(uname -s)" != "Darwin" || "$(uname -m)" != "arm64" ]]; then echo "ERROR: Build this macOS MLX bundle on an Apple Silicon Mac using native arm64 Python." >&2; exit 2; fi
for required in index.html worker.js pcm-worklet.js models.manifest.json backend/apple/server.py backend/rdt_model_lock.json locales assets/opencc-js-1.4.2-full.js assets/rdt_logo_BIG_yellow_halo_trans_bg.png; do
  if [[ ! -e "$REPO_ROOT/$required" ]]; then echo "ERROR: Required repository item is missing: $REPO_ROOT/$required" >&2; exit 2; fi
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
cp "$REPO_ROOT/index.html" "$WEB/index.html"; cp "$REPO_ROOT/worker.js" "$WEB/worker.js"; cp "$REPO_ROOT/pcm-worklet.js" "$WEB/pcm-worklet.js"
cp "$REPO_ROOT/models.manifest.json" "$WEB/models.manifest.json"; cp -R "$REPO_ROOT/locales" "$WEB/locales"; cp -R "$REPO_ROOT/assets" "$WEB/assets"
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
"$PY" "$SCRIPT_DIR/patch_desktop_backend.py" "$REPO_ROOT/backend/apple/server.py" "$STAGE/server.py"
ARGS=(--noconfirm --onedir --add-data "$REPO_ROOT/backend/rdt_model_lock.json:." --name rdt_voice_backend --distpath "$NEXT_RESOURCES" --workpath "$WORK" --specpath "$SPEC" --collect-all mlx --collect-data mlx_whisper --collect-all fastapi --collect-all uvicorn --collect-all huggingface_hub --collect-all qrcode --collect-all multipart --hidden-import qrcode.image.svg --exclude-module torch)
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

echo
echo "Preservation-safe macOS resources staged successfully."
echo "Frontend: $WEB"
echo "Backend: $BACKEND/rdt_voice_backend"
echo "Notices: $NOTICES"
echo "Retained build directory: $RUN_ROOT"
echo "Existing src-tauri/resources, app icons, and public logo were not replaced."
echo "No staging or backup files were deleted, and live resources were not promoted."
