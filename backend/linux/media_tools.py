"""Resolve FFmpeg tools for development and frozen Linux application bundles."""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path
from typing import Callable


def resolve_media_tool(
    name: str,
    override_env: str,
    *,
    frozen: bool | None = None,
    executable: str | Path | None = None,
    path_lookup: Callable[[str], str | None] | None = None,
) -> str | None:
    """Prefer explicit override, then private frozen bundle, then development PATH."""
    override = os.environ.get(override_env)
    if override:
        return override

    is_frozen = bool(getattr(sys, "frozen", False)) if frozen is None else frozen
    if is_frozen:
        executable_path = Path(executable or sys.executable).resolve()
        packaged = executable_path.parent / "ffmpeg" / "bin" / name
        if packaged.is_file() and os.access(packaged, os.X_OK):
            return str(packaged)

    lookup = path_lookup or shutil.which
    return lookup(name)
