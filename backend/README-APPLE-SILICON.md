# RDT Voice — Apple Silicon backend (macOS)

This is the optional backend for **Apple Silicon Macs (M-series)**. It currently runs Whisper through MLX on the Mac you control. “Apple Silicon” names the supported platform; MLX is the current implementation and could change without changing the platform guide's purpose. The static GitHub Pages frontend cannot run Python; this backend is a separate local process. An iPhone or iPad can use RDT Voice in its browser and connect to this Mac, but cannot host this Python/MLX backend.

## Why run your own backend?

On a capable Apple Silicon Mac, running Whisper locally through MLX can be **dramatically faster** than running a larger EasyWhisper model inside a browser, especially for longer recordings. It can also be a more dependable option for long jobs because inference is handled by the Mac instead of depending on a browser tab's memory and worker lifecycle. In testing on an M1 Max, the speed difference was night-and-day for some tasks. Actual speed, stability, and transcript quality vary by hardware, model, browser, audio, and language; this is a practical advantage, not a universal guarantee.

Your audio is sent to the backend URL you choose. If the server runs on your own Mac and you connect to that Mac, inference runs there. If you configure another host, the audio goes to that host instead.

## Platform scope

This implementation is for **macOS 14 or later on Apple Silicon**. It uses MLX Whisper and is not intended for Intel Macs, Windows, or general Linux systems.

**Separate Windows and Linux backend implementations or step-by-step instructions may be provided in a future update.** They will need platform-specific dependencies and setup; do not try to install this MLX backend on Windows or generic Linux.

For browser-only use, you do not need this backend at all—open RDT Voice and use EasyWhisper.

## 1. Check your Mac

Open Terminal on the Mac that will run Whisper:

```bash
uname -m
sw_vers -productVersion
```

`uname -m` should report `arm64`; macOS should be version 14 or later. Stop here if those checks do not match.

## 2. Install prerequisites

Install Homebrew from [brew.sh](https://brew.sh/) if needed. Then install Python 3.12 and FFmpeg:

```bash
brew install python@3.12 ffmpeg
```

FFmpeg is used to decode supported audio formats. This guide uses standard `pip` inside a virtual environment, so installing another package manager is not required. `uv` is a valid alternative for users who already use it, but it is not needed for this setup.

## 3. Keep the repository layout intact

For a normal repository checkout, keep the folders arranged like this:

```text
rdt-voice/
├── index.html
├── worker.js
├── pcm-worklet.js
├── models.manifest.json
├── locales/
└── backend/
    ├── server.py
    ├── requirements.txt
    ├── README.md
    └── README-APPLE-SILICON.md
```

The frontend files stay at the repository root; `server.py` and `requirements.txt` stay in `backend/`. The backend searches the parent folder for `index.html`, then serves the worker, worklet, model manifest, and locales from that same frontend folder. Do not move or duplicate the frontend assets into `backend/`.

If you deliberately deploy `backend/` separately from the frontend, keep the frontend folder beside it or set `RDT_VOICE_HTML` to the absolute path of your intended HTML. Its companion assets must remain beside that HTML.

Do not put certificates, private keys, token files, recordings, or `.env` files into a public repository.

## 4. Create an isolated Python environment and install dependencies

Run these commands from `backend/`, the directory containing `server.py` and `requirements.txt`:

```bash
"$(brew --prefix python@3.12)/bin/python3.12" -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -c 'import mlx_whisper; import qrcode; from qrcode.image.svg import SvgPathImage; print("MLX Whisper and QR imports OK")'
```

The final command should print `MLX Whisper and QR imports OK`. The `qrcode` dependency is required by the optional browser-pairing page. If the import check fails, stop and inspect the error before proceeding. Do not mix this virtual environment with another project's Python environment. If you use a custom shell launcher, it must invoke the Python executable from this same `.venv`; installing packages into a different Python will not fix a startup error.

## 5. Start the backend with authentication enabled

With the virtual environment active, start Whisper:

```bash
RDT_AUTH=on \
WHISPER_MODEL_ID="mlx-community/whisper-large-v3-turbo" \
python server.py --host 127.0.0.1 --port 8888
```

Leave this Terminal window open while using the backend. The first run creates a strong random bearer token outside the repository at:

`~/.config/rdt-voice/server-token`

The server restricts the file to your macOS user where supported, reuses it across normal restarts, and does not print the secret into routine startup logs. This is the fallback credential; the recommended r12 flow is QR pairing, described below. To copy the fallback token to the clipboard, run this locally on the Mac:

```bash
pbcopy < ~/.config/rdt-voice/server-token
```

If you are upgrading from an earlier development backend that generated a new token on each launch, you will need to update the browser token once to match this new persistent token. After that one-time change, normal restarts will not require a new token.

Only if QR pairing is unavailable, paste the copied token into **RDT Voice → Settings → Backend token key**. On a personal, trusted browser profile, you may check **Remember on this device** so the browser can retain the token. It is stored in that browser's local storage, which is not encrypted credential storage; do not enable this on a shared/public computer. QR-paired browsers do not need to enter this fallback token.

If you explicitly set `RDT_TOKEN` in your environment, that value overrides the saved token file. Do so only if you understand that configuration and keep the value private; keep it stable across restarts.

Keep `RDT_AUTH=on` whenever any other device, Tailscale Serve, a proxy, or a tunnel can reach the backend. Do not expose port `8888` directly to the public internet. CORS is not authentication.

## 6. Check the local health endpoint

Open a second Terminal window on the same Mac and run:

```bash
curl -sS --max-time 5 http://127.0.0.1:8888/health
```

A healthy response should contain `"status":"ok"` and the selected model. This verifies that the terminal request received a response; it does not prove that browser CORS, TLS trust, authorization, or remote routing will work.

## Use the backend from the same Mac when RDT Voice is served over HTTPS

A browser page hosted over HTTPS may block calls to a local HTTP backend due to mixed-content or loopback/local-network security rules. Matching the scheme is often necessary, but it cannot override browser permissions or certificate trust.

If you want the browser on the same Mac to connect to `https://127.0.0.1:8888`, create a locally trusted development certificate with [mkcert](https://github.com/FiloSottile/mkcert):

```bash
brew install mkcert
mkcert -install
mkdir -p certs
mkcert -cert-file certs/localhost.pem -key-file certs/localhost-key.pem localhost 127.0.0.1 ::1
```

Then start the backend with the virtual environment active:

```bash
SSL_CERTFILE="$PWD/certs/localhost.pem" \
SSL_KEYFILE="$PWD/certs/localhost-key.pem" \
RDT_AUTH=on \
WHISPER_MODEL_ID="mlx-community/whisper-large-v3-turbo" \
python server.py --host 127.0.0.1 --port 8888
```

Set the Local Server URL in RDT Voice to `https://127.0.0.1:8888`. `mkcert -install` trusts the development certificate authority on **this Mac only**; it does not automatically make a certificate trusted on your phone. Keep `certs/` and all private keys out of GitHub.

## Use a phone or another computer over Tailscale

Use the shared [backend README](README.md#private-remote-access-with-tailscale-serve) for the full Tailscale Serve procedure and safety checklist. For the no-paste workflow, use the [QR pairing guide](README.md#one-time-qr-pairing-experimental-r12): pair the browser once, then its HttpOnly session cookie is sent automatically to the same-origin app/API. The persistent bearer token remains a fallback. Optional Tailscale identity authentication is an additional gate, not a replacement for pairing, in r12's default configuration.

## Allow a different frontend origin (advanced)

The backend allows the production GitHub Pages origin and common local development origins by default. If deliberately testing from another HTTPS frontend, supply its exact **origin** when starting the server. An origin contains the scheme, hostname, and optional port, with no page path. Do not commit personal preview hostnames.

Example (replace the placeholder with the origin you control):

```bash
RDT_ALLOWED_ORIGINS="https://your-own-frontend.example" \
RDT_AUTH=on \
WHISPER_MODEL_ID="mlx-community/whisper-large-v3-turbo" \
python server.py --host 127.0.0.1 --port 8888
```

This extends the allowed origins; it does not publish the server by itself. CORS is not authentication.

## Token and data management

- **Normal restart:** the token file remains the same; remembered browser tokens remain valid.
- **New browser profile (recommended):** open `/pairing` on the backend computer, generate a QR code for the exact Tailscale Serve HTTPS origin, and scan it on that browser. See the [QR pairing guide](README.md#one-time-qr-pairing-experimental-r12).
- **Bearer-token fallback:** run `pbcopy < ~/.config/rdt-voice/server-token` on the Mac and paste it into Settings only if QR pairing is unavailable. On a trusted personal profile, you may select **Remember on this device**.
- **Rotate the bearer-token fallback:** stop the server, delete `~/.config/rdt-voice/server-token`, and restart. A new fallback token is created, invalidating the old bearer key. This does **not** revoke QR-paired browser sessions; revoke those individually in `/pairing`. Removing the auth database (`~/.config/rdt-voice/auth.sqlite3`) while the server is stopped will invalidate all paired sessions, but also clears the device list and outstanding pairing codes.
- **Recordings:** received audio may be cached locally under `~/.rdt-voice/recordings` by default. Check retention and delete cached recordings according to your needs.
- **Public repository:** never commit tokens, `.env`, certificates, `*.pem`/`*.key`, private keys, recordings, logs, or personal hostnames. The parent `backend/.gitignore` excludes common local-only files.

## Troubleshooting

- **`mlx_whisper` import or install fails:** confirm `uname -m` reports `arm64`, macOS is supported, and the `.venv` is active. Keep the complete error output for diagnosis.
- **`ModuleNotFoundError: No module named 'qrcode'`:** from this backend folder, activate `.venv`, run `python -m pip install -r requirements.txt`, then repeat the import check above. If you start the server through a custom shell launcher, confirm that it invokes this backend folder's `.venv/bin/python`; installing into a different interpreter will not fix the error.
- **`ffmpeg` not found:** install it with `brew install ffmpeg`, then restart the backend process.
- **`401 Unauthorized`:** verify that the browser completed QR pairing on the same HTTPS origin used to open the app. If it did not, generate a fresh QR code; alternatively use the saved bearer token as a fallback. If you deliberately rotated the token or set `RDT_TOKEN`, the fallback token must match.
- **CORS error:** verify the exact frontend origin, including scheme and port. CORS does not authenticate requests.
- **Local health check works, but the browser cannot connect:** check mixed content, loopback/local-network permissions, certificate trust, CORS, and the configured URL. A `curl` response alone does not verify browser access.
- **Phone cannot connect:** verify both devices are on the same tailnet; use the exact MagicDNS URL/port in `tailscale serve status`; keep the backend and Serve route running.
- **Transcription is slow or fails:** first verify a short recording. The first run may download the selected model; check server output and confirm RDT Voice selected the paired backend origin.

## Troubleshooting microphone recording in macOS

These steps apply when RDT Voice opens but recording does not begin or no audio is captured. They are safer first checks than resetting macOS audio preferences or running a system-repair script.

1. **Check macOS permission.** Open **System Settings → Privacy & Security → Microphone** and make sure access is enabled for the browser you are using. If the browser is not listed, try starting a recording once so macOS can prompt for permission, then check again.
2. **Check website permission.** In Safari, open **Safari → Settings → Websites → Microphone** and set the RDT Voice website to **Ask** or **Allow**. In other browsers, open that site's permissions and allow microphone access. See [Apple's Safari website-settings guide](https://support.apple.com/en-gb/guide/safari/ibrwe2159f50/27.0/mac/27).
3. **Select the correct input.** Open **System Settings → Sound → Input**, select your intended microphone, and speak to see whether the input level responds. For a USB microphone, disconnect and reconnect it if it is missing.
4. **Release the microphone and retry.** Quit other recording/audio apps that may be using the microphone, fully quit and reopen the browser, reload RDT Voice, and make a short test recording. If needed, test the microphone in another trusted app such as Voice Memos.
5. **Check the macOS privacy indicator.** A small orange indicator next to Control Centre normally means the microphone is in use; a green indicator means the camera is in use. If recording is supposedly active but no indicator appears, the browser may not have acquired the microphone, although the indicator alone does not diagnose the cause. See [Apple's explanation of Mac privacy indicators](https://support.apple.com/en-gb/118449) and [microphone permissions](https://support.apple.com/zh-cn/guide/mac-help/mchla1b1e1fe/27/mac/27).
6. **If it still fails, restart the Mac** and repeat the short test before changing more system settings. Avoid deleting audio preference files or running commands with `sudo` as a first step; such actions are not a general fix and may remove machine-specific audio configuration.

## Return to the main app guide

For browser-only use, app features, EasyWhisper model recommendations, known browser limitations, privacy, and third-party acknowledgements, see the [main RDT Voice README](../README.md).


## Browser QR pairing (experimental)

The r12 backend includes a local pairing manager at `http://127.0.0.1:8888/pairing` (adjust the scheme/port if your server configuration differs). Open this page on the backend computer, enter the exact HTTPS Tailscale Serve URL that the client device will use, and generate a one-time QR code. Scan only on the device you intend to authorize. The QR code expires after 60 seconds and works once. Scanning submits a pending request; the owner must explicitly approve it on the pairing manager. The pending request expires after 3 minutes if not approved. Keep the backend on loopback behind Tailscale Serve, keep authentication enabled, and never use Tailscale Funnel for this private workflow. The manager also lists and revokes paired browsers. This new path is experimental and needs end-to-end browser testing before regular use.
