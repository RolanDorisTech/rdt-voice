# RDT Voice — Backend guide (shared remote-access and security instructions)

This folder contains the optional Python backend for people who want to run Whisper on a computer they control. In this repository, the Apple Silicon implementation lives at `backend/apple/server.py`; `requirements.txt` and `rdt_model_lock.json` remain in `backend/`. The frontend files (`index.html`, `worker.js`, `pcm-worklet.js`, `models.manifest.json`, and `locales/`) stay at the repository root beside the main README. The relocated server searches the repository root for frontend assets in this layout, so there is no need to duplicate the frontend bundle. For a standalone download, keep `server.py`, `rdt_model_lock.json`, and `requirements.txt` together in one folder. For r12 QR pairing, the backend serves the selected HTML and its companion assets from the same HTTPS origin so its HttpOnly browser session can authenticate API calls. The browser app can also run from GitHub Pages with EasyWhisper without installing a backend.

## Current platform support

The backend currently provided here runs on **macOS on Apple Silicon**. It currently uses MLX Whisper, but the platform name describes the supported Mac hardware—not a promise that MLX must remain the inference engine forever. See [Apple Silicon backend setup](README-APPLE-SILICON.md).

An **iPhone or iPad can use RDT Voice in its browser and connect to a Mac backend**; this Python/MLX backend is not an iPhone or iPad server. Separate Windows and Linux backend implementations or setup instructions may be provided in a future update. Do not assume that the current backend runs on Windows or generic Linux.

## Why use your own backend?

On suitable hardware, running Whisper on your own computer can be dramatically faster than running a larger model in the browser, especially on longer recordings. It can also be more dependable for long jobs because inference is handled by your backend rather than relying on a browser tab's memory and worker lifecycle. On an M1 Max, the speed difference was night-and-day in some tests. Performance, reliability, and transcript quality still depend on the machine, model, audio, and language; this is an observed advantage, not a universal guarantee.

## Private remote access with Tailscale Serve

This is the pattern to use when you want to record on a phone or another computer but run Whisper on your Mac. The same network design can be used by future backend implementations, although their server startup commands will differ.

1. Install and sign in to [Tailscale](https://tailscale.com/) on the computer running Whisper and the client device. Confirm the devices can reach one another within your tailnet.
2. Start the backend listening on the server computer's **loopback address**, normally `127.0.0.1:8888`. Do not bind it to every network interface just to make phone access work.
3. Check existing Serve routes before selecting an external port:

   ```bash
   tailscale serve status
   ```

4. If external port `8444` is available, let Tailscale Serve provide HTTPS and proxy to the local backend. For the HTTP backend shown in the Apple guide:

   ```bash
   tailscale serve --bg --https=8444 http://127.0.0.1:8888
   tailscale serve status
   ```

5. In RDT Voice, use the exact HTTPS MagicDNS URL printed by `tailscale serve status`, including the port. It will resemble `https://your-device.your-tailnet.ts.net:8444`. Do not copy the example literally, and do not publish your personal hostname.

The external port and the local backend port are intentionally distinct: `8444` is the Tailscale HTTPS entry point, and `8888` is the backend's local listener. If `8444` is already in use, choose another unused Serve port and use the matching URL in RDT Voice.

**Keep the backend's authentication enabled. Do not enable Tailscale Funnel for this private setup.** Serve is intended for private tailnet access; Funnel is the separate public-exposure feature. Read the [official Tailscale Serve guide](https://tailscale.com/docs/features/tailscale-serve) and [CLI reference](https://tailscale.com/docs/reference/tailscale-cli/serve) before changing an existing Serve configuration.

To disable only the `8444` route later:

```bash
tailscale serve --https=8444 off
```

If you chose another external port, substitute that port.

### Choose how RDT Voice authenticates

RDT Voice supports two authentication paths in the currently supplied Apple backend. Both keep server-side authentication enabled.

#### Option A — persistent bearer token (works for direct/local connections)

By default, the backend creates a strong token outside the repository at `~/.config/rdt-voice/server-token`, restricts the file to your OS user where supported, and reuses the same token after normal restarts. It does **not** print the secret into routine startup logs.

On macOS, copy the token to the clipboard when you pair a browser for the first time:

```bash
pbcopy < ~/.config/rdt-voice/server-token
```

Paste it once into **RDT Voice → Settings → Backend token key**. On a personal browser profile, you may select **Remember on this device** so you do not need to enter it again. The browser stores the value in local storage, which is not encrypted credential storage; avoid remembering it on shared or public browser profiles. Normal backend restarts do not rotate the saved token.

This is still a one-time copy/paste for each browser profile when using bearer-token authentication. It should not be required again on every server restart. If the token file is missing or you intentionally delete it, a new token will be created and remembered browser values will need to be updated. An explicit `RDT_TOKEN` environment variable overrides the file, so keep that value stable if you choose to use it.

**Upgrade note:** earlier development backend builds generated a new token on each launch. The first time you switch to this persistent-token version, pair the browser once with the new token; normal restarts after that will keep it valid.

#### Optional: add a Tailscale identity restriction

The r12 default is **QR pairing plus a persistent browser session**. This gives each paired browser its own revocable authorization. The backend also keeps the persistent bearer token as a compatibility fallback.

You may add a Tailscale login-identity allowlist as a second gate for requests arriving through Tailscale Serve. Keep the backend on loopback and keep `RDT_AUTH=on`:

```bash
RDT_AUTH=on \
RDT_TAILSCALE_AUTH=on \
RDT_TAILSCALE_ALLOWED_USERS="your-tailscale-login@example.com" \
WHISPER_MODEL_ID="mlx-community/whisper-large-v3-turbo" \
python apple/server.py --host 127.0.0.1 --port 8888
```

Replace the example identity with the exact login shown by Tailscale. With r12 defaults, an allowlisted Tailscale identity is an **additional restriction**, not a replacement for QR pairing or the bearer-token fallback. This is intentional: if identity-only access were allowed, revoking one browser session would not stop that same Tailscale identity from connecting again. Do not set `RDT_PAIRING_REQUIRE_SESSION=off` for the normal setup.

- **Tailscale identity identifies a user, not one unique browser.** Use the device manager to revoke an individual paired browser; use Tailscale's own controls to remove network access for a user/device.
- Trust Tailscale identity headers only when the backend binds to `127.0.0.1`/`localhost` and Tailscale Serve is the remote ingress. Never use this as justification to bind the backend to `0.0.0.0` or a LAN address. See the [Tailscale Serve identity-header documentation](https://tailscale.com/docs/features/tailscale-serve).
- Do not put personal Tailscale logins, MagicDNS hostnames, bearer tokens, or other private configuration in public source files. A future Windows/Linux backend must implement and test this behavior before its documentation claims support.

### Why HTTPS matters

The public RDT Voice page uses HTTPS. Browsers may block a request from an HTTPS page to `http://127.0.0.1:8888` as mixed content or loopback/local-network access. A successful terminal `curl` does not prove the browser is allowed to make that same request.

For a phone or another computer, use the HTTPS Tailscale Serve URL—not `127.0.0.1`, which would refer to the phone itself. For a browser running on the same Mac, the Apple guide explains how to use a locally trusted `mkcert` certificate instead.

### Observed browser caveat

In one iPhone-to-Mac test, Safari could open the backend's `/health` URL directly but the RDT Voice page's JavaScript requests failed; Chrome on the same phone connected and transcribed successfully. This is an observation from one setup, not proof that Safari always fails. Keep the browser and Tailscale updated, and use RDT Voice's diagnostics rather than disabling browser security protections.

## Dependency note

The QR pairing page uses the Python package `qrcode`. Install backend dependencies from this folder with the same Python environment that launches `server.py`:

```bash
python -m pip install -r requirements.txt
python -c 'import qrcode; from qrcode.image.svg import SvgPathImage; print("QR imports OK")'
```

If startup reports `ModuleNotFoundError: No module named 'qrcode'`, the Python environment used to launch the server is missing this dependency. Installing it into a different Python environment will not fix the error.

## Security checklist

- Keep authentication enabled for any backend reachable by another device, Tailscale Serve, a proxy, or a tunnel.
- Bind the backend to loopback when Tailscale Serve is the remote entry point.
- Do not expose port `8888` directly to the public internet, and do not enable Funnel for this private use.
- CORS is not authentication. Allow only the frontend origins you intentionally use.
- Never commit bearer-token files, `auth.sqlite3*`, `.env` files, logs, recordings, certificates, private keys, or personal Tailscale hostnames.
- Before sharing diagnostic output, remove tokens, personal hostnames, and other private details.

## More information

- [Apple Silicon installation and local HTTPS](README-APPLE-SILICON.md)
- [Main RDT Voice README](../README.md) — browser-only use, EasyWhisper, known limitations, privacy, and third-party acknowledgements.


## One-time QR pairing (experimental r12)

The r12 backend adds an experimental browser-pairing flow. The backend still requires authentication; pairing exchanges a short-lived, single-use code for an HttpOnly browser-session cookie. The persistent bearer-token file remains on the backend computer unless an operator deliberately copies it into a browser as the fallback. In this repository, keep the frontend runtime assets at the repository root and keep the Apple server source at `backend/apple/server.py`. When run from this repository's `backend/apple/server.py`, the server searches the repository root for `index.html`; the worker, audio worklet, model manifest, and `locales/` folder should stay beside that HTML so they are served from the same origin. Do not maintain a second copy of the frontend inside `backend/`.

### Test from the backend computer

1. Install/update dependencies from `requirements.txt`, then start the server as usual with authentication enabled (`RDT_AUTH=on`, the default).
2. On the backend computer, open `http://127.0.0.1:8888/pairing` (or the matching local HTTPS URL if you run TLS directly).
3. In the public HTTPS address field, enter the exact private Tailscale Serve URL, including its port, such as `https://YOUR-MAC-MAGICDNS-HOST:8444`. Do not use Funnel or publish the service to the public internet.
4. Click **Generate pairing QR**. Scan it only with the browser you intend to authorize. The QR code expires after **60 seconds** and can be used once.
5. Scanning submits a pairing request; it does **not** authorize the browser by itself. On the Mac, review the pending request in the `/pairing` page and explicitly approve the intended device. The request expires after **3 minutes** if it is not approved. After approval, the browser opens RDT Voice at the same Tailscale HTTPS origin. Use a private browsing window for the first test if you want to avoid an old saved backend URL or bearer token taking precedence.
6. Use **Paired browsers** on the local `/pairing` page to revoke a browser. Revocation applies on its next backend request.

For remote access to the `/pairing` administration page itself, configure `RDT_TAILSCALE_ALLOWED_USERS` with exact Tailscale login identities; those identities are trusted for pairing administration. In r12, when `RDT_TAILSCALE_AUTH=on`, the allowlisted identity is an additional gate: a valid paired browser session (or fallback bearer token) is still required by default so individual device revocation works. Set `RDT_PAIRING_REQUIRE_SESSION=off` only if you deliberately want legacy identity-only access and accept that browser-session revocation cannot block an already allowlisted Tailscale identity. You may also administer pairing locally on the backend computer and enter the HTTPS Serve URL in the address field. Keep `RDT_AUTH=on`, keep the server bound to loopback, use Tailscale Serve (not Funnel), and do not publish the QR code.

### Session behavior and limitations

- Pairing QR codes are single-use and expire after 60 seconds. After scanning, the owner must explicitly approve the pending browser request; that request expires after 3 minutes. The code secret appears in the URL fragment, not the HTTP request path, and is removed by the landing page before redemption.
- The backend stores only a SHA-256 hash of each browser-session secret; the browser receives a `Secure` (for remote HTTPS), `HttpOnly`, `SameSite=Strict`, host-only cookie.
- Sessions renew on successful same-origin API use, with a 30-day idle timeout and 90-day absolute timeout. Browser data clearing, expiry, server database loss, or revocation requires pairing again.
- The `/pairing` device manager is only available locally, to an allowlisted Tailscale identity, or with the backend bearer token. A normal paired browser cannot manage other devices.
- The QR feature is experimental. It has had static/source checks in this package but has not yet been end-to-end validated on a clean macOS installation or across browsers. Test one phone and one desktop browser before relying on it for important recordings.
- This backend and its MLX dependencies currently target macOS on Apple Silicon.
