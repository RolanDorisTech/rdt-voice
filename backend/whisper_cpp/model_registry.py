"""Cross-platform GGML model registry and fail-closed downloader for RDT Connect.

The artifact hashes are approved candidate pins from CLAUDE_HANDOFF. A download is
promoted only when the immutable-revision Hugging Face metadata reports the same
SHA-256 and an exact positive byte count, and the entire staged file matches both.
No model weights are bundled with the source tree.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import pathlib
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Iterator

SOURCE_REPO = "ggerganov/whisper.cpp"
REVISION = "fb5ed9369c26eaf08392504d8446da431e0fffb9"
MAX_MODEL_BYTES = 4_000_000_000
CHUNK_BYTES = 1024 * 1024

# SHA-256 values below are the user's approved candidate pins. They are not
# described as verified until exact-revision metadata and the complete bytes match.
MODELS: dict[str, dict[str, Any]] = {
    "tiny": {
        "name": "Tiny", "file": "ggml-tiny-q8_0.bin", "quantization": "Q8_0",
        "sha256": "c2085835d3f50733e2ff6e4b41ae8a2b8d8110461e18821b09a15c40c42d1cca",
        "approx_bytes": 43_500_000,
    },
    "base": {
        "name": "Base", "file": "ggml-base-q8_0.bin", "quantization": "Q8_0",
        "sha256": "c577b9a86e7e048a0b7eada054f4dd79a56bbfa911fbdacf900ac5b567cbb7d9",
        "approx_bytes": 81_800_000,
    },
    "small": {
        "name": "Small", "file": "ggml-small-q8_0.bin", "quantization": "Q8_0",
        "sha256": "49c8fb02b65e6049d5fa6c04f81f53b867b5ec9540406812c643f177317f779f",
        "approx_bytes": 264_000_000,
    },
    "large-v3-turbo": {
        "name": "Large v3 Turbo", "file": "ggml-large-v3-turbo-q5_0.bin", "quantization": "Q5_0",
        "sha256": "394221709cd5ad1f40c46e6031ca61bce88931e6e088c188294c6d5a55ffa7e2",
        "approx_bytes": 574_041_195,
    },
    "large-v3": {
        "name": "Large v3", "file": "ggml-large-v3-q5_0.bin", "quantization": "Q5_0",
        "sha256": "d75795ecff3f83b5faa89d1900604ad8c780abd5739fae406de19f23ecd98ad1",
        "approx_bytes": 1_080_000_000,
    },
}


def _candidate_path() -> pathlib.Path:
    # One portable override lets Windows and Linux reuse the same verified GGML files.
    explicit = (os.environ.get("RDT_GGML_MODEL_DIR", "").strip()
                or os.environ.get("RDT_WINDOWS_MODEL_DIR", "").strip())
    if explicit:
        return pathlib.Path(explicit).expanduser()
    if os.name == "nt":
        local = os.environ.get("LOCALAPPDATA", "").strip()
        if local:
            return pathlib.Path(local) / "RDT Connect" / "models"
        return pathlib.Path.home() / "AppData" / "Local" / "RDT Connect" / "models"
    data_home = os.environ.get("XDG_DATA_HOME", "").strip()
    base = pathlib.Path(data_home).expanduser() if data_home else pathlib.Path.home() / ".local" / "share"
    return base / "RDT Connect" / "models"


def model_root() -> pathlib.Path:
    root = _candidate_path()
    root.mkdir(parents=True, exist_ok=True)
    if root.is_symlink() or not root.is_dir():
        raise RuntimeError("The GGML model cache must be a normal directory, not a symbolic link.")
    return root.resolve(strict=True)


def model_spec(model_id: str) -> dict[str, Any]:
    if not isinstance(model_id, str) or model_id not in MODELS:
        raise ValueError("unsupported_ggml_model_id")
    return MODELS[model_id]


def model_path(model_id: str, *, require_verified: bool = True) -> pathlib.Path:
    spec = model_spec(model_id)
    path = model_root() / spec["file"]
    if not require_verified:
        return path
    verify_cached_model(model_id, path)
    return path


def _safe_sidecar(path: pathlib.Path) -> pathlib.Path:
    return path.with_name(path.name + ".rdt-verified.json")


def _sha256(path: pathlib.Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(CHUNK_BYTES), b""):
            h.update(chunk)
    return h.hexdigest()


def verify_cached_model(model_id: str, path: pathlib.Path | None = None) -> dict[str, Any]:
    spec = model_spec(model_id)
    path = path or (model_root() / spec["file"])
    root = model_root()
    # Resolve and reject escapes, symbolic links, and reparse points before hashing.
    try:
        resolved = path.resolve(strict=True)
        resolved.relative_to(root.resolve(strict=True))
        if path.is_symlink() or not resolved.is_file():
            raise ValueError
        if os.name == "nt":
            attrs = getattr(resolved.stat(), "st_file_attributes", 0)
            if attrs & getattr(__import__("stat"), "FILE_ATTRIBUTE_REPARSE_POINT", 0x400):
                raise ValueError
        sidecar = _safe_sidecar(resolved)
        if sidecar.is_symlink() or not sidecar.is_file():
            raise ValueError
        metadata = json.loads(sidecar.read_text(encoding="utf-8"))
        size = resolved.stat().st_size
        if (not isinstance(metadata, dict)
                or metadata.get("schema_version") != 1
                or metadata.get("model_id") != model_id
                or metadata.get("repo") != SOURCE_REPO
                or metadata.get("revision") != REVISION
                or metadata.get("filename") != spec["file"]
                or metadata.get("sha256") != spec["sha256"]
                or metadata.get("size_bytes") != size
                or not isinstance(size, int) or size <= 0 or size > MAX_MODEL_BYTES):
            raise ValueError
        digest = _sha256(resolved)
        if digest != spec["sha256"]:
            raise ValueError
        return metadata
    except (OSError, ValueError, TypeError, json.JSONDecodeError, RuntimeError) as exc:
        raise RuntimeError(f"Model {model_id!r} is not a fully verified GGML artifact; use Download Model again.") from exc


def _request_json(url: str, timeout: int = 20) -> Any:
    request = urllib.request.Request(url, headers={"User-Agent": "RDT-Connect-GGML/0D53", "Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        if response.status != 200:
            raise RuntimeError("model_metadata_http_error")
        body = response.read(2 * 1024 * 1024 + 1)
    if len(body) > 2 * 1024 * 1024:
        raise RuntimeError("model_metadata_too_large")
    return json.loads(body.decode("utf-8"))


def _remote_metadata(spec: dict[str, Any]) -> tuple[int, str]:
    # Metadata is always queried at the pinned commit. Mutable branches are never used.
    query = urllib.parse.urlencode({"recursive": "true", "expand": "true"})
    url = f"https://huggingface.co/api/models/{SOURCE_REPO}/tree/{REVISION}?{query}"
    listing = _request_json(url)
    if not isinstance(listing, list):
        raise RuntimeError("invalid_model_metadata")
    found = next((item for item in listing if isinstance(item, dict) and item.get("path") == spec["file"]), None)
    if not found:
        raise RuntimeError("pinned_model_artifact_missing")
    try:
        size = int(found.get("size", 0))
        lfs = found.get("lfs") or {}
        sha = str(lfs.get("oid", "")).removeprefix("sha256:").lower()
        if not re.fullmatch(r"[0-9a-f]{64}", sha):
            # Some API representations expose sha256 explicitly rather than oid.
            sha = str(lfs.get("sha256", "")).lower()
        if not re.fullmatch(r"[0-9a-f]{64}", sha):
            raise ValueError
    except (TypeError, ValueError, AttributeError) as exc:
        raise RuntimeError("model_metadata_missing_size_or_sha256") from exc
    if size <= 0 or size > MAX_MODEL_BYTES:
        raise RuntimeError("model_metadata_size_out_of_range")
    if sha != spec["sha256"]:
        raise RuntimeError("pinned_metadata_sha256_does_not_match_approved_candidate")
    return size, sha


def _emit(line: str) -> None:
    # The Rust progress parser consumes these exact prefixes and colon-delimited fields.
    print(line[:240], flush=True)


def download_model(model_id: str, *, retries: int = 2) -> pathlib.Path:
    spec = model_spec(model_id)
    root = model_root()
    destination = root / spec["file"]
    sidecar = _safe_sidecar(destination)
    part = destination.with_name(destination.name + ".part")
    _emit("RDT_MODEL_DOWNLOAD_STAGE:checking")
    if destination.exists() and sidecar.exists():
        try:
            verify_cached_model(model_id, destination)
            _emit("RDT_MODEL_DOWNLOAD_META:0:0:0")
            _emit("RDT_MODEL_DOWNLOAD_STAGE:complete")
            return destination
        except RuntimeError:
            pass
    # Never replace or recreate an artifact while an inference service holds its
    # cross-process lease, including the case where the file/sidecar was removed.
    if is_model_in_use(model_id):
        _emit("RDT_MODEL_DOWNLOAD_ERROR:model_in_use")
        raise RuntimeError("model_in_use")
    sidecar_part = sidecar.with_name(sidecar.name + ".part")
    for staged in (part, sidecar_part):
        if staged.is_symlink():
            _emit("RDT_MODEL_DOWNLOAD_ERROR:unsafe_staging_path")
            raise RuntimeError("unsafe_staging_path")
        with contextlib.suppress(FileNotFoundError):
            staged.unlink()
    try:
        expected_size, expected_sha = _remote_metadata(spec)
    except Exception as exc:
        _emit(f"RDT_MODEL_DOWNLOAD_ERROR:{_safe_error_code(exc)}")
        raise
    _emit(f"RDT_MODEL_DOWNLOAD_META:{expected_size}:1:0")
    url = f"https://huggingface.co/{SOURCE_REPO}/resolve/{REVISION}/{spec['file']}"
    last_error: Exception | None = None
    for attempt in range(max(1, retries + 1)):
        transferred = 0
        digest = hashlib.sha256()
        try:
            if part.exists():
                part.unlink()
            request = urllib.request.Request(url, headers={"User-Agent": "RDT-Connect-GGML/0D53"})
            with urllib.request.urlopen(request, timeout=60) as response, part.open("xb") as out:
                if response.status != 200:
                    raise RuntimeError("model_download_http_error")
                declared = response.headers.get("Content-Length")
                if declared and declared.isdigit() and int(declared) != expected_size:
                    raise RuntimeError("model_content_length_mismatch")
                last_report = time.monotonic()
                while True:
                    chunk = response.read(CHUNK_BYTES)
                    if not chunk:
                        break
                    transferred += len(chunk)
                    if transferred > expected_size or transferred > MAX_MODEL_BYTES:
                        raise RuntimeError("model_download_exceeded_expected_size")
                    digest.update(chunk)
                    out.write(chunk)
                    now = time.monotonic()
                    if now - last_report >= 0.2:
                        pct = min(98, int(transferred * 100 / expected_size))
                        _emit(f"RDT_MODEL_DOWNLOAD_PROGRESS:{transferred}:{expected_size}:{pct}")
                        last_report = now
                out.flush()
                os.fsync(out.fileno())
            _emit("RDT_MODEL_DOWNLOAD_STAGE:verifying")
            if transferred != expected_size:
                raise RuntimeError("model_size_mismatch")
            if digest.hexdigest() != expected_sha:
                raise RuntimeError("model_sha256_mismatch")
            metadata = {
                "schema_version": 1, "model_id": model_id, "repo": SOURCE_REPO,
                "revision": REVISION, "url": url, "filename": spec["file"],
                "quantization": spec["quantization"], "size_bytes": expected_size,
                "sha256": expected_sha,
            }
            # Promote data atomically. The sidecar is committed last, so interrupted
            # writes can never appear as a verified cache entry.
            os.replace(part, destination)
            with sidecar_part.open("x", encoding="utf-8", newline="\n") as stream:
                json.dump(metadata, stream, sort_keys=True, separators=(",", ":"))
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(sidecar_part, sidecar)
            verify_cached_model(model_id, destination)
            _emit(f"RDT_MODEL_DOWNLOAD_PROGRESS:{expected_size}:{expected_size}:98")
            _emit("RDT_MODEL_DOWNLOAD_STAGE:complete")
            return destination
        except Exception as exc:
            last_error = exc
            with contextlib.suppress(OSError):
                part.unlink()
            with contextlib.suppress(OSError):
                sidecar_part.unlink()
            # One automatic retry for transient transport faults; integrity failure is terminal.
            if _safe_error_code(exc) in {"model_size_mismatch", "model_sha256_mismatch", "model_content_length_mismatch", "model_download_exceeded_expected_size", "pinned_metadata_sha256_does_not_match_approved_candidate"}:
                break
            if attempt < retries:
                time.sleep(min(1.5 * (attempt + 1), 4.0))
    code = _safe_error_code(last_error or RuntimeError("model_download_failed"))
    _emit(f"RDT_MODEL_DOWNLOAD_ERROR:{code}")
    raise RuntimeError(code) from last_error


def _safe_error_code(exc: BaseException) -> str:
    # Never print arbitrary exception text (which may contain file paths, headers or tokens).
    message = str(exc).split(":", 1)[0].strip().lower().replace(" ", "_")
    if message and re.fullmatch(r"[a-z0-9_-]{1,64}", message):
        return message
    return type(exc).__name__.lower()[:48]


@contextlib.contextmanager
def model_usage_lock(model_id: str, *, blocking: bool = False) -> Iterator[None]:
    """Process-wide per-model lease; downloading/deletion cannot race active inference."""
    model_spec(model_id)
    root = model_root()
    lock_dir = root / ".locks"
    lock_dir.mkdir(exist_ok=True)
    lock_file = lock_dir / f"{model_id}.lock"
    stream = lock_file.open("a+b")
    acquired = False
    try:
        if os.name == "nt":
            import msvcrt
            stream.seek(0, os.SEEK_END)
            if stream.tell() == 0:
                stream.write(b"\0")
                stream.flush()
            stream.seek(0)
            mode = msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK
            try:
                msvcrt.locking(stream.fileno(), mode, 1)
                acquired = True
            except OSError as exc:
                raise RuntimeError("model_in_use") from exc
        else:
            import fcntl
            flags = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
            try:
                fcntl.flock(stream.fileno(), flags)
                acquired = True
            except BlockingIOError as exc:
                raise RuntimeError("model_in_use") from exc
        yield
    finally:
        if acquired:
            try:
                if os.name == "nt":
                    import msvcrt
                    stream.seek(0)
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
        stream.close()


def is_model_in_use(model_id: str) -> bool:
    try:
        with model_usage_lock(model_id, blocking=False):
            return False
    except RuntimeError:
        return True


def emit_download_error(error: BaseException) -> None:
    _emit(f"RDT_MODEL_DOWNLOAD_ERROR:{_safe_error_code(error)}")
