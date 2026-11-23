from __future__ import annotations

import hashlib
import json
import logging
import os
import pathlib
import re
import tempfile
import threading
import time
from typing import Any

_LOG = logging.getLogger(__name__)
DAY = 24 * 60 * 60
SCHEMA_VERSION = 1


def sha256_path(path: pathlib.Path | str) -> str:
    raw_target = pathlib.Path(path).expanduser()
    if raw_target.is_symlink():
        raise ValueError("symlink_not_allowed_for_hardware_identity")
    target = raw_target.resolve(strict=True)
    digest = hashlib.sha256()
    if target.is_file():
        with target.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()
    if not target.is_dir():
        raise ValueError("unsupported_hardware_identity_path")
    for item in sorted(target.rglob("*")):
        if item.is_symlink():
            raise ValueError("symlink_not_allowed_for_hardware_identity")
        if not item.is_file():
            continue
        relative = item.relative_to(target).as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        with item.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
    return digest.hexdigest()


def _state_path() -> pathlib.Path:
    if os.name == "nt":
        root = pathlib.Path(
            os.environ.get("LOCALAPPDATA")
            or pathlib.Path.home() / "AppData" / "Local"
        )
        return root / "RDTConnect" / "state" / "hardware-incidents.json"
    root = pathlib.Path(
        os.environ.get("XDG_STATE_HOME")
        or pathlib.Path.home() / ".local" / "state"
    )
    return root / "rdt-connect" / "hardware-incidents.json"


def _context_key(context: dict[str, Any]) -> str:
    identity = context.get("hardware_identity") or {}
    model = context.get("model") or {}
    required = (
        identity.get("kind"), identity.get("value"),
        context.get("provider"), context.get("worker_variant"),
        context.get("worker_sha256"), model.get("id"), model.get("sha256"),
    )
    if any(value in (None, "", "-", "unknown") for value in required):
        raise ValueError("incomplete_hardware_breaker_identity")

    runtime = context.get("runtime") or {}
    fingerprint = str(runtime.get("runtime_fingerprint") or "").lower()
    if not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
        raise ValueError("hardware_breaker_runtime_fingerprint_missing")

    provider = str(context.get("provider") or "").lower()
    variant = str(context.get("worker_variant") or "").lower()
    selector = context.get("selector") or {}
    if provider in {"cuda", "vulkan"}:
        for key in ("driver_version", "runtime_version"):
            value = str(runtime.get(key) or "").strip().lower()
            if value in {"", "-", "unknown", "none"}:
                raise ValueError("hardware_breaker_driver_runtime_incomplete")
        if not selector.get("runtime_selector_kind") or not selector.get("runtime_selector"):
            raise ValueError("hardware_breaker_selector_incomplete")
    elif variant == "arm-dotprod":
        signature = str(runtime.get("hardware_signature") or "").strip().lower()
        if signature in {"", "-", "unknown", "none"}:
            raise ValueError("hardware_breaker_cpu_signature_missing")
    else:
        raise ValueError("hardware_breaker_worker_variant_invalid")

    canonical = json.dumps(context, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class PersistentHardwareCircuitBreaker:
    """Private, corruption-tolerant Windows/Linux hardware incident history."""

    def __init__(self, path: pathlib.Path | str | None = None):
        self.path = pathlib.Path(path) if path else _state_path()
        self._lock = threading.RLock()

    def _read(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"version": SCHEMA_VERSION, "entries": {}}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if (
                not isinstance(data, dict)
                or data.get("version") != SCHEMA_VERSION
                or not isinstance(data.get("entries"), dict)
            ):
                raise ValueError("invalid_breaker_state_schema")
            return data
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            _LOG.warning("Ignoring corrupt hardware incident history: %s", type(exc).__name__)
            return {"version": SCHEMA_VERSION, "entries": {}}

    def _write(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.path.parent, 0o700)
        except OSError:
            pass
        fd, temporary = tempfile.mkstemp(prefix=".hardware-incidents-", dir=self.path.parent)
        try:
            try:
                os.fchmod(fd, 0o600)
            except (AttributeError, OSError):
                pass
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(data, stream, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            try:
                os.chmod(self.path, 0o600)
            except OSError:
                pass
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass

    @staticmethod
    def same_context(left: dict[str, Any], right: dict[str, Any]) -> bool:
        try:
            return _context_key(left) == _context_key(right)
        except (TypeError, ValueError):
            return False

    def record_failure(self, context: dict[str, Any], now: float | None = None) -> dict[str, Any]:
        timestamp = time.time() if now is None else float(now)
        key = _context_key(context)
        with self._lock:
            data = self._read()
            entry = data["entries"].setdefault(key, {
                "context": context,
                "incidents": [],
                "quarantined_until": 0.0,
                "quarantine_triggered_at": 0.0,
                "recovery_probe_passed_at": 0.0,
            })
            incidents = [
                float(value) for value in entry.get("incidents", [])
                if timestamp - float(value) <= DAY and float(value) <= timestamp
            ]
            incidents.append(timestamp)
            entry["incidents"] = incidents
            if len(incidents) >= 3 and not entry.get("quarantined_until"):
                entry["quarantined_until"] = timestamp + DAY
                entry["quarantine_triggered_at"] = timestamp
                entry["recovery_probe_passed_at"] = 0.0
                _LOG.warning("Hardware candidate quarantined after 3 classified incidents.")
            data["entries"][key] = entry
            self._write(data)
            return {
                "incident_count": len(incidents),
                "quarantined": bool(entry.get("quarantined_until")),
                "quarantined_until": float(entry.get("quarantined_until") or 0.0),
            }

    def is_quarantined(self, context: dict[str, Any], now: float | None = None) -> bool:
        key = _context_key(context)
        with self._lock:
            entry = self._read()["entries"].get(key)
            if not entry:
                return False
            until = float(entry.get("quarantined_until") or 0.0)
            passed_at = float(entry.get("recovery_probe_passed_at") or 0.0)
            # Expiry makes a probe due; it does not itself re-enable the worker.
            return until > 0 and passed_at < float(entry.get("quarantine_triggered_at") or until)

    def recovery_probes_due(self, now: float | None = None) -> list[dict[str, Any]]:
        timestamp = time.time() if now is None else float(now)
        with self._lock:
            entries = self._read()["entries"].values()
            return [
                entry["context"] for entry in entries
                if float(entry.get("quarantined_until") or 0.0) > 0
                and timestamp >= float(entry.get("quarantined_until") or 0.0)
                and float(entry.get("recovery_probe_passed_at") or 0.0)
                    < float(entry.get("quarantine_triggered_at") or 0.0)
            ]

    def clear_after_probe(
        self, context: dict[str, Any], passed: bool, now: float | None = None
    ) -> bool:
        timestamp = time.time() if now is None else float(now)
        key = _context_key(context)
        with self._lock:
            data = self._read()
            entry = data["entries"].get(key)
            if not entry:
                return False
            if not passed:
                entry["last_recovery_probe_at"] = timestamp
                data["entries"][key] = entry
                self._write(data)
                return False
            entry["incidents"] = []
            entry["quarantined_until"] = 0.0
            entry["quarantine_triggered_at"] = 0.0
            entry["recovery_probe_passed_at"] = timestamp
            entry["last_recovery_probe_at"] = timestamp
            data["entries"][key] = entry
            self._write(data)
            _LOG.info("Hardware candidate recovered after a passing bundled-fixture probe.")
            return True
