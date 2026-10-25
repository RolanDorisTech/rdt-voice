#!/usr/bin/env python3
"""Patch a staged copy of RDT Voice server.py for its packaged asset layout.

The user's repository server.py is never edited. Usage:
  python patch_desktop_backend.py SOURCE_SERVER.py STAGED_SERVER.py
"""
from __future__ import annotations

import pathlib
import re
import sys

WARMUP_START = "# Warmup\n"
WARMUP_END = "\ndef clean_transcript_enhanced"

ASSET_ROUTE = '''\n# RDT Voice Desktop: safely serve same-origin assets such as the bundled OpenCC converter.\n@app.get("/assets/{asset_path:path}")\ndef _serve_frontend_asset_path(asset_path: str, host: Optional[str] = Header(None)):\n    check_host(host)\n    root = (_find_frontend_asset_dir() / "assets").resolve()\n    candidate = (root / asset_path).resolve()\n    if root not in candidate.parents or not candidate.is_file():\n        raise HTTPException(status_code=404, detail="Frontend asset not found")\n    return FileResponse(str(candidate), headers={"Cache-Control": "public, max-age=3600", "X-Content-Type-Options": "nosniff"})\n\n'''


def main() -> int:
    if len(sys.argv) != 3:
        print("Usage: patch_desktop_backend.py SOURCE_SERVER.py STAGED_SERVER.py", file=sys.stderr)
        return 2
    source = pathlib.Path(sys.argv[1]).expanduser().resolve()
    target = pathlib.Path(sys.argv[2]).expanduser().resolve()
    if not source.is_file():
        print(f"ERROR: source server not found: {source}", file=sys.stderr)
        return 2
    text = source.read_text(encoding="utf-8")
    # Defer model resolution and inference until the user's first real transcription.
    # This avoids GPU work/downloads before bind and prevents failed starts from warming the model.
    if WARMUP_START in text:
        warm_start = text.index(WARMUP_START)
        warm_end = text.find(WARMUP_END, warm_start)
        if warm_end < 0:
            print("ERROR: could not find the end of the MLX warmup block; refusing to patch an unknown backend.", file=sys.stderr)
            return 3
        text = text[:warm_start] + "# RDT Connect: model loading is deferred until a real transcription request.\n" + text[warm_end+1:]
    if '@app.get("/assets/{asset_path:path}")' not in text:
        marker = '_LOCALES_DIR = _find_frontend_asset_dir() / "locales"'
        if marker not in text:
            print("ERROR: could not find the expected frontend asset marker in server.py; not patching an unknown layout.", file=sys.stderr)
            return 3
        text = text.replace(marker, ASSET_ROUTE + marker, 1)
    # Patch only the staged backend: permit the official site or its exact
    # same-origin loopback frontend. The loopback Host check remains mandatory.
    route_pattern = re.compile(
        r'(@app\.post\("/v1/browser-handoff/exchange"\)\s*'
        r'def exchange_rdt_browser_handoff\(\s*'
        r'payload: dict = None,\s*'
        r'host: Optional\[str\] = Header\(None\),\s*'
        r'origin: Optional\[str\] = Header\(None\),\s*\):)'
    )
    text, route_count = route_pattern.subn(
        '@app.post("/v1/browser-handoff/exchange")\\n'
        'def exchange_rdt_browser_handoff(\\n'
        '    request: Request,\\n'
        '    payload: dict = None,\\n'
        '    host: Optional[str] = Header(None),\\n'
        '    origin: Optional[str] = Header(None),\\n'
        '):',
        text,
        count=1,
    )
    if route_count != 1:
        print("ERROR: handoff route signature not recognized; refusing to patch.", file=sys.stderr)
        return 3

    guard = 'if origin != _RDT_OFFICIAL_BROWSER_ORIGIN:'
    replacement = (
        'if origin != _RDT_OFFICIAL_BROWSER_ORIGIN and origin != _request_origin(request):'
    )
    text, guard_count = re.subn(re.escape(guard), replacement, text, count=1)
    if guard_count != 1:
        print("ERROR: handoff origin guard not recognized; refusing to patch.", file=sys.stderr)
        return 3

    patched = text
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(patched, encoding="utf-8")
    compile(patched, str(target), "exec")
    print(f"Prepared desktop-specific backend copy: {target}")
    print("Original server.py was not modified.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
