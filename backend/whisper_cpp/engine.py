"""OS-neutral bounded persistent-worker client for the RDT-owned whisper.cpp C API worker."""
from __future__ import annotations

import base64
import ctypes
import math
import os
import pathlib
import platform
import queue
import re
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


def _linux_arm_dotprod_available() -> bool:
    """Return true only when Linux reports FEAT_DotProd for this ARM CPU."""
    if not sys.platform.startswith("linux") or platform.machine().lower() not in {"arm64", "aarch64"}:
        return False
    try:
        with open("/proc/cpuinfo", "r", encoding="ascii", errors="ignore") as cpuinfo:
            for line in cpuinfo:
                key, sep, value = line.partition(":")
                if sep and key.strip().lower() in {"features", "flags"}:
                    flags = set(value.strip().lower().split())
                    if "asimddp" in flags or "dotprod" in flags:
                        return True
    except OSError:
        return False
    return False


MAX_IPC_LINE = 64 * 1024
READY_TIMEOUT_SECONDS = 60
try:
    TRANSCRIPTION_TIMEOUT_SECONDS = max(1.0, min(7200.0, float(os.environ.get("RDT_WORKER_TRANSCRIPTION_TIMEOUT", "1800"))))
except (TypeError, ValueError):
    TRANSCRIPTION_TIMEOUT_SECONDS = 1800.0


def _encode(value: str | bytes) -> str:
    raw = value if isinstance(value, bytes) else value.encode("utf-8", "strict")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode(value: str) -> str:
    if not isinstance(value, str) or len(value) > MAX_IPC_LINE:
        raise RuntimeError("worker_protocol_line_too_long")
    value += "=" * (-len(value) % 4)
    try:
        raw = base64.b64decode(value.encode("ascii"), altchars=b"-_", validate=True)
        return raw.decode("utf-8", "strict")
    except (ValueError, UnicodeError) as exc:
        raise RuntimeError("worker_protocol_base64_invalid") from exc


class _WorkerTranscriptionError(RuntimeError):
    """A well-formed ERROR record sent by the worker for this request."""


def _linux_x64_avx2_available() -> bool:
    """Conservatively require AVX2 and FMA on every CPU flags record."""
    if not sys.platform.startswith("linux") or platform.machine().lower() not in {"x86_64", "amd64"}:
        return False
    records = []
    try:
        with open("/proc/cpuinfo", "r", encoding="ascii", errors="ignore") as cpuinfo:
            for line in cpuinfo:
                key, sep, value = line.partition(":")
                if sep and key.strip().lower() == "flags":
                    records.append(set(value.lower().split()))
    except OSError:
        return False
    return bool(records) and all({"avx2", "fma"}.issubset(flags) for flags in records)


class PersistentWhisperEngine:
    """One resident native worker, serialized requests, bounded stdout and IPC.

    The backend owns a model-use lease for this instance's whole lifetime. Actual
    mid-inference cancellation is supported by the worker's ggml abort callback.
    ``cancel`` reports whether the command was sent; the request caller observes the
    worker's terminal ERROR record if cancellation actually interrupts inference.
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
        self._cancel_requested_ids: set[str] = set()
        self._reader: threading.Thread | None = None
        self.architecture = "unknown"
        self.engine_version = "unknown"
        self.provider = "CPU"
        self.worker_variant = "unknown"
        self.provider_reason = "Worker has not completed startup."
        self._bad_worker_paths: set[str] = set()
        self._active_worker_path: str | None = None

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
        elif _linux_arm_dotprod_available():
            candidates.append(root / "rdt_whisper_worker_dotprod")
        if sys.platform.startswith("linux") and _linux_x64_avx2_available():
            candidates.append(root / "rdt_whisper_worker_avx2")
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
        available = [
            path for path in candidates
            if path.is_file() and str(path.resolve()) not in self._bad_worker_paths
        ]
        if not available:
            if any(path.is_file() for path in candidates):
                raise RuntimeError("persistent_worker_no_healthy_candidate")
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
                # Bind each reader to its own process/queue. A reader thread from a
                # failed worker must never enqueue stale records into a later worker's
                # queue after a retry.
                worker_process = self.process
                worker_lines = self._lines
                self._reader = threading.Thread(
                    target=self._read_stdout,
                    args=(worker_process, worker_lines),
                    name="rdt-whisper-stdout",
                    daemon=True,
                )
                self._reader.start()
                line = self._lines.get(timeout=READY_TIMEOUT_SECONDS)

                if isinstance(line, BaseException) or line is None:
                    raise RuntimeError("persistent_worker_exited_before_ready")

                parts = line.decode("utf-8", "strict").rstrip("\r\n").split("\t")
                if (len(parts) < 3 or len(parts) > 5 or parts[0] != "READY"
                        or not parts[1] or not parts[2]):
                    raise RuntimeError("persistent_worker_handshake_invalid")

                self.architecture = parts[1][:32]
                self.engine_version = parts[2][:128]
                self._active_worker_path = str(exe.resolve())
                self.provider = parts[3][:32] if len(parts) > 3 and parts[3] else "CPU"
                device = parts[4][:256] if len(parts) > 4 and parts[4] else self.provider
                self.worker_variant = {
                    "rdt_whisper_worker_cuda": "cuda",
                    "rdt_whisper_worker_vulkan": "vulkan",
                    "rdt_whisper_worker_dotprod": "arm-dotprod",
                    "rdt_whisper_worker_avx2": "x64-avx2",
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

    def _read_stdout(
        self,
        process: subprocess.Popen[bytes] | None = None,
        lines: queue.Queue[bytes | BaseException | None] | None = None,
    ) -> None:
        process = process or self.process
        lines = lines or self._lines
        if process is None or process.stdout is None:
            return
        try:
            while True:
                line = process.stdout.readline(MAX_IPC_LINE + 1)
                if not line:
                    self._enqueue(None, lines=lines, process=process)
                    return
                if len(line) > MAX_IPC_LINE or not line.endswith(b"\n"):
                    self._enqueue(RuntimeError("worker_protocol_line_too_long"), lines=lines, process=process)
                    return
                self._enqueue(line, lines=lines, process=process)
        except BaseException as exc:
            self._enqueue(exc, lines=lines, process=process)

    def _enqueue(
        self,
        value: bytes | BaseException | None,
        *,
        lines: queue.Queue[bytes | BaseException | None] | None = None,
        process: subprocess.Popen[bytes] | None = None,
    ) -> None:
        target = lines or self._lines
        try:
            target.put(value, timeout=1)
        except queue.Full:
            # Kill the worker only if this is still the current process. A stale
            # reader can never terminate a newly spawned replacement.
            if process is None or self.process is process:
                self.stop()

    def _send(
        self, line: str, *, activate_request_id: str | None = None
    ) -> None:
        # Activation and initial command transmission are ordered against CANCEL.
        with self._write_lock:
            process = self.process
            if process is None or process.stdin is None or process.poll() is not None:
                raise RuntimeError("persistent_worker_not_running")
            if activate_request_id is not None:
                with self._state_lock:
                    self._active_id = activate_request_id
            process.stdin.write(line.encode("utf-8") + b"\n")
            process.stdin.flush()

    def _demote_failed_non_cpu_worker(self, reason: BaseException | str) -> None:
        """Remember a failed non-CPU candidate so a future request tries the next worker."""
        reason_text = str(reason).lower()
        if "cancel" in reason_text or self.worker_variant == "cpu":
            return
        with self._state_lock:
            active_id = self._active_id
            cancelled_by_client = bool(active_id and active_id in self._cancel_requested_ids)
        if cancelled_by_client:
            return
        if isinstance(reason, _WorkerTranscriptionError) and "inference_failed" not in reason_text:
            return
        active_path = self._active_worker_path
        if active_path:
            self._bad_worker_paths.add(str(pathlib.Path(active_path).resolve()))
            self.stop()

    def transcribe(
        self,
        wav_path: str | pathlib.Path,
        language: str | None = None,
        timeout_seconds: float | None = None,
        *,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        path = pathlib.Path(wav_path).resolve(strict=True)
        if not path.is_file() or path.stat().st_size > 2 * 1024 * 1024 * 1024:
            raise RuntimeError("worker_audio_file_invalid")
        # Keep Windows' duration-scaled timeout API and Linux's bounded default.
        try:
            requested_timeout = TRANSCRIPTION_TIMEOUT_SECONDS if timeout_seconds is None else float(timeout_seconds)
        except (TypeError, ValueError, OverflowError):
            raise ValueError("timeout_seconds must be a finite positive number") from None
        if not math.isfinite(requested_timeout) or requested_timeout <= 0:
            raise ValueError("timeout_seconds must be a finite positive number")
        request_timeout = max(1.0, requested_timeout)

        # Start under the same lock as the request: concurrent first calls must not
        # race into spawning two persistent workers for one model.
        with self._request_lock:
            request_id = request_id or uuid.uuid4().hex[:24]
            if not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,48}", request_id):
                raise RuntimeError("worker_request_id_invalid")
            self.start()
            lang_b64 = _encode(language or "auto")
            segments: list[dict[str, Any]] = []
            detected_language: str | None = None
            last_start_cs = -1
            try:
                self._send(
                    f"TRANSCRIBE\t{request_id}\t{lang_b64}\t{_encode(os.fsencode(path))}",
                    activate_request_id=request_id,
                )
                while True:
                    try:
                        raw = self._lines.get(timeout=request_timeout)
                    except queue.Empty as exc:
                        raise RuntimeError("worker_transcription_timeout") from exc
                    if isinstance(raw, BaseException) or raw is None:
                        raise RuntimeError("persistent_worker_crashed") from (raw if isinstance(raw, BaseException) else None)
                    if len(raw) > MAX_IPC_LINE or not raw.endswith(b"\n"):
                        raise RuntimeError("worker_protocol_line_too_long")
                    line = raw.decode("utf-8", "strict").rstrip("\r\n")
                    parts = line.split("\t")
                    if parts[0] == "SEGMENT":
                        if len(parts) != 5 or parts[1] != request_id:
                            raise RuntimeError("worker_protocol_response_mismatch")
                        try:
                            start_cs = int(parts[2])
                            end_cs = int(parts[3])
                            text = _decode(parts[4])
                        except (ValueError, OverflowError) as exc:
                            raise RuntimeError("worker_protocol_segment_invalid") from exc
                        if (start_cs < 0 or end_cs < start_cs or end_cs > 10_000_000
                                or start_cs < last_start_cs):
                            raise RuntimeError("worker_timestamp_invalid")
                        last_start_cs = start_cs
                        segments.append({"text": text, "start": start_cs / 100.0, "end": end_cs / 100.0})
                    elif parts[0] == "DONE":
                        if len(parts) != 3 or parts[1] != request_id:
                            raise RuntimeError("worker_protocol_response_mismatch")
                        detected_language = _decode(parts[2]) if parts[2] != "-" else None
                        break
                    elif parts[0] == "ERROR":
                        if len(parts) != 3 or parts[1] != request_id:
                            raise RuntimeError("worker_protocol_response_mismatch")
                        code = re_safe_code(parts[2])
                        raise _WorkerTranscriptionError(f"worker_{code}")
                    else:
                        # A record for another request, malformed terminal record, or
                        # unexpected status is a hard fault. Stop the worker so no
                        # pending output can contaminate the next request.
                        raise RuntimeError("worker_protocol_response_mismatch")
            except _WorkerTranscriptionError as exc:
                # Cancellation is request-scoped; inference errors from non-CPU
                # variants demote that worker for this process and allow CPU fallback.
                self._demote_failed_non_cpu_worker(exc)
                raise RuntimeError(str(exc)) from None
            except Exception as exc:
                # IPC faults, worker crashes, and timeouts poison the stream. For a
                # non-CPU worker, also skip this candidate on subsequent starts.
                self._demote_failed_non_cpu_worker(exc)
                self.stop()
                raise
            finally:
                with self._state_lock:
                    self._active_id = None
                    self._cancel_requested_ids.discard(request_id)

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
            self._cancel_requested_ids.add(active)
        try:
            self._send(f"CANCEL\t{active}")
            return True
        except Exception:
            with self._state_lock:
                self._cancel_requested_ids.discard(active)
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
            try:
                process.terminate()
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
        except OSError:
            pass
        finally:
            for pipe in (process.stdin, process.stdout, process.stderr):
                if pipe is not None:
                    try:
                        pipe.close()
                    except Exception:
                        pass
            reader = self._reader
            self._reader = None
            if reader is not None and reader is not threading.current_thread():
                reader.join(timeout=1.0)


def re_safe_code(value: str) -> str:
    import re
    value = value.lower()
    return value if re.fullmatch(r"[a-z0-9_-]{1,64}", value) else "unknown_error"
