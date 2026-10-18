#!/bin/bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
REPO_ROOT="${1:-}"
if [[ -z "$REPO_ROOT" ]]; then echo "Usage: bash scripts/prepare-macos-bundle.sh /path/to/rdt-voice-repository" >&2; exit 2; fi
REPO_ROOT="$(cd "$REPO_ROOT" && pwd)"
if [[ "$(uname -s)" != "Darwin" || "$(uname -m)" != "arm64" ]]; then echo "ERROR: Build this macOS MLX bundle on an Apple Silicon Mac using native arm64 Python." >&2; exit 2; fi
for required in index.html worker.js pcm-worklet.js models.manifest.json backend/server.py locales assets/opencc-js-1.4.2-full.js assets/rdt_logo_BIG_yellow_halo_trans_bg.png; do
  if [[ ! -e "$REPO_ROOT/$required" ]]; then echo "ERROR: Required repository item is missing: $REPO_ROOT/$required" >&2; exit 2; fi
done
DEFAULT_PY="$HOME/dev/testing/RDT_Voice_Desktop_POC/.venv/bin/python"
if [[ ! -x "$DEFAULT_PY" ]]; then DEFAULT_PY="$HOME/dev/rdt-secure/servers/whisper/.venv/bin/python"; fi
if [[ ! -x "$DEFAULT_PY" ]]; then DEFAULT_PY="$HOME/mlx-env/bin/python"; fi
PY="${RDT_BUILD_PYTHON:-$DEFAULT_PY}"
if [[ ! -x "$PY" ]]; then echo "ERROR: No usable Python found. Set RDT_BUILD_PYTHON to the isolated native MLX build environment." >&2; exit 2; fi
"$PY" - <<'PY'
import importlib.util, platform, sys
print(f"Build Python: {sys.executable}")
print(f"Python: {sys.version.split()[0]} / machine={platform.machine()}")
missing = [name for name in ("mlx_whisper", "mlx", "fastapi", "uvicorn", "numpy", "multipart", "qrcode", "imageio_ffmpeg", "PyInstaller") if importlib.util.find_spec(name) is None]
if missing:
 print("Missing build modules: " + ", ".join(missing)); raise SystemExit("Install missing modules in the dedicated build environment before rebuilding.")
PY
RESOURCES="$PROJECT_DIR/src-tauri/resources"
NEXT_RESOURCES="$PROJECT_DIR/build/desktop-resources-new"
OLD_RESOURCES="$PROJECT_DIR/build/desktop-resources-old"
WEB="$NEXT_RESOURCES/rdt_voice_web"; BACKEND="$NEXT_RESOURCES/rdt_voice_backend"; NOTICES="$NEXT_RESOURCES/third_party_notices"
STAGE="$PROJECT_DIR/build/desktop-backend-stage"; WORK="$PROJECT_DIR/build/pyinstaller-work"; SPEC="$PROJECT_DIR/build/pyinstaller-spec"
# Build everything in staging. Do not destroy the current runnable resources until the replacement succeeds.
rm -rf "$NEXT_RESOURCES" "$OLD_RESOURCES" "$STAGE" "$WORK" "$SPEC"
mkdir -p "$WEB" "$BACKEND" "$NOTICES" "$STAGE" "$WORK" "$SPEC" "$PROJECT_DIR/build" "$PROJECT_DIR/public"
cp "$REPO_ROOT/index.html" "$WEB/index.html"; cp "$REPO_ROOT/worker.js" "$WEB/worker.js"; cp "$REPO_ROOT/pcm-worklet.js" "$WEB/pcm-worklet.js"
cp "$REPO_ROOT/models.manifest.json" "$WEB/models.manifest.json"; cp -R "$REPO_ROOT/locales" "$WEB/locales"; cp -R "$REPO_ROOT/assets" "$WEB/assets"
LOGO_SOURCE="$REPO_ROOT/assets/rdt_logo_BIG_yellow_halo_trans_bg.png"
cp "$LOGO_SOURCE" "$PROJECT_DIR/public/rdt-brand-icon.png"
TAURI_CLI="$PROJECT_DIR/node_modules/.bin/tauri"
if [[ ! -x "$TAURI_CLI" ]]; then echo "ERROR: Tauri CLI missing. Run npm install once from the desktop folder." >&2; exit 4; fi
ICON_DIR="$PROJECT_DIR/src-tauri/icons"; mkdir -p "$ICON_DIR"
"$TAURI_CLI" icon "$LOGO_SOURCE" --output "$ICON_DIR"
for icon_file in icon.png icon.icns 32x32.png 128x128.png 128x128@2x.png; do if [[ ! -s "$ICON_DIR/$icon_file" ]]; then echo "ERROR: Tauri icon generation did not create $ICON_DIR/$icon_file." >&2; exit 4; fi; done
"$PY" "$SCRIPT_DIR/patch_desktop_backend.py" "$REPO_ROOT/backend/server.py" "$STAGE/server.py"
ARGS=(--noconfirm --clean --onedir --name rdt_voice_backend --distpath "$NEXT_RESOURCES" --workpath "$WORK" --specpath "$SPEC" --collect-all mlx --collect-data mlx_whisper --collect-all fastapi --collect-all uvicorn --collect-all huggingface_hub --collect-all qrcode --collect-all multipart --hidden-import qrcode.image.svg --exclude-module torch)
if "$PY" -c 'import importlib.util,sys; sys.exit(0 if importlib.util.find_spec("mlx_metal") else 1)'; then ARGS+=(--collect-all mlx_metal); fi
"$PY" -m PyInstaller "${ARGS[@]}" "$STAGE/server.py"
if [[ ! -x "$BACKEND/rdt_voice_backend" ]]; then echo "ERROR: PyInstaller output missing; preserving previous resources." >&2; exit 4; fi
FFMPEG_SOURCE="$("$PY" -c 'import imageio_ffmpeg; print(imageio_ffmpeg.get_ffmpeg_exe())')"
if [[ ! -x "$FFMPEG_SOURCE" ]]; then echo "ERROR: bundled FFmpeg not executable; preserving previous resources." >&2; exit 4; fi
cp -f "$FFMPEG_SOURCE" "$BACKEND/ffmpeg"; chmod 755 "$BACKEND/ffmpeg"
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
cat > "$NOTICES/BUILD-NOTICES.md" <<'EOF'
# RDT Connect — bundled component notice index

This development bundle contains MLX Whisper, MLX native dependencies, FFmpeg from imageio-ffmpeg, OpenCC-JS and OpenCC dictionary data, and the RDT Voice frontend (including the original logo and local OpenCC bundle).

Model weights are not bundled in the installer. The selected model is resolved by Hugging Face repository ID using the Hugging Face Hub cache. Missing files may download when the user submits a transcription. The desktop backend does not scan the full drive for models and skips artificial inference during server startup.

This is not a complete transitive license audit. Public distribution still requires license review, clean-Mac testing, code signing and notarization.
EOF
"$PY" -m py_compile "$STAGE/server.py"
mkdir -p "$RESOURCES" "$OLD_RESOURCES"
# Promote all staged resources. If any move fails, roll back every resource already promoted.
PROMOTED=""
for name in rdt_voice_web rdt_voice_backend third_party_notices; do
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
