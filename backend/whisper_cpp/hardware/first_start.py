"""First-start benchmark orchestration and private report persistence."""
from __future__ import annotations

import hashlib
import json
import math
import os
import platform
import sys
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_first_start_runtime_identity(candidates, cpu_worker_path=None):
    """Build the canonical runtime and packaged-worker identity."""
    worker_file_set = {
        str(Path(item["worker_path"]).resolve())
        for item in candidates
        if item.get("worker_path") and Path(item["worker_path"]).is_file()
    }
    if cpu_worker_path:
        cpu_path = Path(cpu_worker_path).expanduser()
        if cpu_path.is_file():
            worker_file_set.add(str(cpu_path.resolve()))

    worker_hashes = [
        (filename, _sha256(Path(filename)))
        for filename in sorted(worker_file_set)
    ]
    worker_hash_by_path = dict(worker_hashes)

    for candidate in candidates:
        raw_path = candidate.get("worker_path")
        if not raw_path:
            continue
        resolved = str(Path(raw_path).resolve())
        if resolved in worker_hash_by_path:
            candidate["worker_sha256"] = worker_hash_by_path[resolved]
        name = Path(resolved).name.lower()
        candidate["worker_variant"] = (
            "arm-dotprod" if "dotprod" in name else
            "cuda" if "cuda" in name else
            "vulkan" if "vulkan" in name else
            "cpu" if name.startswith("rdt_whisper_worker") else
            "custom"
        )
        if candidate.get("provider") == "cpu" and not candidate.get("hardware_class"):
            candidate["hardware_class"] = f"cpu:{platform.machine().lower()}"

    runtime_keys = (
        "candidate_id", "provider", "identity_kind", "identity_value",
        "driver", "driver_version", "runtime_version", "hardware_signature",
        "runtime_identity_kind", "runtime_identity_value",
        "runtime_selector_kind", "runtime_selector", "device", "worker_variant",
    )
    runtime_description = {
        "platform": sys.platform,
        "platform_release": platform.release(),
        "platform_version": platform.version(),
        "machine": platform.machine(),
        "candidates": [
            {key: item.get(key) for key in runtime_keys}
            for item in candidates
        ],
    }
    return {
        "runtime_fingerprint": hashlib.sha256(
            json.dumps(runtime_description, sort_keys=True).encode("utf-8")
        ).hexdigest(),
        "worker_build": hashlib.sha256(
            json.dumps(worker_hashes, sort_keys=True).encode("utf-8")
        ).hexdigest(),
        "candidates": candidates,
    }



MANUAL_BENCHMARK_FIXTURE_SPECS = {
    "12s": {
        "fixture_id": "rdt-user-voice-first-run-12s-v1",
        "relative_path": Path("first-run-benchmark/audio.wav"),
        "sha256": "55c7b7d3b127a50ce6943a08e0078ff3aa67f42ca926814201a819654914450a",
        "duration_seconds": 12.0294375,
    },
    "21s": {
        "fixture_id": "rdt-user-voice-manual-21s-v1",
        "relative_path": Path("benchmark/user-voice-21s.wav"),
        "sha256": "060ce7a72e16521bb90a646015b27bc714c662a155f3bd78371ed66c264d1938",
        "duration_seconds": 20.72575,
    },
}


def _benchmark_resource_roots() -> list[Path]:
    roots: list[Path] = []
    explicit = os.environ.get("RDT_BENCHMARK_RESOURCE_DIR", "").strip()
    if explicit:
        roots.append(Path(explicit).expanduser())
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        roots.append(Path(meipass))
    executable_parent = Path(sys.executable).resolve().parent
    roots.extend((
        executable_parent,
        executable_parent.parent,
        executable_parent.parent / "Resources",
        executable_parent / "resources",
        Path(__file__).resolve().parents[1],
        Path(__file__).resolve().parents[2],
        Path(__file__).resolve().parents[2] / "rdt_connect" / "src-tauri" / "resources",
        Path.cwd() / "rdt_connect" / "src-tauri" / "resources",
    ))
    unique: list[Path] = []
    seen: set[str] = set()
    for root in roots:
        key = str(root.expanduser().resolve())
        if key not in seen:
            seen.add(key)
            unique.append(Path(key))
    return unique


def locate_manual_fixture(fixture: str) -> Path:
    spec = MANUAL_BENCHMARK_FIXTURE_SPECS.get(fixture)
    if spec is None:
        raise ValueError("manual_benchmark_fixture_invalid")
    for root in _benchmark_resource_roots():
        candidate = root / spec["relative_path"]
        if not candidate.is_file():
            continue
        if _sha256(candidate) != spec["sha256"]:
            raise ValueError("benchmark_fixture_hash_mismatch")
        import wave
        try:
            with wave.open(str(candidate), "rb") as audio:
                duration = audio.getnframes() / audio.getframerate()
                valid = (
                    audio.getnchannels() == 1
                    and audio.getframerate() == 16000
                    and abs(duration - spec["duration_seconds"]) <= 0.001
                )
        except (OSError, EOFError, wave.Error):
            valid = False
        if not valid:
            raise ValueError("benchmark_fixture_format_invalid")
        return candidate.resolve(strict=True)
    raise FileNotFoundError("benchmark_fixture_missing:" + fixture)


def locate_fixture() -> Path:
    # The mandatory first-start fixture is the pinned owner recording.
    try:
        return locate_manual_fixture("12s")
    except FileNotFoundError:
        # Preserve source-only test/development compatibility. Release packaging
        # tests separately require the pinned resource to be present.
        filename = "smoke-multilingual-16k-mono.wav"
        for root in _benchmark_resource_roots():
            for candidate in (
                root / "rdt_benchmark_fixture.wav",
                root / "tests" / "fixtures" / filename,
                root / "whisper_cpp" / "tests" / "fixtures" / filename,
                root / "backend" / "whisper_cpp" / "tests" / "fixtures" / filename,
            ):
                if candidate.is_file():
                    return candidate.resolve()
    raise FileNotFoundError("bundled_benchmark_fixture_missing")



class BenchmarkAlreadyRunningError(RuntimeError):
    """Another process currently owns this benchmark profile."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_json_object(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    try:
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError:
        pass


@contextmanager
def _profile_run_lock(profile_path: Path):
    """Acquire a nonblocking OS lock, released automatically after process death."""
    profile = Path(profile_path).expanduser().resolve()
    profile.parent.mkdir(parents=True, exist_ok=True)
    lock_path = profile.with_name(profile.name + ".lock")
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    stream = os.fdopen(descriptor, "r+b", buffering=0)
    locked = False
    try:
        if os.fstat(stream.fileno()).st_size == 0:
            stream.write(b"\0")
            stream.flush()
        stream.seek(0)
        if os.name == "nt":
            import msvcrt
            try:
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise BenchmarkAlreadyRunningError("benchmark_profile_locked") from exc
        else:
            import fcntl
            try:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise BenchmarkAlreadyRunningError("benchmark_profile_locked") from exc
        locked = True
        yield
    finally:
        if locked:
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


def _remove_run_marker(path: Path) -> None:
    try:
        path.unlink()
        _fsync_directory(path.parent)
    except FileNotFoundError:
        pass


def write_json_private(path: Path, value: Mapping[str, Any]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    payload = (json.dumps(value, sort_keys=True, indent=2) + "\n").encode("utf-8")
    descriptor = None
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = None
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        _fsync_directory(path.parent)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass



def provisional_report(reason: str) -> dict[str, Any]:
    return {
        "schema": 1,
        "status": "provisional",
        "selection_kind": "provisional",
        "selected_candidate_id": None,
        "selected_runtime": None,
        "reason": reason[:160],
        "candidate_results": [],
        "excluded_candidates": [],
    }


CPU_READINESS_TIMEOUT_ENV = "RDT_BENCHMARK_CPU_READINESS_TIMEOUT_SECONDS"
FIRST_START_NEW_CANDIDATE_RESERVE_SECONDS = 15.0


def _configured_cpu_readiness_timeout_seconds() -> float | None:
    """Return an explicitly configured cap; no unmeasured default is allowed."""
    raw = os.environ.get(CPU_READINESS_TIMEOUT_ENV, "").strip()
    if not raw:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError, OverflowError):
        raise ValueError("cpu_readiness_timeout_configuration_invalid") from None
    if not math.isfinite(value) or value <= 0 or value >= 180.0:
        raise ValueError("cpu_readiness_timeout_configuration_invalid")
    return value


def _portable_cpu_readiness_candidate(
    worker_paths: Iterable[str | Path],
) -> dict[str, Any] | None:
    expected = "rdt_whisper_worker.exe" if os.name == "nt" else "rdt_whisper_worker"
    for raw_worker in worker_paths:
        worker = Path(raw_worker).expanduser()
        if worker.name.casefold() != expected.casefold():
            continue
        try:
            worker = worker.resolve(strict=True)
        except OSError:
            continue
        if not worker.is_file() or not os.access(worker, os.X_OK):
            continue
        return {
            "candidate_id": "cpu-readiness-portable",
            "provider": "cpu",
            "device": "CPU (portable fallback)",
            "identity_kind": "cpu",
            "identity_value": "host",
            "worker_path": str(worker),
            "runtime_selector": None,
            "runtime_selector_kind": None,
            "eligible": True,
            "fallback_usable": False,
        }
    return None


def _usable_successful_profile(report: Mapping[str, Any]) -> bool:
    if (
        report.get("completed") is not True
        or report.get("completion_schema") != 1
        or str(report.get("status") or "").lower()
            in {"failed", "incomplete", "running"}
    ):
        return False
    profile = report.get("engine_profile")
    selected = report.get("selected_runtime")
    if (
        not isinstance(profile, dict)
        or profile.get("schema") != 1
        or profile.get("completed") is not True
        or not isinstance(selected, dict)
        or str(profile.get("selection_kind") or "").lower() in {"", "none"}
    ):
        return False
    if str(selected.get("provider") or "").lower() not in {"cpu", "cuda", "vulkan"}:
        return False
    if not str(selected.get("identity_kind") or "").strip():
        return False
    if not str(selected.get("identity_value") or "").strip():
        return False
    if not str(selected.get("worker_path") or "").strip():
        return False
    digest = str(selected.get("worker_sha256") or "").lower()
    if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
        return False
    for key in ("model_sha256", "runtime_fingerprint", "worker_build", "fixture_sha256"):
        value = str(profile.get(key) or "").lower()
        if len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
            return False
    return True


def _persist_last_successful_engine_profile(
    report_file: Path, report: Mapping[str, Any]
) -> bool:
    if not _usable_successful_profile(report):
        return False
    last_good = report_file.with_name(
        f"{report_file.stem}-last-successful{report_file.suffix}"
    )
    write_json_private(last_good, dict(report))
    return True


def _best_partial_qualified_candidate(
    candidates: list[Mapping[str, Any]], results: Any
) -> Mapping[str, Any] | None:
    """Choose the fastest individually validated candidate, not an overall winner."""
    import math

    by_id = {
        str(item.get("candidate_id")): item
        for item in candidates
        if item.get("candidate_id")
    }
    qualified = []
    if not isinstance(results, list):
        return None

    for result in results:
        if not isinstance(result, dict):
            continue
        if result.get("status") != "qualified" or result.get("transcript_valid") is not True:
            continue
        if not isinstance(result.get("segment_count"), int) or result["segment_count"] < 1:
            continue
        if not isinstance(result.get("repetitions"), int) or result["repetitions"] < 2:
            continue
        try:
            median = float(result.get("median_inference_ms"))
        except (TypeError, ValueError):
            continue
        if not math.isfinite(median) or median <= 0:
            continue

        candidate = by_id.get(str(result.get("candidate_id") or ""))
        if candidate is None:
            continue
        digest = str(candidate.get("worker_sha256") or "").lower()
        if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            continue
        if not str(candidate.get("identity_kind") or "").strip():
            continue
        if not str(candidate.get("identity_value") or "").strip():
            continue
        qualified.append((median, candidate))

    return min(qualified, key=lambda item: item[0])[1] if qualified else None


def execute_first_start_benchmark(
    model_path: Path,
    model_id: str,
    worker_paths: Iterable[str | Path],
    profile_path: Path,
    report_path: Path,
) -> dict[str, Any]:
    """Run a locked first-start session with durable interrupted-run recovery."""
    from .benchmark import _cache_key, run_first_start_benchmark
    from .discovery import discover_hardware_candidates
    from .runner import run_candidate_benchmark, run_cpu_readiness

    session_deadline = time.monotonic() + 180.0
    model = Path(model_path).expanduser().resolve(strict=True)
    profile = Path(profile_path).expanduser().resolve()
    report_file = Path(report_path).expanduser().resolve()
    profile.parent.mkdir(parents=True, exist_ok=True)
    report_file.parent.mkdir(parents=True, exist_ok=True)
    last_good = report_file.with_name(
        f"{report_file.stem}-last-successful{report_file.suffix}"
    )
    # Migrate an older completed report before the latest-attempt file is replaced.
    if not last_good.exists() and report_file.is_file():
        try:
            previous = json.loads(report_file.read_text(encoding="utf-8"))
            if isinstance(previous, dict) and _usable_successful_profile(previous):
                write_json_private(last_good, previous)
        except (OSError, UnicodeError, json.JSONDecodeError):
            pass
    marker = profile.with_name(profile.name + ".running")
    requested_run_id = os.environ.get("RDT_BENCHMARK_RUN_ID", "").strip()
    valid_run_id = (
        bool(requested_run_id)
        and len(requested_run_id) <= 128
        and all(char.isalnum() or char in "-_" for char in requested_run_id)
    )
    run_id = requested_run_id if valid_run_id else uuid.uuid4().hex
    worker_paths_list = list(worker_paths)

    # Lock covers discovery, inference, profile-cache write and report publish.
    with _profile_run_lock(profile):
        recovered = marker.exists()
        previous_report = _load_json_object(report_file)
        if (
            previous_report is not None
            and previous_report.get("status") == "running"
            and previous_report.get("completed") is not True
        ):
            recovered = True

        started_at = _utc_now()
        progress_raw = os.environ.get("RDT_BENCHMARK_PROGRESS_PATH", "").strip()
        progress_file = Path(progress_raw).expanduser().resolve() if progress_raw else None

        def report_progress(event: Mapping[str, Any]) -> None:
            if progress_file is None:
                return
            payload = {
                "schema": 1,
                "run_id": run_id,
                "updated_at": _utc_now(),
                **dict(event),
            }
            try:
                write_json_private(progress_file, payload)
            except OSError:
                pass

        report_progress({"phase": "cpu-readiness"})
        write_json_private(marker, {
            "schema": 1,
            "status": "preparing",
            "run_id": run_id,
            "pid": os.getpid(),
            "started_at": started_at,
            "report_path": str(report_file),
            "profile_path": str(profile),
            "recovered_interrupted_run": recovered,
        })
        write_json_private(report_file, {
            "schema": 1,
            "status": "running",
            "selection_kind": "provisional",
            "completed": False,
            "run_id": run_id,
            "reason": "benchmark_in_progress",
            "candidate_results": [],
            "excluded_candidates": [],
        })

        try:
            fixture = locate_fixture()
            cpu_candidate = _portable_cpu_readiness_candidate(worker_paths_list)
            cpu_readiness = {
                "verified": False,
                "status": "unavailable" if cpu_candidate is None else "unconfigured",
                "timeout_calibrated": False,
                "calibration_status": "requires_slowest_supported_cpu_measurement",
                "timeout_seconds": None,
                "worker_path": cpu_candidate["worker_path"] if cpu_candidate else None,
                "reason": (
                    "portable_cpu_worker_not_packaged"
                    if cpu_candidate is None else "cpu_readiness_timeout_not_configured"
                ),
            }

            try:
                readiness_timeout = _configured_cpu_readiness_timeout_seconds()
            except Exception as exc:
                readiness_timeout = None
                cpu_readiness.update({
                    "status": "failed",
                    "reason": "cpu_readiness_timeout_configuration:" + type(exc).__name__,
                })

            if cpu_candidate is not None and readiness_timeout is not None:
                readiness_started = time.monotonic()
                readiness_deadline = readiness_started + readiness_timeout
                latest_safe_deadline = (
                    session_deadline - FIRST_START_NEW_CANDIDATE_RESERVE_SECONDS
                )
                if readiness_deadline > latest_safe_deadline:
                    cpu_readiness.update({
                        "status": "failed",
                        "reason": "cpu_readiness_cap_conflicts_with_session_reserve",
                        "timeout_seconds": readiness_timeout,
                    })
                else:
                    cpu_readiness["timeout_seconds"] = readiness_timeout
                    report_progress({
                        "phase": "cpu-readiness-start",
                        "provider": "cpu",
                        "timeout_seconds": readiness_timeout,
                    })
                    try:
                        readiness_result = run_cpu_readiness(
                            cpu_candidate,
                            {
                                "model_id": str(model_id),
                                "architecture": platform.machine().lower(),
                                "model_sha256": _sha256(model),
                                "fixture_sha256": _sha256(fixture),
                                "language": "auto",
                                "_benchmark_deadline_monotonic": session_deadline,
                                "_cpu_readiness_deadline_monotonic": readiness_deadline,
                            },
                            model,
                            fixture,
                        )
                        cpu_readiness.update(readiness_result)
                        cpu_readiness.update({
                            "verified": True,
                            "status": "verified",
                            "timeout_calibrated": False,
                            "calibration_status": (
                                "requires_slowest_supported_cpu_measurement"
                            ),
                        })
                        cpu_candidate["fallback_usable"] = True
                        report_progress({
                            "phase": "cpu-readiness-complete",
                            "provider": "cpu",
                            "verified": True,
                            "total_ms": cpu_readiness.get("total_ms"),
                        })
                    except Exception as exc:
                        cpu_readiness.update({
                            "verified": False,
                            "status": "failed",
                            "reason": "cpu_readiness_failed:" + type(exc).__name__,
                        })
                        report_progress({
                            "phase": "cpu-readiness-failed",
                            "provider": "cpu",
                            "verified": False,
                            "reason": cpu_readiness["reason"],
                        })
            else:
                report_progress({
                    "phase": "cpu-readiness-not-verified",
                    "provider": "cpu",
                    "verified": False,
                    "reason": cpu_readiness["reason"],
                })

            # Portable CPU is not eligible as a fallback until the explicit
            # readiness inference above has passed. Other CPU workers such as
            # ARM dotprod remain independent comparison candidates.
            candidates = discover_hardware_candidates(
                worker_paths=worker_paths_list,
                deadline_monotonic=session_deadline,
            )
            portable_name = (
                "rdt_whisper_worker.exe" if os.name == "nt" else "rdt_whisper_worker"
            )
            for item in candidates:
                worker_name = Path(str(item.get("worker_path", ""))).name.casefold()
                if (
                    str(item.get("provider", "")).lower() == "cpu"
                    and worker_name == portable_name.casefold()
                    and not cpu_readiness["verified"]
                ):
                    item["eligible"] = False
                    item["fallback_usable"] = False
                    item["exclusion_reason"] = "cpu_readiness_not_verified"
                elif (
                    str(item.get("provider", "")).lower() == "cpu"
                    and worker_name == portable_name.casefold()
                ):
                    item["fallback_usable"] = True

            report_progress({
                "phase": "hardware-discovered",
                "eligible_count": sum(1 for item in candidates if item.get("eligible", True)),
                "discovered_count": len(candidates),
            })

            cpu_worker_path = (
                cpu_candidate.get("worker_path")
                if isinstance(cpu_candidate, dict) else None
            )
            runtime_identity = build_first_start_runtime_identity(
                candidates, cpu_worker_path
            )
            context = {
                "os": "windows" if os.name == "nt" else "linux",
                "architecture": platform.machine().lower(),
                "runtime_fingerprint": runtime_identity["runtime_fingerprint"],
                "worker_build": runtime_identity["worker_build"],
                "model_id": str(model_id),
                "model_sha256": _sha256(model),
                "fixture_sha256": _sha256(fixture),
                "repetitions": 3,
                "language": "auto",
                "decode_config": {"language": "auto", "task": "transcribe"},
            }
            cache_key = _cache_key(context, candidates)
            context["_benchmark_deadline_monotonic"] = session_deadline

            write_json_private(marker, {
                "schema": 1,
                "status": "running",
                "run_id": run_id,
                "pid": os.getpid(),
                "started_at": started_at,
                "report_path": str(report_file),
                "profile_path": str(profile),
                "recovered_interrupted_run": recovered,
                "context": context,
                "cache_key": cache_key,
                "candidate_ids": [
                    str(item.get("candidate_id", "")) for item in candidates
                ],
                "worker_hashes": worker_hashes,
                "cpu_readiness": cpu_readiness,
            })

            report = run_first_start_benchmark(
                candidates,
                context,
                lambda candidate, ctx: run_candidate_benchmark(
                    candidate, ctx, model, fixture
                ),
                cache_path=profile,
                force=recovered,
                progress_callback=report_progress,
            )

            selected_id = report.get("selected_candidate_id")
            selected = next(
                (
                    item for item in candidates
                    if item.get("candidate_id") == selected_id
                ),
                None,
            )
            is_incomplete = report.get("status") in {"incomplete", "failed"}
            if (
                selected is None
                and not selected_id
                and cpu_candidate is not None
                and cpu_readiness.get("verified") is True
                and report.get("status") in {"failed", "incomplete"}
            ):
                selected = cpu_candidate
                selected_id = cpu_candidate["candidate_id"]
                report["selected_candidate_id"] = selected_id
                if not is_incomplete:
                    report["status"] = "cpu-fallback"
                report["selection_kind"] = "cpu-readiness-fallback"
                report["reason"] = (
                    "no_comparative_candidate_qualified_using_verified_cpu_readiness"
                )

            report["cpu_readiness"] = cpu_readiness
            if selected is not None:
                selected_digest = selected.get("worker_sha256")
                if (
                    not isinstance(selected_digest, str)
                    or len(selected_digest) != 64
                    or any(
                        character not in "0123456789abcdefABCDEF"
                        for character in selected_digest
                    )
                ):
                    raise RuntimeError(
                        "selected_worker_sha256_missing_or_invalid"
                    )
            if is_incomplete:
                partial = _best_partial_qualified_candidate(
                    candidates, report.get("candidate_results")
                )
                if partial is not None:
                    selected = partial
                    selected_id = str(partial.get("candidate_id") or "")
                    report["selected_candidate_id"] = selected_id
                    report["selection_kind"] = "partial-provisional"
                    report["partial_selection_reason"] = (
                        "Fastest individually qualified candidate from an incomplete "
                        "comparison; not a measured overall winner."
                    )

            report["selected_runtime"] = (
                {
                    key: selected.get(key)
                    for key in (
                        "candidate_id", "provider", "identity_kind",
                        "identity_value", "worker_path", "worker_sha256",
                        "worker_variant", "worker_version", "driver",
                        "driver_version", "runtime_version",
                        "runtime_selector", "runtime_selector_kind", "device",
                        "dxgi_adapter_luid",
                    )
                }
                if selected is not None else None
            )
            profile_data = (
                {
                    "schema": 1,
                    "completed": True,
                    "selection_kind": str(report.get("selection_kind") or ""),
                    "model_id": str(context["model_id"]),
                    "model_sha256": str(context["model_sha256"]),
                    "os": str(context["os"]),
                    "architecture": str(context["architecture"]),
                    "runtime_fingerprint": str(context["runtime_fingerprint"]),
                    "worker_build": str(context["worker_build"]),
                    "fixture_sha256": str(context["fixture_sha256"]),
                    "selected_runtime": report["selected_runtime"],
                }
                if selected is not None else None
            )
            report["engine_profile"] = (
                profile_data if profile_data is not None and not is_incomplete else None
            )
            report["partial_engine_profile"] = (
                profile_data if profile_data is not None and is_incomplete else None
            )
            report.update({
                "run_id": run_id,
                "completed": not is_incomplete,
                "recovered_interrupted_run": recovered,
            })
            if is_incomplete:
                report["completion_schema"] = None
                report["pending_candidate_ids"] = list(
                    report.get("pending_candidate_ids", [])
                )
            else:
                report["completion_schema"] = 1
                report["completed_at"] = _utc_now()
            write_json_private(report_file, report)
            if not is_incomplete:
                try:
                    _persist_last_successful_engine_profile(report_file, report)
                except OSError as exc:
                    print(
                        "[RDT Connect] Could not update last-successful selection; "
                        f"the previous one is preserved ({type(exc).__name__})."
                    )
            report_progress({
                "phase": "incomplete" if is_incomplete else "complete",
                "status": report.get("status"),
                "selection_kind": report.get("selection_kind"),
                "selected_candidate_id": report.get("selected_candidate_id"),
                "pending_count": len(report.get("pending_candidate_ids", [])),
                "reason": report.get("reason"),
                "cpu_readiness_verified": bool(cpu_readiness.get("verified")),
            })
            if not is_incomplete:
                _remove_run_marker(marker)
            return report

        except Exception as exc:
            failure = provisional_report(
                "benchmark_orchestration_error:" + type(exc).__name__
            )
            failure.update({
                "status": "failed",
                "selection_kind": "none",
                "run_id": run_id,
                "completed": False,
                "completion_schema": None,
                "completed_at": _utc_now(),
                "recovered_interrupted_run": recovered,
            })
            write_json_private(report_file, failure)
            report_progress({
                "phase": "failed",
                "status": "failed",
                "selection_kind": "none",
                "completed": False,
                "reason": failure["reason"],
            })
            _remove_run_marker(marker)
            return failure
