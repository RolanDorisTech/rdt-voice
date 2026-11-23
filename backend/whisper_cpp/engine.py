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
import time
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
SUPPORTED_PROVIDER_CHOICES = frozenset({"auto", "cpu", "cuda", "vulkan"})
try:
    TRANSCRIPTION_TIMEOUT_SECONDS = max(
        1.0,
        min(7200.0, float(os.environ.get("RDT_WORKER_TRANSCRIPTION_TIMEOUT", "1800"))),
    )
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


def _is_executable_worker(path: pathlib.Path) -> bool:
    try:
        if not path.is_file():
            return False
        return os.name == "nt" or os.access(path, os.X_OK)
    except OSError:
        return False


def _worker_variant(exe) -> str:
    name = pathlib.Path(exe).name.lower()
    if "dotprod" in name:
        return "arm-dotprod"
    if "cuda" in name:
        return "cuda"
    if "vulkan" in name:
        return "vulkan"
    if name.startswith("rdt_whisper_worker"):
        return "cpu"
    return "custom"


def _provider_for_variant(variant: str) -> str | None:
    providers = {
        "cpu": "cpu",
        "arm-dotprod": "cpu",
        "cuda": "cuda",
        "vulkan": "vulkan",
    }
    if variant == "custom":
        return None
    try:
        return providers[variant]
    except KeyError:
        raise RuntimeError("persistent_worker_provider_unrecognized") from None


def _normalize_architecture(value: str) -> str:
    value = str(value).strip().lower()
    aliases = {
        "aarch64": "arm64",
        "arm64": "arm64",
        "x86_64": "x64",
        "amd64": "x64",
        "x64": "x64",
        "i386": "x86",
        "i486": "x86",
        "i586": "x86",
        "i686": "x86",
        "x86": "x86",
    }
    return aliases.get(value, value)


def _normalise_reported_provider(value: str) -> str:
    value = str(value).strip().lower()
    if value == "cpu" or value.startswith("cpu "):
        return "cpu"
    if "cuda" in value:
        return "cuda"
    if "vulkan" in value:
        return "vulkan"
    return value


def _first_start_file_sha256(path: pathlib.Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _packaged_worker_root() -> pathlib.Path:
    if getattr(sys, "frozen", False):
        return pathlib.Path(sys.executable).resolve().parent
    return pathlib.Path(__file__).resolve().parent / "bin"




def _current_first_start_runtime_identity():
    """Re-discover current hardware and worker hashes before profile reuse."""
    try:
        from backend.whisper_cpp.hardware.discovery import discover_hardware_candidates
        from backend.whisper_cpp.hardware.first_start import build_first_start_runtime_identity
    except ImportError:
        from hardware.discovery import discover_hardware_candidates
        from hardware.first_start import build_first_start_runtime_identity

    workers = PersistentWhisperEngine.worker_candidates()
    candidates = discover_hardware_candidates(
        worker_paths=workers, timeout_seconds=5.0
    )
    if not candidates:
        raise RuntimeError("first_start_current_inventory_unavailable")

    cpu_worker = next(
        (
            path for path in workers
            if _worker_variant(path) == "cpu" and _is_executable_worker(path)
        ),
        None,
    )
    return build_first_start_runtime_identity(candidates, cpu_worker)


def _validate_first_start_profile_runtime(profile, current_identity):
    """Reject a saved winner unless the current physical candidate matches."""
    if profile.get("runtime_fingerprint") != current_identity.get("runtime_fingerprint"):
        raise ValueError("profile_runtime_fingerprint_stale")
    if profile.get("worker_build") != current_identity.get("worker_build"):
        raise ValueError("profile_worker_build_stale")

    selected = profile.get("selected_runtime")
    if not isinstance(selected, dict):
        raise ValueError("profile_selection_missing")
    candidate_id = str(selected.get("candidate_id") or "").strip()
    matches = [
        item for item in current_identity.get("candidates", [])
        if str(item.get("candidate_id") or "").strip() == candidate_id
    ]
    if len(matches) != 1:
        raise ValueError("profile_candidate_identity_stale")

    current = matches[0]
    for key in (
        "provider", "identity_kind", "identity_value", "driver",
        "driver_version", "runtime_version", "hardware_signature",
        "runtime_identity_kind", "runtime_identity_value",
        "runtime_selector_kind", "runtime_selector", "device",
        "worker_variant", "worker_sha256",
    ):
        if str(selected.get(key) or "") != str(current.get(key) or ""):
            raise ValueError("profile_selected_runtime_stale:" + key)

    saved_path = str(selected.get("worker_path") or "").strip()
    current_path = str(current.get("worker_path") or "").strip()
    if not saved_path or not current_path:
        raise ValueError("profile_selected_worker_path_missing")
    if pathlib.Path(saved_path).expanduser().resolve() != pathlib.Path(current_path).expanduser().resolve():
        raise ValueError("profile_selected_worker_path_stale")

def _load_first_start_engine_profile(model_path, model_id):
    """Prefer only a completed profile matching the current machine and workers."""
    report_value = os.environ.get(
        "RDT_WHISPER_FIRST_START_REPORT_PATH", ""
    ).strip()
    if not report_value:
        return None, None

    base = pathlib.Path(report_value).expanduser()
    last_good = base.with_name(f"{base.stem}-last-successful{base.suffix}")
    paths = (last_good, base)
    if not any(path.exists() or path.is_symlink() for path in paths):
        return None, None

    try:
        current_identity = _current_first_start_runtime_identity()
    except Exception:
        return None, "first_start_profile_rejected"

    last_reason = None
    for path in paths:
        if not path.exists() and not path.is_symlink():
            continue
        selection, reason = _load_first_start_engine_profile_from_path(
            str(path), model_path, model_id,
            current_runtime_identity=current_identity,
        )
        if selection is not None:
            return selection, None
        if reason:
            last_reason = reason
    return None, last_reason

def _load_first_start_engine_profile_from_path(report_value, model_path, model_id, *, current_runtime_identity=None):
    """Return a strictly validated packaged worker preference, or reject it."""
    try:
        import json

        report_path = pathlib.Path(report_value).expanduser()
        if not report_path.exists() and not report_path.is_symlink():
            return None, None
        if report_path.is_symlink() or not report_path.is_file():
            raise ValueError("report_path_invalid")
        report = json.loads(report_path.read_text(encoding="utf-8"))
        if not isinstance(report, dict):
            raise ValueError("report_not_object")

        partial = report.get("partial_engine_profile")
        if (
            report.get("completed") is not True
            and isinstance(partial, dict)
            and partial.get("completed") is True
            and partial.get("selection_kind") in {"partial-provisional", "cpu-readiness-fallback"}
        ):
            report = dict(report)
            report["completed"] = True
            report["engine_profile"] = partial

        if report.get("completed") is not True:
            raise ValueError("report_incomplete")

        profile = report.get("engine_profile")
        if (
            not isinstance(profile, dict)
            or profile.get("schema") != 1
            or profile.get("completed") is not True
        ):
            raise ValueError("profile_schema_or_completion_invalid")
        if profile.get("model_id") != model_id:
            raise ValueError("profile_model_id_mismatch")
        if profile.get("model_sha256") != _first_start_file_sha256(model_path):
            raise ValueError("profile_model_hash_mismatch")

        current_os = "windows" if os.name == "nt" else "linux"
        if profile.get("os") != current_os:
            raise ValueError("profile_os_mismatch")
        if _normalize_architecture(str(profile.get("architecture", ""))) != (
            _normalize_architecture(platform.machine())
        ):
            raise ValueError("profile_architecture_mismatch")

        for key in ("runtime_fingerprint", "worker_build", "fixture_sha256"):
            value = str(profile.get(key) or "")
            if not re.fullmatch(r"[0-9a-fA-F]{64}", value):
                raise ValueError("profile_context_invalid")

        selection_kind = str(profile.get("selection_kind") or "").strip().lower()
        if not selection_kind or selection_kind == "none":
            raise ValueError("profile_selection_kind_invalid")

        selected = profile.get("selected_runtime")
        if not isinstance(selected, dict):
            raise ValueError("profile_selection_missing")
        if not str(selected.get("candidate_id") or "").strip():
            raise ValueError("profile_candidate_id_missing")

        if current_runtime_identity is not None:
            _validate_first_start_profile_runtime(
                profile, current_runtime_identity
            )
        raw_worker_value = str(selected.get("worker_path") or "").strip()
        if not raw_worker_value:
            raise ValueError("profile_worker_path_missing")
        raw_worker = pathlib.Path(raw_worker_value).expanduser()
        if raw_worker.is_symlink():
            raise ValueError("profile_worker_path_is_symlink")
        worker_path = raw_worker.resolve(strict=True)
        worker_root = _packaged_worker_root().resolve(strict=True)
        if worker_path.parent != worker_root or not worker_path.is_file():
            raise ValueError("profile_worker_outside_package")
        if os.name != "nt" and not os.access(worker_path, os.X_OK):
            raise ValueError("profile_worker_not_executable")

        expected_hash = str(selected.get("worker_sha256") or "").lower()
        if not re.fullmatch(r"[0-9a-f]{64}", expected_hash):
            raise ValueError("profile_worker_hash_invalid")
        if _first_start_file_sha256(worker_path) != expected_hash:
            raise ValueError("profile_worker_hash_mismatch")

        provider = str(selected.get("provider") or "").strip().lower()
        variant = _worker_variant(worker_path)
        if provider not in {"cpu", "cuda", "vulkan"}:
            raise ValueError("profile_provider_invalid")
        if _provider_for_variant(variant) != provider:
            raise ValueError("profile_provider_worker_mismatch")
        saved_variant = str(selected.get("worker_variant") or "").strip()
        if saved_variant and saved_variant != variant:
            raise ValueError("profile_worker_variant_mismatch")

        identity_kind = str(selected.get("identity_kind") or "").strip()
        identity_value = str(selected.get("identity_value") or "").strip()
        if (
            not identity_kind
            or not identity_value
            or identity_kind.lower() in {"unknown", "none", "-"}
            or identity_value.lower() in {"unknown", "none", "-"}
        ):
            raise ValueError("profile_device_identity_invalid")

        selector = str(selected.get("runtime_selector") or "").strip()
        selector_kind = str(selected.get("runtime_selector_kind") or "").strip()
        if provider == "cuda":
            valid_selector = bool(
                re.fullmatch(r"[0-9]+", selector)
                or re.fullmatch(
                    r"GPU-[0-9A-Fa-f]{8}(?:-[0-9A-Fa-f]{4}){3}-[0-9A-Fa-f]{12}",
                    selector,
                )
            )
            if not selector_kind or not valid_selector:
                raise ValueError("profile_cuda_selector_invalid")
        elif provider == "vulkan":
            if (
                not selector_kind
                or not selector.isascii()
                or not selector.isdecimal()
            ):
                raise ValueError("profile_vulkan_selector_invalid")
        elif selector or selector_kind:
            raise ValueError("profile_cpu_selector_unexpected")

        result = dict(selected)
        result.update({
            "worker_path": str(worker_path),
            "worker_sha256": expected_hash,
            "provider": provider,
            "worker_variant": variant,
            "identity_kind": identity_kind,
            "identity_value": identity_value,
            "runtime_selector": selector,
            "runtime_selector_kind": selector_kind,
        })
        return result, None
    except Exception:
        # Do not expose private paths or parser details through the backend API.
        return None, "first_start_profile_rejected"


def _validate_first_start_ready(ready, selection) -> None:
    actual_provider = _normalise_reported_provider(ready.get("provider", ""))
    if actual_provider != selection["provider"]:
        raise RuntimeError("first_start_profile_ready_provider_mismatch")
    actual_identity = (
        str(ready.get("identity_kind", "")).strip(),
        str(ready.get("identity_value", "")).strip().lower(),
    )
    expected_identity = (
        selection["identity_kind"],
        selection["identity_value"].lower(),
    )
    if actual_identity != expected_identity:
        raise RuntimeError("first_start_profile_ready_identity_mismatch")


def _worker_launch_environment(exe, *, profile_selection=None) -> dict[str, str]:
    """Apply explicit device selection while retaining the caller's environment."""
    env = os.environ.copy()
    variant = _worker_variant(exe)
    runtime_dir = pathlib.Path(exe).resolve().parent / "cuda-runtime"
    if variant == "cuda" and runtime_dir.is_dir():
        if os.name == "nt":
            env["PATH"] = os.pathsep.join(
                item for item in (str(runtime_dir), env.get("PATH", "")) if item
            )
        elif sys.platform.startswith("linux"):
            env["LD_LIBRARY_PATH"] = os.pathsep.join(
                item for item in (str(runtime_dir), env.get("LD_LIBRARY_PATH", "")) if item
            )
    env.pop("GGML_CUDA_DEVICES", None)
    if profile_selection is not None:
        for key in (
            "RDT_WHISPER_DEVICE_SELECTOR",
            "RDT_WHISPER_DEVICE_INDEX",
            "CUDA_VISIBLE_DEVICES",
            "GGML_VK_VISIBLE_DEVICES",
        ):
            env.pop(key, None)
        selected_value = str(
            profile_selection.get("runtime_selector") or ""
        ).strip()
        if selected_value:
            env["RDT_WHISPER_DEVICE_SELECTOR"] = selected_value
    selector = env.get("RDT_WHISPER_DEVICE_SELECTOR", "").strip()
    legacy_index = env.get("RDT_WHISPER_DEVICE_INDEX", "").strip()

    if selector and legacy_index:
        raise RuntimeError("persistent_worker_device_selector_conflict")

    selected = selector or legacy_index
    if not selected:
        return env

    is_decimal = selected.isascii() and selected.isdecimal()

    if variant == "cuda":
        if is_decimal or re.fullmatch(
            r"GPU-[0-9A-Fa-f]{8}(?:-[0-9A-Fa-f]{4}){3}-[0-9A-Fa-f]{12}",
            selected,
        ):
            env["CUDA_VISIBLE_DEVICES"] = selected
        else:
            raise RuntimeError("persistent_worker_device_selector_invalid")
    elif variant == "vulkan":
        if not is_decimal:
            raise RuntimeError("persistent_worker_device_index_invalid")
        env["GGML_VK_VISIBLE_DEVICES"] = selected
    else:
        raise RuntimeError("persistent_worker_device_selection_requires_gpu_worker")

    return env


def _parse_worker_ready(line: bytes) -> dict[str, str]:
    try:
        parts = line.decode("utf-8", "strict").rstrip("\r\n").split("\t")
    except UnicodeDecodeError as exc:
        raise RuntimeError("persistent_worker_handshake_invalid") from exc

    if len(parts) == 5 and parts[0] == "READY" and all(parts[1:]):
        provider = _normalise_reported_provider(parts[3])
        if provider not in {"cpu", "cuda", "vulkan"}:
            raise RuntimeError("persistent_worker_provider_unrecognized")
        return {
            "architecture": parts[1],
            "engine_version": parts[2],
            "provider": provider,
            "device": parts[4],
            "identity_kind": "legacy",
            "identity_value": "-",
        }

    if len(parts) != 8 or parts[0] != "READY" or parts[1] != "2" or not all(parts[2:]):
        raise RuntimeError("persistent_worker_handshake_invalid")

    _, _, architecture, engine_version, raw_provider, device, kind, value = parts
    provider = _normalise_reported_provider(raw_provider)
    if provider not in {"cpu", "cuda", "vulkan"}:
        raise RuntimeError("persistent_worker_provider_unrecognized")

    if provider == "cpu":
        valid_identity = kind == "cpu" and value == "host"
    elif kind == "pci-bdf":
        valid_identity = bool(
            re.fullmatch(r"[0-9a-f]{4}:[0-9a-f]{2}:[0-9a-f]{2}\.[0-7]", value)
        )
    else:
        valid_identity = kind == "unavailable" and value == "-"

    if not valid_identity:
        raise RuntimeError("persistent_worker_device_identity_invalid")

    return {
        "architecture": architecture,
        "engine_version": engine_version,
        "provider": provider,
        "device": device,
        "identity_kind": kind,
        "identity_value": value,
    }


def _validate_expected_worker_identity(ready: dict[str, str]) -> None:
    expected_kind = os.environ.get("RDT_WHISPER_EXPECTED_DEVICE_IDENTITY_KIND", "").strip()
    expected_value = os.environ.get("RDT_WHISPER_EXPECTED_DEVICE_IDENTITY_VALUE", "").strip().lower()
    if not expected_kind and not expected_value:
        return
    if not expected_kind or not expected_value:
        raise RuntimeError("persistent_worker_expected_identity_incomplete")
    actual_kind = str(ready.get("identity_kind", "")).strip()
    actual_value = str(ready.get("identity_value", "")).strip().lower()
    if (actual_kind, actual_value) != (expected_kind, expected_value):
        raise RuntimeError("persistent_worker_selected_device_identity_mismatch")


class PersistentWhisperEngine:
    """One resident native worker, serialized requests, bounded stdout and IPC.

    The backend owns a model-use lease for this instance's whole lifetime. Actual
    mid-inference cancellation is supported by the worker's ggml abort callback.
    ``cancel`` reports whether the command was sent; the request caller observes the
    worker's terminal ERROR record if cancellation actually interrupts inference.
    """

    def __init__(
        self,
        model_path: pathlib.Path,
        model_id: str,
        *,
        recovery_probe_context: dict[str, Any] | None = None,
    ) -> None:
        self.model_path = model_path.resolve(strict=True)
        self.model_id = model_id
        self.provider_preference = os.environ.get("RDT_WHISPER_PROVIDER", "auto").strip().lower() or "auto"
        self._worker_override = os.environ.get("RDT_WHISPER_WORKER_EXE", "").strip()
        self.selection_mode = "manual" if self._worker_override or self.provider_preference != "auto" else "automatic"
        self.requested_provider = "custom" if self._worker_override and self.provider_preference == "auto" else self.provider_preference
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
        # The worker's READY record identifies an initialized provider, but an active
        # provider is not exposed until a real transcription succeeds.
        self.provider = "unknown"
        self.worker_variant = "unknown"
        self.provider_reason = "Worker has not completed startup."
        self._active_device = "unknown"
        self._active_device_identity_kind = "unknown"
        self._active_device_identity_value = "-"
        self._inference_verified = False
        self._last_transcription_ms: float | None = None
        self._successful_transcriptions = 0
        self._performance_samples: dict[str, dict[str, float | int | str]] = {}
        self._fallback_reason: str | None = None
        if self.selection_mode == "automatic":
            (
                self._first_start_profile_selection,
                self._first_start_profile_rejection,
            ) = _load_first_start_engine_profile(self.model_path, self.model_id)
        else:
            self._first_start_profile_selection = None
            self._first_start_profile_rejection = None
        if self._first_start_profile_rejection:
            self._fallback_reason = (
                "The first-start hardware profile was rejected; "
                "using the packaged automatic fallback order."
            )
        self._active_first_start_profile_selection = None
        self._first_start_profile_reverified = False
        self._active_hardware_breaker_context = None
        self._recovery_probe_context = (
            dict(recovery_probe_context)
            if isinstance(recovery_probe_context, dict) else None
        )
        self._startup_recovery_probes_completed = False
        self._hardware_breaker = None
        if sys.platform == "win32" or sys.platform.startswith("linux"):
            try:
                from backend.whisper_cpp.hardware.circuit_breaker import PersistentHardwareCircuitBreaker
            except ImportError:
                from hardware.circuit_breaker import PersistentHardwareCircuitBreaker
            self._hardware_breaker = PersistentHardwareCircuitBreaker()
        self._bad_worker_paths: set[str] = set()
        self._active_worker_path: str | None = None

    @staticmethod
    def worker_candidates() -> list[pathlib.Path]:
        override = os.environ.get("RDT_WHISPER_WORKER_EXE", "").strip()
        requested_provider = os.environ.get("RDT_WHISPER_PROVIDER", "auto").strip().lower() or "auto"
        if requested_provider not in SUPPORTED_PROVIDER_CHOICES:
            raise RuntimeError("persistent_worker_provider_invalid")
        if override:
            # An explicit executable is authoritative in development, but an
            # installed app may only launch workers shipped beside that app.
            if requested_provider != "auto":
                raise RuntimeError("persistent_worker_override_provider_conflict")
            raw_override = pathlib.Path(override).expanduser()
            if getattr(sys, "frozen", False):
                if raw_override.is_symlink():
                    raise RuntimeError("persistent_worker_override_is_symlink")
                packaged_root = pathlib.Path(sys.executable).resolve().parent
                resolved_override = raw_override.resolve()
                if resolved_override.parent != packaged_root:
                    raise RuntimeError("persistent_worker_override_outside_package")
                return [resolved_override]
            return [raw_override.resolve()]

        suffix = ".exe" if os.name == "nt" else ""
        if getattr(sys, "frozen", False):
            root = pathlib.Path(sys.executable).resolve().parent
        else:
            root = pathlib.Path(__file__).resolve().parent / "bin"

        workers = {
            "cuda": root / f"rdt_whisper_worker_cuda{suffix}",
            "vulkan": root / f"rdt_whisper_worker_vulkan{suffix}",
            "cpu": root / f"rdt_whisper_worker{suffix}",
        }
        if os.name == "nt" and _windows_arm_dotprod_available():
            dotprod = root / "rdt_whisper_worker_dotprod.exe"
        elif sys.platform.startswith("linux") and _linux_arm_dotprod_available():
            dotprod = root / "rdt_whisper_worker_dotprod"
        else:
            dotprod = None

        if requested_provider != "auto":
            # A manual choice is a constraint, not a preference. start() will report
            # a specific missing-package error instead of trying another provider.
            return [workers[requested_provider]]

        # Deterministic fallback order when no valid benchmark winner is available:
        # try packaged accelerators first, then a runtime-qualified ARM CPU worker,
        # and finally the portable CPU worker. A packaged worker is not considered
        # usable until its READY identity checks pass; successful inference is still
        # required before the backend reports the provider as verified.
        candidates = [workers["cuda"], workers["vulkan"]]
        if dotprod is not None:
            candidates.append(dotprod)
        candidates.append(workers["cpu"])
        return candidates

    @staticmethod
    def worker_path() -> pathlib.Path:
        # Compatibility for callers that still expect a single default path.
        candidates = PersistentWhisperEngine.worker_candidates()
        for candidate in candidates:
            if candidate.is_file():
                return candidate
        return candidates[-1]

    def _run_due_hardware_recovery_probes(self) -> None:
        """Requalify expired quarantines before the server accepts transcription.

        The server holds the cross-process model-use lease before engine.start().
        First-start and manual benchmark CLI processes require that same lease, so
        this startup probe cannot overlap either benchmark or another model user.
        """
        import logging
        logger = logging.getLogger(__name__)
        breaker = getattr(self, "_hardware_breaker", None)
        if breaker is None or getattr(self, "_recovery_probe_context", None) is not None:
            return

        try:
            from backend.whisper_cpp.hardware.first_start import locate_manual_fixture
            from backend.whisper_cpp.hardware.runner import run_candidate_benchmark
        except ImportError:
            from hardware.first_start import locate_manual_fixture
            from hardware.runner import run_candidate_benchmark

        due_contexts = breaker.recovery_probes_due()
        if not due_contexts:
            return

        try:
            inventory = _current_first_start_runtime_identity()
            candidates = inventory.get("candidates", [])
            if not isinstance(candidates, list):
                raise ValueError("recovery_probe_inventory_invalid")

            # Use only the pinned owner recording. Do not fall back to the
            # synthetic development smoke fixture for a production recovery probe.
            fixture = locate_manual_fixture("12s")
            fixture_hash = _first_start_file_sha256(fixture)
            model_hash = _first_start_file_sha256(self.model_path)
            runtime_fingerprint = str(inventory["runtime_fingerprint"])
            worker_build = str(inventory["worker_build"])
        except Exception as exc:
            logger.warning(
                "Hardware recovery probe deferred: %s", type(exc).__name__
            )
            return

        for due_context in due_contexts:
            matches = []
            for discovered in candidates:
                if not isinstance(discovered, dict) or not discovered.get("eligible", True):
                    continue
                provider = str(discovered.get("provider") or "").lower()
                worker_variant = str(discovered.get("worker_variant") or "").strip().lower()
                if provider not in {"cuda", "vulkan"} and not (
                    provider == "cpu" and worker_variant == "arm-dotprod"
                ):
                    continue
                raw_path = pathlib.Path(str(discovered.get("worker_path") or "")).expanduser()
                if raw_path.is_symlink():
                    continue
                try:
                    worker_path = raw_path.resolve(strict=True)
                    if not worker_path.is_file() or not _is_executable_worker(worker_path):
                        continue
                    if provider == "cpu" and _worker_variant(worker_path) != "arm-dotprod":
                        continue
                    worker_hash = _first_start_file_sha256(worker_path)
                except (OSError, ValueError):
                    continue

                discovered = dict(discovered)
                discovered["worker_path"] = str(worker_path)
                discovered["worker_sha256"] = worker_hash
                ready = {
                    "provider": provider,
                    "identity_kind": discovered.get("identity_kind"),
                    "identity_value": discovered.get("identity_value"),
                    "architecture": discovered.get("architecture") or platform.machine(),
                    "device": discovered.get("device") or "",
                }
                try:
                    current_context = self._build_hardware_breaker_context(
                        worker_path, ready, discovered
                    )
                except Exception:
                    continue
                if current_context and breaker.same_context(current_context, due_context):
                    matches.append((discovered, worker_path, worker_hash))

            # A missing or ambiguous current identity is not permission to probe
            # a different device. Keep the old quarantine untouched.
            if len(matches) != 1:
                logger.warning(
                    "Hardware recovery probe deferred: candidate identity is not unique."
                )
                continue

            candidate, worker_path, worker_hash = matches[0]
            probe_context = {
                "os": "windows" if os.name == "nt" else "linux",
                "architecture": _normalize_architecture(platform.machine()),
                "runtime_fingerprint": runtime_fingerprint,
                "worker_build": worker_build,
                "model_id": str(self.model_id),
                "model_sha256": model_hash,
                "fixture_sha256": fixture_hash,
                "repetitions": 2,
                "language": "auto",
                "decode_config": {"language": "auto", "task": "transcribe"},
                "driver_version": str(candidate.get("driver_version") or "unknown"),
                "backend_version": str(candidate.get("runtime_version") or "unknown"),
            }

            try:
                result = run_candidate_benchmark(
                    candidate,
                    probe_context,
                    self.model_path,
                    fixture,
                    recovery_probe_context=due_context,
                )
                if not isinstance(result, dict):
                    raise RuntimeError("recovery_probe_result_invalid")
                if result.get("candidate_id") != candidate.get("candidate_id"):
                    raise RuntimeError("recovery_probe_candidate_mismatch")
                if str(result.get("actual_provider", "")).lower() != str(candidate["provider"]).lower():
                    raise RuntimeError("recovery_probe_provider_mismatch")
                if result.get("identity_kind") != candidate.get("identity_kind"):
                    raise RuntimeError("recovery_probe_identity_kind_mismatch")
                if str(result.get("identity_value", "")).lower() != str(candidate.get("identity_value", "")).lower():
                    raise RuntimeError("recovery_probe_identity_value_mismatch")
                if result.get("model_sha256") != model_hash:
                    raise RuntimeError("recovery_probe_model_hash_mismatch")
                if result.get("fixture_sha256") != fixture_hash:
                    raise RuntimeError("recovery_probe_fixture_hash_mismatch")
                if (
                    _first_start_file_sha256(worker_path) != worker_hash
                    or _first_start_file_sha256(fixture) != fixture_hash
                    or _first_start_file_sha256(self.model_path) != model_hash
                ):
                    raise RuntimeError("recovery_probe_input_changed")

                post_inventory = _current_first_start_runtime_identity()
                if (
                    str(post_inventory.get("runtime_fingerprint") or "")
                    != runtime_fingerprint
                    or str(post_inventory.get("worker_build") or "")
                    != worker_build
                ):
                    raise RuntimeError("recovery_probe_runtime_changed")
                post_candidates = post_inventory.get("candidates", [])
                post_matches = [
                    item for item in post_candidates
                    if isinstance(item, dict)
                    and str(item.get("provider") or "").lower()
                        == str(candidate.get("provider") or "").lower()
                    and str(item.get("identity_kind") or "")
                        == str(candidate.get("identity_kind") or "")
                    and str(item.get("identity_value") or "").lower()
                        == str(candidate.get("identity_value") or "").lower()
                    and str(item.get("worker_sha256") or "").lower()
                        == worker_hash.lower()
                    and str(item.get("runtime_selector_kind") or "")
                        == str(candidate.get("runtime_selector_kind") or "")
                    and str(item.get("runtime_selector") or "")
                        == str(candidate.get("runtime_selector") or "")
                    and str(item.get("driver_version") or "")
                        == str(candidate.get("driver_version") or "")
                    and str(item.get("runtime_version") or "")
                        == str(candidate.get("runtime_version") or "")
                ]
                if len(post_matches) != 1:
                    raise RuntimeError("recovery_probe_post_identity_mismatch")

                timings = result.get("inference_times_ms")
                if (
                    not isinstance(timings, list)
                    or len(timings) != 2
                    or any(
                        isinstance(value, bool)
                        or not isinstance(value, (int, float))
                        or not math.isfinite(float(value))
                        or float(value) <= 0
                        for value in timings
                    )
                ):
                    raise RuntimeError("recovery_probe_measurements_invalid")

                if breaker.clear_after_probe(due_context, passed=True):
                    logger.info(
                        "Hardware candidate recovered after a passing bundled-fixture probe."
                    )
            except Exception as exc:
                try:
                    breaker.clear_after_probe(due_context, passed=False)
                except Exception:
                    logger.warning("Could not persist failed recovery-probe result.")
                logger.warning(
                    "Hardware recovery probe failed; candidate remains quarantined: %s",
                    type(exc).__name__,
                )

    def start(
        self,
        *,
        candidate_path: pathlib.Path | None = None,
        ready_timeout_seconds: float = READY_TIMEOUT_SECONDS,
        mark_failed_candidate: bool = True,
    ) -> None:
        if self.process is not None and self.process.poll() is None:
            return

        if not getattr(self, "_startup_recovery_probes_completed", False):
            self._startup_recovery_probes_completed = True
            if getattr(self, "_recovery_probe_context", None) is None:
                self._run_due_hardware_recovery_probes()

        self._active_first_start_profile_selection = None
        self._first_start_profile_reverified = False
        self._active_hardware_breaker_context = None
        candidates = (
            [pathlib.Path(candidate_path)]
            if candidate_path is not None else self.worker_candidates()
        )
        if (
            candidate_path is None
            and self.selection_mode == "automatic"
            and self._first_start_profile_selection is not None
        ):
            preferred = pathlib.Path(
                self._first_start_profile_selection["worker_path"]
            )
            preferred_resolved = preferred.resolve()
            candidates = [preferred] + [
                path for path in candidates
                if path.resolve() != preferred_resolved
            ]
        available = [
            path for path in candidates
            if _is_executable_worker(path) and str(path.resolve()) not in self._bad_worker_paths
        ]
        if not available:
            if candidate_path is not None:
                raise RuntimeError("persistent_worker_candidate_unavailable")
            if self.selection_mode == "manual":
                selected = self.requested_provider if not self._worker_override else "custom worker"
                raise RuntimeError(f"persistent_worker_provider_not_packaged:{selected}")
            if any(path.is_file() for path in candidates):
                raise RuntimeError("persistent_worker_no_healthy_candidate")
            raise RuntimeError("persistent_worker_not_packaged")

        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        last_error = (
            RuntimeError("first_start_profile_rejected")
            if self._first_start_profile_rejection else None
        )

        for exe in available:
            attempt_error = None
            self._lines = queue.Queue(maxsize=16)
            try:
                attempt_profile_selection = None
                if self._first_start_profile_selection is not None:
                    profile_worker = pathlib.Path(
                        self._first_start_profile_selection["worker_path"]
                    ).resolve()
                    if pathlib.Path(exe).resolve() == profile_worker:
                        if _first_start_file_sha256(profile_worker) != (
                            self._first_start_profile_selection["worker_sha256"]
                        ):
                            raise RuntimeError(
                                "first_start_profile_worker_changed_before_launch"
                            )
                        attempt_profile_selection = (
                            self._first_start_profile_selection
                        )
                self.process = subprocess.Popen(
                    [str(exe), "--model", str(self.model_path)],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=None,  # Preserve native worker diagnostics in the backend log.
                    bufsize=0,
                    creationflags=creationflags,
                    env=_worker_launch_environment(
                        exe, profile_selection=attempt_profile_selection
                    ),
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
                ready_wait = max(0.001, min(READY_TIMEOUT_SECONDS, float(ready_timeout_seconds)))
                line = self._lines.get(timeout=ready_wait)

                if isinstance(line, BaseException) or line is None:
                    raise RuntimeError("persistent_worker_exited_before_ready")

                if line.rstrip(b"\r\n") == b"STARTUP_ERROR\tmodel_load_out_of_memory":
                    raise RuntimeError("worker_model_load_out_of_memory")

                ready = _parse_worker_ready(line)
                _validate_expected_worker_identity(ready)
                if attempt_profile_selection is not None:
                    _validate_first_start_ready(ready, attempt_profile_selection)
                breaker_context = None
                if getattr(self, "_hardware_breaker", None) is not None:
                    try:
                        breaker_context = self._build_hardware_breaker_context(
                            exe, ready, attempt_profile_selection
                        )
                    except (OSError, ValueError):
                        breaker_context = None
                    if breaker_context and self._hardware_breaker.is_quarantined(breaker_context):
                        probe_context = getattr(self, "_recovery_probe_context", None)
                        authorized_probe = bool(
                            probe_context
                            and self._hardware_breaker.same_context(
                                breaker_context, probe_context
                            )
                        )
                        if not authorized_probe:
                            raise RuntimeError("persistent_worker_candidate_quarantined")
                variant = _worker_variant(exe)
                reported_provider = _normalise_reported_provider(ready["provider"])
                expected_provider = _provider_for_variant(variant)
                expected_architecture = _normalize_architecture(platform.machine())
                reported_architecture = _normalize_architecture(ready["architecture"])
                if expected_architecture not in {"arm64", "x64", "x86"}:
                    raise RuntimeError("persistent_worker_host_architecture_unrecognized")
                if reported_architecture != expected_architecture:
                    raise RuntimeError("persistent_worker_architecture_mismatch")
                if reported_provider not in {"cpu", "cuda", "vulkan"}:
                    raise RuntimeError("persistent_worker_provider_unrecognized")
                if expected_provider is not None and reported_provider != expected_provider:
                    raise RuntimeError("persistent_worker_provider_mismatch")
                if self.provider_preference != "auto" and reported_provider != self.provider_preference:
                    raise RuntimeError("persistent_worker_selected_provider_mismatch")

                self.architecture = ready["architecture"][:32]
                self.engine_version = ready["engine_version"][:128]
                self._active_worker_path = str(exe.resolve())
                if self.selection_mode == "automatic" and last_error is not None and self._fallback_reason is None:
                    error_text = str(last_error).strip().lower()
                    safe_error = error_text if re.fullmatch(r"[a-z0-9_-]{1,64}", error_text) else "worker_initialization_failed"
                    self._fallback_reason = (
                        f"Automatic startup fallback selected {variant} after a higher-priority packaged "
                        f"worker failed initialization ({safe_error})."
                    )
                self.provider = {
            "cpu": "CPU",
            "cuda": "CUDA",
            "vulkan": "Vulkan",
        }.get(reported_provider, reported_provider[:32])
                device = ready["device"][:256]
                self._active_device = device
                self._active_device_identity_kind = ready["identity_kind"]
                self._active_device_identity_value = ready["identity_value"]
                self.worker_variant = variant
                self._active_first_start_profile_selection = attempt_profile_selection
                self._active_hardware_breaker_context = breaker_context
                self._first_start_profile_reverified = False
                self._inference_verified = False
                self.provider_reason = (
                    f"{self._fallback_reason} Active worker variant {variant}; provider {self.provider}; device {device}"
                    if self._fallback_reason else
                    f"Active worker variant {variant}; provider {self.provider}; device {device}"
                )
                return

            except queue.Empty:
                attempt_error = RuntimeError("persistent_worker_ready_timeout")
            except (OSError, RuntimeError, UnicodeError) as exc:
                attempt_error = exc
            finally:
                if attempt_error is not None:
                    self.stop()
                    # A broken candidate must not impose the same slow startup delay
                    # on every subsequent request in Automatic mode. Manual mode never
                    # blacklists or substitutes the explicitly selected provider.
                    if (
                        self.selection_mode == "automatic"
                        and mark_failed_candidate
                        and str(attempt_error) != "worker_model_load_out_of_memory"
                        and str(attempt_error) != "persistent_worker_candidate_quarantined"
                    ):
                        self._bad_worker_paths.add(str(exe.resolve()))

            if attempt_error is not None:
                if str(attempt_error) == "worker_model_load_out_of_memory":
                    raise RuntimeError("worker_model_load_out_of_memory") from None
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

    def _build_hardware_breaker_context(
        self, worker_path: pathlib.Path, ready: dict[str, Any], profile: dict[str, Any] | None
    ) -> dict[str, Any] | None:
        if getattr(self, "_hardware_breaker", None) is None:
            return None
        try:
            from backend.whisper_cpp.hardware.circuit_breaker import sha256_path
        except ImportError:
            from hardware.circuit_breaker import sha256_path

        profile = profile if isinstance(profile, dict) else {}
        worker_path = pathlib.Path(worker_path).resolve(strict=True)
        worker_hash = profile.get("worker_sha256") or sha256_path(worker_path)
        model_hash = (
            profile.get("model_sha256")
            or profile.get("model_hash")
            or sha256_path(self.model_path)
        )
        identity_kind = ready.get("identity_kind") or self._active_device_identity_kind
        identity_value = ready.get("identity_value") or self._active_device_identity_value
        provider = str(ready.get("provider") or self.provider or "").strip().lower()
        runtime = {}
        selector = {}
        runtime_names = (
            "driver", "driver_version", "runtime", "runtime_version",
            "runtime_fingerprint", "runtime_identity_kind",
            "runtime_identity_value", "engine_version", "architecture",
        )
        selector_names = (
            "runtime_selector_kind", "runtime_selector",
            "selector_kind", "selector", "device_id",
        )
        for values in (ready, profile):
            if not isinstance(values, dict):
                continue
            for key in runtime_names:
                if values.get(key) not in (None, ""):
                    runtime[key] = str(values[key])[:256]
            for key in selector_names:
                if values.get(key) not in (None, ""):
                    selector[key] = str(values[key])[:256]
        # Add the discovered physical GPU's driver/runtime and provider selector
        # to the key. READY alone does not carry a reliable driver fingerprint.
        try:
            inventory = _current_first_start_runtime_identity()
            inventory_candidates = inventory.get("candidates", [])
            matches = [
                item for item in inventory_candidates
                if isinstance(item, dict)
                and str(item.get("provider") or "").lower() == provider
                and str(item.get("identity_kind") or "") == str(identity_kind or "")
                and str(item.get("identity_value") or "").lower()
                    == str(identity_value or "").lower()
                and (
                    not item.get("worker_sha256")
                    or str(item.get("worker_sha256")).lower() == str(worker_hash).lower()
                )
            ]
            if len(matches) != 1:
                return None
            discovered = matches[0]
            for key in (
                "driver", "driver_version", "runtime_version",
                "hardware_signature", "runtime_identity_kind",
                "runtime_identity_value",
            ):
                if discovered.get(key) not in (None, ""):
                    runtime[key] = str(discovered[key])[:256]
            for key in ("runtime_selector_kind", "runtime_selector"):
                if discovered.get(key) not in (None, ""):
                    selector[key] = str(discovered[key])[:256]
            if inventory.get("runtime_fingerprint"):
                runtime["runtime_fingerprint"] = str(
                    inventory["runtime_fingerprint"]
                )[:256]
            if inventory.get("worker_build"):
                runtime["worker_build"] = str(inventory["worker_build"])[:256]
        except Exception:
            # Discovery failure must never prevent a usable worker from starting,
            # but it must not create a weak or ambiguous breaker identity.
            return None

        fingerprint = str(runtime.get("runtime_fingerprint") or "").lower()
        if not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
            return None
        variant = _worker_variant(worker_path)
        if provider in {"cuda", "vulkan"}:
            if any(
                str(runtime.get(key) or "").strip().lower()
                in {"", "-", "unknown", "none"}
                for key in ("driver_version", "runtime_version")
            ):
                return None
            if not selector.get("runtime_selector_kind") or not selector.get("runtime_selector"):
                return None
        elif variant == "arm-dotprod":
            if not runtime.get("hardware_signature"):
                return None
        else:
            return None

        runtime.setdefault("host_platform", sys.platform)
        runtime.setdefault("host_release", platform.release())
        runtime.setdefault("host_machine", platform.machine())

        return {
            "hardware_identity": {
                "kind": str(identity_kind or ""),
                "value": str(identity_value or ""),
            },
            "provider": provider,
            "worker_variant": _worker_variant(worker_path),
            "worker_sha256": str(worker_hash),
            "runtime": runtime,
            "selector": selector,
            "model": {
                "id": str(self.model_id),
                "sha256": str(model_hash),
            },
        }

    def _demote_failed_non_cpu_worker(self, reason: BaseException | str) -> None:
        """Quarantine a failed Automatic-mode candidate only for classified engine faults."""
        if self.selection_mode != "automatic" or self.worker_variant not in {"cuda", "vulkan", "arm-dotprod"}:
            return
        reason_text = str(reason).lower()
        # OOM is a workload/model-memory condition, not hardware instability.
        if (
            "cancel" in reason_text
            or "timeout" in reason_text
            or "out_of_memory" in reason_text
            or "out of memory" in reason_text
            or "worker_oom" in reason_text
        ):
            return
        with self._state_lock:
            active_id = self._active_id
            cancelled_by_client = bool(active_id and active_id in self._cancel_requested_ids)
        if cancelled_by_client:
            return
        if isinstance(reason, _WorkerTranscriptionError):
            # The worker uses distinct terminal codes for cancellation, malformed
            # audio, and inference failure. Only the last is safe to replay.
            if reason_text != "worker_inference_failed":
                return
        elif not reason_text.startswith((
            "persistent_worker_crashed",
            "persistent_worker_not_running",
            "worker_protocol_",
            "worker_timestamp_invalid",
        )):
            return
        active_path = self._active_worker_path
        if active_path:
            self._bad_worker_paths.add(str(pathlib.Path(active_path).resolve()))
            failure_code = re_safe_code(str(reason))
            self._fallback_reason = (
                f"Automatic fallback is switching to another eligible worker after "
                f"{self.worker_variant} failed ({failure_code}); the failed request was not replayed."
            )
            breaker = getattr(self, "_hardware_breaker", None)
            breaker_context = getattr(self, "_active_hardware_breaker_context", None)
            if breaker is not None and breaker_context:
                breaker.record_failure(breaker_context)
            self.stop()

    @staticmethod
    def _is_unavailable_candidate_failure(reason: BaseException | str) -> bool:
        text = str(reason).lower()
        return text.startswith((
            "persistent_worker_candidate_unavailable",
            "persistent_worker_provider_not_packaged",
            "persistent_worker_not_packaged",
            "persistent_worker_no_healthy_candidate",
            "persistent_worker_host_architecture_unrecognized",
            "persistent_worker_architecture_mismatch",
            "persistent_worker_provider_unrecognized",
            "persistent_worker_provider_mismatch",
            "persistent_worker_selected_provider_mismatch",
            "worker_device_unavailable",
            "worker_driver_unavailable",
            "worker_runtime_unavailable",
            "worker_cuda_unavailable",
            "worker_vulkan_unavailable",
        ))

    @staticmethod
    def _is_restartable_candidate_failure(reason: BaseException | str) -> bool:
        if isinstance(reason, _WorkerTranscriptionError):
            return str(reason) == "worker_inference_failed"
        text = str(reason).lower()
        return text.startswith((
            "persistent_worker_crashed",
            "persistent_worker_not_running",
            "persistent_worker_exited_before_ready",
            "persistent_worker_ready_timeout",
            "worker_protocol_",
            "worker_timestamp_invalid",
            "worker_transcription_timeout",
        ))

    def _record_successful_inference(self, elapsed_ms: float) -> None:
        selection = self._active_first_start_profile_selection
        if selection is not None and not self._first_start_profile_reverified:
            process = self.process
            active_path = pathlib.Path(self._active_worker_path).resolve()
            if process is None or process.poll() is not None:
                raise RuntimeError("first_start_profile_worker_exited_after_inference")
            if active_path != pathlib.Path(selection["worker_path"]).resolve():
                raise RuntimeError("first_start_profile_worker_path_changed")
            if _first_start_file_sha256(active_path) != selection["worker_sha256"]:
                raise RuntimeError("first_start_profile_worker_hash_changed")
            if self.provider.lower() != selection["provider"]:
                raise RuntimeError("first_start_profile_provider_changed")
            if self.worker_variant != selection["worker_variant"]:
                raise RuntimeError("first_start_profile_variant_changed")
            if (
                self._active_device_identity_kind != selection["identity_kind"]
                or self._active_device_identity_value.lower()
                != selection["identity_value"].lower()
            ):
                raise RuntimeError("first_start_profile_identity_changed")
            self._first_start_profile_reverified = True

        self._inference_verified = True
        self._last_transcription_ms = round(max(0.0, elapsed_ms), 3)
        self._successful_transcriptions += 1
        # Small, process-local observations only. No startup benchmark, persisted
        # device fingerprint, or automatic provider ranking from incomparable jobs.
        sample_key = "|".join((
            self.model_id,
            self.engine_version,
            self.architecture,
            self.worker_variant,
            self.provider,
            self._active_device,
        ))
        sample = self._performance_samples.get(sample_key)
        if sample is None:
            sample = {
                "model": self.model_id,
                "engine_version": self.engine_version,
                "architecture": self.architecture,
                "worker_variant": self.worker_variant,
                "provider": self.provider,
                "device": self._active_device,
                "samples": 0,
                "last_ms": self._last_transcription_ms,
                "average_ms": self._last_transcription_ms,
                "_total_ms": 0.0,
            }
            self._performance_samples[sample_key] = sample
        count = int(sample["samples"]) + 1
        total_ms = float(sample["_total_ms"]) + self._last_transcription_ms
        sample["samples"] = count
        sample["last_ms"] = self._last_transcription_ms
        sample["average_ms"] = round(total_ms / count, 3)
        sample["_total_ms"] = total_ms
        while len(self._performance_samples) > 8:
            self._performance_samples.pop(next(iter(self._performance_samples)))

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
        try:
            requested_timeout = TRANSCRIPTION_TIMEOUT_SECONDS if timeout_seconds is None else float(timeout_seconds)
        except (TypeError, ValueError, OverflowError):
            raise ValueError("timeout_seconds must be a finite positive number") from None
        if not math.isfinite(requested_timeout) or requested_timeout <= 0:
            raise ValueError("timeout_seconds must be a finite positive number")
        request_timeout = requested_timeout

        with self._request_lock:
            request_id = request_id or uuid.uuid4().hex[:24]
            if not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,48}", request_id):
                raise RuntimeError("worker_request_id_invalid")
            lang_b64 = _encode(language or "auto")
            request_deadline = time.monotonic() + request_timeout

            resident = self.process is not None and self.process.poll() is None
            if resident:
                candidates: list[pathlib.Path | None] = [
                    pathlib.Path(self._active_worker_path) if self._active_worker_path else None
                ]
                candidates_loaded = False
            else:
                candidates = list(self.worker_candidates())
                candidates_loaded = True

            last_error: BaseException | None = None
            seen = {
                str(item.resolve()) for item in candidates if item is not None
            }
            candidate_index = 0

            try:
                while candidate_index < len(candidates):
                    if time.monotonic() >= request_deadline:
                        break

                    candidate = candidates[candidate_index]
                    candidate_variant = (
                        _worker_variant(candidate) if candidate is not None else self.worker_variant
                    )
                    if candidate is not None:
                        candidate_key = str(candidate.resolve())
                        if candidate_key in self._bad_worker_paths:
                            candidate_index += 1
                            continue
                        resident_candidate = (
                            self.process is not None
                            and self.process.poll() is None
                            and self._active_worker_path is not None
                            and pathlib.Path(self._active_worker_path).resolve()
                            == candidate.resolve()
                        )
                        if not resident_candidate and not _is_executable_worker(candidate):
                            self._bad_worker_paths.add(candidate_key)
                            last_error = RuntimeError("persistent_worker_candidate_unavailable")
                            candidate_index += 1
                            continue

                    retryable_variant = candidate_variant in {
                        "cuda", "vulkan", "arm-dotprod", "cpu"
                    }
                    max_attempts = (
                        2 if self.selection_mode == "automatic" and retryable_variant else 1
                    )
                    attempts = 0
                    candidate_failed = False

                    oom_attempts = 0
                    while attempts < max_attempts:
                        if time.monotonic() >= request_deadline:
                            break

                        try:
                            process_running = (
                                self.process is not None and self.process.poll() is None
                            )
                            active_path_matches = (
                                candidate is None
                                or (
                                    self._active_worker_path is not None
                                    and pathlib.Path(self._active_worker_path).resolve()
                                    == candidate.resolve()
                                )
                            )
                            if not process_running or not active_path_matches:
                                if candidate is None:
                                    raise RuntimeError("persistent_worker_not_running")
                                ready_remaining = request_deadline - time.monotonic()
                                if ready_remaining <= 0:
                                    raise RuntimeError("worker_transcription_timeout")
                                self.start(
                                    candidate_path=candidate,
                                    ready_timeout_seconds=min(
                                        READY_TIMEOUT_SECONDS, ready_remaining
                                    ),
                                    mark_failed_candidate=False,
                                )

                            # A fresh buffer per attempt ensures partial segments from
                            # a crashed/restarted worker can never escape this request.
                            segments: list[dict[str, Any]] = []
                            detected_language: str | None = None
                            last_start_cs = -1
                            inference_started = time.perf_counter()
                            self._send(
                                f"TRANSCRIBE\t{request_id}\t{lang_b64}\t{_encode(os.fsencode(path))}",
                                activate_request_id=request_id,
                            )

                            while True:
                                remaining = request_deadline - time.monotonic()
                                if remaining <= 0:
                                    raise RuntimeError("worker_transcription_timeout")
                                try:
                                    raw = self._lines.get(timeout=remaining)
                                except queue.Empty as exc:
                                    raise RuntimeError("worker_transcription_timeout") from exc

                                if isinstance(raw, BaseException) or raw is None:
                                    raise RuntimeError("persistent_worker_crashed") from (
                                        raw if isinstance(raw, BaseException) else None
                                    )
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
                                        segment_text = _decode(parts[4])
                                    except (ValueError, OverflowError) as exc:
                                        raise RuntimeError("worker_protocol_segment_invalid") from exc
                                    if (
                                        start_cs < 0 or end_cs < start_cs
                                        or end_cs > 10_000_000 or start_cs < last_start_cs
                                    ):
                                        raise RuntimeError("worker_timestamp_invalid")
                                    last_start_cs = start_cs
                                    segments.append({
                                        "text": segment_text,
                                        "start": start_cs / 100.0,
                                        "end": end_cs / 100.0,
                                    })
                                elif parts[0] == "DONE":
                                    if len(parts) != 3 or parts[1] != request_id:
                                        raise RuntimeError("worker_protocol_response_mismatch")
                                    detected_language = (
                                        _decode(parts[2]) if parts[2] != "-" else None
                                    )
                                    self._record_successful_inference(
                                        (time.perf_counter() - inference_started) * 1000.0
                                    )
                                    return {
                                        "text": "".join(s["text"] for s in segments),
                                        "segments": segments,
                                        "language": detected_language,
                                    }
                                elif parts[0] == "ERROR":
                                    if len(parts) != 3 or parts[1] != request_id:
                                        raise RuntimeError("worker_protocol_response_mismatch")
                                    code = re_safe_code(parts[2])
                                    raise _WorkerTranscriptionError(f"worker_{code}")
                                else:
                                    raise RuntimeError("worker_protocol_response_mismatch")

                        except Exception as exc:
                            with self._state_lock:
                                cancelled = request_id in self._cancel_requested_ids
                            if cancelled:
                                raise RuntimeError(str(exc)) from None

                            if str(exc) == "persistent_worker_candidate_quarantined":
                                self.stop()
                                last_error = exc
                                candidate_failed = True
                                break

                            if (
                                isinstance(exc, _WorkerTranscriptionError)
                                and str(exc) == "worker_out_of_memory"
                            ):
                                oom_attempts += 1
                                if oom_attempts < 2:
                                    self.stop()
                                    continue
                                self.stop()
                                raise RuntimeError("worker_out_of_memory") from None

                            if self._is_unavailable_candidate_failure(exc):
                                self.stop()
                                if candidate is not None:
                                    self._bad_worker_paths.add(str(candidate.resolve()))
                                last_error = exc
                                candidate_failed = True
                                break

                            restartable = self._is_restartable_candidate_failure(exc)
                            if (
                                isinstance(exc, _WorkerTranscriptionError)
                                and candidate_variant not in {"cuda", "vulkan", "arm-dotprod"}
                            ):
                                # A classified inference failure is retryable only
                                # for an Automatic-mode accelerator candidate.
                                # Preserve CPU workers on well-formed terminal errors.
                                restartable = False
                            if self.selection_mode != "automatic" or not restartable:
                                # Preserve a resident process for well-formed worker
                                # errors such as invalid_audio; kill protocol/crash faults.
                                if not isinstance(exc, _WorkerTranscriptionError):
                                    self.stop()
                                raise RuntimeError(str(exc)) from None

                            last_error = exc
                            attempts += 1
                            if attempts < max_attempts and time.monotonic() < request_deadline:
                                self.stop()
                                continue

                            self.stop()
                            if candidate is not None:
                                candidate_key = str(candidate.resolve())
                                self._bad_worker_paths.add(candidate_key)
                                if candidate_variant in {"cuda", "vulkan", "arm-dotprod"}:
                                    self.worker_variant = candidate_variant
                                    self._active_worker_path = candidate_key
                                    self._demote_failed_non_cpu_worker(exc)

                            failure_code = re_safe_code(str(exc))
                            self._fallback_reason = (
                                f"Automatic fallback replayed this request after "
                                f"{candidate_variant} failed ({failure_code}); "
                                f"continuing with the next eligible worker."
                            )
                            candidate_failed = True
                            break

                    if candidate_failed:
                        # A previously resident worker was the first candidate.
                        # Discover the remaining provider workers only after it fails.
                        if (
                            not candidates_loaded
                            and self.selection_mode == "automatic"
                            and candidate_variant in {
                                "cuda", "vulkan", "arm-dotprod", "cpu"
                            }
                        ):
                            for item in self.worker_candidates():
                                key = str(item.resolve())
                                if key not in seen:
                                    candidates.append(item)
                                    seen.add(key)
                            candidates_loaded = True
                        candidate_index += 1
                        continue

                    if attempts >= max_attempts:
                        candidate_index += 1
                        continue
                    break

                if time.monotonic() >= request_deadline:
                    raise RuntimeError("worker_transcription_timeout")
                if last_error is not None:
                    raise RuntimeError(str(last_error)) from None
                raise RuntimeError("persistent_worker_no_healthy_candidate")
            finally:
                with self._state_lock:
                    self._active_id = None
                    self._cancel_requested_ids.discard(request_id)

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
        # start() only leaves a process attached after a complete READY handshake;
        # the native worker emits READY after provider selection and model loading.
        # Keep initialization readiness distinct from a provider claim: provider and
        # device stay unknown until a real transcription succeeds.
        verified = bool(alive and self._inference_verified)
        samples = []
        for sample in self._performance_samples.values():
            samples.append({key: value for key, value in sample.items() if not key.startswith("_")})
        return {
            "ready": bool(alive),
            "worker_ready": bool(alive),
            "running": bool(alive),
            "engine": self.engine_version,
            "architecture": self.architecture,
            "provider": self.provider if verified else "unknown",
            "worker_variant": self.worker_variant if verified else "unknown",
            "provider_reason": self.provider_reason if verified else (
                "Provider is not advertised until this worker completes a successful inference."
            ),
            "inference_verified": verified,
            "requested_provider": self.requested_provider,
            "selection_mode": self.selection_mode,
            "fallback_reason": self._fallback_reason,
            "performance": {
                "last_transcription_ms": self._last_transcription_ms if verified else None,
                "successful_transcriptions": self._successful_transcriptions,
                "samples": samples,
            },
        }

    def worker_identity(self) -> dict[str, str]:
        """Return the identity reported by this worker's READY handshake."""
        process = self.process
        if process is None or process.poll() is not None:
            raise RuntimeError("persistent_worker_not_running")
        return {
            "architecture": self.architecture,
            "engine_version": self.engine_version,
            "provider": self.provider.lower(),
            "worker_variant": self.worker_variant,
            "device": self._active_device,
            "identity_kind": self._active_device_identity_kind,
            "identity_value": self._active_device_identity_value,
            "worker_path": self._active_worker_path,
        }

    def stop(self) -> None:
        process = self.process
        self.process = None
        if process is None:
            return
        deadline = getattr(self, "_benchmark_deadline_monotonic", None)
        try:
            if deadline is None:
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
            else:
                def remaining() -> float:
                    return max(0.0, float(deadline) - time.monotonic())
                try:
                    if process.poll() is None and process.stdin and remaining() > 0.05:
                        with self._write_lock:
                            process.stdin.write(b"SHUTDOWN\n")
                            process.stdin.flush()
                except Exception:
                    pass
                try:
                    wait_for = min(5.0, remaining())
                    if wait_for > 0.01:
                        process.wait(timeout=wait_for)
                    elif process.poll() is None:
                        raise subprocess.TimeoutExpired(process.args, wait_for)
                except subprocess.TimeoutExpired:
                    try:
                        if process.poll() is None:
                            process.terminate()
                            wait_for = min(3.0, remaining())
                            if wait_for > 0.01:
                                process.wait(timeout=wait_for)
                        if process.poll() is None:
                            process.kill()
                            wait_for = min(0.05, remaining())
                            if wait_for > 0.005:
                                process.wait(timeout=wait_for)
                    except (subprocess.TimeoutExpired, OSError):
                        try:
                            if process.poll() is None:
                                process.kill()
                        except OSError:
                            pass
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
                join_for = 1.0 if deadline is None else min(1.0, max(0.0, float(deadline) - time.monotonic()))
                if join_for > 0:
                    reader.join(timeout=join_for)


def re_safe_code(value: str) -> str:
    import re
    value = value.lower()
    return value if re.fullmatch(r"[a-z0-9_-]{1,64}", value) else "unknown_error"
