# RDT Voice local transcription server — v0.2-DEV.6
# Adaptive silence filtering, local transcription, and request safeguards
import os, re, math, bisect, tempfile, subprocess, wave, pathlib, datetime, uuid, platform, asyncio, time, threading, secrets, hmac, shutil, ipaddress, queue, sqlite3, hashlib, json
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
from fastapi import FastAPI, UploadFile, File, Form, Query, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response, JSONResponse, FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
import uvicorn
import mlx_whisper
import qrcode
from qrcode.image.svg import SvgPathImage
from contextlib import contextmanager

MODEL_ID = os.getenv("WHISPER_MODEL_ID", "mlx-community/whisper-large-v3-turbo")
SAMPLE_RATE=16000
CUT_SILENCE_SECONDS=2.0
MIN_MAX_RMS_GATE=35.0
SILENCE_THRESHOLD_CAP_DBFS=-50.0
SILENCE_THRESHOLD_FLOOR_DBFS=-70.0
FRAME_MS=20
HOP_MS=10
FRAME_SAMPLES=SAMPLE_RATE*FRAME_MS//1000
HOP_SAMPLES=SAMPLE_RATE*HOP_MS//1000
SPEECH_MARGIN_DB=3.0
BOUNDARY_PAD_MS=50

# === SECURITY / CONFIG ===
ALLOWED_ROOT = pathlib.Path(
    os.getenv("RDT_DATA_ROOT", str(pathlib.Path.home() / ".rdt-voice" / "recordings"))
).expanduser().resolve()
_extra_root = os.getenv("RDT_CACHE_ROOT", "").strip()
CACHE_ROOT = pathlib.Path(_extra_root).expanduser().resolve() if _extra_root else None
ALLOWED_ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
try:
    ALLOWED_ROOT.chmod(0o700)
except OSError:
    pass

# Authentication defaults to a stable, per-install token stored outside the repo.
# This prevents every server restart from invalidating a browser's remembered key.
AUTH_ENABLED = os.getenv("RDT_AUTH", "on").lower() not in ("off", "0", "false", "no")
TOKEN_PATH = pathlib.Path(
    os.getenv("RDT_TOKEN_FILE", str(pathlib.Path.home() / ".config" / "rdt-voice" / "server-token"))
).expanduser()
RDT_TOKEN_OVERRIDE = os.getenv("RDT_TOKEN")


def _load_or_create_server_token() -> tuple[str, str]:
    """Return (token, source). Never print the token into routine server logs."""
    if RDT_TOKEN_OVERRIDE:
        if len(RDT_TOKEN_OVERRIDE) < 32:
            print("[RDT] WARNING: RDT_TOKEN is shorter than 32 characters; use a strong random secret.")
        return RDT_TOKEN_OVERRIDE, "environment"

    try:
        TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        TOKEN_PATH.parent.chmod(0o700)
        if TOKEN_PATH.is_symlink():
            raise RuntimeError("token file path must not be a symbolic link")

        try:
            fd = os.open(
                str(TOKEN_PATH),
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
        except FileExistsError:
            # A concurrently starting second process may see the file between
            # creation and its first write. Briefly retry before failing closed.
            token = ""
            for _ in range(20):
                if TOKEN_PATH.is_symlink():
                    raise RuntimeError("token file path must not be a symbolic link")
                token = TOKEN_PATH.read_text(encoding="utf-8").strip()
                if token:
                    break
                time.sleep(0.05)
            if not token:
                raise RuntimeError("token file exists but is empty; stop other server instances and remove it to regenerate")
            TOKEN_PATH.chmod(0o600)
            return token, "file"

        token = secrets.token_urlsafe(32)
        try:
            os.write(fd, (token + "\n").encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)
        TOKEN_PATH.chmod(0o600)
        return token, "file"
    except (OSError, RuntimeError) as exc:
        raise RuntimeError(
            f"Could not safely load/create the backend token file at {TOKEN_PATH}: {exc}. "
            "Fix the file permissions/path or set RDT_TOKEN explicitly."
        ) from exc


try:
    TOKEN, TOKEN_SOURCE = _load_or_create_server_token()
except RuntimeError as exc:
    raise SystemExit(f"[RDT] AUTH TOKEN ERROR: {exc}") from exc

# Optional no-paste authentication for clients arriving through Tailscale Serve.
# Serve injects and sanitizes these identity headers; this mode is only safe when
# this backend listens on loopback and Serve is the remote ingress.
TAILSCALE_IDENTITY_AUTH = os.getenv("RDT_TAILSCALE_AUTH", "off").lower() in ("on", "1", "true", "yes")
PAIRING_REQUIRE_SESSION = os.getenv("RDT_PAIRING_REQUIRE_SESSION", "on").lower() not in ("off", "0", "false", "no")
TAILSCALE_ALLOWED_USERS = {
    entry.strip().casefold()
    for entry in os.getenv("RDT_TAILSCALE_ALLOWED_USERS", "").split(",")
    if entry.strip()
}
if TAILSCALE_IDENTITY_AUTH and not TAILSCALE_ALLOWED_USERS:
    raise SystemExit("[RDT] RDT_TAILSCALE_AUTH=on requires RDT_TAILSCALE_ALLOWED_USERS with one or more exact Tailscale login identities.")
if TAILSCALE_IDENTITY_AUTH and not AUTH_ENABLED:
    raise SystemExit("[RDT] Tailscale identity authentication requires RDT_AUTH=on.")

def _display_path(p) -> str:
    """Show paths without exposing the OS username (home folder shown as ~)."""
    try:
        return "~/" + str(pathlib.Path(p).relative_to(pathlib.Path.home()))
    except ValueError:
        return str(p)

def _csv_env(name: str, default: str) -> list[str]:
    return [x.strip() for x in os.getenv(name, default).split(",") if x.strip()]

# --- Web ports the page may be served from (override with RDT_WEB_PORTS_STR) ---
_raw_ports = os.getenv("RDT_WEB_PORTS_STR", "3000,3080,5173,5174,5175,8888")
RDT_WEB_PORTS = [p.strip() for p in _raw_ports.replace(" ", ",").split(",") if p.strip()]

# ORIGINS: https:// only (from RDT_ALLOWED_ORIGINS) + localhost on the ports above
_allowed = set()
for _o in _csv_env("RDT_ALLOWED_ORIGINS", ""):
    _o = _o.rstrip("/").strip()
    if _o.startswith("https://"):
        _allowed.add(_o)
for _p in RDT_WEB_PORTS:
    _allowed.add(f"http://127.0.0.1:{_p}")
    _allowed.add(f"http://localhost:{_p}")

# Convenience: allow the verified public production GitHub Pages ORIGIN.
# Do NOT hard-code private Alpha/Beta previews, Netlify, or personal Tailscale hosts.
_allowed.add("https://rolandoristech.github.io")
_ts_origin_host = os.getenv("TAILSCALE_HOST","").strip().split(":")[0]
if _ts_origin_host and "/" not in _ts_origin_host:
    _allowed.add(f"https://{_ts_origin_host}")
ALLOWED_ORIGINS = _allowed

# HOSTS: host:port only, no https://, no ts.net:PORT leak
_hosts = set()
for _p in RDT_WEB_PORTS:
    _hosts.add(f"127.0.0.1:{_p}")
    _hosts.add(f"localhost:{_p}")
_ts_host = os.getenv("TAILSCALE_HOST","").strip()
_ts_ip = os.getenv("TAILSCALE_IP","").strip()
if _ts_host:
    _hosts.add(_ts_host)
if _ts_ip:
    _hosts.add(_ts_ip)
for _h in _csv_env("RDT_ALLOWED_HOSTS", ""):
    _h = _h.strip()
    if _h and "https://" not in _h and "http://" not in _h:
        _hosts.add(_h)
ALLOWED_HOSTS = _hosts
MAX_UPLOAD_BYTES = 150 * 1024 * 1024
UPLOAD_CHUNK_BYTES = 1024 * 1024
FFMPEG = shutil.which("ffmpeg") or os.getenv("FFMPEG_PATH")
FFPROBE = shutil.which("ffprobe") or (FFMPEG.replace("ffmpeg","ffprobe") if FFMPEG else None)
FFPROBE_TIMEOUT_SECONDS = max(1.0, min(30.0, float(os.getenv("RDT_FFPROBE_TIMEOUT", "8"))))
FFMPEG_TIMEOUT_SECONDS = int(os.getenv("RDT_FFMPEG_TIMEOUT", "900"))

print(f"[RDT] Recordings folder: {_display_path(ALLOWED_ROOT)}")
print(f"[RDT] FFMPEG={FFMPEG or '(not found)'}")
if AUTH_ENABLED:
    if TOKEN_SOURCE == "file":
        print(f"[RDT] AUTH ENABLED — persistent fallback token file: {_display_path(TOKEN_PATH)} (secret not printed)")
        print("[RDT] The token remains the same across normal server restarts.")
        if TAILSCALE_IDENTITY_AUTH:
            print("[RDT] Open /pairing on this computer to pair browsers with a one-time QR code; direct token access remains available as a fallback.")
        else:
            print("[RDT] Open /pairing on this computer to create a one-time QR pairing code; bearer-token access remains available as a fallback.")
    else:
        print("[RDT] AUTH ENABLED — using the RDT_TOKEN environment override (secret not printed). Keep it stable across restarts.")
    if TAILSCALE_IDENTITY_AUTH:
        if PAIRING_REQUIRE_SESSION:
            print("[RDT] Tailscale identity auth enabled as an additional gate; browsers still need a paired session or the fallback bearer token.")
        else:
            print("[RDT] LEGACY Tailscale identity-only auth enabled; per-browser session revocation cannot block an allowlisted identity. Set RDT_PAIRING_REQUIRE_SESSION=on to require paired sessions.")
else:
    print("[RDT] WARNING: AUTHENTICATION IS DISABLED (RDT_AUTH=off).")
    print("[RDT] Use this only for isolated local testing. A loopback listener can still be exposed by Tailscale Serve, a tunnel, or a reverse proxy.")
    print("[RDT] Keep RDT_AUTH=on whenever another device can reach this server.")

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=sorted(ALLOWED_ORIGINS),
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
    allow_credentials=False,
)

def _host_name(host: str) -> str:
    host = (host or "").strip()
    if not host:
        return ""
    from urllib.parse import urlsplit
    try:
        return (urlsplit("//" + host).hostname or "").lower().rstrip(".")
    except ValueError:
        return ""

def check_host(host: Optional[str]):
    if not host:
        raise HTTPException(status_code=400, detail="Invalid Host header")
    host_l = host.strip().lower().rstrip(".")
    if host_l in {h.lower().rstrip(".") for h in ALLOWED_HOSTS}:
        return
    hostname = _host_name(host)
    if not hostname:
        raise HTTPException(status_code=400, detail="Invalid Host header")
    if hostname in ("127.0.0.1", "localhost"):
        return
    # Tailscale MagicDNS names are accepted so Serve/reverse-proxy requests work.
    # Raw Tailscale IP Host headers require explicit configuration (TAILSCALE_IP,
    # RDT_ALLOWED_HOSTS, or the opt-in RDT_TAILSCALE mode below).
    if hostname.endswith(".ts.net") or hostname.endswith(".tailscale.net"):
        return
    if os.getenv("RDT_TAILSCALE", "0") == "1":
        try:
            ip = ipaddress.ip_address(hostname)
            if isinstance(ip, ipaddress.IPv4Address) and ip in ipaddress.ip_network("100.64.0.0/10"):
                return
        except ValueError:
            pass
    raise HTTPException(status_code=400, detail="Invalid Host header")

def require_bearer(authorization: Optional[str], tailscale_user_login: Optional[str] = None):
    if not AUTH_ENABLED:
        return
    identity = (tailscale_user_login or "").strip().casefold()
    if TAILSCALE_IDENTITY_AUTH and identity and identity not in TAILSCALE_ALLOWED_USERS:
        # Never fall back to a shared token for an identity rejected by policy.
        raise HTTPException(status_code=403, detail="This Tailscale identity is not allowed by this backend.")
    if TAILSCALE_IDENTITY_AUTH and identity in TAILSCALE_ALLOWED_USERS and not PAIRING_REQUIRE_SESSION:
        # Legacy compatibility mode only. In r12 default, identity is an additional gate.
        return
    expected = f"Bearer {TOKEN}".encode("utf-8")
    supplied = (authorization or "").encode("utf-8", "ignore")
    if not supplied or not hmac.compare_digest(supplied, expected):
        raise HTTPException(
            status_code=401,
            detail="Authentication required. Pair this browser with the QR code, or use the persistent backend token fallback.",
            headers={"WWW-Authenticate":"Bearer"},
        )

# === BROWSER PAIRING / COOKIE SESSIONS (r12) ===
# Pairing codes are single-use 256-bit secrets delivered only in a URL fragment.
# Browser sessions are HttpOnly, SameSite=Strict cookies. Only hashes are stored.
AUTH_DB_PATH = pathlib.Path(os.getenv("RDT_AUTH_DB", str(pathlib.Path.home() / ".config" / "rdt-voice" / "auth.sqlite3"))).expanduser()
SESSION_COOKIE = "rdt_session"
PAIRING_TTL_SECONDS = 60
PAIR_REQUEST_TTL_SECONDS = 180
SESSION_IDLE_SECONDS = 30 * 24 * 60 * 60
SESSION_ABSOLUTE_SECONDS = 90 * 24 * 60 * 60
RDT_PUBLIC_URL = os.getenv("RDT_PUBLIC_URL", "").strip().rstrip("/")


def _auth_db_connect():
    AUTH_DB_PATH.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        AUTH_DB_PATH.parent.chmod(0o700)
    except OSError:
        pass
    if AUTH_DB_PATH.is_symlink():
        raise RuntimeError("authentication database path must not be a symbolic link")
    if not AUTH_DB_PATH.exists():
        fd = os.open(str(AUTH_DB_PATH), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(fd)
    try:
        AUTH_DB_PATH.chmod(0o600)
    except OSError:
        pass
    conn = sqlite3.connect(str(AUTH_DB_PATH), timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


@contextmanager
def _auth_db():
    conn = _auth_db_connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _init_auth_db():
    with _auth_db() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS pair_codes (
            code_hash TEXT PRIMARY KEY, created_at REAL NOT NULL, expires_at REAL NOT NULL,
            used_at REAL, created_by TEXT NOT NULL DEFAULT '', public_origin TEXT NOT NULL DEFAULT ''
        )""")
        # Safe, forward-compatible migration for an already-created r12 test database.
        pair_columns = {row["name"] for row in conn.execute("PRAGMA table_info(pair_codes)").fetchall()}
        if "public_origin" not in pair_columns:
            conn.execute("ALTER TABLE pair_codes ADD COLUMN public_origin TEXT NOT NULL DEFAULT ''")
        conn.execute("""CREATE TABLE IF NOT EXISTS browser_sessions (
            id TEXT PRIMARY KEY, token_hash TEXT UNIQUE NOT NULL, device_label TEXT NOT NULL,
            user_agent TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL, last_seen REAL NOT NULL,
            idle_expires_at REAL NOT NULL, absolute_expires_at REAL NOT NULL, revoked_at REAL
        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_browser_sessions_active ON browser_sessions(revoked_at, idle_expires_at, absolute_expires_at)")
        conn.execute("""CREATE TABLE IF NOT EXISTS pair_requests (
            id TEXT PRIMARY KEY, code_hash TEXT UNIQUE NOT NULL, claim_hash TEXT UNIQUE NOT NULL,
            device_label TEXT NOT NULL, user_agent TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL,
            expires_at REAL NOT NULL, status TEXT NOT NULL DEFAULT 'pending', approved_at REAL,
            denied_at REAL, session_id TEXT UNIQUE
        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_pair_requests_pending ON pair_requests(status, expires_at, created_at)")
    try:
        AUTH_DB_PATH.chmod(0o600)
    except OSError:
        pass


try:
    _init_auth_db()
except Exception as exc:
    raise SystemExit(f"[RDT] AUTH SESSION ERROR: cannot initialize the pairing database at {_display_path(AUTH_DB_PATH)}: {exc}") from exc


def _secret_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _request_host(request: Request) -> str:
    return (request.headers.get("host") or "").strip()


def _request_scheme(request: Request) -> str:
    host_name = _host_name(_request_host(request))
    # Tailscale Serve terminates HTTPS and proxies to the loopback HTTP listener.
    # The public .ts.net origin must therefore stay HTTPS for origin checks and cookies.
    if host_name.endswith(".ts.net") or host_name.endswith(".tailscale.net"):
        return "https"
    try:
        configured = (urlsplit(RDT_PUBLIC_URL).hostname or "").lower()
    except Exception:
        configured = ""
    if configured and host_name == configured and RDT_PUBLIC_URL.startswith("https://"):
        return "https"
    forwarded = (request.headers.get("x-forwarded-proto") or "").split(",", 1)[0].strip().lower()
    if forwarded in ("http", "https"):
        return forwarded
    return request.url.scheme.lower()


def _request_origin(request: Request) -> str:
    return f"{_request_scheme(request)}://{_request_host(request)}".rstrip("/")


def _local_direct_request(request: Request) -> bool:
    peer = (request.client.host if request.client else "").strip("[]").lower()
    host_name = _host_name(_request_host(request))
    try:
        peer_local = ipaddress.ip_address(peer).is_loopback
    except ValueError:
        peer_local = peer == "localhost"
    return peer_local and host_name in ("127.0.0.1", "localhost", "::1") and not (request.headers.get("tailscale-user-login") or "").strip()


def _valid_bearer_value(value: Optional[str]) -> bool:
    expected = f"Bearer {TOKEN}".encode("utf-8")
    supplied = (value or "").encode("utf-8", "ignore")
    return bool(AUTH_ENABLED and supplied and hmac.compare_digest(supplied, expected))


def _is_allowed_tailscale_identity(request: Request) -> bool:
    identity = (request.headers.get("tailscale-user-login") or "").strip().casefold()
    return bool(identity and identity in TAILSCALE_ALLOWED_USERS)


def _require_pairing_admin(request: Request) -> None:
    check_host(_request_host(request))
    identity = (request.headers.get("tailscale-user-login") or "").strip().casefold()
    if TAILSCALE_IDENTITY_AUTH and identity and identity not in TAILSCALE_ALLOWED_USERS:
        raise HTTPException(status_code=403, detail="This Tailscale identity is not allowed to administer browser pairing.")
    if _local_direct_request(request) or _is_allowed_tailscale_identity(request) or _valid_bearer_value(request.headers.get("authorization")):
        return
    raise HTTPException(status_code=403, detail=("Pairing administration is allowed only from the backend computer itself, "
        "an explicitly allowlisted Tailscale identity, or a request authenticated with the backend token."))



# Tailscale helpers used by the pairing UI. We discover this node's own DNS name
# from the local Tailscale CLI instead of trusting a possibly stale TAILSCALE_HOST.
def _tailscale_cli_path() -> Optional[str]:
    candidates = [
        shutil.which("tailscale"),
        "/Applications/Tailscale.app/Contents/MacOS/Tailscale",
        "/usr/local/bin/tailscale",
        "/opt/homebrew/bin/tailscale",
    ]
    for candidate in candidates:
        if candidate and pathlib.Path(candidate).is_file() and os.access(candidate, os.X_OK):
            return candidate
    return None


def _tailscale_run_json(cli: str, args: List[str], timeout: float = 5.0) -> dict:
    result = subprocess.run([cli, *args], capture_output=True, text=True, timeout=timeout, check=False)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "Tailscale returned an error.").strip().splitlines()
        raise RuntimeError(detail[-1][:240] if detail else "Tailscale returned an error.")
    output = (result.stdout or "").strip()
    if not output:
        return {}
    # Some CLI versions may write a warning before the JSON payload.
    begin = output.find("{")
    if begin < 0:
        raise RuntimeError("Tailscale did not return the expected JSON status.")
    try:
        return json.loads(output[begin:])
    except json.JSONDecodeError as exc:
        raise RuntimeError("Tailscale status JSON could not be read.") from exc


def _tailscale_self_dns_name() -> str:
    cli = _tailscale_cli_path()
    if not cli:
        return ""
    try:
        status = _tailscale_run_json(cli, ["status", "--json"])
    except (OSError, RuntimeError, subprocess.TimeoutExpired):
        return ""
    self_info = status.get("Self") if isinstance(status, dict) else None
    dns_name = str((self_info or {}).get("DNSName", "") or "").strip().rstrip(".").lower()
    if dns_name.endswith((".ts.net", ".tailscale.net")):
        return dns_name
    return ""


def _tailscale_serve_status(cli: str) -> dict:
    return _tailscale_run_json(cli, ["serve", "status", "--json"])


def _tailscale_used_serve_ports(status: dict) -> set:
    used = set()
    # Current status output lists HTTPS listeners under Web and TCP listeners under TCP.
    for section in ("Web", "TCP"):
        block = status.get(section, {}) if isinstance(status, dict) else {}
        if not isinstance(block, dict):
            continue
        for listener in block:
            listener = str(listener)
            match = re.search(r":(\d+)\s*$", listener)
            if match:
                used.add(int(match.group(1)))
            elif section == "TCP" and listener.isdigit():
                used.add(int(listener))
    return used


def _tailscale_origin_for_local_port(status: dict, dns_name: str, local_port: int) -> str:
    block = status.get("Web", {}) if isinstance(status, dict) else {}
    if not isinstance(block, dict):
        return ""
    target_patterns = (
        re.compile(r"(?:127\.0\.0\.1|localhost|\[::1\]):" + str(local_port) + r"(?:\D|$)"),
    )
    for listener, config in block.items():
        host_port = str(listener)
        if "://" in host_port:
            host_port = host_port.split("://", 1)[1]
        host = host_port.rsplit(":", 1)[0].strip("[]").lower()
        if host != dns_name:
            continue
        serialized = json.dumps(config, separators=(",", ":"))
        if any(pattern.search(serialized) for pattern in target_patterns):
            return "https://" + str(listener).removeprefix("https://").rstrip("/")
    return ""


def _local_server_port(request: Request) -> int:
    try:
        from urllib.parse import urlsplit
        port = urlsplit("//" + _request_host(request)).port
        if port:
            return int(port)
    except ValueError:
        pass
    return int(os.getenv("WHISPER_PORT", "8888"))


def _allowed_pair_origin(value: str, request: Request) -> str:
    from urllib.parse import urlsplit
    raw = (value or "").strip().rstrip("/")
    try:
        u = urlsplit(raw)
        host = (u.hostname or "").lower().rstrip(".")
        if u.scheme != "https" or not host or u.username or u.password or u.query or u.fragment or u.path not in ("", "/"):
            raise ValueError("Use an HTTPS origin only, with no path, query, or credentials.")
        if host in ("localhost", "127.0.0.1", "::1"):
            raise ValueError("The pairing address must be reachable by the other device, not localhost.")
        origin = f"https://{u.netloc}".rstrip("/")
        configured_origin = ""
        if RDT_PUBLIC_URL:
            cu = urlsplit(RDT_PUBLIC_URL)
            if cu.scheme == "https" and cu.hostname:
                configured_origin = f"https://{cu.netloc}".rstrip("/")
        current_origin = _request_origin(request)
        host_is_tailscale = host.endswith(".ts.net") or host.endswith(".tailscale.net")
        # Prefer the current node's DNS name as reported by Tailscale itself.
        # A stale TAILSCALE_HOST from shell configuration must not break pairing.
        expected_host = (_tailscale_self_dns_name() or os.getenv("TAILSCALE_HOST", "").strip().split(":", 1)[0]).lower().rstrip(".")
        if host_is_tailscale and not expected_host:
            raise ValueError("RDT Voice could not verify this Tailscale address. Open pairing on the Mac and use Set up private access.")
        if expected_host and host_is_tailscale and host != expected_host:
            raise ValueError("This isn't the current Mac's Tailscale address. Use Set up private access to detect the correct address automatically.")
        if not host_is_tailscale and origin != configured_origin and origin != current_origin:
            raise ValueError("For a custom HTTPS hostname, set RDT_PUBLIC_URL on the backend to that exact origin first.")
        if origin == current_origin and _request_scheme(request) != "https":
            raise ValueError("The pairing URL must use a trusted HTTPS address for remote devices.")
        return origin
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid pairing URL: {exc}") from exc


def _new_browser_session(user_agent: str) -> tuple[str, str, float]:
    now = time.time()
    raw = secrets.token_urlsafe(32)
    session_id = str(uuid.uuid4())
    absolute = now + SESSION_ABSOLUTE_SECONDS
    agent = (user_agent or "Unknown browser")[:240]
    label = agent[:160] or "Browser"
    with _auth_db() as conn:
        conn.execute("INSERT INTO browser_sessions (id, token_hash, device_label, user_agent, created_at, last_seen, idle_expires_at, absolute_expires_at, revoked_at) VALUES (?,?,?,?,?,?,?,?,NULL)",
            (session_id, _secret_hash(raw), label, agent, now, now, now + SESSION_IDLE_SECONDS, absolute))
    try:
        AUTH_DB_PATH.chmod(0o600)
    except OSError:
        pass
    return raw, session_id, absolute


def _active_session(raw: str):
    if not raw or len(raw) > 256:
        return None
    now = time.time()
    with _auth_db() as conn:
        row = conn.execute("SELECT id, created_at, idle_expires_at, absolute_expires_at, revoked_at FROM browser_sessions WHERE token_hash=?", (_secret_hash(raw),)).fetchone()
    if not row or row["revoked_at"] is not None or float(row["idle_expires_at"]) <= now or float(row["absolute_expires_at"]) <= now:
        return None
    return row


def _cookie_is_secure(request: Request) -> bool:
    if _request_scheme(request) == "https":
        return True
    host_name = _host_name(_request_host(request))
    return host_name not in ("", "localhost", "127.0.0.1", "::1")


def _same_origin_mutation(request: Request) -> bool:
    origin = (request.headers.get("origin") or "").rstrip("/")
    expected = _request_origin(request)
    return bool(origin and hmac.compare_digest(origin.encode("utf-8"), expected.encode("utf-8")))


def _touch_browser_session(session_id: str, absolute_expires_at: float) -> int:
    now = time.time()
    idle_expiry = min(now + SESSION_IDLE_SECONDS, absolute_expires_at)
    with _auth_db() as conn:
        conn.execute("UPDATE browser_sessions SET last_seen=?, idle_expires_at=? WHERE id=? AND revoked_at IS NULL", (now, idle_expiry, session_id))
    return max(0, int(min(SESSION_IDLE_SECONDS, absolute_expires_at - now)))


@app.middleware("http")
async def rdt_browser_session_middleware(request: Request, call_next):
    """Translate a valid same-origin HttpOnly browser session into internal bearer auth."""
    path = request.url.path
    is_auth_management = path.startswith("/auth/") or path in ("/pair", "/pairing")
    session_row = None
    raw_session = request.cookies.get(SESSION_COOKIE, "")
    if AUTH_ENABLED and raw_session and not is_auth_management and not request.headers.get("authorization"):
        safe_method = request.method.upper() in ("GET", "HEAD", "OPTIONS")
        if safe_method or _same_origin_mutation(request):
            session_row = _active_session(raw_session)
            if session_row:
                headers = list(request.scope.get("headers", []))
                headers.append((b"authorization", f"Bearer {TOKEN}".encode("latin-1")))
                request.scope["headers"] = headers
    response = await call_next(request)
    if session_row and AUTH_ENABLED:
        max_age = _touch_browser_session(session_row["id"], float(session_row["absolute_expires_at"]))
        if max_age > 0:
            response.set_cookie(key=SESSION_COOKIE, value=raw_session, max_age=max_age, httponly=True,
                secure=_cookie_is_secure(request), samesite="strict", path="/")
    if path.startswith("/auth/") or path == "/pair":
        response.headers.setdefault("Cache-Control", "no-store")
    return response


PAIRING_PAGE_HTML = r"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="referrer" content="no-referrer"><title>RDT Voice Connect</title><style>
:root{color-scheme:dark;font:16px/1.45 system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;background:#101216;color:#eef2f6;--panel:#191d23;--border:#303943;--muted:#a7b0bc;--cyan:#38bdf8;--mint:#10b981;--amber:#f59e0b;--red:#fb7185}*{box-sizing:border-box}body{margin:0;min-height:100dvh;padding:16px;background:radial-gradient(ellipse at 6% 0%,rgba(56,189,248,.09),transparent 35%),radial-gradient(ellipse at 98% 100%,rgba(16,185,129,.07),transparent 33%),#101216}.shell{width:min(1180px,100%);height:calc(100dvh - 32px);min-height:600px;margin:0 auto;display:grid;grid-template-rows:auto minmax(0,1fr) auto;gap:14px}.top{display:flex;align-items:center;justify-content:space-between;gap:16px;padding:2px 4px}.brand{display:flex;align-items:center;gap:11px}.brand-mark{width:48px;height:48px;flex:0 0 48px;display:grid;place-items:center;border:0;border-radius:12px;background:transparent;position:relative;overflow:hidden}.brand-logo{display:block;width:100%;height:100%;object-fit:contain;position:relative;z-index:1}.brand-mark:after{content:"";position:absolute;z-index:2;pointer-events:none;inset:-40%;background:linear-gradient(100deg,transparent 35%,rgba(255,255,255,.19) 50%,transparent 65%);transform:translateX(-100%);animation:shine 5s ease-in-out infinite}.brand-name{font-weight:850;letter-spacing:.055em;font-size:1rem}.brand-name span{color:var(--cyan)}.head-copy{margin-top:3px;color:var(--muted);font-size:.95rem}.private-pill,.state-pill{display:inline-flex;align-items:center;gap:7px;border:1px solid rgba(16,185,129,.28);background:rgba(16,185,129,.08);color:#8ce7c3;border-radius:999px;padding:6px 10px;font-size:.82rem;font-weight:700;white-space:nowrap}.dot{width:7px;height:7px;border-radius:50%;background:var(--mint);box-shadow:0 0 0 3px rgba(16,185,129,.12)}h1,h2,p{margin:0}h1,h2{font-size:1.24rem;letter-spacing:-.02em;line-height:1.2;font-weight:800}p{color:var(--muted)}.subhead{margin-top:6px;font-size:.95rem}.workspace{min-height:0;height:100%;display:grid;grid-template-columns:minmax(390px,.92fr) minmax(0,1.28fr);gap:16px;align-items:stretch}.card{min-width:0;min-height:0;background:linear-gradient(145deg,rgba(29,34,42,.98),rgba(23,27,33,.98));border:1px solid var(--border);border-radius:17px;box-shadow:0 12px 38px rgba(0,0,0,.12)}.connect-card{width:100%;height:100%;align-self:stretch;padding:16px;display:flex;flex-direction:column;gap:10px;overflow:hidden}.card-head{display:flex;justify-content:space-between;align-items:center;gap:10px}.eyebrow{font-size:.78rem;letter-spacing:.11em;text-transform:uppercase;font-weight:800;color:#8a96a5}.address{display:flex;align-items:center;gap:8px;background:#101419;border:1px solid #36414c;border-radius:10px;padding:10px 12px;min-width:0}.address-text{overflow-wrap:anywhere;font-size:.84rem;color:#dce5ec;min-width:0;flex:1}.state-pill{padding:4px 8px;font-size:.7rem}.state-pill.off{background:rgba(245,158,11,.08);color:#f7c774;border-color:rgba(245,158,11,.25)}.btn{border:0;border-radius:10px;padding:10px 13px;font:inherit;font-weight:780;cursor:pointer;transition:transform .16s ease,filter .16s ease,background .16s ease;display:inline-flex;align-items:center;justify-content:center;gap:8px}.btn:hover:not(:disabled){filter:brightness(1.08);transform:translateY(-1px)}.btn:focus-visible,input:focus-visible,summary:focus-visible{outline:2px solid var(--cyan);outline-offset:3px}.btn:disabled{opacity:.52;cursor:wait}.primary{background:var(--cyan);color:#062032}.secondary{background:#303943;color:#e9f0f6}.approve{background:#a7f3d0;color:#06392c}.danger{background:#50262d;color:#ffd9de;border:1px solid #723741}.btn-row{display:flex;gap:8px;flex-wrap:wrap}.btn-row .btn{flex:1}.status{font-size:.95rem;line-height:1.4;color:#c4ced8;min-height:1.2em}.status.error{color:#fca5a5}.status.good{color:#86efac}.fine{font-size:.95rem;line-height:1.45}.qr-zone{flex:1;min-height:0;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:9px;border:1px dashed #35424e;border-radius:13px;padding:10px;background:rgba(11,15,20,.3);position:relative;overflow:hidden}.qr-zone:before{content:"";position:absolute;top:0;left:-100%;width:40%;height:1px;background:linear-gradient(90deg,transparent,var(--cyan),transparent);animation:scan 6s ease-in-out infinite}#qr{width:min(290px,78%);max-height: min(35dvh, 350px);aspect-ratio:1;background:#fff;border-radius:12px;padding:8px;object-fit:contain;box-shadow:0 0 0 1px rgba(255,255,255,.3),0 6px 28px rgba(56,189,248,.09)}.qr-placeholder{width:100%;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:8px;text-align:center;padding:14px}.qr-symbol{height:62px;width:62px;border-radius:17px;display:grid;place-items:center;border:1px solid #35424e;color:#708391;background:rgba(56,189,248,.035);font-size:1.7rem}.qr-title{color:#e0e8ef;font-weight:750}.countdown{font-variant-numeric:tabular-nums;color:var(--cyan);font-weight:850;font-size:.84rem}.progress{width:min(220px,88%);height:4px;border-radius:9px;background:#303943;overflow:hidden}.progress span{height:100%;display:block;width:100%;background:linear-gradient(90deg,var(--mint),var(--cyan));transform-origin:left}.advanced{border-top:1px solid #2f3943;padding-top:9px}.advanced summary{font-size:.86rem;color:#9eabba;cursor:pointer;user-select:none}.advanced-inner{padding-top:8px}.advanced label{display:block;font-size:.76rem;color:var(--muted);margin-bottom:5px}.advanced input{width:100%;min-width:0;background:#101419;color:#eaf4fb;border:1px solid #3d4853;border-radius:8px;padding:8px 10px;font:inherit;font-size:.79rem}.right{min-height:0;height:100%;display:grid;grid-template-rows:minmax(0,1fr) minmax(0,1fr);gap:0;background:linear-gradient(145deg,rgba(29,34,42,.98),rgba(23,27,33,.98));border:1px solid var(--border);border-radius:17px;box-shadow:0 12px 38px rgba(0,0,0,.12);overflow:hidden}.list-card{display:flex;flex-direction:column;padding:16px 18px;min-height:0;overflow:hidden;background:transparent;border:0;border-radius:0;box-shadow:none}.list-card:first-child{border-bottom:1px solid var(--border)}.list-heading{display:flex;justify-content:space-between;align-items:center;gap:10px}.count{background:#29323b;border:1px solid #3a4652;color:#c9d7e1;min-width:26px;text-align:center;padding:2px 7px;border-radius:8px;font-size:.75rem;font-weight:800}.card-description{font-size:.95rem;line-height:1.45;margin-top:5px}.list{min-height:0;flex:1;overflow:auto;margin-top:8px;padding-right:3px}.empty{display:flex;align-items:center;gap:8px;height:100%;min-height:52px;color:#8593a1;font-size:.9rem}.empty:before{content:"·";font-size:1.7rem;color:#536271}.device{display:flex;justify-content:space-between;gap:12px;align-items:center;padding:9px 0;border-top:1px solid #2c353e}.device:first-child{border-top:0}.device-info{min-width:0;overflow-wrap:anywhere}.device-badges{display:flex;flex-wrap:wrap;gap:6px;margin-top:5px}.device-chip{display:inline-flex;align-items:center;border:1px solid rgba(56,189,248,.28);background:rgba(56,189,248,.09);color:#b9e9ff;padding:3px 8px;border-radius:999px;font-size:.76rem;font-weight:750}.device strong{display:block;font-size:.94rem;font-weight:750;color:#e5edf3}.device small{display:block;color:#b3c0cc;font-size:.88rem;line-height:1.4;overflow-wrap:anywhere}.device .btn{padding:7px 10px;font-size:.82rem;margin:0;flex-shrink:0}.request-actions{display:flex;gap:5px;flex-shrink:0}.footer{display:flex;justify-content:space-between;align-items:center;gap:12px;padding:0 4px;font-size:.85rem;color:#81909d}.footer strong{color:#a9b8c4;font-weight:650}.hidden,[hidden]{display:none!important}@keyframes shine{0%,65%,100%{transform:translateX(-100%)}82%{transform:translateX(260%)}}@keyframes scan{0%,20%{left:-40%}80%,100%{left:110%}}@media(prefers-reduced-motion:reduce){*,*:before,*:after{animation:none!important;transition:none!important}}@media(max-width:820px){body{padding:12px;overflow:auto}.shell{height:auto;min-height:calc(100dvh - 24px);grid-template-rows:auto auto auto}.workspace{height:auto;grid-template-columns:1fr;align-items:stretch}.connect-card{aspect-ratio:auto;min-height:570px}.right{height:auto;grid-template-rows:minmax(180px,auto) minmax(180px,auto);gap:12px;border:0;background:transparent;box-shadow:none;overflow:visible}.list-card{min-height:190px;background:linear-gradient(145deg,rgba(29,34,42,.98),rgba(23,27,33,.98));border:1px solid var(--border);border-radius:17px;box-shadow:0 12px 38px rgba(0,0,0,.12)}.list-card:first-child{border-bottom:1px solid var(--border)}.footer{flex-wrap:wrap}}@media(max-height:720px) and (min-width:821px){body{padding:10px}.shell{height:calc(100dvh - 20px);min-height:0;gap:8px}.workspace{gap:8px}.right{gap:0}.connect-card{padding:12px;gap:8px}.list-card{padding:11px 13px}.qr-zone{gap:5px}#qr{width:min(250px,62%);max-height: min(30dvh, 280px)}.card-description{margin-top:2px}.footer{font-size:.82rem}}
</style></head><body><main class="shell"><header class="top"><div class="brand"><div class="brand-mark" aria-hidden="true"><img class="brand-logo" alt="" src="data:image/png;base64,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"></div><div><div class="brand-name">RDT <span>VOICE</span> CONNECT</div><div class="head-copy">Scan once. Approve access here.</div></div></div><div class="private-pill"><span class="dot"></span>Private setup</div></header><section class="workspace"><article class="card connect-card"><div class="card-head"><div><div class="eyebrow">01 · Secure connection</div><h1>Connect a device</h1></div><span id="routeState" class="state-pill off">Checking…</span></div><p class="fine">Set up private access, then create a one-use QR code. Scan it with your chosen browser and approve the request here.</p><div class="address"><span class="dot" aria-hidden="true"></span><div id="addressLabel" class="address-text">Checking Tailscale on this Mac…</div></div><div class="btn-row"><button id="setupRemote" class="btn primary" type="button">Set up private access</button><button id="generate" class="btn secondary" type="button" disabled>Generate QR</button></div><div id="status" class="status" role="status" aria-live="polite"></div><div id="qrZone" class="qr-zone"><div id="qrEmpty" class="qr-placeholder"><div class="qr-symbol" aria-hidden="true">▦</div><div class="qr-title">Your QR code will appear here</div><p class="fine">It expires in 60 seconds and works once.</p></div><img id="qr" alt="Single-use RDT Voice pairing QR code" hidden><div id="qrMeta" class="hidden" style="text-align:center"><div id="expiry" class="fine"></div><div class="countdown">Time left: <span id="countdown">60s</span></div><div class="progress"><span id="progressBar"></span></div></div><div id="qrActions" class="btn-row hidden" style="width:100%"><button id="again" class="btn secondary" type="button">Refresh QR</button><button id="copyLink" class="btn secondary" type="button" disabled>Copy link</button></div></div><details class="advanced"><summary>Advanced · use a custom HTTPS address</summary><div class="advanced-inner"><label for="publicOrigin">Backend HTTPS origin</label><input id="publicOrigin" spellcheck="false" autocomplete="url" placeholder="https://your-backend.example:port"><p class="fine" style="margin-top:5px">Custom addresses must already route securely to this backend.</p></div></details></article><div class="right"><article class="card list-card"><div class="list-heading"><h2>02 · Waiting for approval</h2><span id="pendingCount" class="count">0</span></div><p class="card-description">Device and browser details are highlighted. Add a device/place label on the phone if helpful. Requests expire 3 minutes after scanning.</p><div id="requests" class="list"><div class="empty">No device is waiting.</div></div></article><article class="card list-card"><div class="list-heading"><h2>03 · Connected browsers</h2><span id="deviceCount" class="count">0</span></div><p class="card-description">Revoke access at any time. Changes apply on the next protected request.</p><div id="devices" class="list"><div class="empty">No browsers paired yet.</div></div><div style="padding-top:7px"><button id="refresh" class="btn secondary" type="button">Refresh status</button></div></article></div></section><footer class="footer"><span><strong>RDT Voice</strong> · Local transcription, private connection.</span><span>QR: 60 seconds · One use · Owner approval required</span></footer></main><script>
const $=id=>document.getElementById(id);let expiryAt=0,timer=null,routeReady=false,setupInfo=null,lastPairUrl='';const initialOrigin=__RDT_PAIR_ORIGIN__;if(initialOrigin)$('publicOrigin').value=initialOrigin;
async function req(url,opts={}){const r=await fetch(url,{cache:'no-store',credentials:'same-origin',...opts});let d={};try{d=await r.json()}catch{}if(!r.ok)throw new Error(d.detail||d.message||('Request failed ('+r.status+')'));return d}
function say(text,kind=''){const s=$('status');s.textContent=text;s.className='status'+(kind?' '+kind:'')}
function stopCountdown(){if(timer){clearInterval(timer);timer=null}}
function updateCountdown(){const left=Math.max(0,Math.ceil(expiryAt-Date.now()/1000));$('countdown').textContent=left+'s';$('progressBar').style.transform='scaleX('+left/60+')';if(left<=0){stopCountdown();lastPairUrl='';$('copyLink').disabled=true;$('qr').hidden=true;$('qr').removeAttribute('src');$('qrEmpty').hidden=false;$('qrMeta').classList.add('hidden');$('qrActions').classList.add('hidden');say('QR expired. Generate a new one when ready.');}}
async function checkRoute(){ $('setupRemote').disabled=true;$('generate').disabled=true;try{const d=await req('/auth/tailscale/status');setupInfo=d;if(d.dns_name)$('addressLabel').textContent=d.dns_name+(d.port?':'+d.port:'');else $('addressLabel').textContent=d.message||'Tailscale status unavailable';if(d.ready&&d.public_url){routeReady=true;$('publicOrigin').value=d.public_url;$('routeState').textContent='Ready';$('routeState').className='state-pill';$('setupRemote').textContent='Refresh private access';$('generate').disabled=false;say('Private HTTPS route is ready. Generate a QR when you are ready.','good')}else{routeReady=false;$('routeState').textContent='Not ready';$('routeState').className='state-pill off';$('setupRemote').textContent='Set up private access';say(d.message||'Set up a private route to use QR pairing.')}}catch(e){routeReady=false;$('addressLabel').textContent='Tailscale status unavailable';$('routeState').textContent='Not ready';$('routeState').className='state-pill off';say(e.message,'error')}finally{$('setupRemote').disabled=false;$('generate').disabled=!(routeReady||$('publicOrigin').value.trim())}}
async function setupRoute(){ $('setupRemote').disabled=true;$('generate').disabled=true;say('Setting up a separate private HTTPS route…');try{const d=await req('/auth/tailscale/setup',{method:'POST'});setupInfo=d;routeReady=true;$('publicOrigin').value=d.public_url;$('addressLabel').textContent=d.public_url;$('routeState').textContent='Ready';$('routeState').className='state-pill';$('setupRemote').textContent='Refresh private access';$('generate').disabled=false;say(d.reused?'Private route is already ready.':'Private route is ready. Your existing Tailscale routes were left unchanged.','good')}catch(e){routeReady=false;$('routeState').textContent='Not ready';$('routeState').className='state-pill off';say(e.message,'error')}finally{$('setupRemote').disabled=false;$('generate').disabled=!(routeReady||$('publicOrigin').value.trim())}}
async function generate(){if(!routeReady&&!$('publicOrigin').value.trim()){say('Set up private access first.','error');return}stopCountdown();$('generate').disabled=true;$('again').disabled=true;say('Creating a QR code that expires in 60 seconds…');$('qr').hidden=true;$('qrEmpty').hidden=false;$('qrMeta').classList.add('hidden');$('qrActions').classList.add('hidden');try{const d=await req('/auth/pair/start',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({public_origin:$('publicOrigin').value.trim()})});expiryAt=d.expires_at;lastPairUrl=d.pairing_url||'';$('copyLink').disabled=!lastPairUrl;$('qr').src='data:image/svg+xml;charset=utf-8,'+encodeURIComponent(d.qr_svg);$('qr').hidden=false;$('qrEmpty').hidden=true;$('expiry').textContent='Created '+new Date().toLocaleTimeString()+'.';$('qrMeta').classList.remove('hidden');$('qrActions').classList.remove('hidden');say('QR ready. Scanning requests access; you approve it here.','good');updateCountdown();timer=setInterval(updateCountdown,1000);await refreshLists()}catch(e){say(e.message,'error')}finally{$('generate').disabled=!(routeReady||$('publicOrigin').value.trim());$('again').disabled=false}}
function clientFacts(ua){ua=String(ua||'');let device='Unknown device';if(/iPhone/i.test(ua))device='iPhone';else if(/iPad/i.test(ua))device='iPad';else if(/Android/i.test(ua))device=/Mobile/i.test(ua)?'Android phone':'Android tablet';else if(/Macintosh|Mac OS X/i.test(ua))device='Mac';else if(/Windows NT/i.test(ua))device='Windows PC';else if(/Linux/i.test(ua))device='Linux device';let browser='Browser unknown';if(/EdgA|EdgiOS|Edg\//i.test(ua))browser='Edge';else if(/OPR\/|Opera/i.test(ua))browser='Opera';else if(/FxiOS|Firefox/i.test(ua))browser='Firefox';else if(/CriOS|Chrome/i.test(ua))browser='Chrome';else if(/Safari/i.test(ua))browser='Safari';return{device,browser}}function addDeviceRow(box,x,kind){const row=document.createElement('div');row.className='device';const info=document.createElement('div');info.className='device-info';const rawLabel=String(x.device_label||'Browser');const parts=rawLabel.split(' — ');const friendly=parts.length>1?parts.slice(0,-1).join(' — '):'';const detailLabel=parts.length>1?parts[parts.length-1]:rawLabel;const labelParts=detailLabel.split(' · ');const uaFacts=clientFacts(x.user_agent);const facts={device:labelParts[0]||uaFacts.device,browser:labelParts[1]||uaFacts.browser};const name=document.createElement('strong');name.textContent=friendly||rawLabel;const chips=document.createElement('div');chips.className='device-badges';for(const label of [facts.device,facts.browser]){const chip=document.createElement('span');chip.className='device-chip';chip.textContent=label;chips.append(chip)}const meta=document.createElement('small');meta.textContent=(kind==='pending'?'Waiting for approval · ':'Connected · ')+new Date(x.created_at*1000).toLocaleString()+(kind==='pending'?' · Expires in '+Math.max(0,Math.ceil(x.expires_at-Date.now()/1000))+'s':' · Active '+new Date(x.last_seen*1000).toLocaleString());info.append(name,chips,meta);row.append(info);if(kind==='pending'){const actions=document.createElement('div');actions.className='request-actions';const approve=document.createElement('button');approve.className='btn approve';approve.textContent='Approve';approve.addEventListener('click',async()=>{approve.disabled=true;try{await req('/auth/pair/requests/'+encodeURIComponent(x.id)+'/approve',{method:'POST'});say('Browser approved. It will connect automatically.','good');await refreshLists()}catch(e){say(e.message,'error');approve.disabled=false}});const deny=document.createElement('button');deny.className='btn danger';deny.textContent='Deny';deny.addEventListener('click',async()=>{deny.disabled=true;try{await req('/auth/pair/requests/'+encodeURIComponent(x.id)+'/deny',{method:'POST'});say('Pairing request denied.');await refreshLists()}catch(e){say(e.message,'error');deny.disabled=false}});actions.append(approve,deny);row.append(actions)}else{const b=document.createElement('button');b.className='btn danger';b.textContent='Revoke';b.addEventListener('click',async()=>{if(!confirm('Revoke this browser’s access?'))return;try{await req('/auth/devices/'+encodeURIComponent(x.id),{method:'DELETE'});say('Browser access revoked.','good');await refreshLists()}catch(e){say(e.message,'error')}});row.append(b)}box.append(row)}
async function refreshLists(){try{const [pr,dr]=await Promise.all([req('/auth/pair/requests'),req('/auth/devices')]);const requests=$('requests');requests.replaceChildren();$('pendingCount').textContent=String(pr.requests.length);if(!pr.requests.length){const empty=document.createElement('div');empty.className='empty';empty.textContent='No device is waiting.';requests.append(empty)}else for(const x of pr.requests)addDeviceRow(requests,x,'pending');const devices=$('devices');devices.replaceChildren();$('deviceCount').textContent=String(dr.devices.length);if(!dr.devices.length){const empty=document.createElement('div');empty.className='empty';empty.textContent='No browsers paired yet.';devices.append(empty)}else for(const x of dr.devices)addDeviceRow(devices,x,'paired')}catch(e){$('requests').textContent='Could not load approval requests.';$('devices').textContent='Could not load paired browsers.';say(e.message,'error')}}
async function copyPairLink(){if(!lastPairUrl){say('Generate a fresh QR first.','error');return}try{if(navigator.clipboard&&navigator.clipboard.writeText){await navigator.clipboard.writeText(lastPairUrl)}else{const t=document.createElement('textarea');t.value=lastPairUrl;t.style.position='fixed';t.style.opacity='0';document.body.append(t);t.select();const ok=document.execCommand('copy');t.remove();if(!ok)throw new Error('Clipboard access is unavailable.')}say('Pairing link copied. Send it to the other device and open it in your preferred browser. It expires with this QR.','good')}catch(e){say('Could not copy the link. Use the QR code or try HTTPS pairing page.','error')}}$('copyLink').addEventListener('click',copyPairLink);$('setupRemote').addEventListener('click',setupRoute);$('generate').addEventListener('click',generate);$('again').addEventListener('click',generate);$('refresh').addEventListener('click',async()=>{await Promise.all([checkRoute(),refreshLists()])});$('publicOrigin').addEventListener('input',()=>{if($('publicOrigin').value.trim()){routeReady=false;$('generate').disabled=false}else{$('generate').disabled=true}});checkRoute();refreshLists();setInterval(refreshLists,3000);
</script></body></html>"""

@app.get("/pairing", response_class=HTMLResponse)
def pairing_admin_page(request: Request):
    check_host(_request_host(request))
    current = _request_origin(request)
    current_name = _host_name(_request_host(request))
    default_origin = ""
    if RDT_PUBLIC_URL:
        default_origin = RDT_PUBLIC_URL
    elif _request_scheme(request) == "https" and current_name not in ("localhost", "127.0.0.1", "::1"):
        default_origin = current
    html = PAIRING_PAGE_HTML.replace("__RDT_PAIR_ORIGIN__", json.dumps(default_origin).replace("<", "\\u003c"))
    nonce = secrets.token_urlsafe(18)
    html = html.replace("<script>", f'<script nonce="{nonce}">', 1)
    return HTMLResponse(html, headers={
        "Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
        "X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY",
        "Content-Security-Policy": f"default-src 'none'; script-src 'nonce-{nonce}'; connect-src 'self'; img-src data:; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'; object-src 'none'; frame-ancestors 'none'",
    })

PAIR_REDEEM_HTML = r"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="referrer" content="no-referrer"><title>Connect RDT Voice</title><style>
:root{color-scheme:dark;font:16px/1.5 system-ui,-apple-system,sans-serif;background:#101116;color:#f4f5f8;--cyan:#38bdf8;--mint:#10b981}*{box-sizing:border-box}body{margin:0;min-height:100vh;display:grid;place-items:center;padding:20px;background:radial-gradient(ellipse at 4% 0%,rgba(56,189,248,.10),transparent 40%),#101116}.card{width:min(100%,560px);background:linear-gradient(145deg,#1d222a,#171b21);border:1px solid #343d47;border-radius:18px;padding:28px;text-align:left;box-shadow:0 16px 50px rgba(0,0,0,.2)}.brand-mark{width:54px;height:54px;display:grid;place-items:center;border:0;border-radius:12px;background:transparent;position:relative;overflow:hidden;margin-bottom:14px}.brand-logo{display:block;width:100%;height:100%;object-fit:contain;position:relative;z-index:1}.brand-mark:after{content:"";position:absolute;z-index:2;pointer-events:none;inset:-40%;background:linear-gradient(100deg,transparent 35%,rgba(255,255,255,.19) 50%,transparent 65%);transform:translateX(-100%);animation:shine 5s ease-in-out infinite}@keyframes shine{0%,65%,100%{transform:translateX(-100%)}82%{transform:translateX(260%)}}h1{font-size:1.75rem;letter-spacing:-.03em;margin:0 0 8px}p{color:#c5c8d1}.status{white-space:pre-wrap;margin-top:14px;min-height:1.5em}.muted{color:#b0bac5;font-size:.96rem}.detected{display:inline-flex;align-items:center;background:rgba(56,189,248,.10);border:1px solid rgba(56,189,248,.28);color:#b9e9ff;border-radius:999px;padding:8px 12px;font-weight:800;margin:18px 0 16px}label{display:block;font-weight:750;margin-bottom:7px}label span{color:#a8adba;font-weight:400}input{display:block;width:100%;border:1px solid #465360;background:#101419;border-radius:10px;padding:12px 13px;color:#f4f5f8;font:inherit}input:focus-visible,button:focus-visible{outline:2px solid var(--cyan);outline-offset:3px}.privacy-note{font-size:.84rem;margin-top:8px}.browser-note{font-size:.84rem;margin-top:16px}button{width:100%;margin-top:18px;border:0;border-radius:10px;background:var(--cyan);color:#082235;padding:13px 16px;font:inherit;font-weight:850;cursor:pointer}button:disabled{opacity:.55;cursor:wait}.spinner{width:28px;height:28px;border:3px solid #48445f;border-top-color:var(--cyan);border-radius:50%;animation:spin .8s linear infinite;margin:18px 0}@keyframes spin{to{transform:rotate(360deg)}}@media(max-width:480px){body{padding:12px}.card{padding:20px}h1{font-size:1.5rem}}@media(prefers-reduced-motion:reduce){*,*:before,*:after{animation:none!important;transition:none!important}}
</style></head><body><main class="card"><div class="brand-mark" aria-hidden="true"><img class="brand-logo" alt="" src="data:image/png;base64,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"></div><h1>Connect to RDT Voice</h1><p class="muted">Confirm the browser you want to pair. Scanning the QR does not grant access by itself.</p><div class="detected" id="detected">Detected device · Browser</div><label for="deviceName">Device name or place <span>(optional)</span></label><input id="deviceName" maxlength="64" autocomplete="off" placeholder="e.g. Personal iPhone · Home"><p class="muted privacy-note">This label helps you recognize the device. RDT Voice does not request your GPS location.</p><button id="requestAccess" type="button">Request approval</button><div class="spinner" id="spinner" hidden></div><p id="status" class="status" role="status" aria-live="polite">Ready to request approval.</p><p id="help" class="muted" hidden>Return to the RDT Voice pairing page on the Mac. You may need to generate a fresh QR code.</p><p class="muted browser-note">Your device opens its default browser when you scan. To use another browser, send the copied pairing link to this device and open it there.</p></main><script>
(()=>{
 const $=id=>document.getElementById(id),status=$('status'),help=$('help'),spinner=$('spinner'),button=$('requestAccess'),labelInput=$('deviceName');
 const code=String(window.location.hash||'').slice(1);
 try{history.replaceState(null,document.title,window.location.pathname+window.location.search)}catch{}
 const showError=message=>{status.textContent=message;help.hidden=false;spinner.hidden=true;button.disabled=false};
 function facts(){const ua=navigator.userAgent||'',platform=navigator.platform||'';let device='Unknown device';if(/iPhone/i.test(ua))device='iPhone';else if(/iPad/i.test(ua)||(/MacIntel/i.test(platform)&&(navigator.maxTouchPoints||0)>1))device='iPad';else if(/Android/i.test(ua))device=/Mobile/i.test(ua)?'Android phone':'Android tablet';else if(/Macintosh|Mac OS X/i.test(ua)||/Mac/i.test(platform))device='Mac';else if(/Windows/i.test(ua)||/Win/i.test(platform))device='Windows PC';else if(/Linux/i.test(ua)||/Linux/i.test(platform))device='Linux device';let browser='Browser';if(navigator.brave&&typeof navigator.brave.isBrave==='function')browser='Brave';else if(/EdgA|EdgiOS|Edg\//i.test(ua))browser='Edge';else if(/SamsungBrowser/i.test(ua))browser='Samsung Internet';else if(/OPR\/|Opera/i.test(ua))browser='Opera';else if(/FxiOS|Firefox/i.test(ua))browser='Firefox';else if(/CriOS|Chrome/i.test(ua))browser='Chrome';else if(/Safari/i.test(ua))browser='Safari';return{device,browser}}
 const client=facts();$('detected').textContent='Detected: '+client.device+' · '+client.browser;
 if(code.length<32||code.length>160){showError('This pairing link is missing or expired. Generate a fresh QR on the Mac.');button.disabled=true;return}
 let active=false;
 button.addEventListener('click',async()=>{if(active)return;active=true;button.disabled=true;spinner.hidden=false;status.textContent='Sending your approval request…';help.hidden=true;let requestId='',claimToken='';try{
   if(!crypto||!crypto.getRandomValues)throw new Error('This browser cannot create a secure pairing session. Try a current browser.');
   const bytes=new Uint8Array(32);crypto.getRandomValues(bytes);claimToken=btoa(String.fromCharCode(...bytes)).replace(/\+/g,'-').replace(/\//g,'_').replace(/=+$/,'');
   const custom=labelInput.value.trim().slice(0,64);const deviceLabel=(custom?custom+' — ':'')+client.device+' · '+client.browser;
   const response=await fetch('/auth/pair/request',{method:'POST',credentials:'same-origin',cache:'no-store',headers:{'Content-Type':'application/json'},body:JSON.stringify({code,claim_token:claimToken,device_label:deviceLabel})});
   const payload=await response.json().catch(()=>({}));if(!response.ok)throw new Error(payload.detail||('Pairing request failed ('+response.status+').'));requestId=payload.request_id;
   labelInput.disabled=true;status.textContent='Request sent. Approve '+(custom||client.device+' · '+client.browser)+' on the RDT Voice pairing page on your Mac. This browser will connect automatically after approval.';spinner.hidden=true;
   const deadline=Date.now()+180000;while(Date.now()<deadline){await new Promise(resolve=>setTimeout(resolve,1500));const sr=await fetch('/auth/pair/status',{method:'POST',credentials:'same-origin',cache:'no-store',headers:{'Content-Type':'application/json'},body:JSON.stringify({request_id:requestId,claim_token:claimToken})});const sd=await sr.json().catch(()=>({}));if(!sr.ok)throw new Error(sd.detail||('Pairing status failed ('+sr.status+').'));if(sd.status==='approved'){status.textContent='Approved! Opening RDT Voice…';window.location.replace('/');return}if(sd.status==='denied')throw new Error('This pairing request was denied. Generate a fresh QR if you want to try again.');if(sd.status==='expired')throw new Error('This pairing request expired. Generate a fresh QR and try again.')}
   throw new Error('No approval was received in time. Generate a fresh QR and try again.');
  }catch(error){showError(String(error&&error.message||'Pairing failed.'));active=false;labelInput.disabled=false}
 });
})();
</script></body></html>"""

@app.get("/pair", response_class=HTMLResponse)
def pair_landing_page(request: Request):
    check_host(_request_host(request))
    nonce = secrets.token_urlsafe(18)
    html = PAIR_REDEEM_HTML.replace("<script>", f'<script nonce="{nonce}">', 1)
    return HTMLResponse(html, headers={
        "Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
        "X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY",
        "Content-Security-Policy": f"default-src 'none'; script-src 'nonce-{nonce}'; connect-src 'self'; img-src data:; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'; object-src 'none'; frame-ancestors 'none'",
    })


def _require_same_origin_admin_mutation(request: Request) -> None:
    if not _same_origin_mutation(request):
        raise HTTPException(status_code=403, detail="This action must be submitted from the same RDT Voice origin.")



@app.get("/auth/tailscale/status")
def auth_tailscale_status(request: Request):
    check_host(_request_host(request))
    _require_pairing_admin(request)
    if not _local_direct_request(request):
        raise HTTPException(status_code=403, detail="Open this page on the Mac running RDT Voice to configure private access.")
    cli = _tailscale_cli_path()
    if not cli:
        return JSONResponse({"available": False, "ready": False, "message": "Tailscale command-line support was not found on this Mac. Start Tailscale, or use the advanced HTTPS address option."}, headers={"Cache-Control": "no-store"})
    try:
        status = _tailscale_run_json(cli, ["status", "--json"])
        self_info = status.get("Self") if isinstance(status, dict) else None
        dns_name = str((self_info or {}).get("DNSName", "") or "").strip().rstrip(".").lower()
        if not dns_name.endswith((".ts.net", ".tailscale.net")):
            return JSONResponse({"available": False, "ready": False, "message": "Tailscale is installed, but this Mac has no active private DNS name. Connect Tailscale and reload."}, headers={"Cache-Control": "no-store"})
        serve_status = _tailscale_serve_status(cli)
        local_port = _local_server_port(request)
        existing_url = _tailscale_origin_for_local_port(serve_status, dns_name, local_port)
        return JSONResponse({"available": True, "ready": bool(existing_url), "dns_name": dns_name, "port": local_port, "public_url": existing_url, "message": "Private HTTPS access is ready." if existing_url else "Tailscale is connected. Set up a separate private HTTPS route for this test server."}, headers={"Cache-Control": "no-store"})
    except subprocess.TimeoutExpired:
        detail = "Tailscale did not respond in time. Confirm the Tailscale app is running, then reload."
    except (OSError, RuntimeError) as exc:
        detail = f"Could not read Tailscale status: {str(exc)[:180]}"
    return JSONResponse({"available": False, "ready": False, "message": detail}, headers={"Cache-Control": "no-store"})


@app.post("/auth/tailscale/setup")
def auth_tailscale_setup(request: Request):
    check_host(_request_host(request))
    _require_pairing_admin(request)
    _require_same_origin_admin_mutation(request)
    if not _local_direct_request(request):
        raise HTTPException(status_code=403, detail="For safety, set up private access from the Mac itself at 127.0.0.1.")
    cli = _tailscale_cli_path()
    if not cli:
        raise HTTPException(status_code=503, detail="Tailscale command-line support was not found on this Mac. Start Tailscale or use the advanced HTTPS address option.")
    try:
        status = _tailscale_run_json(cli, ["status", "--json"])
        self_info = status.get("Self") if isinstance(status, dict) else None
        dns_name = str((self_info or {}).get("DNSName", "") or "").strip().rstrip(".").lower()
        if not dns_name.endswith((".ts.net", ".tailscale.net")):
            raise HTTPException(status_code=503, detail="Tailscale is not connected on this Mac. Connect it and try again.")
        serve_status = _tailscale_serve_status(cli)
        local_port = _local_server_port(request)
        existing_url = _tailscale_origin_for_local_port(serve_status, dns_name, local_port)
        if existing_url:
            return JSONResponse({"ok": True, "ready": True, "reused": True, "dns_name": dns_name, "port": local_port, "public_url": existing_url}, headers={"Cache-Control": "no-store"})
        used_ports = _tailscale_used_serve_ports(serve_status)
        serve_port = next((p for p in range(8445, 8461) if p not in used_ports), None)
        if serve_port is None:
            raise HTTPException(status_code=409, detail="No unused HTTPS port was found between 8445 and 8460. Review Tailscale Serve routes before trying again.")
        target = f"http://127.0.0.1:{local_port}"
        result = subprocess.run([cli, "serve", f"--https={serve_port}", "--bg", target], capture_output=True, text=True, timeout=20, check=False)
        if result.returncode != 0:
            message = (result.stderr or result.stdout or "Tailscale could not create the private route.").strip().splitlines()
            raise HTTPException(status_code=502, detail=(message[-1][:220] if message else "Tailscale could not create the private route."))
        public_url = f"https://{dns_name}:{serve_port}"
        # Re-read status to confirm the exact route, but never reset or rewrite other routes.
        try:
            verified = _tailscale_serve_status(cli)
            detected = _tailscale_origin_for_local_port(verified, dns_name, local_port)
            if detected:
                public_url = detected
        except (OSError, RuntimeError, subprocess.TimeoutExpired):
            pass
        return JSONResponse({"ok": True, "ready": True, "reused": False, "dns_name": dns_name, "port": local_port, "public_url": public_url, "serve_port": serve_port}, headers={"Cache-Control": "no-store"})
    except subprocess.TimeoutExpired as exc:
        raise HTTPException(status_code=504, detail="Tailscale took too long to configure the route. Check Tailscale status, then refresh this page.") from exc
    except (OSError, RuntimeError) as exc:
        raise HTTPException(status_code=503, detail=f"Could not configure private access: {str(exc)[:200]}") from exc

@app.post("/auth/pair/start")
def auth_pair_start(request: Request, payload: dict):
    if not AUTH_ENABLED:
        raise HTTPException(status_code=503, detail="Pairing is unavailable because backend authentication is disabled.")
    _require_pairing_admin(request)
    _require_same_origin_admin_mutation(request)
    public_origin = _allowed_pair_origin(str(payload.get("public_origin", "")), request)
    code = secrets.token_urlsafe(32)
    now = time.time()
    with _auth_db() as conn:
        # Only one unused QR is valid at a time: requesting another invalidates the prior one immediately.
        conn.execute("UPDATE pair_codes SET used_at=? WHERE used_at IS NULL AND expires_at>?", (now, now))
        conn.execute("DELETE FROM pair_codes WHERE expires_at<=? OR used_at IS NOT NULL", (now,))
        conn.execute("UPDATE pair_requests SET status='expired' WHERE status='pending' AND expires_at<=?", (now,))
        conn.execute("DELETE FROM pair_requests WHERE expires_at<=? OR status IN ('denied','expired')", (now,))
        conn.execute("DELETE FROM pair_codes WHERE code_hash IN (SELECT code_hash FROM pair_codes ORDER BY created_at DESC LIMIT -1 OFFSET 10)")
        conn.execute(
            "INSERT INTO pair_codes (code_hash, created_at, expires_at, used_at, created_by, public_origin) VALUES (?,?,?,NULL,?,?)",
            (_secret_hash(code), now, now + PAIRING_TTL_SECONDS, (request.headers.get("tailscale-user-login") or "local operator")[:240], public_origin),
        )
    try:
        AUTH_DB_PATH.chmod(0o600)
    except OSError:
        pass
    pairing_url = public_origin + "/pair#" + code
    qr = qrcode.QRCode(version=None, error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=7, border=4)
    qr.add_data(pairing_url)
    qr.make(fit=True)
    qr_svg = qr.make_image(image_factory=SvgPathImage).to_string().decode("utf-8")
    return JSONResponse({"ok": True, "pairing_url": pairing_url, "qr_svg": qr_svg, "expires_at": now + PAIRING_TTL_SECONDS}, headers={"Cache-Control":"no-store"})


@app.post("/auth/pair/request")
def auth_pair_request(request: Request, payload: dict):
    if not AUTH_ENABLED:
        raise HTTPException(status_code=503, detail="Pairing is unavailable because backend authentication is disabled.")
    check_host(_request_host(request))
    if not _same_origin_mutation(request):
        raise HTTPException(status_code=403, detail="Pairing must be requested from the HTTPS origin shown in the QR code.")
    code = str(payload.get("code", "")).strip()
    claim_token = str(payload.get("claim_token", "")).strip()
    if not 32 <= len(code) <= 160 or not 40 <= len(claim_token) <= 100:
        raise HTTPException(status_code=400, detail="Pairing code is invalid. Generate a fresh QR code.")
    now = time.time()
    code_hash = _secret_hash(code)
    claim_hash = _secret_hash(claim_token)
    request_id = str(uuid.uuid4())
    user_agent = (request.headers.get("user-agent", "") or "Unknown browser")[:240]
    label = str(payload.get("device_label", "Browser")).strip()[:120] or "Browser"
    with _auth_db() as conn:
        row = conn.execute("SELECT expires_at, used_at, public_origin FROM pair_codes WHERE code_hash=?", (code_hash,)).fetchone()
        if not row or row["used_at"] is not None or float(row["expires_at"]) <= now:
            raise HTTPException(status_code=400, detail="This pairing QR expired or was already used. Generate a fresh QR code.")
        request_origin = _request_origin(request)
        expected_origin = str(row["public_origin"] or "")
        if not expected_origin or not hmac.compare_digest(expected_origin.encode("utf-8"), request_origin.encode("utf-8")):
            raise HTTPException(status_code=403, detail="This QR was created for a different HTTPS address. Generate a fresh QR code from the correct backend.")
        updated = conn.execute("UPDATE pair_codes SET used_at=? WHERE code_hash=? AND used_at IS NULL AND expires_at>?", (now, code_hash, now)).rowcount
        if updated != 1:
            raise HTTPException(status_code=409, detail="This QR has already been used. Generate a fresh QR code.")
        conn.execute("INSERT INTO pair_requests (id, code_hash, claim_hash, device_label, user_agent, created_at, expires_at, status, approved_at, denied_at, session_id) VALUES (?,?,?,?,?,?,?,'pending',NULL,NULL,NULL)",
                     (request_id, code_hash, claim_hash, label, user_agent, now, now + PAIR_REQUEST_TTL_SECONDS))
    try:
        AUTH_DB_PATH.chmod(0o600)
    except OSError:
        pass
    return JSONResponse({"ok": True, "request_id": request_id, "expires_at": now + PAIR_REQUEST_TTL_SECONDS, "status": "pending"}, headers={"Cache-Control":"no-store"})


@app.post("/auth/pair/status")
def auth_pair_status(request: Request, payload: dict):
    check_host(_request_host(request))
    if not _same_origin_mutation(request):
        raise HTTPException(status_code=403, detail="Pairing status must be checked from the same origin.")
    try:
        request_id = str(uuid.UUID(str(payload.get("request_id", ""))))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid pairing request.") from exc
    claim_token = str(payload.get("claim_token", "")).strip()
    if not 40 <= len(claim_token) <= 100:
        raise HTTPException(status_code=400, detail="Invalid pairing request credential.")
    claim_hash = _secret_hash(claim_token)
    now = time.time()
    with _auth_db() as conn:
        row = conn.execute("SELECT id, claim_hash, expires_at, status, session_id FROM pair_requests WHERE id=?", (request_id,)).fetchone()
        if not row or not hmac.compare_digest(str(row["claim_hash"]), claim_hash):
            raise HTTPException(status_code=404, detail="Pairing request not found. Scan a fresh QR code.")
        status = str(row["status"])
        if status == "pending" and float(row["expires_at"]) <= now:
            conn.execute("UPDATE pair_requests SET status='expired' WHERE id=? AND status='pending'", (request_id,))
            status = "expired"
        session_row = None
        if status == "approved":
            session_row = conn.execute("SELECT id, idle_expires_at, absolute_expires_at, revoked_at FROM browser_sessions WHERE id=? AND token_hash=?", (row["session_id"], claim_hash)).fetchone()
            if not session_row or session_row["revoked_at"] is not None or float(session_row["idle_expires_at"]) <= now or float(session_row["absolute_expires_at"]) <= now:
                status = "revoked"
    if status == "approved" and session_row:
        max_age = max(0, int(min(SESSION_IDLE_SECONDS, float(session_row["absolute_expires_at"]) - now)))
        response = JSONResponse({"ok": True, "status": "approved", "device_id": session_row["id"]}, headers={"Cache-Control":"no-store"})
        response.set_cookie(key=SESSION_COOKIE, value=claim_token, max_age=max_age, httponly=True,
                            secure=_cookie_is_secure(request), samesite="strict", path="/")
        return response
    if status == "revoked":
        raise HTTPException(status_code=403, detail="This session is no longer valid. Start pairing again.")
    return JSONResponse({"ok": True, "status": status}, headers={"Cache-Control":"no-store"})


@app.get("/auth/pair/requests")
def auth_list_pair_requests(request: Request):
    _require_pairing_admin(request)
    now = time.time()
    with _auth_db() as conn:
        conn.execute("UPDATE pair_requests SET status='expired' WHERE status='pending' AND expires_at<=?", (now,))
        rows = conn.execute("SELECT id, device_label, user_agent, created_at, expires_at FROM pair_requests WHERE status='pending' AND expires_at>? ORDER BY created_at ASC", (now,)).fetchall()
    return JSONResponse({"requests": [dict(r) for r in rows]}, headers={"Cache-Control":"no-store"})


@app.post("/auth/pair/requests/{request_id}/approve")
def auth_approve_pair_request(request_id: str, request: Request):
    _require_pairing_admin(request)
    _require_same_origin_admin_mutation(request)
    try:
        request_id = str(uuid.UUID(request_id))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid pairing request identifier.") from exc
    now = time.time()
    session_id = str(uuid.uuid4())
    absolute = now + SESSION_ABSOLUTE_SECONDS
    with _auth_db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT claim_hash, device_label, user_agent, expires_at, status FROM pair_requests WHERE id=?", (request_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Pairing request no longer exists.")
        if row["status"] != "pending":
            raise HTTPException(status_code=409, detail=f"This pairing request is {row['status']}.")
        if float(row["expires_at"]) <= now:
            conn.execute("UPDATE pair_requests SET status='expired' WHERE id=? AND status='pending'", (request_id,))
            raise HTTPException(status_code=410, detail="This pairing request expired. Ask the browser to scan a fresh QR code.")
        conn.execute("INSERT INTO browser_sessions (id, token_hash, device_label, user_agent, created_at, last_seen, idle_expires_at, absolute_expires_at, revoked_at) VALUES (?,?,?,?,?,?,?,?,NULL)",
            (session_id, row["claim_hash"], row["device_label"], row["user_agent"], now, now, min(now + SESSION_IDLE_SECONDS, absolute), absolute))
        conn.execute("UPDATE pair_requests SET status='approved', approved_at=?, session_id=? WHERE id=? AND status='pending'", (now, session_id, request_id))
    try:
        AUTH_DB_PATH.chmod(0o600)
    except OSError:
        pass
    return JSONResponse({"ok": True, "approved": True}, headers={"Cache-Control":"no-store"})


@app.post("/auth/pair/requests/{request_id}/deny")
def auth_deny_pair_request(request_id: str, request: Request):
    _require_pairing_admin(request)
    _require_same_origin_admin_mutation(request)
    try:
        request_id = str(uuid.UUID(request_id))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid pairing request identifier.") from exc
    with _auth_db() as conn:
        changed = conn.execute("UPDATE pair_requests SET status='denied', denied_at=? WHERE id=? AND status='pending' AND expires_at>?", (time.time(), request_id, time.time())).rowcount
    if not changed:
        raise HTTPException(status_code=409, detail="This pairing request is no longer pending.")
    return JSONResponse({"ok": True, "denied": True}, headers={"Cache-Control":"no-store"})


@app.get("/auth/devices")
def auth_list_devices(request: Request):
    _require_pairing_admin(request)
    now = time.time()
    with _auth_db() as conn:
        rows = conn.execute(
            "SELECT id, device_label, user_agent, created_at, last_seen, idle_expires_at, absolute_expires_at FROM browser_sessions WHERE revoked_at IS NULL AND idle_expires_at>? AND absolute_expires_at>? ORDER BY last_seen DESC",
            (now, now),
        ).fetchall()
    return JSONResponse({"devices": [dict(r) for r in rows]}, headers={"Cache-Control":"no-store"})


@app.delete("/auth/devices/{session_id}")
def auth_revoke_device(session_id: str, request: Request):
    _require_pairing_admin(request)
    _require_same_origin_admin_mutation(request)
    try:
        session_id = str(uuid.UUID(session_id))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid device identifier.") from exc
    with _auth_db() as conn:
        changed = conn.execute("UPDATE browser_sessions SET revoked_at=? WHERE id=? AND revoked_at IS NULL", (time.time(), session_id)).rowcount
    return JSONResponse({"ok": True, "revoked": bool(changed)}, headers={"Cache-Control":"no-store"})

def _validate_raw_path(raw: str) -> None:
    if "\x00" in raw or len(raw) > 512:
        raise HTTPException(status_code=400, detail="bad path")
    if raw.startswith(("\\", "//")):
        raise HTTPException(status_code=400, detail="bad path")

def _path_candidate(raw: str) -> pathlib.Path:
    p = pathlib.Path(raw).expanduser()
    if not p.is_absolute():
        p = ALLOWED_ROOT / p
    return p

def _reject_symlink_components(candidate: pathlib.Path, resolved_root: pathlib.Path) -> None:
    try:
        rel = candidate.relative_to(resolved_root)
    except ValueError:
        return
    cur = resolved_root
    for part in rel.parts:
        cur = cur / part
        try:
            if cur.is_symlink():
                raise HTTPException(status_code=403, detail="symlink not allowed")
        except OSError:
            pass

def _is_inside(path: pathlib.Path, root: pathlib.Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False

def resolve_cache_dir_safe(requested: Optional[str]) -> pathlib.Path:
    raw = (requested or str(ALLOWED_ROOT)).strip() or str(ALLOWED_ROOT)
    if "browser hides full path" in raw:
        raw = str(ALLOWED_ROOT)
    _validate_raw_path(raw)
    candidate = _path_candidate(raw)
    resolved = candidate.resolve()
    allowed_roots = [ALLOWED_ROOT.resolve()]
    if CACHE_ROOT is not None:
        allowed_roots.append(CACHE_ROOT.resolve())
    if not any(_is_inside(resolved, r) for r in allowed_roots):
        raise HTTPException(status_code=403, detail="outside allowed recording/cache roots")
    for r in allowed_roots:
        if _is_inside(candidate, r):
            _reject_symlink_components(candidate, r)
            break
    try:
        resolved.mkdir(parents=True, exist_ok=True, mode=0o700)
        resolved.chmod(0o700)
    except OSError as e:
        raise HTTPException(status_code=400, detail="cannot use that recording/cache folder")
    return resolved

def resolve_recording_file_safe(requested: str) -> pathlib.Path:
    raw = (requested or "").strip()
    if "browser hides full path" in raw:
        raise HTTPException(status_code=400, detail="bad path")
    _validate_raw_path(raw)
    root = ALLOWED_ROOT.resolve()
    candidate = _path_candidate(raw)
    resolved = candidate.resolve()
    if not _is_inside(resolved, root):
        raise HTTPException(status_code=403, detail="outside allowed recording root")
    _reject_symlink_components(candidate, root)
    if resolved == root:
        raise HTTPException(status_code=400, detail="not a file")
    return resolved

def safe_filename(name: str) -> str:
    name = os.path.basename(name or "rec.webm")
    name = re.sub(r'[^a-zA-Z0-9_.-]','_',name)[:120]
    return name or "rec.webm"

# Supported container/codec hints for the user-facing diagnostics. ffmpeg is still
# the actual authority: this list is guidance, not a whitelist.
MIME_TO_EXT = {
    "audio/webm":".webm", "audio/ogg":".ogg", "audio/opus":".opus",
    "audio/mpeg":".mp3", "audio/wav":".wav", "audio/x-wav":".wav",
    "audio/mp4":".m4a", "audio/x-m4a":".m4a", "audio/aac":".aac",
    "audio/flac":".flac", "audio/mp4a-latm":".m4a",
    "video/webm":".webm", "video/mp4":".mp4",
}
SUPPORTED_HINTS = "WebM/Opus, Ogg/Opus, MP3, WAV, M4A/MP4, AAC, and FLAC"

def _input_suffix(filename: Optional[str], content_type: Optional[str]) -> str:
    ext = pathlib.Path(filename or "").suffix.lower()
    if ext in {".webm",".ogg",".oga",".opus",".mp3",".wav",".m4a",".mp4",".aac",".flac",".wma",".aiff",".aif"}:
        return ext
    return MIME_TO_EXT.get((content_type or "").lower(), ".bin")

def rms_to_dbfs(rms: float) -> float:
    return -120.0 if rms<=0 else 20.0*math.log10(rms/32768.0)
def dbfs_to_rms(dbfs: float) -> float:
    return 32768.0*(10.0**(dbfs/20.0))

def choose_adaptive_threshold_dbfs(rms: np.ndarray) -> float:
    if rms.size==0: return SILENCE_THRESHOLD_CAP_DBFS
    db = np.array([rms_to_dbfs(float(x)) for x in rms], dtype=np.float64)
    finite = db[np.isfinite(db)]
    if finite.size==0: return SILENCE_THRESHOLD_CAP_DBFS
    p20=float(np.percentile(finite,20)); p80=float(np.percentile(finite,80))
    threshold = (p20+p80)/2.0 if (p80-p20)>=6.0 else p20-3.0
    if (p80-p20)>=6.0: threshold=min(threshold, p20+4.0)
    return float(max(min(threshold, SILENCE_THRESHOLD_CAP_DBFS), SILENCE_THRESHOLD_FLOOR_DBFS))

def calculate_frame_rms(data: np.ndarray, sr: int, frame_ms=FRAME_MS, hop_ms=HOP_MS):
    if len(data)==0: return np.array([],dtype=np.float64), np.array([],dtype=np.int64)
    fl,hl=int(sr*frame_ms/1000), int(sr*hop_ms/1000)
    if len(data)<=fl:
        return np.array([np.sqrt(np.mean(data.astype(np.float64)**2))]), np.array([0],dtype=np.int64)
    starts=np.arange(0,len(data)-fl+1,hl,dtype=np.int64)
    rms=np.empty(len(starts),dtype=np.float64)
    for i,s in enumerate(starts):
        rms[i]=np.sqrt(np.mean(data[s:s+fl].astype(np.float64)**2))
    return rms,starts

def detect_long_silences(data: np.ndarray, sr: int):
    if len(data)==0: return [],[],{"threshold_dbfs":SILENCE_THRESHOLD_CAP_DBFS,"max_rms":0.0}
    rms,starts=calculate_frame_rms(data,sr)
    if len(rms)==0: return [],[],{"threshold_dbfs":SILENCE_THRESHOLD_CAP_DBFS,"max_rms":0.0}
    max_rms=float(np.max(rms))
    if max_rms < MIN_MAX_RMS_GATE:
        return [],[],{"threshold_dbfs":SILENCE_THRESHOLD_CAP_DBFS,"max_rms":max_rms,"max_dbfs":rms_to_dbfs(max_rms),"gate_rejected":True}
    thr_db=choose_adaptive_threshold_dbfs(rms); thr_rms=dbfs_to_rms(thr_db)
    cand=rms<thr_rms
    min_frames=int(math.floor(CUT_SILENCE_SECONDS*1000.0/HOP_MS))+1
    runs=[]; rs=None
    for i,sil in enumerate(cand):
        if sil and rs is None: rs=i
        elif not sil and rs is not None:
            if i-rs>=min_frames: runs.append((rs,i-1))
            rs=None
    if rs is not None and len(cand)-rs>=min_frames: runs.append((rs,len(cand)-1))
    boundary_pad=int(sr*BOUNDARY_PAD_MS/1000.0)
    raw_cuts=[]
    for rf,rl in runs:
        ss,es=int(starts[rf]), int(starts[rl]+sr*FRAME_MS/1000.0)
        ss,es=max(0,ss), min(len(data),es)
        if (es-ss)/float(sr) <= CUT_SILENCE_SECONDS: continue
        has_before,has_after=rf>0, rl < len(rms)-1
        before_rms=float(rms[rf-1]) if has_before else 0.0
        after_rms=float(rms[rl+1]) if has_after else 0.0
        conf_rms=dbfs_to_rms(min(thr_db+SPEECH_MARGIN_DB, -47.0))
        if has_before and has_after:
            if not (before_rms>=conf_rms and after_rms>=conf_rms): continue
        elif has_after:
            if not after_rms>=conf_rms: continue
        elif has_before:
            if not before_rms>=conf_rms: continue
        else: continue
        raw_cuts.append((max(0,ss-boundary_pad), min(len(data),es+boundary_pad)))
    raw_cuts.sort()
    cuts=[]
    for s,e in raw_cuts:
        if e<=s: continue
        if not cuts or s>cuts[-1][1]: cuts.append((s,e))
        else: cuts[-1]=(cuts[-1][0], max(cuts[-1][1], e))
    kept=[]; cur=0
    for s,e in cuts:
        if s>cur: kept.append((cur,s))
        cur=max(cur,e)
    if cur < len(data): kept.append((cur,len(data)))
    return kept,cuts,{"threshold_dbfs":thr_db,"threshold_rms":thr_rms,"max_rms":max_rms,"max_dbfs":rms_to_dbfs(max_rms),"gate_rejected":False}

# Progress
PROGRESS_LOCK=threading.Lock(); PROGRESS={}; PROGRESS_TTL_SECONDS=3600
INFERENCE_LOCK=threading.Lock()
ACTIVE_JOBS=set(); ACTIVE_JOBS_LOCK=threading.Lock()

def _prune_progress_locked(now=None):
    now = now or time.time()
    cutoff = now - PROGRESS_TTL_SECONDS
    stale = []
    for jid, item in PROGRESS.items():
        try:
            if datetime.datetime.fromisoformat(item.get("updated","")).timestamp() < cutoff:
                stale.append(jid)
        except Exception:
            stale.append(jid)
    for jid in stale:
        PROGRESS.pop(jid, None)

def set_progress(job_id,pct,stage,**extra):
    if not job_id: return
    with PROGRESS_LOCK:
        _prune_progress_locked()
        PROGRESS[job_id]={"percent":max(0,min(100,float(pct))),"stage":stage,"updated":datetime.datetime.now().isoformat(),**extra}

def get_progress(job_id):
    with PROGRESS_LOCK:
        _prune_progress_locked()
        return PROGRESS.get(job_id)

# Warmup
with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as _wf:
    _warm_path = _wf.name
try:
    with wave.open(_warm_path, "wb") as wf:
        wf.setnchannels(1); wf.setsampwidth(2); wf.setframerate(16000); wf.writeframes(b"\x00\x00"*8000)
    mlx_whisper.transcribe(_warm_path, path_or_hf_repo=MODEL_ID, verbose=False)
finally:
    try: os.unlink(_warm_path)
    except OSError: pass

def clean_transcript_enhanced(txt: str) -> str:
    if not txt: return ""
    t=txt.strip(); low=t.lower()
    blacklist={"thank you.","thank you","thanks.","thanks","thank","thank thank","thank you!","subtitles by amara.org community","amara.org community","thanks for watching","subtitles by","amara.org"}
    if low in blacklist: return ""
    t=re.sub(r'(?i)\bthanks? for watching\b[.!?]*',' ',t)
    t=re.sub(r'(?i)\bsubtitles\s+by\b[.!?:-]*',' ',t)
    t=re.sub(r'(?i)\bamara\.org(?:\s+community)?\b[.!?]*',' ',t)
    t=re.sub(r'^(thank you\.?|thanks\.?|thank thank\.?|thank)\s+','',t,flags=re.I).strip()
    t=re.sub(r'\s+(thank you\.?|thanks\.?|thank thank\.?)\s*$','',t,flags=re.I).strip()
    t=re.sub(r'\b(\w+)(?:\s+\1){2,}\b', r'\1', t, flags=re.I)
    parts=[p.strip() for p in re.split(r'(?<=[.!?])\s+', t) if p.strip()]
    if len(parts)>=2:
        norm=[re.sub(r'\s+',' ',p).strip().casefold() for p in parts]
        collapsed=[]; i=0
        while i < len(parts):
            run=1
            # Only collapse exact, substantial repeated sentences; preserve short
            # natural repetitions such as "Yes. Yes." or "Go! Go!".
            if len(norm[i]) >= 12 or (re.search(r'[一-鿿]', norm[i]) and len(norm[i]) >= 5):
                while i+run < len(parts) and norm[i+run] == norm[i]: run += 1
            collapsed.append(parts[i]); i += run
        t=' '.join(collapsed)
    return re.sub(r'\s{2,}',' ',t).strip()

@app.get("/health")
def health(host: Optional[str]=Header(None)):
    check_host(host)
    return {"status":"ok","model":MODEL_ID,"features":{"concurrent":True,"concurrent_requests":True,"parallel_inference":False,"active_endpoint":True,"inference_serialized":True,"formats":SUPPORTED_HINTS}}

@app.get("/v1/models")
def models(host: Optional[str]=Header(None), authorization: Optional[str]=Header(None), tailscale_user_login: Optional[str]=Header(None, alias="Tailscale-User-Login")):
    check_host(host); require_bearer(authorization, tailscale_user_login)
    return {"data":[{"id":MODEL_ID,"object":"model","owned_by":"RDT Voice / MLX"}]}

@app.post("/v1/open_recordings")
def open_recordings(authorization: Optional[str]=Header(None), host: Optional[str]=Header(None), payload: dict=None, tailscale_user_login: Optional[str]=Header(None, alias="Tailscale-User-Login")):
    check_host(host); require_bearer(authorization, tailscale_user_login)
    file_arg = (payload or {}).get("file") if isinstance(payload, dict) else None
    try:
        if file_arg:
            target = resolve_recording_file_safe(file_arg)
            if not target.exists(): raise HTTPException(status_code=404, detail="file not found")
            if platform.system() == "Darwin": subprocess.Popen(["open", "-R", str(target)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            elif platform.system() == "Windows": subprocess.Popen(["explorer", "/select,", str(target)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            else: subprocess.Popen(["xdg-open", str(target.parent)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return {"ok":True,"path":str(target.relative_to(ALLOWED_ROOT))}
        if platform.system() == "Darwin": subprocess.Popen(["open", str(ALLOWED_ROOT)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        elif platform.system() == "Windows": os.startfile(str(ALLOWED_ROOT))  # type: ignore[attr-defined]
        else: subprocess.Popen(["xdg-open", str(ALLOWED_ROOT)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except HTTPException: raise
    except Exception as e: raise HTTPException(status_code=500, detail="could not open the recordings folder")
    return {"ok":True,"path":"."}

@app.post("/v1/open_recording")
def open_recording(authorization: Optional[str]=Header(None), host: Optional[str]=Header(None), payload: dict=None, tailscale_user_login: Optional[str]=Header(None, alias="Tailscale-User-Login")):
    check_host(host); require_bearer(authorization, tailscale_user_login)
    file_arg = (payload or {}).get("file") if isinstance(payload, dict) else None
    if not file_arg: raise HTTPException(status_code=400, detail="missing file")
    target = resolve_recording_file_safe(file_arg)
    if not target.exists() or not target.is_file(): raise HTTPException(status_code=404, detail="recording not found")
    try:
        if platform.system() == "Darwin": subprocess.Popen(["open", str(target)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        elif platform.system() == "Windows": os.startfile(str(target))  # type: ignore[attr-defined]
        else: subprocess.Popen(["xdg-open", str(target)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e: raise HTTPException(status_code=500, detail="could not open the recording")
    return {"ok":True,"path":str(target.relative_to(ALLOWED_ROOT))}

@app.post("/v1/audio/transcriptions")
def trans(file: UploadFile=File(...), model: str=Form(default="whisper-1"), audio_cache_dir: str=Form(default=None), language: str=Form(default="auto"), job_id: Optional[str]=Form(default=None), authorization: Optional[str]=Header(None), host: Optional[str]=Header(None), origin: Optional[str]=Header(None), tailscale_user_login: Optional[str]=Header(None, alias="Tailscale-User-Login")):
    check_host(host); require_bearer(authorization, tailscale_user_login)
    if not FFMPEG:
        raise HTTPException(status_code=500, detail="ffmpeg is not installed. Install ffmpeg or set FFMPEG_PATH.")

    job_id = job_id or uuid.uuid4().hex
    set_progress(job_id, 0, "receiving", label="Receiving audio")
    cache_dir = resolve_cache_dir_safe(audio_cache_dir)
    original_name = safe_filename(file.filename or "rec.webm")
    suffix = _input_suffix(file.filename, file.content_type)

    inp = None; w1 = None; w2 = None
    saved_path = None
    try:
        # Stream upload to disk so large mobile recordings do not create a second
        # full-size copy in Python RAM.
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tf:
            inp = tf.name
            total = 0
            while True:
                chunk = file.file.read(UPLOAD_CHUNK_BYTES)
                if not chunk: break
                total += len(chunk)
                if total > MAX_UPLOAD_BYTES:
                    raise HTTPException(status_code=413, detail=f"file too large; maximum is {MAX_UPLOAD_BYTES // (1024*1024)} MB")
                tf.write(chunk)
        if total < 256:
            set_progress(job_id, 100, "empty", label="Audio file was empty")
            return {"text":"","reason":"empty","job_id":job_id}
        set_progress(job_id, 8, "received", label=f"Received {total / (1024*1024):.1f} MB")

        timestamp=datetime.datetime.now().strftime("%Y%m%d_%H%M%S"); short_id=uuid.uuid4().hex[:6]
        save_name=f"{timestamp}_{short_id}_{original_name}"
        saved_path=cache_dir/save_name
        try:
            shutil.copyfile(inp, saved_path)
            try: os.chmod(saved_path, 0o600)
            except OSError: pass
            print(f"[CACHE] saved {saved_path.name}")
        except Exception as e:
            print(f"[CACHE ERROR] {type(e).__name__}")
            saved_path=None
        set_progress(job_id, 15, "saved", label="Recording saved")

        w1=inp+".1.wav"; w2=inp+".2.wav"
        cmd=[FFMPEG,"-hide_banner","-loglevel","error","-y","-i",inp,"-vn","-sn","-dn","-ar","16000","-ac","1","-f","wav",w1]
        p=subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, timeout=FFMPEG_TIMEOUT_SECONDS, check=False)
        if p.returncode != 0 or not pathlib.Path(w1).exists() or pathlib.Path(w1).stat().st_size < 1000:
            detail=(p.stderr or "").strip().splitlines()[-1] if (p.stderr or "").strip() else "unknown decoder error"
            raise HTTPException(status_code=415, detail=f"RDT Voice could not decode this audio file. Supported formats include {SUPPORTED_HINTS}. Decoder: {detail[:300]}")
        set_progress(job_id, 32, "decoded", label="Audio decoded")

        try:
            with wave.open(w1,"rb") as wf:
                sr=wf.getframerate(); raw=np.frombuffer(wf.readframes(wf.getnframes()), dtype=np.int16).copy()
        except wave.Error as e:
            raise HTTPException(status_code=415, detail=f"RDT Voice decoded the file but could not read its PCM audio: {e}")
        if len(raw)==0:
            set_progress(job_id, 100, "empty", label="No audio samples found")
            return {"text":"","reason":"empty","job_id":job_id}
        set_progress(job_id, 42, "analyzing", label="Checking speech and silence")

        kept,cuts,vad_info=detect_long_silences(raw,sr)
        if vad_info.get("gate_rejected"):
            set_progress(job_id, 100, "silence", label="No meaningful audio detected")
            return {"text":"","reason":"silence","duration_original":len(raw)/float(sr),"vad":vad_info,"job_id":job_id}
        if not kept:
            set_progress(job_id, 100, "silence", label="No speech region remained after filtering")
            return {"text":"","reason":"silence","duration_original":len(raw)/float(sr),"vad":vad_info,"job_id":job_id}

        mapping=[]; cur=0
        with wave.open(w2,"wb") as wf:
            wf.setnchannels(1); wf.setsampwidth(2); wf.setframerate(sr)
            for os_,oe in kept:
                if oe<=os_: continue
                chunk=raw[os_:oe]
                if len(chunk)==0: continue
                ts=cur/float(sr); wf.writeframes(chunk.astype(np.int16).tobytes()); cur+=len(chunk); te=cur/float(sr)
                mapping.append({"orig_start":os_/float(sr),"orig_end":oe/float(sr),"trimmed_start":ts,"trimmed_end":te})
        if not mapping:
            set_progress(job_id, 100, "silence", label="No usable audio remained")
            return {"text":"","reason":"silence","duration_original":len(raw)/float(sr),"vad":vad_info,"job_id":job_id}
        trimmed_duration=mapping[-1]["trimmed_end"]
        set_progress(job_id, 50, "transcribing", label="Transcribing with MLX", duration_original=len(raw)/float(sr))

        with ACTIVE_JOBS_LOCK: ACTIVE_JOBS.add(job_id)
        try:
            # Multiple HTTP requests can receive/upload/decode concurrently. MLX
            # inference is still serialized to avoid competing for unified RAM.
            if INFERENCE_LOCK.locked():
                set_progress(job_id, 49, "waiting", label="Waiting for transcription engine", duration_original=len(raw)/float(sr))
            with INFERENCE_LOCK:
                set_progress(job_id, 50, "transcribing", label="Transcribing with MLX", duration_original=len(raw)/float(sr))
                result=mlx_whisper.transcribe(
                    w2,
                    path_or_hf_repo=MODEL_ID,
                    verbose=False,
                    condition_on_previous_text=False,
                    **({"language": language} if language and language != "auto" else {}),
                    temperature=0.0,
                    no_speech_threshold=0.6,
                    logprob_threshold=-1.0,
                )
        finally:
            with ACTIVE_JOBS_LOCK: ACTIVE_JOBS.discard(job_id)

        set_progress(job_id, 92, "finalizing", label="Cleaning transcript")
        txt=clean_transcript_enhanced((result.get("text","") or "").strip())
        segments=result.get("segments",[]) or []
        set_progress(job_id, 100, "complete", label="Transcription complete", text_length=len(txt))
        return {
            "text":txt,
            "segments":segments,
            "duration_original":len(raw)/float(sr),
            "duration_filtered":trimmed_duration,
            "vad":vad_info,
            "job_id":job_id,
            "saved_recording":bool(saved_path),
            "recording_name":saved_path.name if saved_path else None,
        }
    except HTTPException as e:
        set_progress(job_id, 100, "error", label="Transcription failed", error=str(e.detail))
        raise
    except subprocess.TimeoutExpired:
        set_progress(job_id, 100, "error", label="Audio decoding timed out")
        raise HTTPException(status_code=504, detail="Audio decoding timed out. The recording may be unusually large or the codec may be problematic.")
    except Exception as e:
        set_progress(job_id, 100, "error", label="Transcription failed", error=str(e)[:500])
        raise HTTPException(status_code=500, detail=f"RDT Voice could not finish transcription: {str(e)[:400]}")
    finally:
        for p in [inp,w1,w2]:
            if p:
                try: os.unlink(p)
                except OSError: pass
        try: file.file.close()
        except Exception: pass


# Duration cache contains metadata only (path, size, mtime, numeric duration),
# never audio bytes. First-time duration probes run in one background worker, not
# inside the list endpoint. The worker waits until MLX inference is idle, so listing
# Recordings remains cheap and probing never competes with active model inference.
_DURATION_CACHE = {}
_DURATION_CACHE_LOCK = threading.Lock()
_DURATION_CACHE_MAX = 4096
_DURATION_CACHE_RETRY_SECONDS = 30.0
_DURATION_PROBE_QUEUE = queue.Queue(maxsize=512)
_DURATION_PENDING = set()
_DURATION_PENDING_LOCK = threading.Lock()


def _duration_cache_key(path: pathlib.Path) -> str:
    try: return str(path.resolve())
    except OSError: return str(path.absolute())


def _drop_duration_cache_for_path(path: pathlib.Path):
    key = _duration_cache_key(path)
    with _DURATION_CACHE_LOCK:
        _DURATION_CACHE.pop(key, None)
    # Queued work checks existence and the file fingerprint before probing/storing.


def _probe_duration(path: pathlib.Path) -> Optional[float]:
    """Return a finite duration from ffprobe, or None without reading audio into Python."""
    if not FFPROBE:
        return None
    try:
        result = subprocess.run(
            [FFPROBE, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            capture_output=True, text=True, timeout=FFPROBE_TIMEOUT_SECONDS,
            check=False,
        )
        if result.returncode != 0:
            return None
        raw = (result.stdout or "").strip().splitlines()
        if not raw:
            return None
        seconds = float(raw[0].strip())
        return seconds if math.isfinite(seconds) and seconds >= 0 else None
    except (OSError, subprocess.SubprocessError, ValueError, OverflowError):
        return None


def _schedule_duration_probe(path: pathlib.Path, key: str, fingerprint: tuple):
    if not FFPROBE:
        return
    with _DURATION_PENDING_LOCK:
        if key in _DURATION_PENDING:
            return
        _DURATION_PENDING.add(key)
        try:
            _DURATION_PROBE_QUEUE.put_nowait((key, str(path), fingerprint))
        except queue.Full:
            _DURATION_PENDING.discard(key)


def _active_transcription_work() -> bool:
    if INFERENCE_LOCK.locked():
        return True
    with ACTIVE_JOBS_LOCK:
        if ACTIVE_JOBS:
            return True
    # Avoid probes during recent upload/decode/VAD work too; completed or stale jobs
    # do not block duration metadata forever.
    now = time.time()
    with PROGRESS_LOCK:
        for item in PROGRESS.values():
            stage = str(item.get("stage", ""))
            if stage in {"receiving", "received", "saved", "decoding", "decoded", "converted", "vad_done", "analyzing", "prepared", "waiting", "transcribing", "post_processing", "finalizing"}:
                try:
                    updated = datetime.datetime.fromisoformat(item.get("updated", "")).timestamp()
                    if now - updated < 300:
                        return True
                except Exception:
                    continue
    return False


def _duration_probe_worker():
    while True:
        try:
            key, raw_path, fingerprint = _DURATION_PROBE_QUEUE.get()
        except Exception:
            continue
        try:
            # Do not compete with server transcription; periodically re-check while busy.
            while _active_transcription_work():
                time.sleep(1.0)
            path = pathlib.Path(raw_path)
            if path.is_symlink() or not path.is_file():
                continue
            resolved_root = ALLOWED_ROOT.resolve()
            resolved_path = path.resolve(strict=True)
            if not _is_inside(resolved_path, resolved_root):
                continue
            before = resolved_path.stat()
            current_fp = (before.st_size, getattr(before, "st_mtime_ns", int(before.st_mtime * 1_000_000_000)))
            if current_fp != fingerprint:
                continue
            duration = _probe_duration(resolved_path)
            after = resolved_path.stat()
            after_fp = (after.st_size, getattr(after, "st_mtime_ns", int(after.st_mtime * 1_000_000_000)))
            if after_fp != fingerprint:
                continue
            now_mono = time.monotonic()
            entry = {
                "fingerprint": fingerprint,
                "duration": duration,
                "retry_after": float("inf") if duration is not None else now_mono + _DURATION_CACHE_RETRY_SECONDS,
                "checked_at": now_mono,
            }
            with _DURATION_CACHE_LOCK:
                _DURATION_CACHE[key] = entry
                if len(_DURATION_CACHE) > _DURATION_CACHE_MAX:
                    oldest = sorted(_DURATION_CACHE, key=lambda k: _DURATION_CACHE[k].get("checked_at", 0))[:len(_DURATION_CACHE) - _DURATION_CACHE_MAX]
                    for old_key in oldest:
                        _DURATION_CACHE.pop(old_key, None)
        except (OSError, ValueError, RuntimeError):
            # Recordings can be deleted or replaced while metadata is being fetched.
            pass
        finally:
            with _DURATION_PENDING_LOCK:
                _DURATION_PENDING.discard(key)
            _DURATION_PROBE_QUEUE.task_done()


def _cached_probe_duration(path: pathlib.Path, stat_result=None):
    """Fast read-through cache. A miss queues a background probe and returns None."""
    if not FFPROBE:
        return None
    try:
        st = stat_result if stat_result is not None else path.stat()
        fingerprint = (st.st_size, getattr(st, "st_mtime_ns", int(st.st_mtime * 1_000_000_000)))
        key = _duration_cache_key(path)
        now = time.monotonic()
        with _DURATION_CACHE_LOCK:
            entry = _DURATION_CACHE.get(key)
            if entry and entry["fingerprint"] == fingerprint:
                if entry["duration"] is not None or now < entry["retry_after"]:
                    return entry["duration"]
            elif entry:
                _DURATION_CACHE.pop(key, None)
        _schedule_duration_probe(path, key, fingerprint)
        return None
    except (OSError, ValueError):
        return None


# A single daemon worker bounds ffprobe concurrency and exits with the server process.
if FFPROBE:
    threading.Thread(target=_duration_probe_worker, name="rdt-duration-probe", daemon=True).start()

@app.get("/v1/recordings/list")
def list_recordings(authorization: Optional[str]=Header(None), host: Optional[str]=Header(None), tailscale_user_login: Optional[str]=Header(None, alias="Tailscale-User-Login")):
    check_host(host); require_bearer(authorization, tailscale_user_login)
    files=[]
    try:
        iterator=ALLOWED_ROOT.rglob("*")
        for p in iterator:
            if not p.is_file() or p.is_symlink(): continue
            try:
                rel=p.relative_to(ALLOWED_ROOT)
                st=p.stat()
            except (ValueError,OSError): continue
            files.append({"name":str(rel),"size":st.st_size,"duration":_cached_probe_duration(p, st),"modified":datetime.datetime.fromtimestamp(st.st_mtime).isoformat()})
    except OSError as e:
        raise HTTPException(status_code=500, detail="could not list recordings")
    files.sort(key=lambda x:x.get("modified","") or "", reverse=True)
    return {"files":files}

@app.delete("/v1/recordings/purge")
def purge_recordings(authorization: Optional[str]=Header(None), host: Optional[str]=Header(None), tailscale_user_login: Optional[str]=Header(None, alias="Tailscale-User-Login")):
    check_host(host); require_bearer(authorization, tailscale_user_login)
    deleted=0; errors=[]
    for root,dirs,files in os.walk(ALLOWED_ROOT,topdown=False,followlinks=False):
        for name in files:
            p=pathlib.Path(root)/name
            try:
                if p.is_symlink() or p.is_file():
                    _drop_duration_cache_for_path(p)
                    p.unlink()
                deleted+=1
            except Exception as e: errors.append(f"{p.name}: {type(e).__name__}")
        for name in dirs:
            p=pathlib.Path(root)/name
            try:
                if p.is_symlink(): p.unlink()
                else: p.rmdir()
            except OSError: pass
    with _DURATION_CACHE_LOCK:
        _DURATION_CACHE.clear()
    return {"ok":not errors,"deleted":deleted,"errors":errors}

@app.get("/v1/recordings/{recording_path:path}")
def get_recording(recording_path: str, authorization: Optional[str]=Header(None), host: Optional[str]=Header(None), tailscale_user_login: Optional[str]=Header(None, alias="Tailscale-User-Login")):
    check_host(host); require_bearer(authorization, tailscale_user_login)
    target=resolve_recording_file_safe(recording_path)
    if not target.exists() or not target.is_file(): raise HTTPException(status_code=404, detail="recording not found")
    return FileResponse(str(target))

@app.delete("/v1/recordings/{recording_path:path}")
def delete_recording(recording_path: str, authorization: Optional[str]=Header(None), host: Optional[str]=Header(None), tailscale_user_login: Optional[str]=Header(None, alias="Tailscale-User-Login")):
    check_host(host); require_bearer(authorization, tailscale_user_login)
    target=resolve_recording_file_safe(recording_path)
    if not target.exists() or not target.is_file(): raise HTTPException(status_code=404, detail="recording not found")
    try: target.unlink()
    except Exception as e: raise HTTPException(status_code=500, detail="delete failed")
    _drop_duration_cache_for_path(target)
    return {"ok":True,"deleted":str(target.relative_to(ALLOWED_ROOT.resolve()))}

# ---------------------------------------------------------------------------
# WebUI at /, progress route, safe CLI bind
# ---------------------------------------------------------------------------
def _frontend_candidates():
    return ["index.html","rdt-voice.html"]

def _find_frontend():
    here=pathlib.Path(__file__).parent
    search_dirs=[here,here/"frontend",pathlib.Path.cwd()]
    env_override=os.getenv("RDT_VOICE_HTML","").strip()
    if env_override:
        p=pathlib.Path(env_override).expanduser()
        if p.is_file(): return p
    for d in search_dirs:
        if not d.is_dir(): continue
        for n in _frontend_candidates():
            f=d/n
            if f.is_file(): return f
    return None

def _find_frontend_asset_dir() -> pathlib.Path:
    """Serve JS/worklet/manifest/locales beside the selected HTML, without duplicating assets."""
    frontend = _find_frontend()
    return frontend.parent if frontend is not None else pathlib.Path(__file__).parent

@app.get("/")
def _serve_webui_root(host: Optional[str]=Header(None)):
    check_host(host)
    f=_find_frontend()
    if f: return FileResponse(str(f))
    return {"status":"ok","model":MODEL_ID,"message":"Place RDT Voice HTML next to server.py or set RDT_VOICE_HTML"}

@app.get("/rdt-voice")
@app.get("/webui")
def _serve_webui_alias(host: Optional[str]=Header(None)):
    return _serve_webui_root(host)

def _serve_frontend_asset(filename: str, host: Optional[str]) -> FileResponse:
    check_host(host)
    candidate = _find_frontend_asset_dir() / filename
    if not candidate.is_file():
        raise HTTPException(status_code=404, detail=f"Frontend asset not found: {filename}")
    return FileResponse(str(candidate), headers={"Cache-Control": "no-cache", "X-Content-Type-Options": "nosniff"})

@app.get("/worker.js")
def _serve_worker_asset(host: Optional[str]=Header(None)):
    return _serve_frontend_asset("worker.js", host)

@app.get("/pcm-worklet.js")
def _serve_worklet_asset(host: Optional[str]=Header(None)):
    return _serve_frontend_asset("pcm-worklet.js", host)

@app.get("/models.manifest.json")
def _serve_model_manifest_asset(host: Optional[str]=Header(None)):
    return _serve_frontend_asset("models.manifest.json", host)

# These are public static translation files, never credentials or private data.
# Mount them only when the complete static frontend bundle is present.
_LOCALES_DIR = _find_frontend_asset_dir() / "locales"
if _LOCALES_DIR.is_dir():
    app.mount("/locales", StaticFiles(directory=str(_LOCALES_DIR), check_dir=True), name="locales")

@app.get("/v1/transcription/active")
def _active_route(host: Optional[str]=Header(None), authorization: Optional[str]=Header(None), tailscale_user_login: Optional[str]=Header(None, alias="Tailscale-User-Login")):
    check_host(host); require_bearer(authorization, tailscale_user_login)
    with ACTIVE_JOBS_LOCK: n=len(ACTIVE_JOBS)
    return {"active":n>0,"count":n,"busy":INFERENCE_LOCK.locked()}

@app.get("/v1/transcription/progress/{job_id}")
def _progress_route(job_id: str, host: Optional[str]=Header(None), authorization: Optional[str]=Header(None), tailscale_user_login: Optional[str]=Header(None, alias="Tailscale-User-Login")):
    check_host(host); require_bearer(authorization, tailscale_user_login)
    p=get_progress(job_id)
    if not p: return JSONResponse(status_code=404, content={"error":"unknown job_id"})
    return p
    
def _parse_args():
    import argparse as _ap
    p=_ap.ArgumentParser()
    p.add_argument("--host",default=os.getenv("WHISPER_HOST","127.0.0.1"))
    p.add_argument("--port",type=int,default=int(os.getenv("WHISPER_PORT","8888")))
    p.add_argument("--ssl-keyfile",default=os.getenv("SSL_KEYFILE",""))
    p.add_argument("--ssl-certfile",default=os.getenv("SSL_CERTFILE",""))
    return p.parse_known_args()[0]

if __name__ == "__main__":
    _args=_parse_args()
    _bind_host=(os.getenv("TAILSCALE_IP","").strip() or "0.0.0.0") if os.getenv("RDT_TAILSCALE","0")=="1" else _args.host
    # Authentication may be disabled only for loopback-only development.
    # Refuse every non-loopback bind, including LAN IPs, not just 0.0.0.0.
    _normalized_bind = str(_bind_host or "").strip().strip("[]").lower()
    _is_loopback_bind = _normalized_bind == "localhost"
    if not _is_loopback_bind:
        try:
            _is_loopback_bind = ipaddress.ip_address(_normalized_bind).is_loopback
        except ValueError:
            _is_loopback_bind = False
    if not AUTH_ENABLED and (os.getenv("RDT_TAILSCALE", "0") == "1" or not _is_loopback_bind):
        raise SystemExit("[RDT] Refusing to start with RDT_AUTH=off on a network-accessible/Tailscale bind. Keep authentication enabled.")
    if TAILSCALE_IDENTITY_AUTH and not _is_loopback_bind:
        raise SystemExit("[RDT] Refusing RDT_TAILSCALE_AUTH=on unless the backend is bound to loopback (127.0.0.1 or localhost). Use Tailscale Serve as the remote proxy.")
    if _bind_host=="0.0.0.0":
        print("[RDT] WARNING: listening on ALL network interfaces. Set TAILSCALE_IP to your Tailscale address to limit access, and keep auth ON.")
    print(f"[RDT] binding {_bind_host}:{_args.port}")
    kwargs={}
    if _args.ssl_keyfile and _args.ssl_certfile:
        kwargs["ssl_keyfile"]=_args.ssl_keyfile
        kwargs["ssl_certfile"]=_args.ssl_certfile
        print("[RDT] SSL ON")
    else:
        print("[RDT] SSL OFF (http)")
    uvicorn.run(app,host=_bind_host,port=_args.port, **kwargs)
