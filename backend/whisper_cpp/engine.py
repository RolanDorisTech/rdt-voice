"""OS-neutral bounded persistent-worker client for the RDT-owned whisper.cpp C API worker."""
from __future__ import annotations

import base64
import ctypes
import os
import pathlib
import platform
import queue
import subprocess
import sys
import threading
import uuid
from typing import Any

def _windows_arm_dotprod_available() -> bool:
    """Use the optimized ARM worker only when Windows confirms CPU support."""
    if os.name != "nt" or platform.machine().lower() not in {"arm64", "aarch64"}:
        return False
    try:
        check = ctypes.WinDLL("kernel32", use_last_error=True).IsProcessorFeaturePresent
        check.argtypes = [ctypes.c_uint32]
        check.restype = ctypes.c_int
        # PF_ARM_V82_DP_INSTRUCTIONS_AVAILABLE
        return bool(check(43))
    except (AttributeError, OSError):
        return False


MAX_IPC_LINE = 64 * 1024
READY_TIMEOUT_SECONDS = 60


def _encode(value: str | bytes) -> str:
    raw = value if isinstance(value, bytes) else value.encode("utf-8", "strict")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode(value: str) -> str:
    if len(value) > MAX_IPC_LINE:
        raise RuntimeError("worker_protocol_line_too_long")
    value += "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value.encode("ascii")).decode("utf-8", "strict")


class PersistentWhisperEngine:
    """One resident native worker, serialized requests, bounded stdout and IPC.

    The backend owns a model-use lease for this instance's whole lifetime. Actual
    mid-inference cancellation is supported by the worker's ggml abort callback;
    the API returns cancellation success only if the worker confirms the request.
    """

    def __init__(self, model_path: pathlib.Path, model_id: str) -> None:
        self.model_path = model_path.resolve(strict=True)
        self.model_id = model_id
        self.process: subprocess.Popen[bytes] | None = None
        self._lines: queue.Queue[bytes | BaseException | None] = queue.Queue(maxsize=16)
        self._request_lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._active_id: str | None = None
        self._reader: threading.Thread | None = None
        self.architecture = "unknown"
        self.engine_version = "whisper.cpp/v1.9.5"
        self.provider = "CPU"
        self.worker_variant = "unknown"
        self.provider_reason = "Worker has not completed startup."

    @staticmethod
    def worker_candidates() -> list[pathlib.Path]:
        override = os.environ.get("RDT_WHISPER_WORKER_EXE", "").strip()
        if override:
            # An explicit override is authoritative; do not silently replace it.
            return [pathlib.Path(override).expanduser().resolve()]

        suffix = ".exe" if os.name == "nt" else ""
        if getattr(sys, "frozen", False):
            root = pathlib.Path(sys.executable).resolve().parent
        else:
            root = pathlib.Path(__file__).resolve().parent / "bin"

        # Prefer usable GPU workers, then runtime-qualified ARM optimization,
        # then the portable CPU fallback.
        candidates = [
            root / f"rdt_whisper_worker_cuda{suffix}",
            root / f"rdt_whisper_worker_vulkan{suffix}",
        ]
        if _windows_arm_dotprod_available():
            candidates.append(root / "rdt_whisper_worker_dotprod.exe")
        candidates.append(root / f"rdt_whisper_worker{suffix}")
        return candidates

    @staticmethod
    def worker_path() -> pathlib.Path:
        # Compatibility for callers that still expect a single default path.
        candidates = PersistentWhisperEngine.worker_candidates()
        for candidate in candidates:
            if candidate.is_file():
                return candidate
        return candidates[-1]

    def start(self) -> None:
        if self.process is not None and self.process.poll() is None:
            return

        candidates = self.worker_candidates()
        available = [path for path in candidates if path.is_file()]
        if not available:
            raise RuntimeError("persistent_worker_not_packaged")

        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        last_error = None

        for exe in available:
            attempt_error = None
            self._lines = queue.Queue(maxsize=16)
            try:
                self.process = subprocess.Popen(
                    [str(exe), "--model", str(self.model_path)],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    bufsize=0,
                    creationflags=creationflags,
                )
                self._reader = threading.Thread(
                    target=self._read_stdout,
                    name="rdt-whisper-stdout",
                    daemon=True,
                )
                self._reader.start()
                line = self._lines.get(timeout=READY_TIMEOUT_SECONDS)

                if isinstance(line, BaseException) or line is None:
                    raise RuntimeError("persistent_worker_exited_before_ready")

                parts = line.decode("utf-8", "strict").strip().split("\t")
                if len(parts) < 3 or parts[0] != "READY":
                    raise RuntimeError("persistent_worker_handshake_invalid")

                self.architecture = parts[1][:32]
                self.engine_version = parts[2][:64]
                self.provider = parts[3][:32] if len(parts) > 3 and parts[3] else "CPU"
                device = parts[4][:256] if len(parts) > 4 and parts[4] else self.provider
                self.worker_variant = {
                    "rdt_whisper_worker_cuda": "cuda",
                    "rdt_whisper_worker_vulkan": "vulkan",
                    "rdt_whisper_worker_dotprod": "arm-dotprod",
                    "rdt_whisper_worker": "cpu",
                }.get(exe.stem, "custom")
                self.provider_reason = (
                    f"Worker variant {self.worker_variant}; "
                    f"provider {self.provider}; device {device}"
                )
                return

            except queue.Empty:
                attempt_error = RuntimeError("persistent_worker_ready_timeout")
            except (OSError, RuntimeError, UnicodeError) as exc:
                attempt_error = exc
            finally:
                if attempt_error is not None:
                    self.stop()

            if attempt_error is not None:
                last_error = attempt_error

        if last_error is not None:
            raise last_error
        raise RuntimeError("persistent_worker_start_failed")

    def _read_stdout(self) -> None:
        assert self.process is not None and self.process.stdout is not None
        try:
            while True:
                line = self.process.stdout.readline(MAX_IPC_LINE + 1)
                if not line:
                    self._enqueue(None)
                    return
                if len(line) > MAX_IPC_LINE or not line.endswith(b"\n"):
                    self._enqueue(RuntimeError("worker_protocol_line_too_long"))
                    return
                self._enqueue(line)
        except BaseException as exc:
            self._enqueue(exc)

    def _enqueue(self, value: bytes | BaseException | None) -> None:
        try:
            self._lines.put(value, timeout=1)
        except queue.Full:
            # Kill the process instead of allowing unbounded output or memory growth.
            self.stop()

    def _send(self, line: str) -> None:
        if len(line.encode("utf-8")) > MAX_IPC_LINE:
            raise RuntimeError("worker_request_too_large")
        process = self.process
        if process is None or process.poll() is not None or process.stdin is None:
            raise RuntimeError("persistent_worker_not_running")
        with self._write_lock:
            process.stdin.write(line.encode("utf-8") + b"\n")
            process.stdin.flush()

    def transcribe(self, wav_path: str | pathlib.Path, language: str | None = None) -> dict[str, Any]:
        path = pathlib.Path(wav_path).resolve(strict=True)
        if not path.is_file() or path.stat().st_size > 2 * 1024 * 1024 * 1024:
            raise RuntimeError("worker_audio_file_invalid")
        self.start()
        with self._request_lock:
            request_id = uuid.uuid4().hex[:24]
            with self._state_lock:
                self._active_id = request_id
            lang_b64 = _encode(language or "auto")
            self._send(f"TRANSCRIBE\t{request_id}\t{lang_b64}\t{_encode(os.fsencode(path))}")
            segments: list[dict[str, Any]] = []
            detected_language: str | None = None
            try:
                while True:
                    try:
                        raw = self._lines.get(timeout=60 * 30)
                    except queue.Empty as exc:
                        self.stop()
                        raise RuntimeError("worker_transcription_timeout") from exc
                    if isinstance(raw, BaseException) or raw is None:
                        self.stop()
                        raise RuntimeError("persistent_worker_crashed") from (raw if isinstance(raw, BaseException) else None)
                    if len(raw) > MAX_IPC_LINE:
                        self.stop()
                        raise RuntimeError("worker_protocol_line_too_long")
                    parts = raw.decode("utf-8", "strict").rstrip("\r\n").split("\t")
                    if len(parts) >= 5 and parts[0] == "SEGMENT" and parts[1] == request_id:
                        start_cs = int(parts[2])
                        end_cs = int(parts[3])
                        text = _decode(parts[4])
                        if end_cs < start_cs or abs(start_cs) > 10_000_000 or abs(end_cs) > 10_000_000:
                            self.stop()
                            raise RuntimeError("worker_timestamp_invalid")
                        segments.append({"text": text, "start": start_cs / 100.0, "end": end_cs / 100.0})
                    elif len(parts) == 3 and parts[0] == "DONE" and parts[1] == request_id:
                        detected_language = _decode(parts[2]) if parts[2] != "-" else None
                        break
                    elif len(parts) >= 3 and parts[0] == "ERROR" and parts[1] == request_id:
                        code = re_safe_code(parts[2])
                        raise RuntimeError(f"worker_{code}")
                    else:
                        # Ignore a status for another request only if it remains bounded;
                        # unexpected protocol records are treated as a hard worker fault.
                        self.stop()
                        raise RuntimeError("worker_protocol_response_mismatch")
            finally:
                with self._state_lock:
                    self._active_id = None
        return {
            "text": "".join(segment["text"] for segment in segments),
            "segments": segments,
            "language": detected_language,
        }

    def cancel(self, request_id: str | None = None) -> bool:
        with self._state_lock:
            active = self._active_id
        if not active or (request_id and active != request_id):
            return False
        try:
            self._send(f"CANCEL\t{active}")
            return True
        except Exception:
            return False

    def status(self) -> dict[str, Any]:
        alive = self.process is not None and self.process.poll() is None
        return {
            "ready": bool(alive), "running": bool(alive), "engine": self.engine_version,
            "architecture": self.architecture, "provider": self.provider,
            "worker_variant": self.worker_variant,
            "provider_reason": self.provider_reason,
        }

    def stop(self) -> None:
        process = self.process
        self.process = None
        if process is None:
            return
        try:
            if process.poll() is None and process.stdin:
                with self._write_lock:
                    process.stdin.write(b"SHUTDOWN\n")
                    process.stdin.flush()
        except Exception:
            pass
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
        for pipe in (process.stdin, process.stdout, process.stderr):
            if pipe:
                try:
                    pipe.close()
                except Exception:
                    pass


def re_safe_code(value: str) -> str:
    import re
    value = value.lower()
    return value if re.fullmatch(r"[a-z0-9_-]{1,64}", value) else "unknown_error"
