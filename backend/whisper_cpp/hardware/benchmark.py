"""Deterministic Windows/Linux hardware benchmark orchestration.

The platform adapter supplies discovered candidates and a real isolated-worker
runner. This module owns preselection, qualification, winner selection, cache
identity, and honest provisional fallback reporting.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import statistics
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping


_PROVIDER_PRIORITY = {"cuda": 3, "vulkan": 2, "cpu": 1}
_BDF_RE = re.compile(r"^[0-9a-f]{4}:[0-9a-f]{2}:[0-9a-f]{2}\.[0-7]$")


def _provider(value: object) -> str:
    value = str(value).strip().lower()
    if value in {"cpu", "cuda", "vulkan"}:
        return value
    raise ValueError("benchmark_provider_invalid")


def _candidate_identity_valid(candidate: Mapping[str, Any]) -> bool:
    """Accept only stable, provider-appropriate hardware identities."""
    provider = _provider(candidate.get("provider", ""))
    kind = str(candidate.get("identity_kind", ""))
    value = str(candidate.get("identity_value", ""))

    if provider == "cpu":
        return kind == "cpu" and value == "host"

    if kind == "pci-bdf":
        return bool(re.fullmatch(
            r"[0-9a-f]{4}:[0-9a-f]{2}:[0-9a-f]{2}\.[0-7]", value
        ))

    if kind == "dxgi-luid":
        # Windows DXGI LUID: two canonical 32-bit hexadecimal components.
        return bool(re.fullmatch(r"[0-9a-f]{8}:[0-9a-f]{8}", value))

    if provider == "cuda" and kind == "cuda-uuid":
        return bool(re.fullmatch(
            r"GPU-[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", value,
            flags=re.IGNORECASE,
        ))

    return False


_RANKING_HISTORY_LIMIT = 128
_RANKING_HISTORY_MAX_AGE_DAYS = 90
_RANKING_HISTORY_HALF_LIFE_DAYS = 30

_RANKING_CONTEXT_KEYS = (
    "os",
    "architecture",
    "runtime_fingerprint",
    "worker_build",
    "model_id",
    "model_sha256",
    "fixture_sha256",
    "driver_version",
    "backend_version",
    "runtime_version",
    "repetitions",
    "language",
    "decode_config",
)


def _ranking_context(context: Mapping[str, Any]) -> dict[str, Any]:
    """Keep only stable, JSON-safe benchmark identity inputs."""
    result = {}
    for key in _RANKING_CONTEXT_KEYS:
        if key in context:
            result[key] = context[key]
    result.setdefault("language", "auto")
    result.setdefault("decode_config", {})
    return result


def _history_context_matches(
    entry: Mapping[str, Any], context: Mapping[str, Any]
) -> bool:
    saved = entry.get("context")
    if not isinstance(saved, dict):
        return False
    defaults = {"language": "auto", "decode_config": {}}
    for key in (
        "os",
        "architecture",
        "model_id",
        "model_sha256",
        "fixture_sha256",
        "language",
        "decode_config",
    ):
        if saved.get(key, defaults.get(key)) != context.get(
            key, defaults.get(key)
        ):
            return False
    return True


def _history_age_weight(entry: Mapping[str, Any]) -> float | None:
    observed = entry.get("observed_at")
    if not isinstance(observed, str) or not observed:
        return None
    try:
        parsed = datetime.fromisoformat(observed.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        age_days = max(
            0.0,
            (datetime.now(timezone.utc) - parsed.astimezone(timezone.utc))
            .total_seconds() / 86400.0,
        )
    except (TypeError, ValueError, OverflowError):
        return None
    if age_days > _RANKING_HISTORY_MAX_AGE_DAYS:
        return None
    return math.pow(0.5, age_days / _RANKING_HISTORY_HALF_LIFE_DAYS)


def _positive_finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(number) or number <= 0:
        return None
    return number


def _weighted_median(values: list[tuple[float, float]]) -> float | None:
    valid = [
        (float(value), float(weight))
        for value, weight in values
        if math.isfinite(float(value))
        and float(value) > 0
        and math.isfinite(float(weight))
        and float(weight) > 0
    ]
    if not valid:
        return None
    valid.sort(key=lambda item: item[0])
    halfway = sum(weight for _, weight in valid) / 2.0
    cumulative = 0.0
    for value, weight in valid:
        cumulative += weight
        if cumulative >= halfway:
            return value
    return valid[-1][0]


def _history_tier(
    candidate: Mapping[str, Any],
    context: Mapping[str, Any],
    entry: Mapping[str, Any],
) -> int | None:
    """Return the strongest compatible history scope; lower is stronger."""
    if entry.get("status") not in {"qualified", "disqualified"}:
        return None
    if not _history_context_matches(entry, context):
        return None

    provider = _provider(candidate.get("provider", ""))
    if str(entry.get("provider", "")).lower() != provider:
        return None

    candidate_hash = str(candidate.get("worker_sha256") or "").lower()
    history_hash = str(entry.get("worker_sha256") or "").lower()
    same_worker = bool(candidate_hash and history_hash and candidate_hash == history_hash)

    same_physical_device = (
        str(entry.get("identity_kind", "")) == str(candidate.get("identity_kind", ""))
        and str(entry.get("identity_value", "")).lower()
        == str(candidate.get("identity_value", "")).lower()
    )

    saved_context = entry.get("context", {})
    exact_runtime = (
        isinstance(saved_context, dict)
        and saved_context.get("runtime_fingerprint")
        == context.get("runtime_fingerprint")
        and saved_context.get("worker_build") == context.get("worker_build")
        and str(entry.get("driver_version") or "")
        == str(candidate.get("driver_version") or "")
        and str(entry.get("runtime_version") or context.get("runtime_version") or "")
        == str(candidate.get("runtime_version") or context.get("runtime_version") or "")
    )

    if same_physical_device and same_worker and exact_runtime:
        return 0

    candidate_class = str(candidate.get("hardware_class") or "")
    history_class = str(entry.get("hardware_class") or "")
    same_class = bool(candidate_class and history_class and candidate_class == history_class)

    if same_class and same_worker:
        return 1
    if same_class:
        return 2
    return None


def _global_history_baseline(
    history: list[Mapping[str, Any]], context: Mapping[str, Any]
) -> float | None:
    """Provider-neutral model/OS baseline; never assigns a provider advantage."""
    samples = []
    for entry in history:
        if entry.get("status") != "qualified":
            continue
        if not _history_context_matches(entry, context):
            continue
        weight = _history_age_weight(entry)
        value = _positive_finite(
            entry.get("attempt_ms") or entry.get("total_ms")
        )
        if weight is not None and value is not None:
            samples.append((value, weight))
    return _weighted_median(samples)


def _ranking_estimate(
    candidate: Mapping[str, Any],
    context: Mapping[str, Any],
    history: list[Mapping[str, Any]],
) -> dict[str, Any]:
    """Estimate end-to-end time-to-qualified-result; never qualify from history."""
    compatible = []
    for entry in history:
        tier = _history_tier(candidate, context, entry)
        weight = _history_age_weight(entry)
        if tier is not None and weight is not None:
            compatible.append((tier, entry, weight))

    baseline = _global_history_baseline(history, context)
    if compatible:
        strongest_tier = min(item[0] for item in compatible)
        samples = [
            (entry, weight)
            for tier, entry, weight in compatible
            if tier == strongest_tier
        ]
        attempt_weight = sum(weight for _, weight in samples)
        success_weight = sum(
            weight for entry, weight in samples
            if entry.get("status") == "qualified"
        )
        # A neutral Beta(1,1) prior avoids claiming certainty from tiny samples.
        probability = (1.0 + success_weight) / (2.0 + attempt_weight)
        successful_times = []
        for entry, weight in samples:
            if entry.get("status") != "qualified":
                continue
            value = _positive_finite(
                entry.get("attempt_ms") or entry.get("total_ms")
            )
            if value is not None:
                successful_times.append((value, weight))

        own_median = _weighted_median(successful_times)
        confidence = min(1.0, attempt_weight / 4.0)
        if own_median is not None:
            expected_ms = (
                own_median
                if baseline is None
                else confidence * own_median + (1.0 - confidence) * baseline
            )
        else:
            expected_ms = baseline

        tier_names = {
            0: "exact_candidate_device_provider_worker_context",
            1: "same_device_class_provider_worker_model",
            2: "same_device_class_provider_model",
        }
        expected_cost = (
            expected_ms / max(0.05, probability)
            if expected_ms is not None else 1.0e300
        )
        return {
            "evidence": tier_names[strongest_tier],
            "sample_count": len(samples),
            "confidence": round(confidence, 4),
            "qualification_probability": round(probability, 4),
            "expected_qualification_ms": (
                round(expected_ms, 3) if expected_ms is not None else None
            ),
            "expected_cost_ms": round(expected_cost, 3),
        }

    # Future discovery code may supply a defensible estimate. Accept it only
    # as an explicit pair; never infer a timing advantage from provider name.
    hint = candidate.get("ranking_estimate")
    if isinstance(hint, Mapping):
        expected_ms = _positive_finite(hint.get("expected_total_ms"))
        try:
            probability = float(hint.get("qualification_probability"))
        except (TypeError, ValueError, OverflowError):
            probability = math.nan
        if (
            expected_ms is not None
            and math.isfinite(probability)
            and 0.0 < probability <= 1.0
        ):
            return {
                "evidence": str(hint.get("source") or "explicit_candidate_estimate"),
                "sample_count": int(max(0, hint.get("sample_count", 0))),
                "confidence": min(1.0, max(0.0, float(hint.get("confidence", 0.0)))),
                "qualification_probability": round(probability, 4),
                "expected_qualification_ms": round(expected_ms, 3),
                "expected_cost_ms": round(expected_ms / probability, 3),
            }

    # An otherwise unseen path receives the same neutral model/OS prior as
    # every other unseen path. No CUDA/Vulkan/GPU/CPU ranking is fabricated.
    if baseline is not None:
        probability = 0.5
        return {
            "evidence": "neutral_same_model_os_history",
            "sample_count": 0,
            "confidence": 0.1,
            "qualification_probability": probability,
            "expected_qualification_ms": round(baseline, 3),
            "expected_cost_ms": round(baseline / probability, 3),
        }

    return {
        "evidence": "no_compatible_performance_history",
        "sample_count": 0,
        "confidence": 0.0,
        "qualification_probability": None,
        "expected_qualification_ms": None,
        "expected_cost_ms": None,
    }


def _stable_candidate_key(candidate: Mapping[str, Any]) -> tuple[str, ...]:
    """Tie-break with stable device/provider/worker identity, not enumeration."""
    worker_name = Path(str(candidate.get("worker_path") or "")).name
    return (
        str(candidate.get("identity_kind", "")),
        str(candidate.get("identity_value", "")).lower(),
        _provider(candidate.get("provider", "")),
        str(candidate.get("worker_sha256") or "").lower(),
        str(candidate.get("hardware_class") or ""),
        str(candidate.get("worker_variant") or ""),
        str(candidate.get("worker_version") or ""),
        worker_name.lower(),
    )


def _candidate_key(
    candidate: Mapping[str, Any],
    estimate: Mapping[str, Any] | None = None,
) -> tuple[Any, ...]:
    """Predicted mandatory-benchmark order, separate from safe fallback."""
    profile = estimate or {}
    cost = profile.get("expected_cost_ms")
    try:
        score = float(cost) if cost is not None else math.inf
    except (TypeError, ValueError, OverflowError):
        score = math.inf
    if not math.isfinite(score):
        score = math.inf
    return (score, _stable_candidate_key(candidate))


def _fallback_candidate_key(candidate: Mapping[str, Any]) -> tuple[Any, ...]:
    """Legacy deterministic fallback only; never used to order comparisons."""
    provider = _provider(candidate["provider"])
    return (
        -int(candidate.get("capability_score", 0)),
        -_PROVIDER_PRIORITY[provider],
        -max(0, int(candidate.get("dedicated_memory_bytes", 0))),
        str(candidate["candidate_id"]),
    )


def _public_candidate(candidate: Mapping[str, Any]) -> dict[str, Any]:
    public = {
        "candidate_id": str(candidate["candidate_id"]),
        "provider": _provider(candidate["provider"]),
        "device": str(candidate.get("device", ""))[:256],
        "identity_kind": str(candidate.get("identity_kind", "")),
        "identity_value": str(candidate.get("identity_value", ""))[:128],
        "capability_score": int(candidate.get("capability_score", 0)),
        "dedicated_memory_bytes": max(
            0, int(candidate.get("dedicated_memory_bytes", 0))
        ),
    }
    for key in (
        "worker_sha256",
        "hardware_class",
        "vendor_id",
        "device_id",
        "hardware_signature",
        "driver",
        "driver_version",
        "runtime_version",
        "worker_variant",
        "worker_version",
    ):
        value = candidate.get(key)
        if isinstance(value, (str, int, float, bool)):
            public[key] = value
    return public


def _load_ranking_history(path: Path | None) -> list[dict[str, Any]]:
    if path is None or not path.is_file():
        return []
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return []
    if not isinstance(value, dict):
        return []
    history = value.get("ranking_history")
    if not isinstance(history, list):
        return []
    return [
        dict(entry)
        for entry in history[-_RANKING_HISTORY_LIMIT:]
        if isinstance(entry, dict)
    ]


def _merge_ranking_history(
    previous: list[Mapping[str, Any]],
    candidates: list[Mapping[str, Any]],
    results: list[Mapping[str, Any]],
    context: Mapping[str, Any],
) -> list[dict[str, Any]]:
    by_id = {
        str(candidate.get("candidate_id", "")): candidate
        for candidate in candidates
    }
    merged = [dict(entry) for entry in previous if isinstance(entry, Mapping)]
    known_ids = {
        str(entry.get("history_id", ""))
        for entry in merged
        if entry.get("history_id")
    }
    saved_context = _ranking_context(context)

    for result in results:
        status = result.get("status")
        if status not in {"qualified", "disqualified"}:
            continue
        candidate_id = str(result.get("candidate_id", ""))
        candidate = by_id.get(candidate_id)
        if candidate is None:
            continue

        public = _public_candidate(candidate)
        observed_at = result.get("observed_at")
        if not isinstance(observed_at, str) or not observed_at:
            observed_at = datetime.now(timezone.utc).isoformat()

        attempt_ms = _positive_finite(result.get("attempt_ms"))
        if attempt_ms is None and status == "qualified":
            attempt_ms = _positive_finite(result.get("total_ms"))

        entry = {
            **public,
            "status": status,
            "observed_at": observed_at,
            "attempt_ms": round(attempt_ms, 3) if attempt_ms is not None else None,
            "total_ms": (
                round(attempt_ms, 3)
                if status == "qualified" and attempt_ms is not None
                else None
            ),
            "context": saved_context,
            "driver_version": candidate.get("driver_version"),
            "runtime_version": candidate.get(
                "runtime_version", context.get("runtime_version")
            ),
        }
        identity_payload = {
            key: entry.get(key)
            for key in (
                "provider",
                "identity_kind",
                "identity_value",
                "worker_sha256",
                "hardware_class",
                "status",
                "observed_at",
                "attempt_ms",
                "context",
            )
        }
        entry["history_id"] = hashlib.sha256(
            json.dumps(
                identity_payload, sort_keys=True, separators=(",", ":"), default=str
            ).encode("utf-8")
        ).hexdigest()
        if entry["history_id"] not in known_ids:
            merged.append(entry)
            known_ids.add(entry["history_id"])

    merged.sort(key=lambda entry: str(entry.get("observed_at", "")))
    return merged[-_RANKING_HISTORY_LIMIT:]


def _persist_ranking_history(
    path: Path | None, history: list[Mapping[str, Any]]
) -> str | None:
    """Update history in the existing profile file, preserving any cached report."""
    if path is None:
        return None

    existing: dict[str, Any] = {}
    if path.is_file():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(loaded, dict):
                return "ranking_history_profile_not_object"
            existing = loaded
        except (OSError, UnicodeError, json.JSONDecodeError):
            return "ranking_history_profile_unreadable"

    existing["schema"] = existing.get("schema", 1)
    existing["ranking_history"] = [dict(item) for item in history]
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.history.tmp")
    descriptor = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            descriptor = None
            json.dump(existing, stream, sort_keys=True, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        return None
    except (OSError, TypeError, ValueError) as exc:
        if descriptor is not None:
            os.close(descriptor)
        try:
            temporary.unlink()
        except OSError:
            pass
        return f"ranking_history_persistence_failed:{type(exc).__name__}"


def _cache_key(context: Mapping[str, Any], candidates: list[Mapping[str, Any]]) -> str:
    hardware = sorted(
        (
            str(candidate.get("candidate_id", "")),
            str(candidate.get("provider", "")).lower(),
            str(candidate.get("identity_kind", "")),
            str(candidate.get("identity_value", "")),
            str(candidate.get("worker_path", "")),
            str(candidate.get("worker_sha256", "")).lower(),
            str(candidate.get("worker_variant", "")),
            str(candidate.get("driver", "")),
            str(candidate.get("driver_version", "")),
            str(candidate.get("runtime_version", "")),
            str(candidate.get("runtime_selector_kind", "")),
            str(candidate.get("runtime_selector", "")),
            str(candidate.get("device", "")),
            str(candidate.get("dxgi_adapter_luid", "")),
        )
        for candidate in candidates
        if candidate.get("eligible", True)
    )
    payload = {
        "os": context["os"],
        "architecture": context["architecture"],
        "runtime_fingerprint": context["runtime_fingerprint"],
        "worker_build": context["worker_build"],
        "model_id": context["model_id"],
        "model_sha256": context["model_sha256"],
        "fixture_sha256": context["fixture_sha256"],
        "driver_version": str(context.get("driver_version", "unknown")),
        "backend_version": str(context.get("backend_version", "unknown")),
        "runtime_version": str(context.get("runtime_version", "unknown")),
        "repetitions": int(context.get("repetitions", 3)),
        "language": str(context.get("language", "auto")),
        "decode_config": context.get("decode_config", {}),
        "hardware": hardware,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _emit_progress(callback, phase: str, **details) -> None:
    if callback is None:
        return
    try:
        callback({"phase": phase, **details})
    except Exception:
        # Progress reporting must never change benchmark qualification.
        pass


def _cached_candidate_matches_current(
    selected: Mapping[str, Any],
    current: Mapping[str, Any],
) -> bool:
    """Require a cached winner to identify the exact current worker and device."""
    exact_fields = (
        "candidate_id",
        "identity_kind",
        "identity_value",
        "worker_path",
        "worker_variant",
        "driver",
        "driver_version",
        "runtime_version",
        "runtime_selector_kind",
        "runtime_selector",
        "device",
        "dxgi_adapter_luid",
    )
    for key in exact_fields:
        if str(selected.get(key) or "") != str(current.get(key) or ""):
            return False

    return (
        str(selected.get("provider") or "").lower()
        == str(current.get("provider") or "").lower()
        and str(selected.get("worker_sha256") or "").lower()
        == str(current.get("worker_sha256") or "").lower()
    )


def _read_cache(
    path: Path | None,
    key: str,
    candidates: list[Mapping[str, Any]],
) -> dict[str, Any] | None:
    if path is None or not path.is_file():
        return None
    try:
        cached = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(cached, dict):
        return None
    if (
        cached.get("schema") != 1
        or cached.get("completed") is not True
        or cached.get("completion_schema") != 1
        or cached.get("cache_key") != key
        or cached.get("status") != "measured"
        or cached.get("selection_kind") != cached.get("status")
    ):
        return None

    selected_id = cached.get("selected_candidate_id")
    selected = cached.get("selected_candidate")
    if (
        not isinstance(selected_id, str)
        or not selected_id
        or not isinstance(selected, dict)
        or selected.get("candidate_id") != selected_id
    ):
        return None

    current = next(
        (
            item for item in candidates
            if item.get("eligible", True)
            and str(item.get("candidate_id", "")) == selected_id
        ),
        None,
    )
    if current is None or not _candidate_identity_valid(current):
        return None
    if not _cached_candidate_matches_current(selected, current):
        return None

    results = cached.get("candidate_results")
    if not isinstance(results, list):
        return None
    qualified_results = [
        item for item in results
        if isinstance(item, dict) and item.get("status") == "qualified"
    ]
    qualified_ids = {
        str(item.get("candidate_id", ""))
        for item in qualified_results
        if item.get("candidate_id")
    }
    if len(qualified_ids) < 2:
        return None
    qualified = next(
        (
            item for item in qualified_results
            if item.get("candidate_id") == selected_id
            and item.get("transcript_valid") is True
        ),
        None,
    )
    if qualified is None:
        return None
    if not isinstance(qualified.get("segment_count"), int) or qualified["segment_count"] < 1:
        return None
    if not isinstance(qualified.get("repetitions"), int) or qualified["repetitions"] < 2:
        return None
    median = qualified.get("median_inference_ms")
    if (
        not isinstance(median, (int, float))
        or not math.isfinite(float(median))
        or median <= 0
    ):
        return None

    cached["cache_hit"] = True
    return cached



def _write_cache(path: Path | None, report: dict[str, Any]) -> str | None:
    if path is None:
        return "cache_path_not_configured"
    if (
        report.get("status") != "measured"
        or report.get("selection_kind") != report.get("status")
        or not report.get("cache_key")
        or not report.get("selected_candidate_id")
        or not isinstance(report.get("selected_candidate"), dict)
    ):
        return None

    if report["status"] == "measured":
        results = report.get("candidate_results")
        winner = next(
            (
                item for item in results
                if isinstance(item, dict)
                and item.get("candidate_id") == report["selected_candidate_id"]
                and item.get("status") == "qualified"
                and item.get("transcript_valid") is True
                and isinstance(item.get("segment_count"), int)
                and item["segment_count"] >= 1
                and isinstance(item.get("repetitions"), int)
                and item["repetitions"] >= 2
                and isinstance(item.get("median_inference_ms"), (int, float))
                and math.isfinite(float(item["median_inference_ms"]))
                and item["median_inference_ms"] > 0
            ),
            None,
        ) if isinstance(results, list) else None
        if winner is None:
            return "cache_write_skipped_invalid_completion"

    report["completed"] = True
    report["completion_schema"] = 1
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    descriptor = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        data = json.dumps(report, sort_keys=True, indent=2).encode("utf-8")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = None
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        if os.name != "nt":
            try:
                directory_fd = os.open(path.parent, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            except OSError:
                pass
        return None
    except OSError as exc:
        return f"profile_persistence_failed:{type(exc).__name__}"
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass








def _validate_measurement(
    candidate: Mapping[str, Any],
    context: Mapping[str, Any],
    measurement: Mapping[str, Any],
) -> dict[str, Any]:
    expected = _public_candidate(candidate)
    actual_provider = _provider(measurement.get("actual_provider", ""))
    actual_kind = str(measurement.get("identity_kind", ""))
    actual_value = str(measurement.get("identity_value", ""))

    if actual_provider != expected["provider"]:
        raise ValueError("benchmark_actual_provider_mismatch")
    if (actual_kind, actual_value) != (
        expected["identity_kind"], expected["identity_value"]
    ):
        raise ValueError("benchmark_actual_device_identity_mismatch")
    if measurement.get("model_sha256") != context["model_sha256"]:
        raise ValueError("benchmark_model_identity_mismatch")
    if measurement.get("fixture_sha256") != context["fixture_sha256"]:
        raise ValueError("benchmark_fixture_identity_mismatch")

    transcript = measurement.get("transcript")
    segments = measurement.get("segments")
    if not isinstance(transcript, str) or not transcript.strip():
        raise ValueError("benchmark_transcript_invalid")
    if not isinstance(segments, list) or not segments:
        raise ValueError("benchmark_segments_invalid")

    for segment in segments:
        if not isinstance(segment, Mapping):
            raise ValueError("benchmark_segment_invalid")
        start = segment.get("start")
        end = segment.get("end")
        text = segment.get("text")
        if (
            not isinstance(start, (int, float))
            or not isinstance(end, (int, float))
            or not math.isfinite(float(start))
            or not math.isfinite(float(end))
            or start < 0
            or end < start
            or not isinstance(text, str)
            or not text.strip()
        ):
            raise ValueError("benchmark_segment_invalid")

    times = measurement.get("inference_times_ms")
    if not isinstance(times, (list, tuple)) or not times:
        raise ValueError("benchmark_timing_missing")
    durations = [float(value) for value in times]
    if any(not math.isfinite(value) or value <= 0 for value in durations):
        raise ValueError("benchmark_timing_invalid")

    audio_ms = float(measurement.get("audio_duration_ms", 0))
    if not math.isfinite(audio_ms) or audio_ms <= 0:
        raise ValueError("benchmark_audio_duration_invalid")

    median_ms = statistics.median(durations)
    load_ms = max(0.0, float(measurement.get("load_ms", 0.0)))
    warmup_ms = max(0.0, float(measurement.get("warmup_ms", 0.0)))
    total_ms = float(
        measurement.get("total_ms", load_ms + warmup_ms + sum(durations))
    )
    if not math.isfinite(total_ms) or total_ms < 0:
        raise ValueError("benchmark_total_time_invalid")
    return {
        **expected,
        "status": "qualified",
        "load_ms": load_ms,
        "warmup_ms": warmup_ms,
        "total_ms": total_ms,
        "inference_times_ms": durations,
        "median_inference_ms": median_ms,
        "inference_variance_ms2": statistics.pvariance(durations)
        if len(durations) > 1 else 0.0,
        "audio_duration_ms": audio_ms,
        "real_time_factor": median_ms / audio_ms,
        "transcript_valid": True,
        "segment_count": len(segments),
        "repetitions": len(durations),
    }



def _checkpoint_path(cache_path: Path | None) -> Path | None:
    return (
        cache_path.with_name(cache_path.name + ".checkpoint")
        if cache_path is not None else None
    )


def _read_checkpoint(
    path: Path | None,
    key: str,
    candidates: list[Mapping[str, Any]],
) -> dict[str, Any] | None:
    if path is None or not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if (
        not isinstance(value, dict)
        or value.get("schema") != 1
        or value.get("checkpoint_schema") != 1
        or value.get("cache_key") != key
        or value.get("completed") is not False
    ):
        return None

    current = {
        str(item.get("candidate_id", "")): item
        for item in candidates
        if item.get("eligible", True) and _candidate_identity_valid(item)
    }
    saved_ids = value.get("candidate_ids")
    if not isinstance(saved_ids, list) or set(saved_ids) != set(current):
        return None

    results = value.get("candidate_results")
    pending = value.get("pending_candidate_ids")
    if not isinstance(results, list) or not isinstance(pending, list):
        return None

    completed_ids = set()
    for result in results:
        if not isinstance(result, dict):
            return None
        candidate_id = str(result.get("candidate_id", ""))
        candidate = current.get(candidate_id)
        if candidate is None or candidate_id in completed_ids:
            return None
        if (
            str(result.get("provider", "")).lower()
            != str(candidate.get("provider", "")).lower()
            or result.get("identity_kind") != candidate.get("identity_kind")
            or result.get("identity_value") != candidate.get("identity_value")
        ):
            return None
        if result.get("status") == "qualified":
            if (
                result.get("transcript_valid") is not True
                or not isinstance(result.get("segment_count"), int)
                or result["segment_count"] < 1
                or not isinstance(result.get("repetitions"), int)
                or result["repetitions"] < 2
                or not isinstance(result.get("median_inference_ms"), (int, float))
                or not math.isfinite(float(result["median_inference_ms"]))
                or result["median_inference_ms"] <= 0
            ):
                return None
        elif result.get("status") != "disqualified":
            return None
        completed_ids.add(candidate_id)

    if (
        any(not isinstance(item, str) or item not in current for item in pending)
        or len(pending) != len(set(pending))
        or set(pending) != set(current) - completed_ids
    ):
        return None
    return value


def _write_checkpoint(
    path: Path | None,
    key: str,
    candidates: list[Mapping[str, Any]],
    results: list[dict[str, Any]],
    pending_ids: list[str],
) -> str | None:
    if path is None:
        return "checkpoint_path_not_configured"
    payload = {
        "schema": 1,
        "checkpoint_schema": 1,
        "completed": False,
        "cache_key": key,
        "candidate_ids": sorted(
            str(item.get("candidate_id", ""))
            for item in candidates
            if item.get("eligible", True) and _candidate_identity_valid(item)
        ),
        "candidate_results": results,
        "pending_candidate_ids": pending_ids,
        "updated_at": __import__("datetime").datetime.now(
            __import__("datetime").timezone.utc
        ).isoformat(),
    }
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    descriptor = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        data = json.dumps(payload, sort_keys=True, indent=2).encode("utf-8")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = None
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        if os.name != "nt":
            try:
                directory_fd = os.open(path.parent, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            except OSError:
                pass
        return None
    except OSError as exc:
        return f"checkpoint_persistence_failed:{type(exc).__name__}"
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _remove_checkpoint(path: Path | None) -> None:
    if path is None:
        return
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def manual_candidate_timeout_seconds(fixture: str, time_limit_enabled: bool) -> float | None:
    """Apply the selected fixture's optional per-candidate manual limit."""
    if fixture not in {"12s", "21s"}:
        raise ValueError("manual_benchmark_fixture_invalid")
    if not isinstance(time_limit_enabled, bool):
        raise ValueError("manual_benchmark_time_limit_invalid")
    if not time_limit_enabled:
        return None
    return 120.0 if fixture == "21s" else 60.0

def run_first_start_benchmark(
    candidates: list[Mapping[str, Any]],
    context: Mapping[str, Any],
    runner: Callable[[Mapping[str, Any], Mapping[str, Any]], Mapping[str, Any]],
    cache_path: Path | None = None,
    *,
    force: bool = False,
    progress_callback: Callable[[Mapping[str, Any]], None] | None = None,
    candidate_timeout_seconds: float | None = 60.0,
    session_deadline_enabled: bool = True,
    session_reserve_seconds: float = 15.0,
    provider_first: str | None = None,
) -> dict[str, Any]:
    """Benchmark every eligible candidate using a real runner.

    `runner` must create an isolated worker, load the selected cached model,
    run the common fixture, and return measured timing plus actual device identity.
    """
    required = (
        "os", "architecture", "runtime_fingerprint", "worker_build",
        "model_id", "model_sha256", "fixture_sha256",
    )
    if candidate_timeout_seconds is not None:
        try:
            timeout_value = float(candidate_timeout_seconds)
        except (TypeError, ValueError, OverflowError):
            raise ValueError("benchmark_candidate_timeout_invalid") from None
        if isinstance(candidate_timeout_seconds, bool) or not math.isfinite(timeout_value) or timeout_value <= 0:
            raise ValueError("benchmark_candidate_timeout_invalid")
    if (
        not isinstance(session_deadline_enabled, bool)
        or isinstance(session_reserve_seconds, bool)
        or not math.isfinite(float(session_reserve_seconds))
        or float(session_reserve_seconds) < 0
    ):
        raise ValueError("benchmark_session_deadline_policy_invalid")
    session_deadline = (
        float(context.get("_benchmark_deadline_monotonic", __import__("time").monotonic() + 180.0))
        if session_deadline_enabled else None
    )
    if provider_first not in {None, "gpu", "cpu"}:
        raise ValueError("benchmark_provider_order_invalid")
    if any(not str(context.get(key, "")).strip() for key in required):
        raise ValueError("benchmark_context_incomplete")

    ids = [str(c.get("candidate_id", "")) for c in candidates]
    if any(not value for value in ids) or len(ids) != len(set(ids)):
        raise ValueError("benchmark_candidate_ids_invalid")

    cache_key = _cache_key(context, candidates)
    if not force:
        cached = _read_cache(cache_path, cache_key, candidates)
        if cached is not None:
            _emit_progress(
                progress_callback, "cache-hit",
                status=cached.get("status"),
                selected_candidate_id=cached.get("selected_candidate_id"),
            )
            return cached

    ranking_history = _load_ranking_history(cache_path)
    eligible = [dict(c) for c in candidates if c.get("eligible", True)]
    benchmarkable = [
        c for c in eligible if _candidate_identity_valid(c)
    ]
    ineligible = [
        {
            "candidate_id": str(c.get("candidate_id", "")),
            "reason": str(c.get("exclusion_reason") or "not_eligible"),
        }
        for c in candidates if not c.get("eligible", True)
    ]
    identity_excluded = [
        {
            "candidate_id": str(c.get("candidate_id", "")),
            "reason": "device_identity_indeterminate",
        }
        for c in eligible if not _candidate_identity_valid(c)
    ]

    ranking_profiles = {
        str(candidate["candidate_id"]): _ranking_estimate(
            candidate, context, ranking_history
        )
        for candidate in benchmarkable
    }
    if provider_first is None:
        # Preserve the existing mandatory first-start ranking exactly.
        ordered = sorted(
            benchmarkable,
            key=lambda candidate: _candidate_key(
                candidate, ranking_profiles[str(candidate["candidate_id"])]
            ),
        )
    else:
        # Manual ordering treats CUDA and Vulkan as GPU candidates while
        # retaining the existing ranking order within each provider group.
        def manual_provider_order(candidate: Mapping[str, Any]):
            provider = _provider(candidate["provider"])
            preferred_group = (
                provider == "cpu"
                if provider_first == "cpu"
                else provider in {"cuda", "vulkan"}
            )
            return (
                0 if preferred_group else 1,
                _candidate_key(
                    candidate,
                    ranking_profiles[str(candidate["candidate_id"])],
                ),
            )

        ordered = sorted(benchmarkable, key=manual_provider_order)
    selected_for_test = ordered
    candidate_order = [
        {
            "candidate_id": str(candidate["candidate_id"]),
            "provider": _provider(candidate["provider"]),
            "identity_kind": str(candidate.get("identity_kind", "")),
            "identity_value": str(candidate.get("identity_value", "")),
            **ranking_profiles[str(candidate["candidate_id"])],
        }
        for candidate in ordered
    ]
    preselection_excluded = []
    _emit_progress(
        progress_callback, "selection-ready",
        eligible_count=len(eligible),
        verified_count=len(benchmarkable),
        candidates_to_test=len(selected_for_test),
        excluded_count=len(ineligible) + len(identity_excluded) + len(preselection_excluded),
    )

    base: dict[str, Any] = {
        "schema": 1,
        "cache_key": cache_key,
        "cache_hit": False,
        "context": {key: str(context[key]) for key in required},
        "eligible_candidate_count": len(eligible),
        "benchmarkable_candidate_count": len(benchmarkable),
        "candidate_results": [],
        "candidate_order": candidate_order,
        "excluded_candidates": ineligible + identity_excluded + preselection_excluded,
        "selected_candidate_id": None,
        "selection_kind": "provisional",
        "reason": "",
    }

    if not selected_for_test:
        base["status"] = "failed"
        base["selection_kind"] = "failed"
        base["reason"] = "no_identity_verified_benchmarkable_candidates"
        _emit_progress(
            progress_callback,
            "benchmark-complete",
            status="failed",
            selection_kind="failed",
            selected_candidate_id=None,
            reason=base["reason"],
        )
        return base

    checkpoint_file = _checkpoint_path(cache_path)
    checkpoint = _read_checkpoint(checkpoint_file, cache_key, selected_for_test)
    candidate_results = (
        list(checkpoint["candidate_results"]) if checkpoint is not None else []
    )
    completed_ids = {item["candidate_id"] for item in candidate_results}
    qualified: list[dict[str, Any]] = [
        item for item in candidate_results if item.get("status") == "qualified"
    ]
    pending_candidates = [
        item for item in selected_for_test
        if str(item["candidate_id"]) not in completed_ids
    ]
    pending_ids = [str(item["candidate_id"]) for item in pending_candidates]
    base["candidate_results"] = candidate_results
    if checkpoint is not None:
        _emit_progress(
            progress_callback, "checkpoint-resumed",
            completed_count=len(candidate_results),
            pending_count=len(pending_candidates),
        )
    checkpoint_warning = _write_checkpoint(
        checkpoint_file, cache_key, selected_for_test,
        candidate_results, pending_ids,
    )
    if checkpoint_warning:
        base["checkpoint_persistence_warning"] = checkpoint_warning

    total_candidates = len(selected_for_test)
    for index, candidate in enumerate(pending_candidates, 1):
        now = __import__("time").monotonic()
        remaining = (
            session_deadline - now if session_deadline is not None else float("inf")
        )
        candidate_id = str(candidate["candidate_id"])
        if session_deadline is not None and remaining <= session_reserve_seconds:
            pending_ids = [
                str(item["candidate_id"])
                for item in pending_candidates[index - 1:]
            ]
            warning = _write_checkpoint(
                checkpoint_file, cache_key, selected_for_test,
                base["candidate_results"], pending_ids,
            )
            if warning:
                base["checkpoint_persistence_warning"] = warning

            # Report unstarted candidates without marking them completed in the checkpoint.
            unstarted_candidates = pending_candidates[index - 1:]
            base["candidate_results"].extend(
                {
                    **_public_candidate(item),
                    "status": "skipped",
                    "reason": "first_start_session_deadline_reserve",
                }
                for item in unstarted_candidates
            )

            base["status"] = "incomplete"
            base["selection_kind"] = "incomplete"
            base["pending_candidate_ids"] = pending_ids
            base["reason"] = "first_start_session_deadline_reserve"
            if qualified:
                best_so_far = min(
                    qualified, key=lambda item: item["median_inference_ms"]
                )
                base["selected_candidate_id"] = best_so_far["candidate_id"]
                base["selected_candidate"] = best_so_far
            ranking_history = _merge_ranking_history(
                ranking_history, selected_for_test,
                base["candidate_results"], context,
            )
            base["ranking_history"] = ranking_history
            history_warning = _persist_ranking_history(cache_path, ranking_history)
            if history_warning:
                base["ranking_history_persistence_warning"] = history_warning
            _emit_progress(
                progress_callback, "benchmark-incomplete",
                completed_count=len(base["candidate_results"]),
                pending_count=len(pending_ids),
                reason=base["reason"],
            )
            return base
        candidate_context = dict(context)
        if session_deadline is None:
            candidate_context.pop("_benchmark_deadline_monotonic", None)
        else:
            candidate_context["_benchmark_deadline_monotonic"] = session_deadline
        if candidate_timeout_seconds is None:
            candidate_context.pop("_candidate_deadline_monotonic", None)
        else:
            candidate_deadline = now + float(candidate_timeout_seconds)
            if session_deadline is not None:
                candidate_deadline = min(session_deadline, candidate_deadline)
            candidate_context["_candidate_deadline_monotonic"] = candidate_deadline
        _emit_progress(
            progress_callback, "candidate-start",
            candidate_index=index,
            candidate_count=total_candidates,
            candidate_id=candidate_id,
            provider=_provider(candidate.get("provider", "")),
        )
        attempt_started = __import__("time").perf_counter()
        try:
            result = _validate_measurement(
                candidate, context, runner(candidate, candidate_context)
            )
            result["attempt_ms"] = round(
                max(
                    0.0,
                    (__import__("time").perf_counter() - attempt_started) * 1000.0,
                ),
                3,
            )
            result["observed_at"] = datetime.now(timezone.utc).isoformat()
            qualified.append(result)
            base["candidate_results"].append(result)
            completed_now = {
                item["candidate_id"] for item in base["candidate_results"]
            }
            pending_ids = [
                str(item["candidate_id"]) for item in selected_for_test
                if str(item["candidate_id"]) not in completed_now
            ]
            warning = _write_checkpoint(
                checkpoint_file, cache_key, selected_for_test,
                base["candidate_results"], pending_ids,
            )
            if warning:
                base["checkpoint_persistence_warning"] = warning
            _emit_progress(
                progress_callback, "candidate-qualified",
                candidate_index=index,
                candidate_count=total_candidates,
                candidate_id=candidate_id,
                median_inference_ms=result["median_inference_ms"],
            )
        except Exception as exc:
            base["candidate_results"].append({
                **_public_candidate(candidate),
                "status": "disqualified",
                "attempt_ms": round(
                    max(
                        0.0,
                        (__import__("time").perf_counter() - attempt_started) * 1000.0,
                    ),
                    3,
                ),
                "observed_at": datetime.now(timezone.utc).isoformat(),
                "reason": str(exc)[:160],
            })
            completed_now = {
                item["candidate_id"] for item in base["candidate_results"]
            }
            pending_ids = [
                str(item["candidate_id"]) for item in selected_for_test
                if str(item["candidate_id"]) not in completed_now
            ]
            warning = _write_checkpoint(
                checkpoint_file, cache_key, selected_for_test,
                base["candidate_results"], pending_ids,
            )
            if warning:
                base["checkpoint_persistence_warning"] = warning
            _emit_progress(
                progress_callback, "candidate-disqualified",
                candidate_index=index,
                candidate_count=total_candidates,
                candidate_id=candidate_id,
                reason=str(exc)[:120],
            )

    _remove_checkpoint(checkpoint_file)
    if len(qualified) >= 2:
        winner = min(qualified, key=lambda item: item["median_inference_ms"])
        base["status"] = "measured"
        base["selection_kind"] = "measured"
        base["selected_candidate_id"] = winner["candidate_id"]
        base["selected_candidate"] = winner
        base["reason"] = "fastest_qualified_median_inference"
        ranking_history = _merge_ranking_history(
            ranking_history, selected_for_test,
            base["candidate_results"], context,
        )
        base["ranking_history"] = ranking_history
        _emit_progress(
            progress_callback, "benchmark-complete",
            status="measured",
            selected_candidate_id=winner["candidate_id"],
            median_inference_ms=winner["median_inference_ms"],
        )
        warning = _write_cache(cache_path, base)
        if warning:
            base["persistence_warning"] = warning
        return base

    if len(qualified) == 1:
        chosen = qualified[0]
        sole_benchmarkable_candidate = len(selected_for_test) == 1
        base["status"] = (
            "single-candidate" if sole_benchmarkable_candidate else "provisional"
        )
        base["selection_kind"] = base["status"]
        base["selected_candidate_id"] = str(chosen["candidate_id"])
        base["selected_candidate"] = chosen
        base["reason"] = (
            "only_one_identity_verified_candidate_passed_real_inference"
            if sole_benchmarkable_candidate
            else "one_candidate_passed_real_inference_comparative_ranking_unavailable"
        )
    else:
        base["status"] = "failed"
        base["selection_kind"] = "failed"
        base["selected_candidate_id"] = None
        base.pop("selected_candidate", None)
        base["reason"] = "all_benchmark_candidates_failed_qualification"
    ranking_history = _merge_ranking_history(
        ranking_history, selected_for_test,
        base["candidate_results"], context,
    )
    base["ranking_history"] = ranking_history
    history_warning = _persist_ranking_history(cache_path, ranking_history)
    if history_warning:
        base["ranking_history_persistence_warning"] = history_warning
    _emit_progress(
        progress_callback, "benchmark-complete",
        status=base["status"],
        selection_kind=base["selection_kind"],
        selected_candidate_id=base.get("selected_candidate_id"),
        reason=base["reason"],
    )
    return base
