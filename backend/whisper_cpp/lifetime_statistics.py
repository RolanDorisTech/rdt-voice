"""Cross-platform aggregate-only RDT Connect lifetime statistics writer."""
from __future__ import annotations

import contextlib
import datetime
import json
import math
import os
import stat
import tempfile
import threading

_SCHEMA = "rdt-connect-lifetime-statistics-v1"
_MAX_BYTES = 1_048_576
_THREAD_LOCKS = {}
_THREAD_LOCKS_GUARD = threading.Lock()


def _thread_lock_for(path):
    with _THREAD_LOCKS_GUARD:
        return _THREAD_LOCKS.setdefault(path, threading.RLock())


def _verify_regular_file(path, fd):
    path_info = os.lstat(path)
    fd_info = os.fstat(fd)
    if stat.S_ISLNK(path_info.st_mode) or not stat.S_ISREG(path_info.st_mode):
        raise RuntimeError("Lifetime statistics path is not a regular file.")
    if not stat.S_ISREG(fd_info.st_mode):
        raise RuntimeError("Lifetime statistics handle is not a regular file.")
    if os.name != "nt" and (
        path_info.st_dev != fd_info.st_dev or path_info.st_ino != fd_info.st_ino
    ):
        raise RuntimeError("Lifetime statistics file changed while opening.")


@contextlib.contextmanager
def _exclusive_lock(lock_path):
    thread_lock = _thread_lock_for(lock_path)
    thread_lock.acquire()
    fd = None
    locked = False
    try:
        flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
        if os.name == "nt":
            flags |= getattr(os, "O_BINARY", 0)
            if os.path.islink(lock_path):
                raise RuntimeError("Lifetime statistics lock is a symbolic link.")
        fd = os.open(lock_path, flags, 0o600)
        _verify_regular_file(lock_path, fd)

        if os.name == "nt":
            import msvcrt
            if os.fstat(fd).st_size == 0:
                os.lseek(fd, 0, os.SEEK_SET)
                os.write(fd, b"\0")
                os.fsync(fd)
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            os.fchmod(fd, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX)
        locked = True
        yield
    finally:
        try:
            if fd is not None:
                try:
                    if locked:
                        if os.name == "nt":
                            import msvcrt
                            os.lseek(fd, 0, os.SEEK_SET)
                            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                        else:
                            import fcntl
                            fcntl.flock(fd, fcntl.LOCK_UN)
                finally:
                    os.close(fd)
        finally:
            thread_lock.release()


def _empty_data():
    return {
        "schema": _SCHEMA,
        "firstTranscriptionAt": None,
        "lastTranscriptionAt": None,
        "totalTranscriptions": 0,
        "totalAudioSeconds": 0.0,
        "totalUploadBytes": 0,
        "totalInferenceMilliseconds": 0.0,
        "hardwareUsage": {},
        "modelUsage": {},
    }


def _read_data(path):
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return _empty_data()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise RuntimeError("Lifetime statistics file is not a regular file.")
    if info.st_size > _MAX_BYTES:
        raise RuntimeError("Lifetime statistics file exceeds the size limit.")

    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    if os.name == "nt":
        flags |= getattr(os, "O_BINARY", 0)
        if os.path.islink(path):
            raise RuntimeError("Lifetime statistics file is a symbolic link.")
    fd = os.open(path, flags)
    try:
        _verify_regular_file(path, fd)
        with os.fdopen(fd, "rb") as stream:
            fd = -1
            raw = stream.read(_MAX_BYTES + 1)
    finally:
        if fd >= 0:
            os.close(fd)

    if len(raw) > _MAX_BYTES:
        raise RuntimeError("Lifetime statistics file exceeds the size limit.")
    data = json.loads(raw.decode("utf-8"))
    required = (
        "totalTranscriptions", "totalAudioSeconds", "totalUploadBytes",
        "totalInferenceMilliseconds", "hardwareUsage", "modelUsage",
    )
    if not isinstance(data, dict) or data.get("schema") != _SCHEMA:
        raise RuntimeError("Lifetime statistics schema is invalid.")
    if any(key not in data for key in required):
        raise RuntimeError("Lifetime statistics fields are incomplete.")
    if not isinstance(data["hardwareUsage"], dict) or not isinstance(data["modelUsage"], dict):
        raise RuntimeError("Lifetime statistics usage maps are invalid.")
    return data


def _count(value, field):
    if isinstance(value, bool):
        raise RuntimeError(f"Lifetime statistics {field} is invalid.")
    try:
        result = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise RuntimeError(f"Lifetime statistics {field} is invalid.") from exc
    if result < 0:
        raise RuntimeError(f"Lifetime statistics {field} is invalid.")
    return result


def _amount(value, field):
    try:
        result = float(value or 0.0)
    except (TypeError, ValueError, OverflowError) as exc:
        raise RuntimeError(f"Lifetime statistics {field} is invalid.") from exc
    if not math.isfinite(result) or result < 0:
        raise RuntimeError(f"Lifetime statistics {field} is invalid.")
    return result


def _sync_parent(parent):
    if os.name == "nt":
        return
    fd = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def record_lifetime_transcription(upload_bytes, audio_seconds, inference_seconds, provider, model_id):
    """Atomically record aggregate counts and durations; never audio or transcript text."""
    path_text = os.environ.get("RDT_CONNECT_STATS_PATH", "").strip()
    if not path_text or not os.path.isabs(path_text):
        raise RuntimeError("Lifetime statistics path is missing or not absolute.")

    path = os.path.abspath(path_text)
    parent = os.path.dirname(path)
    os.makedirs(parent, mode=0o700, exist_ok=True)

    duration = _amount(audio_seconds, "audio duration")
    inference_ms = _amount(inference_seconds or 0.0, "inference duration") * 1000.0
    upload = _count(upload_bytes, "upload bytes")
    provider = provider if isinstance(provider, dict) else {}
    hardware_key = "{} ({})".format(
        str(provider.get("provider", "unknown"))[:80],
        str(provider.get("device", "unknown"))[:80],
    )
    model_key = str(model_id or "unknown")[:200]
    now = datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")

    with _exclusive_lock(path + ".lock"):
        data = _read_data(path)
        data["firstTranscriptionAt"] = data.get("firstTranscriptionAt") or now
        data["lastTranscriptionAt"] = now
        data["totalTranscriptions"] = _count(data["totalTranscriptions"], "transcription count") + 1
        data["totalAudioSeconds"] = _amount(data["totalAudioSeconds"], "total audio") + duration
        data["totalUploadBytes"] = _count(data["totalUploadBytes"], "total upload") + upload
        data["totalInferenceMilliseconds"] = (
            _amount(data["totalInferenceMilliseconds"], "total inference") + inference_ms
        )

        for usage_map, key in (
            (data["hardwareUsage"], hardware_key),
            (data["modelUsage"], model_key),
        ):
            entry = usage_map.setdefault(key, {
                "transcriptions": 0, "audioSeconds": 0.0,
                "uploadBytes": 0, "inferenceMilliseconds": 0.0,
            })
            if not isinstance(entry, dict):
                raise RuntimeError("Lifetime statistics usage entry is invalid.")
            entry["transcriptions"] = _count(entry.get("transcriptions", 0), "usage count") + 1
            entry["audioSeconds"] = _amount(entry.get("audioSeconds", 0), "usage audio") + duration
            entry["uploadBytes"] = _count(entry.get("uploadBytes", 0), "usage upload") + upload
            entry["inferenceMilliseconds"] = (
                _amount(entry.get("inferenceMilliseconds", 0), "usage inference") + inference_ms
            )

        encoded = (json.dumps(data, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        if len(encoded) > _MAX_BYTES:
            raise RuntimeError("Lifetime statistics would exceed the size limit.")

        fd, temp_path = tempfile.mkstemp(prefix=".lifetime-statistics-", dir=parent)
        try:
            if os.name != "nt":
                os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb") as output:
                fd = -1
                output.write(encoded)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temp_path, path)
            _sync_parent(parent)
        finally:
            if fd >= 0:
                os.close(fd)
            if os.path.exists(temp_path):
                os.unlink(temp_path)
