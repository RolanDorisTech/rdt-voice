#!/usr/bin/env python3
import ast
import subprocess
import sys
import tempfile
from pathlib import Path

root = Path(__file__).resolve().parents[1]
source = Path.home() / "dev/rdt-voice/backend/server.py"
patcher = root / "scripts/patch_desktop_backend.py"
assert source.is_file(), "Canonical Voice backend missing."

with tempfile.TemporaryDirectory() as temp:
    staged = Path(temp) / "server.py"
    result = subprocess.run(
        [sys.executable, str(patcher), str(source), str(staged)],
        capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr[-600:]
    code = staged.read_text()
    tree = ast.parse(code)
    helper = next(
        node for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "_rdt_desktop_loopback_origin"
    )
    namespace = {}
    exec(compile(ast.Module(body=[helper], type_ignores=[]), str(staged), "exec"), namespace)
    allow = namespace[helper.name]

    assert allow("https://127.0.0.1:31415", "127.0.0.1:31415")
    assert allow("https://localhost:31415", "localhost:31415")
    assert allow("https://[::1]:31415", "[::1]:31415")
    assert not allow("https://attacker.example", "127.0.0.1:31415")
    assert not allow("http://127.0.0.1:31415", "127.0.0.1:31415")
    assert not allow("https://127.0.0.1:31416", "127.0.0.1:31415")
    assert not allow("https://user@127.0.0.1:31415", "127.0.0.1:31415")

    exchange = next(
        node for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "exchange_rdt_browser_handoff"
    )
    guards = [
        ast.unparse(node.test) for node in exchange.body
        if isinstance(node, ast.If)
    ]
    assert any("_RDT_OFFICIAL_BROWSER_ORIGIN" in guard
               and "_rdt_desktop_loopback_origin" in guard for guard in guards)
    assert "_RDT_BROWSER_HANDOFFS.pop(code, None)" in code
    assert "_RDT_BROWSER_HANDOFF_TTL = 60.0" in code

print("PASS: official Origin, exact HTTPS loopback, and rejection cases.")
print("PASS: 60-second expiry and single-use exchange preserved.")
