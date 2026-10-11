# RDT Voice local transcription server — v0.2-DEV.6
# Adaptive silence filtering, local transcription, and request safeguards
import os, re, math, bisect, tempfile, subprocess, wave, pathlib, datetime, uuid, platform, asyncio, time, threading, secrets, hmac, shutil, ipaddress, queue
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
from fastapi import FastAPI, UploadFile, File, Form, Query, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response, JSONResponse, FileResponse
import uvicorn
import mlx_whisper

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

TOKEN = os.getenv("RDT_TOKEN") or secrets.token_urlsafe(32)
if os.getenv("RDT_TOKEN") and len(TOKEN) < 16:
    print("[RDT] WARNING: RDT_TOKEN is short. Use at least 16 random characters, or unset it to get a strong random token each run.")

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
if os.getenv("RDT_AUTH", "on").lower() not in ("off", "0", "false", "no"):
    print(f"[RDT] AUTH ENABLED — paste this token into RDT Voice (keep it private): {TOKEN}")
else:
    print("[RDT] WARNING: AUTHENTICATION IS DISABLED (RDT_AUTH=off).")
    print("[RDT] Use this only for genuinely local-only testing. A loopback listener can still be exposed by Tailscale Serve, a tunnel, or a reverse proxy.")
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

AUTH_ENABLED = os.getenv("RDT_AUTH", "on").lower() not in ("off", "0", "false", "no")

def require_bearer(authorization: Optional[str]):
    if not AUTH_ENABLED:
        return
    expected = f"Bearer {TOKEN}".encode("utf-8")
    if not authorization or not hmac.compare_digest(authorization.encode("utf-8", "ignore"), expected):
        raise HTTPException(
            status_code=401,
            detail="Copy token from server terminal",
            headers={"WWW-Authenticate":"Bearer"},
        )

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
def models(host: Optional[str]=Header(None), authorization: Optional[str]=Header(None)):
    check_host(host); require_bearer(authorization)
    return {"data":[{"id":MODEL_ID,"object":"model","owned_by":"RDT Voice / MLX"}]}

@app.post("/v1/open_recordings")
def open_recordings(authorization: Optional[str]=Header(None), host: Optional[str]=Header(None), payload: dict=None):
    check_host(host); require_bearer(authorization)
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
def open_recording(authorization: Optional[str]=Header(None), host: Optional[str]=Header(None), payload: dict=None):
    check_host(host); require_bearer(authorization)
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
def trans(file: UploadFile=File(...), model: str=Form(default="whisper-1"), audio_cache_dir: str=Form(default=None), language: str=Form(default="auto"), job_id: Optional[str]=Form(default=None), authorization: Optional[str]=Header(None), host: Optional[str]=Header(None), origin: Optional[str]=Header(None)):
    check_host(host); require_bearer(authorization)
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
def list_recordings(authorization: Optional[str]=Header(None), host: Optional[str]=Header(None)):
    check_host(host); require_bearer(authorization)
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
def purge_recordings(authorization: Optional[str]=Header(None), host: Optional[str]=Header(None)):
    check_host(host); require_bearer(authorization)
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
def get_recording(recording_path: str, authorization: Optional[str]=Header(None), host: Optional[str]=Header(None)):
    check_host(host); require_bearer(authorization)
    target=resolve_recording_file_safe(recording_path)
    if not target.exists() or not target.is_file(): raise HTTPException(status_code=404, detail="recording not found")
    return FileResponse(str(target))

@app.delete("/v1/recordings/{recording_path:path}")
def delete_recording(recording_path: str, authorization: Optional[str]=Header(None), host: Optional[str]=Header(None)):
    check_host(host); require_bearer(authorization)
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
    here=pathlib.Path(__file__).parent; home=pathlib.Path.home()
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

@app.get("/v1/transcription/active")
def _active_route(host: Optional[str]=Header(None), authorization: Optional[str]=Header(None)):
    check_host(host); require_bearer(authorization)
    with ACTIVE_JOBS_LOCK: n=len(ACTIVE_JOBS)
    return {"active":n>0,"count":n,"busy":INFERENCE_LOCK.locked()}

@app.get("/v1/transcription/progress/{job_id}")
def _progress_route(job_id: str, host: Optional[str]=Header(None), authorization: Optional[str]=Header(None)):
    check_host(host); require_bearer(authorization)
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
