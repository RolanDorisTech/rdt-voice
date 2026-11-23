"""Compatibility import for the platform-specific Windows API service.

The GGML registry/downloader is OS-neutral and lives in backend/whisper_cpp so
Linux can consume the same immutable model artifacts without duplicating logic.
"""
from __future__ import annotations

import sys
from pathlib import Path

_BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))

from whisper_cpp.model_registry import (  # noqa: F401,E402
    MODELS, SOURCE_REPO, REVISION, MAX_MODEL_BYTES, CHUNK_BYTES,
    model_root, model_spec, model_path, verify_cached_model,
    download_model, model_usage_lock, is_model_in_use, emit_download_error,
)
