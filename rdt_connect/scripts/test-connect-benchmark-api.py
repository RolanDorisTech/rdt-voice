#!/usr/bin/env python3
import ast
import hashlib
import subprocess
import sys
import tempfile
from pathlib import Path

root = Path(__file__).resolve().parents[1]
patcher = root / "scripts/patch_desktop_backend.py"
source = Path.home() / "dev/rdt-voice/backend/server.py"
assert source.is_file(), "Canonical RDT Voice backend missing."
before = hashlib.sha256(source.read_bytes()).hexdigest()

with tempfile.TemporaryDirectory(prefix="rdt-connect-benchmark-") as temp:
    first = Path(temp) / "staged-one.py"
    second = Path(temp) / "staged-two.py"
    subprocess.run([sys.executable, str(patcher), str(source), str(first)], check=True)
    text = first.read_text()
    tree = ast.parse(text)

    routes = (
        '@app.get("/v1/benchmark/capabilities")',
        '@app.post("/v1/benchmark/lease")',
        '@app.post("/v1/benchmark/lease/renew")',
        '@app.post("/v1/benchmark/lease/release")',
    )
    for route in routes:
        assert text.count(route) == 1, f"Missing/duplicate route: {route}"

    trans = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "trans")
    args = {arg.arg for arg in trans.args.args}
    assert {"save_recording", "benchmark_session"} <= args
    body = ast.get_source_segment(text, trans) or ""
    assert "_rdt_require_benchmark_lease(benchmark_session, authorization, tailscale_user_login)" in body
    assert "private_mode = True" in body
    assert "save_recording is not False" in body
    assert "No-save benchmark transcription requires an active lease" in body
    assert '"supportsNoSaveTranscription": True' in text
    assert '"supportsLeaseRenewal": True' in text
    assert '"supportsLeaseRelease": True' in text
    assert "time.monotonic()" in text and "secrets.compare_digest" in text
    assert '@app.get("/assets/{asset_path:path}")' in text
    assert "_rdt_desktop_loopback_origin" in text

    subprocess.run([sys.executable, str(patcher), str(first), str(second)], check=True)
    assert first.read_bytes() == second.read_bytes(), "Backend staging patch is not idempotent."

assert hashlib.sha256(source.read_bytes()).hexdigest() == before, "Canonical Voice backend changed."
print("PASS: Connect benchmark API, authenticated expiring lease, renewal/release, no-save enforcement, browser-origin guard, asset route, idempotency, canonical backend unchanged.")
