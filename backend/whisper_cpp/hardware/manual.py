"""User-started Windows/Linux manual hardware benchmark execution.

This module deliberately reuses the shared discovery, qualification coordinator,
fixture validation and real worker runner. Its persistence is independent from
the mandatory first-start benchmark.
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import sys
import uuid
from pathlib import Path
from typing import Any, Iterable, Mapping

from .benchmark import (
    _candidate_identity_valid,
    manual_candidate_timeout_seconds,
    run_first_start_benchmark,
)
from .discovery import (
    count_distinct_physical_hardware_choices,
    discover_hardware_candidates,
)
from .first_start import (
    BenchmarkAlreadyRunningError,
    MANUAL_BENCHMARK_FIXTURE_SPECS,
    _load_json_object,
    _profile_run_lock,
    _remove_run_marker,
    _sha256,
    _utc_now,
    locate_manual_fixture,
    write_json_private,
)
from .runner import run_candidate_benchmark


_RUN_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


def _worker_inventory_paths(
    worker_paths: Iterable[str | Path] | None,
) -> list[Path]:
    if worker_paths is None:
        try:
            from backend.whisper_cpp.engine import PersistentWhisperEngine
        except ImportError:
            from engine import PersistentWhisperEngine
        worker_paths = PersistentWhisperEngine.worker_candidates()

    resolved: list[Path] = []
    seen: set[str] = set()
    for raw in worker_paths:
        try:
            path = Path(raw).expanduser().resolve(strict=True)
        except (OSError, RuntimeError, TypeError, ValueError):
            continue
        if not path.is_file():
            continue
        if os.name != "nt" and not os.access(path, os.X_OK):
            continue
        key = os.path.normcase(str(path))
        if key not in seen:
            seen.add(key)
            resolved.append(path)
    return resolved


def _candidate_public_inventory(candidate: Mapping[str, Any]) -> dict[str, Any]:
    """Return UI-safe inventory data without exposing executable paths."""
    return {
        "candidate_id": str(candidate.get("candidate_id", "")),
        "provider": str(candidate.get("provider", "")).lower(),
        "device": str(candidate.get("device", ""))[:256],
        "identity_kind": str(candidate.get("identity_kind", "")),
        "identity_value": str(candidate.get("identity_value", ""))[:128],
        "eligible": candidate.get("eligible", True) is not False,
        "reason": (
            str(candidate.get("exclusion_reason", ""))[:160]
            if candidate.get("eligible", True) is False else None
        ),
        "dedicated_memory_bytes": max(
            0, int(candidate.get("dedicated_memory_bytes", 0) or 0)
        ),
        "worker_sha256": candidate.get("worker_sha256"),
    }


def discover_manual_benchmark_candidates(
    worker_paths: Iterable[str | Path] | None = None,
) -> dict[str, Any]:
    """Discover current packaged candidates for the manual selector."""
    workers = _worker_inventory_paths(worker_paths)
    candidates = discover_hardware_candidates(worker_paths=workers)

    ids = [
        str(item.get("candidate_id", ""))
        for item in candidates
    ]
    if any(not item for item in ids) or len(ids) != len(set(ids)):
        raise RuntimeError("manual_benchmark_candidate_inventory_ambiguous")

    worker_hashes: dict[str, str] = {}
    for item in candidates:
        raw_path = item.get("worker_path")
        if not raw_path:
            continue
        path = Path(str(raw_path)).expanduser().resolve(strict=True)
        if path not in workers:
            item["eligible"] = False
            item["exclusion_reason"] = "worker_not_in_packaged_inventory"
            continue
        worker_hashes[str(path)] = _sha256(path)
        item["worker_sha256"] = worker_hashes[str(path)]

    physical_count = count_distinct_physical_hardware_choices(candidates)
    return {
        "schema": 1,
        "platform": "windows" if os.name == "nt" else "linux",
        "architecture": platform.machine().lower(),
        "candidates": [
            _candidate_public_inventory(item) for item in candidates
        ],
        "physical_choice_count": physical_count,
        "show_selector": physical_count >= 3,
    }


def validate_manual_benchmark_request(
    request: Mapping[str, Any],
    discovered_candidates: Iterable[Mapping[str, Any]],
    worker_paths: Iterable[str | Path],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Resolve user-selected IDs against fresh backend discovery, never UI paths."""
    if not isinstance(request, Mapping):
        raise ValueError("manual_benchmark_request_invalid")
    if request.get("schema") != 1 or isinstance(request.get("schema"), bool):
        raise ValueError("manual_benchmark_request_schema_invalid")

    candidate_ids = request.get("candidate_ids")
    if (
        not isinstance(candidate_ids, list)
        or not candidate_ids
        or len(candidate_ids) > 256
        or any(not isinstance(item, str) or not item.strip() for item in candidate_ids)
        or len(candidate_ids) != len(set(candidate_ids))
    ):
        raise ValueError("manual_benchmark_candidate_selection_invalid")

    order = request.get("order")
    if order not in {"recommended", "gpu", "cpu"}:
        raise ValueError("manual_benchmark_order_invalid")

    fixture = request.get("fixture")
    if fixture not in MANUAL_BENCHMARK_FIXTURE_SPECS:
        raise ValueError("manual_benchmark_fixture_invalid")

    time_limit_enabled = request.get("time_limit_enabled")
    if not isinstance(time_limit_enabled, bool):
        raise ValueError("manual_benchmark_time_limit_invalid")

    # Canonicalize both sides. macOS may report the same temporary/resource
    # path as /var/... or /private/var/... depending on the caller.
    allowed_workers = {
        os.path.normcase(str(Path(path).expanduser().resolve(strict=True)))
        for path in _worker_inventory_paths(worker_paths)
    }
    if not allowed_workers:
        raise ValueError("manual_benchmark_no_packaged_workers")

    inventory = list(discovered_candidates)
    by_id: dict[str, Mapping[str, Any]] = {}
    for candidate in inventory:
        candidate_id = str(candidate.get("candidate_id", ""))
        if not candidate_id or candidate_id in by_id:
            raise ValueError("manual_benchmark_candidate_inventory_ambiguous")
        by_id[candidate_id] = candidate

    selected: list[dict[str, Any]] = []
    for candidate_id in candidate_ids:
        source = by_id.get(candidate_id)
        if source is None:
            raise ValueError("manual_benchmark_candidate_stale")

        candidate = dict(source)
        if candidate.get("eligible", True) is False:
            raise ValueError("manual_benchmark_candidate_ineligible")

        try:
            identity_valid = _candidate_identity_valid(candidate)
        except (TypeError, ValueError):
            identity_valid = False
        if not identity_valid:
            raise ValueError("manual_benchmark_candidate_identity_invalid")

        raw_worker_path = candidate.get("worker_path")
        if not isinstance(raw_worker_path, str) or not raw_worker_path.strip():
            raise ValueError("manual_benchmark_worker_path_missing")
        try:
            worker_path = Path(raw_worker_path).expanduser().resolve(strict=True)
        except (OSError, RuntimeError, ValueError):
            raise ValueError("manual_benchmark_worker_missing") from None

        worker_key = os.path.normcase(str(worker_path))
        if worker_key not in allowed_workers or not worker_path.is_file():
            raise ValueError("manual_benchmark_worker_not_packaged")
        if os.name != "nt" and not os.access(worker_path, os.X_OK):
            raise ValueError("manual_benchmark_worker_not_executable")

        candidate["worker_path"] = str(worker_path)
        candidate["worker_sha256"] = _sha256(worker_path)
        selected.append(candidate)

    run_id = request.get("run_id") or uuid.uuid4().hex
    if not isinstance(run_id, str) or not _RUN_ID_RE.fullmatch(run_id):
        raise ValueError("manual_benchmark_run_id_invalid")

    normalized = {
        "schema": 1,
        "run_id": run_id,
        "candidate_ids": list(candidate_ids),
        "order": order,
        "fixture": fixture,
        "time_limit_enabled": time_limit_enabled,
    }
    return selected, normalized


def execute_manual_hardware_benchmark(
    model_path: str | Path,
    model_id: str,
    worker_paths: Iterable[str | Path],
    request: Mapping[str, Any],
    profile_path: str | Path,
    report_path: str | Path,
    progress_path: str | Path,
) -> dict[str, Any]:
    """Execute a user-selected subset with no aggregate deadline.

    The caller owns the model-use lease for the entire call. Tauri must ensure
    its managed server is stopped before invoking this process.
    """
    model = Path(model_path).expanduser().resolve(strict=True)
    if not model.is_file():
        raise ValueError("manual_benchmark_model_invalid")

    profile = Path(profile_path).expanduser().resolve()
    report_file = Path(report_path).expanduser().resolve()
    progress_file = Path(progress_path).expanduser().resolve()
    if len({str(profile), str(report_file), str(progress_file)}) != 3:
        raise ValueError("manual_benchmark_persistence_paths_must_be_distinct")

    for path in (profile, report_file, progress_file):
        path.parent.mkdir(parents=True, exist_ok=True)

    workers = _worker_inventory_paths(worker_paths)
    run_lock_marker = profile.with_name(profile.name + ".running")

    # Discovery and identity reconciliation are repeated for every manual run.
    discovered = discover_hardware_candidates(worker_paths=workers)
    selected, normalized = validate_manual_benchmark_request(
        request, discovered, workers
    )

    fixture_name = normalized["fixture"]
    fixture = locate_manual_fixture(fixture_name)
    fixture_spec = MANUAL_BENCHMARK_FIXTURE_SPECS[fixture_name]
    model_sha256 = _sha256(model)
    fixture_sha256 = _sha256(fixture)

    worker_hashes = sorted({
        (
            str(Path(item["worker_path"]).resolve(strict=True)),
            str(item["worker_sha256"]),
        )
        for item in selected
    })
    runtime_description = {
        "platform": sys.platform,
        "platform_release": platform.release(),
        "platform_version": platform.version(),
        "machine": platform.machine(),
        "selected_candidates": [
            {
                key: item.get(key)
                for key in (
                    "candidate_id", "provider", "identity_kind",
                    "identity_value", "runtime_identity_kind",
                    "runtime_identity_value", "driver", "driver_version",
                    "hardware_signature", "worker_sha256",
                )
            }
            for item in selected
        ],
    }
    worker_build = hashlib.sha256(
        json.dumps(worker_hashes, sort_keys=True).encode("utf-8")
    ).hexdigest()
    context = {
        "os": "windows" if os.name == "nt" else "linux",
        "architecture": platform.machine().lower(),
        "runtime_fingerprint": hashlib.sha256(
            json.dumps(runtime_description, sort_keys=True).encode("utf-8")
        ).hexdigest(),
        "worker_build": worker_build,
        "model_id": str(model_id),
        "model_sha256": model_sha256,
        "fixture_sha256": fixture_sha256,
        "repetitions": 3,
        "language": "auto",
        "decode_config": {"language": "auto", "task": "transcribe"},
    }

    candidate_timeout = manual_candidate_timeout_seconds(
        fixture_name, normalized["time_limit_enabled"]
    )
    provider_first = (
        None if normalized["order"] == "recommended" else normalized["order"]
    )
    selected_ids = set(normalized["candidate_ids"])
    unselected_ids = [
        str(item.get("candidate_id", ""))
        for item in discovered
        if str(item.get("candidate_id", "")) not in selected_ids
    ]

    with _profile_run_lock(profile):
        recovered = run_lock_marker.exists()
        previous_report = _load_json_object(report_file)
        if (
            previous_report is not None
            and previous_report.get("status") == "running"
            and previous_report.get("completed") is not True
        ):
            recovered = True

        started_at = _utc_now()
        progress_payload = {
            "schema": 1,
            "run_id": normalized["run_id"],
            "phase": "starting",
            "updated_at": started_at,
        }

        def report_progress(event: Mapping[str, Any]) -> None:
            progress_payload.update(dict(event))
            progress_payload["run_id"] = normalized["run_id"]
            progress_payload["updated_at"] = _utc_now()
            write_json_private(progress_file, progress_payload)

        running_report = {
            "schema": 1,
            "mode": "manual",
            "status": "running",
            "selection_kind": "provisional",
            "completed": False,
            "run_id": normalized["run_id"],
            "started_at": started_at,
            "reason": "manual_benchmark_in_progress",
            "candidate_results": [],
            "excluded_candidates": [],
            "manual_settings": {
                **normalized,
                "fixture_id": fixture_spec["fixture_id"],
                "candidate_timeout_seconds": candidate_timeout,
                "aggregate_deadline_seconds": None,
                "selected_candidate_ids": normalized["candidate_ids"],
                "unselected_candidate_ids": unselected_ids,
            },
        }
        write_json_private(run_lock_marker, {
            "schema": 1,
            "mode": "manual",
            "status": "running",
            "run_id": normalized["run_id"],
            "pid": os.getpid(),
            "started_at": started_at,
            "report_path": str(report_file),
            "profile_path": str(profile),
            "recovered_interrupted_run": recovered,
            "candidate_ids": normalized["candidate_ids"],
            "worker_build": worker_build,
        })
        write_json_private(report_file, running_report)
        report_progress({"phase": "manual-benchmark-started"})

        completed_candidate_measurements: list[dict[str, Any]] = []

        def run_manual_candidate(
            candidate: Mapping[str, Any],
            candidate_context: Mapping[str, Any],
        ) -> Mapping[str, Any]:
            result = run_candidate_benchmark(
                candidate, candidate_context, model, fixture
            )
            if isinstance(result, Mapping):
                preserved = dict(result)
                preserved["status"] = "execution_completed_qualification_pending"
                preserved["qualification_pending"] = True
                completed_candidate_measurements.append(preserved)
            return result

        try:
            report = run_first_start_benchmark(
                selected,
                context,
                run_manual_candidate,
                cache_path=profile,
                force=True,
                progress_callback=report_progress,
                candidate_timeout_seconds=candidate_timeout,
                session_deadline_enabled=False,
                provider_first=provider_first,
            )
            report.update({
                "schema": 1,
                "mode": "manual",
                "run_id": normalized["run_id"],
                "completed": True,
                "started_at": started_at,
                "completed_at": _utc_now(),
                "recovered_interrupted_run": recovered,
                "manual_settings": running_report["manual_settings"],
                "selected_candidate_ids": normalized["candidate_ids"],
                "unselected_candidate_ids": unselected_ids,
                "selected_runtime": next(
                    (
                        {
                            key: item.get(key)
                            for key in (
                                "candidate_id", "provider", "identity_kind",
                                "identity_value", "worker_path",
                                "runtime_selector", "runtime_selector_kind",
                                "device", "dxgi_adapter_luid",
                            )
                        }
                        for item in selected
                        if item.get("candidate_id") == report.get("selected_candidate_id")
                    ),
                    None,
                ),
            })
            write_json_private(report_file, report)
            report_progress({
                "phase": "complete",
                "status": report.get("status"),
                "selection_kind": report.get("selection_kind"),
                "selected_candidate_id": report.get("selected_candidate_id"),
                "reason": report.get("reason"),
            })
            return report
        except Exception as exc:
            completed_ids = {
                str(item.get("candidate_id", ""))
                for item in completed_candidate_measurements
            }
            pending_ids = [
                str(candidate.get("candidate_id", ""))
                for candidate in selected
                if str(candidate.get("candidate_id", "")) not in completed_ids
            ]
            failure = {
                "schema": 1,
                "mode": "manual",
                "status": "failed",
                "selection_kind": "failed",
                "completed": False,
                "partial_results": bool(completed_candidate_measurements),
                "pending_candidate_ids": pending_ids,
                "run_id": normalized["run_id"],
                "started_at": started_at,
                "completed_at": _utc_now(),
                "reason": "manual_benchmark_error:" + type(exc).__name__,
                "detail": str(exc)[:180],
                "candidate_results": completed_candidate_measurements,
                "excluded_candidates": [],
                "selected_candidate_id": None,
                "selected_runtime": None,
                "manual_settings": running_report["manual_settings"],
                "selected_candidate_ids": normalized["candidate_ids"],
                "unselected_candidate_ids": unselected_ids,
            }
            write_json_private(report_file, failure)
            report_progress({
                "phase": "failed",
                "status": "failed",
                "reason": failure["reason"],
            })
            return failure
        finally:
            _remove_run_marker(run_lock_marker)
