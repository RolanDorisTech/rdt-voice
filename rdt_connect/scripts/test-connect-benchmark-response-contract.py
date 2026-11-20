import ast
import hashlib
import pathlib
import re
import subprocess
import sys
import tempfile

root = pathlib.Path(__file__).resolve().parents[1]
patcher = root / "scripts/patch_desktop_backend.py"
voice = pathlib.Path.home() / "dev/rdt-voice/backend/server.py"
voice_hash = hashlib.sha256(voice.read_bytes()).hexdigest()

def targets_name(node, wanted):
    targets = node.targets if isinstance(node, ast.Assign) else (
        [node.target] if isinstance(node, ast.AnnAssign) else []
    )
    return any(
        isinstance(child, ast.Name) and child.id == wanted
        for target in targets for child in ast.walk(target)
    )

with tempfile.TemporaryDirectory(prefix="rdt-benchmark-contract-") as tmp:
    staged = pathlib.Path(tmp) / "server.py"
    repeated = pathlib.Path(tmp) / "server-again.py"
    subprocess.run([sys.executable, str(patcher), str(voice), str(staged)], check=True)

    code = staged.read_text()
    tree = ast.parse(code)
    compile(tree, str(staged), "exec")
    functions = {
        node.name: node for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert "_rdt_configure_mlx_device" in functions
    assert "trans" in functions
    trans = functions["trans"]

    required = (
        r"import mlx\.core as mx",
        r"mx\.set_default_device\(mx\.gpu\)",
        r"mx\.set_default_device\(mx\.cpu\)",
        r"mx\.default_device\(\)",
        r"mx\.is_available\(active\)",
        r"""['"]provider_verified['"]\s*:\s*True""",
        r"""['"]provider['"]\s*:\s*['"]mlx-metal-gpu['"]""",
        r"""['"]provider['"]\s*:\s*['"]mlx-cpu-selected['"]""",
        r'"inference_ms": round\(_rdt_benchmark_inference_seconds \* 1000\.0, 3\)',
        r'"inference_provider": _rdt_benchmark_provider',
        r'"inference_seconds": _rdt_benchmark_inference_seconds',
        r"_rdt_require_benchmark_lease",
        r"Benchmark requests must explicitly disable recording saves",
        r"RDT-Benchmark-Session",
    )
    for pattern in required:
        assert re.search(pattern, code), f"Missing contract: {pattern}"

    mlx_calls = [
        node for node in ast.walk(trans)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "transcribe"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "mlx_whisper"
    ]
    assert len(mlx_calls) == 1, f"Expected one MLX call in trans(), found {len(mlx_calls)}"

    provider_assignments = [
        node for node in ast.walk(trans)
        if isinstance(node, (ast.Assign, ast.AnnAssign))
        and targets_name(node, "_rdt_benchmark_provider")
    ]
    provider_selectors = [
        node for node in provider_assignments
        if isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Name)
        and node.value.func.id == "_rdt_configure_mlx_device"
    ]
    assert len(provider_selectors) == 1, (
        "Expected exactly one real provider-selection assignment; "
        f"found {len(provider_selectors)}"
    )
    assert any(
        isinstance(node.value, ast.Constant) and node.value.value is None
        for node in provider_assignments
    ), "Non-benchmark transcription must leave benchmark provider unset."

    timer_assignments = [
        node for node in ast.walk(trans)
        if isinstance(node, (ast.Assign, ast.AnnAssign))
        and targets_name(node, "_rdt_benchmark_inference_started")
    ]
    assert len(timer_assignments) == 1, (
        f"Expected one timer assignment, found {len(timer_assignments)}"
    )

    assert provider_selectors[0].lineno < mlx_calls[0].lineno
    assert timer_assignments[0].lineno < mlx_calls[0].lineno
    assert "inferenceSeconds" not in code

    subprocess.run([sys.executable, str(patcher), str(staged), str(repeated)], check=True)
    assert staged.read_bytes() == repeated.read_bytes(), "Patcher is not idempotent."

assert hashlib.sha256(voice.read_bytes()).hexdigest() == voice_hash
print("PASS: AST order, benchmark response, GPU/CPU selection, lease/no-save, idempotency, Voice isolation")
