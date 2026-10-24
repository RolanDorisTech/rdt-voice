"""Qualification-only model verification with an explicit cache directory."""
from __future__ import annotations

import os
from pathlib import Path
import sys
from typing import Any


def verify_model_in_directory(
    model_id: str, model_dir: Path
) -> tuple[dict[str, Any], Path, dict[str, Any]]:
    """Verify the model while making the explicit directory the verifier's cache root."""
    repo_root = Path(__file__).resolve().parents[2]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))

    resolved_dir = Path(model_dir).resolve(strict=True)
    os.environ["RDT_GGML_MODEL_DIR"] = str(resolved_dir)

    from backend.whisper_cpp.model_registry import model_spec, verify_cached_model

    spec = model_spec(model_id)
    model_path = resolved_dir / spec["file"]
    sidecar = verify_cached_model(model_id, model_path)
    return spec, model_path, sidecar
