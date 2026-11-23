"""Isolated, identity-checked real-inference hardware benchmark runner."""
from __future__ import annotations

import hashlib
import importlib
import math
import os
import re
import statistics
import time
import wave
from pathlib import Path
from typing import Any, Mapping


_ENV_KEYS = (
    "RDT_WHISPER_PROVIDER",
    "RDT_WHISPER_DEVICE_SELECTOR",
    "RDT_WHISPER_DEVICE_INDEX",
    "RDT_WHISPER_WORKER_EXE",
    "RDT_WHISPER_EXPECTED_DEVICE_IDENTITY_KIND",
    "RDT_WHISPER_EXPECTED_DEVICE_IDENTITY_VALUE", "GGML_CUDA_DEVICES",
)
_CUDA_UUID = re.compile(r"GPU-[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _engine_class():
    try:
        module = importlib.import_module("engine")
    except ImportError:
        module = importlib.import_module("backend.whisper_cpp.engine")
    return module.PersistentWhisperEngine


def _restore_environment(previous: Mapping[str, str | None]) -> None:
    for key, value in previous.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


def _set_candidate_environment(candidate: Mapping[str, Any]) -> dict[str, str | None]:
    previous = {key: os.environ.get(key) for key in _ENV_KEYS}
    try:
        provider = str(candidate.get("provider", "")).strip().lower()
        if provider not in {"cpu", "cuda", "vulkan"}:
            raise ValueError("benchmark_candidate_provider_invalid")

        identity_kind = str(candidate.get("identity_kind", "")).strip()
        identity_value = str(candidate.get("identity_value", "")).strip().lower()
        if provider == "cpu":
            if (identity_kind, identity_value) != ("cpu", "host"):
                raise ValueError("benchmark_cpu_identity_invalid")
        elif identity_kind != "pci-bdf" or not re.fullmatch(
            r"[0-9a-f]{4}:[0-9a-f]{2}:[0-9a-f]{2}\.[0-7]",
            identity_value,
        ):
            raise ValueError("benchmark_gpu_identity_invalid")

        for key in _ENV_KEYS:
            os.environ.pop(key, None)

        worker_path = str(candidate.get("worker_path", "")).strip()
        if worker_path:
            resolved = Path(worker_path).expanduser().resolve(strict=True)
            if not resolved.is_file() or not os.access(resolved, os.X_OK):
                raise ValueError("benchmark_worker_not_executable")
            os.environ["RDT_WHISPER_WORKER_EXE"] = str(resolved)
            os.environ["RDT_WHISPER_PROVIDER"] = "auto"
        else:
            os.environ["RDT_WHISPER_PROVIDER"] = provider

        os.environ["RDT_WHISPER_EXPECTED_DEVICE_IDENTITY_KIND"] = identity_kind
        os.environ["RDT_WHISPER_EXPECTED_DEVICE_IDENTITY_VALUE"] = identity_value

        if provider == "cpu":
            return previous

        selector = str(candidate.get("runtime_selector", "")).strip()
        selector_kind = str(candidate.get("runtime_selector_kind", "")).strip()
        if provider == "cuda":
            valid = (
                selector_kind == "cuda-uuid" and bool(_CUDA_UUID.fullmatch(selector))
            ) or (
                selector_kind == "cuda-ordinal"
                and selector.isascii() and selector.isdecimal()
            )
        else:
            valid = (
                selector_kind == "vulkan-ordinal"
                and selector.isascii() and selector.isdecimal()
            )
        if not valid:
            raise ValueError("benchmark_runtime_selector_missing_or_invalid")
        os.environ["RDT_WHISPER_DEVICE_SELECTOR"] = selector
        return previous
    except Exception:
        _restore_environment(previous)
        raise


def _validate_transcription(result: object, duration_seconds: float) -> dict[str, Any]:
    if not isinstance(result, dict):
        raise RuntimeError("benchmark_transcription_result_invalid")
    text = result.get("text")
    segments = result.get("segments")
    if not isinstance(text, str) or not text.strip():
        raise RuntimeError("benchmark_transcript_empty")
    if not isinstance(segments, list) or not segments:
        raise RuntimeError("benchmark_segments_empty")

    normalized = []
    previous_start = -1.0
    for segment in segments:
        if not isinstance(segment, dict):
            raise RuntimeError("benchmark_segment_invalid")
        try:
            start = float(segment["start"])
            end = float(segment["end"])
        except (KeyError, TypeError, ValueError, OverflowError):
            raise RuntimeError("benchmark_timestamp_invalid") from None
        segment_text = segment.get("text")
        if (
            not math.isfinite(start) or not math.isfinite(end)
            or start < 0 or end < start
            or end > duration_seconds + 1.0
            or start < previous_start
            or not isinstance(segment_text, str) or not segment_text.strip()
        ):
            raise RuntimeError("benchmark_timestamp_or_segment_invalid")
        previous_start = start
        normalized.append({"start": start, "end": end, "text": segment_text})
    return {"text": text.strip(), "segments": normalized}


def run_cpu_readiness(
    candidate: Mapping[str, Any],
    context: Mapping[str, Any],
    model_path: Path,
    fixture_path: Path,
) -> dict[str, Any]:
    """Verify the explicit portable CPU worker; never produce a ranking score."""
    model = Path(model_path).resolve(strict=True)
    fixture = Path(fixture_path).resolve(strict=True)
    if not model.is_file() or not fixture.is_file():
        raise ValueError("cpu_readiness_input_file_invalid")
    if _sha256(model) != str(context.get("model_sha256", "")):
        raise ValueError("cpu_readiness_model_hash_mismatch")
    if _sha256(fixture) != str(context.get("fixture_sha256", "")):
        raise ValueError("cpu_readiness_fixture_hash_mismatch")
    if str(candidate.get("provider", "")).lower() != "cpu":
        raise ValueError("cpu_readiness_provider_invalid")
    if (
        str(candidate.get("identity_kind", "")).lower(),
        str(candidate.get("identity_value", "")).lower(),
    ) != ("cpu", "host"):
        raise ValueError("cpu_readiness_identity_invalid")

    with wave.open(str(fixture), "rb") as audio:
        if audio.getnchannels() != 1 or audio.getframerate() != 16000:
            raise ValueError("cpu_readiness_fixture_format_invalid")
        duration = audio.getnframes() / audio.getframerate()
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError("cpu_readiness_fixture_duration_invalid")

    deadline = min(
        float(context.get("_benchmark_deadline_monotonic", float("inf"))),
        float(context.get("_cpu_readiness_deadline_monotonic", float("inf"))),
    )
    if not math.isfinite(deadline):
        raise ValueError("cpu_readiness_deadline_invalid")

    previous = _set_candidate_environment(candidate)
    engine = None
    started = time.perf_counter()
    try:
        engine = _engine_class()(model, str(context["model_id"]))
        engine._benchmark_deadline_monotonic = deadline
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("cpu_readiness_deadline_exceeded")

        engine.start(
            candidate_path=Path(candidate["worker_path"]),
            ready_timeout_seconds=remaining,
            mark_failed_candidate=False,
        )
        identity = engine.worker_identity()
        if str(identity.get("provider", "")).lower() != "cpu":
            raise RuntimeError("cpu_readiness_actual_provider_mismatch")
        if (
            str(identity.get("identity_kind", "")).lower(),
            str(identity.get("identity_value", "")).lower(),
        ) != ("cpu", "host"):
            raise RuntimeError("cpu_readiness_actual_identity_mismatch")
        if (
            str(identity.get("architecture", "")).lower()
            != str(context["architecture"]).lower()
        ):
            raise RuntimeError("cpu_readiness_architecture_mismatch")

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("cpu_readiness_deadline_exceeded")
        inference_started = time.perf_counter()
        validated = _validate_transcription(
            engine.transcribe(
                fixture,
                language=str(context.get("language", "auto")),
                timeout_seconds=remaining,
            ),
            duration,
        )
        inference_ms = (time.perf_counter() - inference_started) * 1000.0
        result = {
            "verified": True,
            "status": "verified",
            "provider": "cpu",
            "identity_kind": "cpu",
            "identity_value": "host",
            "architecture": str(identity.get("architecture", "")),
            "worker_variant": str(identity.get("worker_variant", "")),
            "worker_version": str(identity.get("engine_version", "")),
            "worker_path": str(Path(candidate["worker_path"]).resolve()),
            "model_sha256": context["model_sha256"],
            "fixture_sha256": context["fixture_sha256"],
            "audio_duration_ms": round(duration * 1000.0, 3),
            "inference_ms": round(inference_ms, 3),
            "transcript_valid": bool(validated["text"] and validated["segments"]),
        }
    finally:
        try:
            if engine is not None:
                engine.stop()
        finally:
            _restore_environment(previous)

    result["total_ms"] = round((time.perf_counter() - started) * 1000.0, 3)
    return result


def run_candidate_benchmark(
    candidate: Mapping[str, Any],
    context: Mapping[str, Any],
    model_path: Path,
    fixture_path: Path,
    *,
    recovery_probe_context: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Run isolated inference; caller resolves runtime selectors afresh per run."""
    model = Path(model_path).resolve(strict=True)
    fixture = Path(fixture_path).resolve(strict=True)
    if not model.is_file() or not fixture.is_file():
        raise ValueError("benchmark_input_file_invalid")
    if _sha256(model) != str(context.get("model_sha256", "")):
        raise ValueError("benchmark_model_hash_mismatch")
    if _sha256(fixture) != str(context.get("fixture_sha256", "")):
        raise ValueError("benchmark_fixture_hash_mismatch")

    with wave.open(str(fixture), "rb") as audio:
        if audio.getnchannels() != 1 or audio.getframerate() != 16000:
            raise ValueError("benchmark_fixture_format_invalid")
        duration_seconds = audio.getnframes() / audio.getframerate()
    if not math.isfinite(duration_seconds) or duration_seconds <= 0:
        raise ValueError("benchmark_fixture_duration_invalid")

    repetitions = context.get("repetitions", 3)
    if isinstance(repetitions, bool) or not isinstance(repetitions, int) or not 2 <= repetitions <= 7:
        raise ValueError("benchmark_repetition_count_invalid")

    previous = _set_candidate_environment(candidate)
    engine = None
    total_started = time.perf_counter()
    try:
        engine_class = _engine_class()
        if recovery_probe_context is None:
            engine = engine_class(model, str(context["model_id"]))
        else:
            engine = engine_class(
                model,
                str(context["model_id"]),
                recovery_probe_context=dict(recovery_probe_context),
            )

        load_started = time.perf_counter()
        deadline_values = []
        for key in ("_benchmark_deadline_monotonic", "_candidate_deadline_monotonic"):
            raw_deadline = context.get(key)
            if raw_deadline is None:
                continue
            value = float(raw_deadline)
            if not math.isfinite(value):
                raise ValueError("benchmark_deadline_invalid")
            deadline_values.append(value)
        deadline = min(deadline_values) if deadline_values else None

        if deadline is None:
            # No manual benchmark cap: retain the engine's ordinary startup guard.
            engine.start()
        else:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("benchmark_candidate_deadline_exceeded")
            engine._benchmark_deadline_monotonic = deadline
            engine.start(ready_timeout_seconds=remaining)
        load_ms = (time.perf_counter() - load_started) * 1000.0

        identity = engine.worker_identity()
        provider = str(candidate["provider"]).lower()
        expected = (
            str(candidate["identity_kind"]),
            str(candidate["identity_value"]).lower(),
        )
        actual = (
            str(identity.get("identity_kind", "")),
            str(identity.get("identity_value", "")).lower(),
        )
        if str(identity.get("provider", "")).lower() != provider:
            raise RuntimeError("benchmark_actual_provider_mismatch")
        if actual != expected:
            raise RuntimeError("benchmark_actual_device_identity_mismatch")
        if str(identity.get("architecture", "")).lower() != str(context["architecture"]).lower():
            raise RuntimeError("benchmark_worker_architecture_mismatch")

        language = str(context.get("language", "auto"))

        def transcribe_fixture():
            if deadline is None:
                # No benchmark-specific timeout; use the normal engine request policy.
                return engine.transcribe(fixture, language=language)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("benchmark_candidate_deadline_exceeded")
            return engine.transcribe(
                fixture, language=language, timeout_seconds=remaining
            )

        warmup_started = time.perf_counter()
        warmup_result = _validate_transcription(
            transcribe_fixture(), duration_seconds
        )
        warmup_ms = (time.perf_counter() - warmup_started) * 1000.0

        inference_times = []
        for _ in range(repetitions):
            started = time.perf_counter()
            result = _validate_transcription(
                transcribe_fixture(), duration_seconds
            )
            inference_times.append((time.perf_counter() - started) * 1000.0)
            if result["text"] != warmup_result["text"]:
                raise RuntimeError("benchmark_transcript_not_comparable")

        return {
            "candidate_id": str(candidate["candidate_id"]),
            "actual_provider": provider,
            "identity_kind": actual[0],
            "identity_value": actual[1],
            "device": str(identity.get("device", "")),
            "architecture": str(identity.get("architecture", "")),
            "worker_variant": str(identity.get("worker_variant", "")),
            "worker_version": str(identity.get("engine_version", "")),
            "model_sha256": context["model_sha256"],
            "fixture_sha256": context["fixture_sha256"],
            "transcript": warmup_result["text"],
            "segments": warmup_result["segments"],
            "load_ms": round(load_ms, 3),
            "warmup_ms": round(warmup_ms, 3),
            "inference_times_ms": [round(value, 3) for value in inference_times],
            "median_inference_ms": round(statistics.median(inference_times), 3),
            "mean_inference_ms": round(statistics.fmean(inference_times), 3),
            "variance_inference_ms2": round(statistics.pvariance(inference_times), 3),
            "audio_duration_ms": round(duration_seconds * 1000.0, 3),
            "total_ms": round((time.perf_counter() - total_started) * 1000.0, 3),
        }
    finally:
        try:
            if engine is not None:
                engine.stop()
        finally:
            _restore_environment(previous)
