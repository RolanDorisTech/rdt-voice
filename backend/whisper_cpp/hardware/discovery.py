"""Discover benchmark candidates from the packaged native worker binaries."""
from __future__ import annotations

import os
import platform
import subprocess
from pathlib import Path
from typing import Any, Iterable


def _hardware_class_key(vendor_id: Any, device_id: Any) -> str | None:
    if vendor_id is None or device_id is None:
        return None
    vendor = str(vendor_id).strip().lower()
    device = str(device_id).strip().lower()
    if not vendor or not device:
        return None
    return f"pci:{vendor}:{device}"


def _provider_from_worker(path: Path) -> str:
    name = path.name.lower()
    if "cuda" in name:
        return "cuda"
    if "vulkan" in name:
        return "vulkan"
    return "cpu"


def _parse_device_list(stdout: str, expected_provider: str) -> list[dict[str, Any]]:
    devices = []
    seen_indices = set()
    for raw in stdout.splitlines():
        if not raw.startswith("DEVICE\t"):
            continue
        parts = raw.split("\t")
        if len(parts) != 8 or parts[1] != "2":
            raise ValueError("benchmark_device_list_protocol_invalid")
        _, _, provider, index_text, name, identity_kind, identity_value, memory_text = parts
        if provider != expected_provider or not name.strip():
            raise ValueError("benchmark_device_list_provider_or_name_invalid")
        if not index_text.isascii() or not index_text.isdecimal():
            raise ValueError("benchmark_device_list_index_invalid")
        index = int(index_text)
        if index in seen_indices:
            raise ValueError("benchmark_device_list_duplicate_index")
        seen_indices.add(index)
        try:
            memory = int(memory_text, 10)
        except ValueError:
            raise ValueError("benchmark_device_list_memory_invalid") from None
        if memory < 0:
            raise ValueError("benchmark_device_list_memory_invalid")
        if identity_kind not in {"pci-bdf", "cpu", "unavailable"}:
            raise ValueError("benchmark_device_list_identity_kind_invalid")
        if identity_kind == "unavailable" and identity_value != "-":
            raise ValueError("benchmark_device_list_unavailable_identity_invalid")
        devices.append({
            "provider": provider,
            "runtime_index": index,
            "device": name.strip(),
            "runtime_identity_kind": identity_kind,
            "runtime_identity_value": identity_value.lower(),
            "dedicated_memory_bytes": memory,
        })
    return devices


def _linux_inventory(inventory: Iterable[Any] | None) -> list[Any]:
    if inventory is not None:
        return list(inventory)
    from backend.whisper_cpp.device_inventory import discover_linux_pci_gpus
    return list(discover_linux_pci_gpus())


def discover_hardware_candidates(
    worker_paths: Iterable[str | Path] | None = None,
    *,
    platform_name: str | None = None,
    linux_inventory: Iterable[Any] | None = None,
    windows_adapters: Iterable[Any] | None = None,
    windows_pci_lookup=None,
    timeout_seconds: float = 20.0,
    deadline_monotonic: float | None = None,
) -> list[dict[str, Any]]:
    """Enumerate packaged workers and reconcile GPU identities without guessing."""
    platform_name = (platform_name or sys_platform()).lower()
    if platform_name not in {"linux", "win32", "windows"}:
        return []

    if worker_paths is None:
        try:
            from backend.whisper_cpp.engine import PersistentWhisperEngine
            worker_paths = PersistentWhisperEngine.worker_candidates()
        except Exception:
            return []

    inventory = _linux_inventory(linux_inventory) if platform_name == "linux" else []
    dxgi_adapters = []
    if platform_name in {"win32", "windows"}:
        try:
            if windows_adapters is None:
                from backend.whisper_cpp.windows_device_inventory import enumerate_windows_adapters
                dxgi_adapters = list(enumerate_windows_adapters())
            else:
                dxgi_adapters = list(windows_adapters)
        except (OSError, RuntimeError, ValueError):
            dxgi_adapters = []
    candidates = []

    for raw_path in worker_paths:
        remaining = (float(deadline_monotonic) - __import__("time").monotonic()) if deadline_monotonic is not None else float(timeout_seconds)
        if remaining <= 0.01:
            break
        worker = Path(raw_path).expanduser()
        if not worker.is_file() or not os.access(worker, os.X_OK):
            continue
        provider = _provider_from_worker(worker)
        env = os.environ.copy()
        for key in (
            "RDT_WHISPER_PROVIDER", "RDT_WHISPER_DEVICE_SELECTOR",
            "RDT_WHISPER_DEVICE_INDEX", "RDT_WHISPER_WORKER_EXE",
            "CUDA_VISIBLE_DEVICES", "GGML_VK_VISIBLE_DEVICES", "GGML_CUDA_DEVICES",
        ):
            env.pop(key, None)
        env["RDT_WHISPER_PROVIDER"] = "auto"

        try:
            result = subprocess.run(
                [str(worker), "--list-devices"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=min(float(timeout_seconds), remaining),
                check=False,
                env=env,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        if result.returncode != 0:
            continue

        for device in _parse_device_list(result.stdout, provider):
            index = device["runtime_index"]
            runtime_kind = device["runtime_identity_kind"]
            runtime_value = device["runtime_identity_value"]
            candidate = {
                "candidate_id": f"{worker.stem}:{index}",
                "provider": provider,
                "device": device["device"],
                "worker_path": str(worker.resolve()),
                "runtime_selector": None,
                "runtime_selector_kind": None,
                "identity_kind": "unavailable",
                "identity_value": "-",
                "eligible": True,
                "fallback_usable": False,
                "hardware_class": "cpu" if provider == "cpu" else None,
                "dedicated_memory_bytes": device["dedicated_memory_bytes"],
                "runtime_identity_kind": runtime_kind,
                "runtime_identity_value": runtime_value,
            }

            if provider == "cpu":
                candidate.update({
                    "identity_kind": "cpu",
                    "identity_value": "host",
                    "candidate_id": f"{worker.stem}:cpu:host",
                    "fallback_usable": True,
                })
            else:
                candidate["runtime_selector"] = str(index)
                candidate["runtime_selector_kind"] = (
                    "cuda-ordinal" if provider == "cuda" else "vulkan-ordinal"
                )
                if runtime_kind == "pci-bdf" and platform_name == "linux":
                    matches = [
                        item for item in inventory
                        if str(getattr(item, "pci_bdf", "") or "").lower() == runtime_value
                    ]
                    if len(matches) == 1:
                        matched_device = matches[0]
                        candidate.update({
                            "candidate_id": f"{worker.stem}:{provider}:{runtime_value}",
                            "identity_kind": "pci-bdf",
                            "identity_value": runtime_value,
                            "fallback_usable": True,
                            "driver": getattr(matched_device, "driver", None),
                            "driver_version": getattr(matched_device, "driver_version", None),
                            "vendor_id": getattr(matched_device, "vendor_id", None),
                            "device_id": getattr(matched_device, "device_id", None),
                            "hardware_class": _hardware_class_key(
                                getattr(matched_device, "vendor_id", None),
                                getattr(matched_device, "device_id", None),
                            ),
                        })
                    else:
                        candidate["exclusion_reason"] = (
                            "linux_pci_identity_missing_or_ambiguous"
                        )
                elif platform_name in {"win32", "windows"}:
                    from backend.whisper_cpp.windows_device_inventory import (
                        match_windows_runtime_pci_identity,
                    )
                    matched = match_windows_runtime_pci_identity(
                        dxgi_adapters,
                        runtime_value,
                        pci_lookup=windows_pci_lookup,
                    )
                    if matched.status == "matched" and matched.adapter is not None:
                        adapter = matched.adapter
                        vendor_id = getattr(adapter, "vendor_id", None)
                        device_id = getattr(adapter, "device_id", None)
                        candidate.update({
                            "identity_kind": "pci-bdf",
                            "identity_value": runtime_value,
                            "fallback_usable": True,
                            "dxgi_adapter_luid": adapter.adapter_luid,
                            "hardware_signature": adapter.hardware_signature,
                            "vendor_id": vendor_id,
                            "device_id": device_id,
                            "hardware_class": _hardware_class_key(vendor_id, device_id),
                            "dedicated_memory_bytes": max(
                                int(getattr(adapter, "dedicated_memory_bytes", 0) or 0),
                                int(device["dedicated_memory_bytes"] or 0),
                            ),
                            "candidate_id": (
                                f"{worker.stem}:{runtime_value}:"
                                f"{adapter.hardware_signature}"
                            ),
                        })
                    else:
                        candidate["exclusion_reason"] = matched.reason
                else:
                    candidate["exclusion_reason"] = "runtime_pci_identity_unavailable"

            candidates.append(candidate)

    return candidates


def sys_platform() -> str:
    import sys
    return sys.platform


MANUAL_HARDWARE_SELECTOR_MIN_PHYSICAL_CHOICES = 3


def count_distinct_physical_hardware_choices(
    candidates: Iterable[dict[str, Any]],
) -> int:
    """Count verified physical devices, not provider-specific candidate rows."""
    identities: set[tuple[str, str]] = set()
    for candidate in candidates:
        if not isinstance(candidate, dict) or candidate.get("eligible", True) is False:
            continue

        kind = str(candidate.get("identity_kind", "")).strip().lower()
        value = str(candidate.get("identity_value", "")).strip().lower()
        if not value or value == "-" or kind == "unavailable":
            continue

        if kind == "cpu" and value == "host":
            identities.add(("cpu", "host"))
        elif kind == "pci-bdf":
            identities.add((kind, value))

    return len(identities)


def should_show_manual_hardware_selector(
    candidates: Iterable[dict[str, Any]],
) -> bool:
    """The selector is for manual runs with at least three physical choices."""
    return (
        count_distinct_physical_hardware_choices(candidates)
        >= MANUAL_HARDWARE_SELECTOR_MIN_PHYSICAL_CHOICES
    )
