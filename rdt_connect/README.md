# RDT Voice Desktop — Phase B (macOS Apple Silicon)

This is the next development scaffold for the optional native **RDT Voice Server** companion. The manager controls the backend; the existing RDT Voice browser interface opens unchanged in its own window. Browser-only RDT Voice remains independent.

**Status:** Phase A's packaged-MLX `/health` and real-transcription checks were completed separately. This Phase B Tauri manager has not yet been built or end-to-end tested on macOS. It is not a public installer and is not signed/notarized.

## What's included

- Tauri 2 manager UI for starting/stopping the production RDT Voice backend, selecting a model, and opening the unchanged RDT Voice UI.
- Optional private Tailscale Serve setup; it doesn't enable Funnel or reset existing routes.
- QR pairing, approval/denial of requests, and revocation of paired browsers through the existing authenticated backend API.
- The master/fallback bearer token stays on the Mac. The Rust sidecar manager reads it to make authenticated calls; the RDT Voice window receives it in **session storage** only because the existing browser UI requires a bearer token. It is not saved in browser localStorage.
- A PyInstaller `onedir` package of the real `backend/server.py`, not the Phase A test-only server. The original repository backend file is not modified. A desktop-only staging copy adds safe static delivery for `/assets/`, which is needed for the bundled offline OpenCC converter.
- Separate desktop token/database and recording paths under `~/Library/Application Support/RDT Voice Server/`, so this companion does not reuse the development server's token or pairing database.

The current manager uses a small number of native controls instead of replacing the RDT Voice UI. App-level icon generation pulls from the current root HTML's embedded WebP favicon.

## Build on your Mac

Prerequisites for *development only*: Node.js/npm, Rust/Cargo and Tauri's macOS build tools, plus the previously tested Apple Silicon Python environment that can freeze MLX. End users will not need these tools after the eventual signed/notarized DMG is ready.

1. Unzip this complete project folder. Move the extracted folder into `~/dev/testing/` and rename it exactly `RDT_Voice_Desktop_PhaseB`. This is the full project, not a patch. Do not use the old `*_iconfix` or `*_BuildFix` patch folders.
2. In Terminal, install the Tauri frontend dependencies once in this project folder:

   ```bash
   cd "$HOME/dev/testing/RDT_Voice_Desktop_PhaseB"
   npm install
   ```

   If npm reports that the `esbuild` install script was blocked, approve it in this folder:

   ```bash
   npm install-scripts approve esbuild
   ```

3. Confirm the root repository `~/dev/rdt-voice/index.html` is the DEV.7 HTML you want to bundle. Then prepare the frontend and Python backend using the Python environment you already installed:

   ```bash
   RDT_BUILD_PYTHON="$HOME/dev/rdt-secure/servers/whisper/.venv/bin/python" \
     bash scripts/prepare-macos-bundle.sh "$HOME/dev/rdt-voice"
   ```

   The preparation script makes a 1024×1024 PNG from the small embedded favicon before asking Tauri to generate `icon.png`, `icon.icns`, and the remaining icon sizes. It does not change `~/dev/rdt-voice/backend/server.py`.

   If the script reports missing build modules, install the backend requirements plus the packaging tools into the dedicated build Python environment, not globally. Example:

   ```bash
   "$HOME/dev/testing/RDT_Voice_Desktop_POC/.venv/bin/python" -m pip install -r "$HOME/dev/rdt-voice/backend/requirements.txt" pyinstaller imageio-ffmpeg
   ```

   Or set `RDT_BUILD_PYTHON` to the correct Python executable before running the preparation script. The script stops if MLX's `mlx.metallib` is missing.

4. Confirm the two required icons exist, then start the development manager:

   ```bash
   ls -lh src-tauri/icons/icon.png src-tauri/icons/icon.icns
   npm run tauri dev
   ```

   The manager window should appear. Click **Start server**, then **Open RDT Voice**. First-time model use may download model weights. Logs go to `~/Library/Logs/RDT Voice Server/backend.log`.

5. In the manager, test private access only if Tailscale is already installed and connected. Enable private HTTPS, generate a QR, scan it with the target phone/browser, and approve the pending request on the Mac. The QR is single-use and expires after 60 seconds. This is an experimental workflow; do not rely on it for important remote recordings until verified in this native build.

## Important implementation details

- The backend binds to `127.0.0.1` and selects an available local port in `8888–8898`; it won't kill or take over a running server on port 8888.
- The manager uses its own token at `~/Library/Application Support/RDT Voice Server/server-token`, auth database at `~/Library/Application Support/RDT Voice Server/auth.sqlite3`, and recordings under `~/Library/Application Support/RDT Voice Server/recordings`. These are distinct from the manual backend's `~/.config/rdt-voice/` token/database and `~/.rdt-voice/` recordings.
- The manager tracks and turns off only a Tailscale Serve port it creates during the current app session. It never runs `tailscale serve reset` or touches Funnel. If the app is force-quit after creating a route, inspect `tailscale serve status` before cleanup; the app intentionally will not guess ownership of a pre-existing route on the next launch.
- The browser UI and all frontend dependencies are packaged locally. The app does not depend on GitHub Pages to load its interface.
- The app does **not** yet auto-update, handle model downloads with a dedicated progress UI, or provide a finished first-run onboarding wizard.

## Before any public DMG

This is not the public release build. Remaining work includes building/running the Tauri app on the developer's Mac, verifying the manager-child-window flow and microphone permissions, checking a clean user account, reviewing accessibility and failure states, completing the full transitive license audit for PyInstaller/MLX/FFmpeg dependencies, code signing with Developer ID, notarization, and then testing the signed artifact from a fresh download. No signing identity or notarization credentials are included here.
