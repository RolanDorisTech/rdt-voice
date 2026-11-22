#!/usr/bin/env python3
"""Apply RDT Connect-only adaptations to a staged RDT Voice backend copy."""
from __future__ import annotations

import ast
import pathlib
import sys

ASSET_ROUTE = r'''
# RDT Connect desktop: safely serve same-origin frontend assets.
@app.get("/assets/{asset_path:path}")
def _serve_frontend_asset_path(asset_path: str, host: Optional[str] = Header(None)):
    check_host(host)
    root = (_find_frontend_asset_dir() / "assets").resolve()
    candidate = (root / asset_path).resolve()
    if root not in candidate.parents or not candidate.is_file():
        raise HTTPException(status_code=404, detail="Frontend asset not found")
    return FileResponse(
        str(candidate),
        headers={
            "Cache-Control": "public, max-age=3600",
            "X-Content-Type-Options": "nosniff",
        },
    )

'''

BENCHMARK_API = r'''
# RDT Connect benchmark lease API v1. Staged Connect backend only.
_RDT_BENCHMARK_LEASE_LOCK = threading.Lock()
_RDT_BENCHMARK_LEASE = None
_RDT_BENCHMARK_LEASE_TTL_SECONDS = 180.0


def _rdt_benchmark_principal(authorization, tailscale_user_login):
    bearer = (authorization or "").strip()
    identity = (tailscale_user_login or "").strip().casefold()
    if bearer:
        return "bearer:" + hashlib.sha256(bearer.encode("utf-8")).hexdigest()
    if identity:
        return "tailscale:" + hashlib.sha256(identity.encode("utf-8")).hexdigest()
    raise HTTPException(status_code=401, detail="Benchmark authorization is required")


def _rdt_benchmark_authorize(authorization, host, tailscale_user_login):
    check_host(host)
    require_bearer(authorization, tailscale_user_login)
    return _rdt_benchmark_principal(authorization, tailscale_user_login)


def _rdt_benchmark_check_lease(session_id, principal, action="check"):
    global _RDT_BENCHMARK_LEASE
    if not session_id or len(session_id) < 32:
        raise HTTPException(status_code=400, detail="A valid benchmark session is required")
    now = time.monotonic()
    with _RDT_BENCHMARK_LEASE_LOCK:
        lease = _RDT_BENCHMARK_LEASE
        if lease is None or lease["expires_at"] <= now:
            _RDT_BENCHMARK_LEASE = None
            raise HTTPException(status_code=409, detail="Benchmark lease is missing or expired")
        if not secrets.compare_digest(lease["session_id"], session_id):
            raise HTTPException(status_code=403, detail="Benchmark session does not match")
        if not secrets.compare_digest(lease["principal"], principal):
            raise HTTPException(status_code=403, detail="Benchmark session owner does not match")
        if action == "renew":
            lease["expires_at"] = now + _RDT_BENCHMARK_LEASE_TTL_SECONDS
        elif action == "release":
            _RDT_BENCHMARK_LEASE = None
    return True


def _rdt_require_benchmark_lease(session_id, authorization, tailscale_user_login):
    principal = _rdt_benchmark_principal(authorization, tailscale_user_login)
    _rdt_benchmark_check_lease(session_id, principal)


@app.get("/v1/benchmark/capabilities")
def _rdt_benchmark_capabilities(
    authorization: Optional[str] = Header(None),
    host: Optional[str] = Header(None),
    tailscale_user_login: Optional[str] = Header(None, alias="Tailscale-User-Login"),
):
    _rdt_benchmark_authorize(authorization, host, tailscale_user_login)
    return {
        "benchmarkLeaseApiVersion": 1,
        "supportsNoSaveTranscription": True,
        "supportsLeaseRenewal": True,
        "supportsLeaseRelease": True,
    }


@app.post("/v1/benchmark/lease")
def _rdt_benchmark_lease_create(
    authorization: Optional[str] = Header(None),
    host: Optional[str] = Header(None),
    tailscale_user_login: Optional[str] = Header(None, alias="Tailscale-User-Login"),
):
    global _RDT_BENCHMARK_LEASE
    principal = _rdt_benchmark_authorize(authorization, host, tailscale_user_login)
    now = time.monotonic()
    with _RDT_BENCHMARK_LEASE_LOCK:
        if _RDT_BENCHMARK_LEASE is not None and _RDT_BENCHMARK_LEASE["expires_at"] > now:
            raise HTTPException(status_code=409, detail="Another benchmark session is already active")
        session_id = uuid.uuid4().hex
        _RDT_BENCHMARK_LEASE = {
            "session_id": session_id,
            "principal": principal,
            "expires_at": now + _RDT_BENCHMARK_LEASE_TTL_SECONDS,
        }
    return {
        "session_id": session_id,
        "expires_in_seconds": int(_RDT_BENCHMARK_LEASE_TTL_SECONDS),
    }


@app.post("/v1/benchmark/lease/renew")
def _rdt_benchmark_lease_renew(
    session_id: Optional[str] = Header(None, alias="RDT-Benchmark-Session"),
    authorization: Optional[str] = Header(None),
    host: Optional[str] = Header(None),
    tailscale_user_login: Optional[str] = Header(None, alias="Tailscale-User-Login"),
):
    principal = _rdt_benchmark_authorize(authorization, host, tailscale_user_login)
    _rdt_benchmark_check_lease(session_id, principal, "renew")
    return {"renewed": True, "expires_in_seconds": int(_RDT_BENCHMARK_LEASE_TTL_SECONDS)}


@app.post("/v1/benchmark/lease/release")
def _rdt_benchmark_lease_release(
    session_id: Optional[str] = Header(None, alias="RDT-Benchmark-Session"),
    authorization: Optional[str] = Header(None),
    host: Optional[str] = Header(None),
    tailscale_user_login: Optional[str] = Header(None, alias="Tailscale-User-Login"),
):
    principal = _rdt_benchmark_authorize(authorization, host, tailscale_user_login)
    _rdt_benchmark_check_lease(session_id, principal, "release")
    return {"released": True}

'''



def _rdt_extract_top_level_function(source_text: str, name: str) -> str:
    tree = ast.parse(source_text)
    matches = [
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == name
    ]
    if len(matches) != 1:
        raise SystemExit(
            f"STOP: expected exactly one reference function {name}; found {len(matches)}."
        )
    lines = source_text.splitlines(keepends=True)
    return "".join(lines[matches[0].lineno - 1:matches[0].end_lineno]).rstrip() + "\n"


def _rdt_function_is_revision_pinned(source_text: str, name: str) -> bool:
    tree = ast.parse(source_text)
    matches = [
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == name
    ]
    if len(matches) != 1:
        return False

    function = matches[0]

    def is_locked_revision(value):
        return (
            isinstance(value, ast.Subscript)
            and isinstance(value.value, ast.Name)
            and value.value.id == "model_spec"
            and isinstance(value.slice, ast.Constant)
            and value.slice.value == "revision"
        )

    def expanded_dict_is_pinned(call, argument_name):
        assignments = []
        for node in ast.walk(function):
            if not isinstance(node, ast.Assign) or node.lineno >= call.lineno:
                continue
            if any(
                isinstance(target, ast.Name) and target.id == argument_name
                for target in node.targets
            ):
                assignments.append(node)

        if not assignments:
            return False

        latest = max(assignments, key=lambda node: node.lineno)
        if not isinstance(latest.value, ast.Dict):
            return False

        for key, value in zip(latest.value.keys, latest.value.values):
            if (
                isinstance(key, ast.Constant)
                and key.value == "revision"
                and is_locked_revision(value)
            ):
                return True
        return False

    calls = []
    for node in ast.walk(function):
        if not isinstance(node, ast.Call):
            continue
        called_name = (
            node.func.id if isinstance(node.func, ast.Name)
            else node.func.attr if isinstance(node.func, ast.Attribute)
            else None
        )
        if called_name == "snapshot_download":
            calls.append(node)

    if not calls:
        return False

    for call in calls:
        revision = next(
            (kw.value for kw in call.keywords if kw.arg == "revision"),
            None,
        )
        if is_locked_revision(revision):
            continue

        expanded = any(
            kw.arg is None
            and isinstance(kw.value, ast.Name)
            and expanded_dict_is_pinned(call, kw.value.id)
            for kw in call.keywords
        )
        if not expanded:
            return False

    return True


def _rdt_replace_top_level_function(
    source_text: str, name: str, replacement: str
) -> str:
    tree = ast.parse(source_text)
    matches = [
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == name
    ]
    if len(matches) != 1:
        raise SystemExit(
            f"STOP: expected exactly one target function {name}; found {len(matches)}."
        )

    node = matches[0]
    lines = source_text.splitlines(keepends=True)
    replacement_lines = (replacement.rstrip() + "\n").splitlines(keepends=True)
    lines[node.lineno - 1:node.end_lineno] = replacement_lines
    return "".join(lines)


def _apply_connect_model_reliability(text: str) -> str:
    reference_path = (
        pathlib.Path(__file__).resolve().parents[2]
        / "backend" / "apple" / "server.py"
    )
    if not reference_path.is_file():
        raise SystemExit(
            "STOP: Connect's reviewed Apple model-lock reference is missing."
        )

    reference = reference_path.read_text(encoding="utf-8")
    helper_names = (
        "_rdt_load_model_lock",
        "_rdt_model_spec",
        "_rdt_marker_path",
        "_rdt_marker_is_current",
        "_rdt_write_verified_marker",
        "_rdt_model_snapshot_is_complete",
        "_rdt_resolve_model_snapshot",
    )

    # Fail closed if the reviewed reference ever loses its pinned downloader contract.
    if not _rdt_function_is_revision_pinned(reference, "_download_model_only"):
        raise SystemExit(
            "STOP: Apple reference downloader no longer pins every snapshot_download call."
        )

    target_tree = ast.parse(text)
    target_names = {
        node.name for node in target_tree.body
        if isinstance(node, ast.FunctionDef)
    }
    present = set(helper_names).intersection(target_names)

    if present and present != set(helper_names):
        raise SystemExit(
            "STOP: staged source has only part of the Connect model-lock helper set."
        )

    if not present:
        # The canonical Voice backend intentionally remains unmodified. Connect adds
        # its private lock resolver only to the staged backend copy.
        imports_tree = ast.parse(text)
        has_sys = any(
            isinstance(node, ast.Import) and
            any(alias.name == "sys" for alias in node.names)
            for node in imports_tree.body
        )
        if not has_sys:
            typing_anchor = "from typing import"
            if text.count(typing_anchor) != 1:
                raise SystemExit("STOP: cannot safely add the frozen-resource sys import.")
            text = text.replace(typing_anchor, "import sys\n" + typing_anchor, 1)

        anchor = (
            'MODEL_ID = os.getenv("WHISPER_MODEL_ID", '
            '"mlx-community/whisper-large-v3-turbo")'
        )
        if text.count(anchor) != 1:
            raise SystemExit("STOP: expected one canonical Voice MODEL_ID declaration.")

        functions = [
            _rdt_extract_top_level_function(reference, name)
            for name in helper_names
        ]
        declarations = (
            "_RDT_MODEL_LOCK_CACHE: Optional[dict] = None\n"
            "_RDT_MODEL_LOCK_PATH: Optional[pathlib.Path] = None"
        )
        injected = declarations + "\n\n" + "\n\n".join(functions)
        text = text.replace(anchor, anchor + "\n\n" + injected, 1)

    # Replace the moving-main downloader with the exact reviewed implementation.
    if not _rdt_function_is_revision_pinned(text, "_download_model_only"):
        pinned_downloader = _rdt_extract_top_level_function(
            reference, "_download_model_only"
        )
        text = _rdt_replace_top_level_function(
            text, "_download_model_only", pinned_downloader
        )

    if not _rdt_function_is_revision_pinned(text, "_download_model_only"):
        raise SystemExit("STOP: staged downloader is not fully revision-pinned.")

    # Both warm-up and real transcription must resolve the same verified local snapshot.
    direct_calls = "path_or_hf_repo=MODEL_ID"
    resolved_calls = (
        "path_or_hf_repo=_rdt_resolve_model_snapshot(MODEL_ID)"
    )
    direct_count = text.count(direct_calls)
    resolved_count = text.count(resolved_calls)

    if direct_count == 2 and resolved_count == 0:
        text = text.replace(direct_calls, resolved_calls)
    elif direct_count == 0 and (resolved_count == 2 or (resolved_count == 1 and "_warm_path" not in text)):
        pass
    else:
        raise SystemExit(
            "STOP: expected exactly two model inference resolver call sites; "
            f"found direct={direct_count}, resolved={resolved_count}."
        )

    try:
        compile(text, "<staged-rdt-connect-backend>", "exec")
    except SyntaxError as exc:
        raise SystemExit(f"STOP: staged model-reliability source is invalid: {exc}") from None

    return text


_CONNECT_SINGLE_PROCESS_GUARD = r"""
# RDT Connect single-process guard restored from the 1D34 baseline.
import sys as _rdt_sys
import sys
import threading
import multiprocessing.resource_tracker as _rdt_resource_tracker

def _rdt_disable_resource_tracking(*_args, **_kwargs):
    return None

_rdt_resource_tracker.register = _rdt_disable_resource_tracking
_rdt_resource_tracker.unregister = _rdt_disable_resource_tracking
_rdt_tracker = _rdt_resource_tracker._resource_tracker
_rdt_tracker.register = _rdt_disable_resource_tracking
_rdt_tracker.unregister = _rdt_disable_resource_tracking
_rdt_tracker.ensure_running = _rdt_disable_resource_tracking

def _rdt_frozen_helper_guard(argv):
    try:
        index = argv.index("-c", 1)
    except ValueError:
        return
    command = argv[index + 1] if index + 1 < len(argv) else ""
    helpers = (
        "from multiprocessing.resource_tracker import main",
        "from multiprocessing.forkserver import main",
        "import sys; from multiprocessing.forkserver import main",
    )
    if command.startswith(helpers):
        raise SystemExit(0)
    _rdt_sys.stderr.write(
        "[RDT] refusing an unexpected frozen Python -c invocation\n"
    )
    raise SystemExit(64)

if __name__ == "__main__":
    _rdt_frozen_helper_guard(_rdt_sys.argv)

try:
    from tqdm import tqdm as _rdt_std_tqdm
    _rdt_std_tqdm.set_lock(threading.RLock())
except ImportError:
    _rdt_std_tqdm = None
"""

def _apply_connect_single_process_guard(text: str) -> str:
    marker = "# RDT Connect single-process guard restored from the 1D34 baseline."
    required = (
        "_rdt_resource_tracker.register = _rdt_disable_resource_tracking",
        "_rdt_tracker.ensure_running = _rdt_disable_resource_tracking",
        "from multiprocessing.forkserver import main",
        "_rdt_std_tqdm.set_lock(threading.RLock())",
    )
    if marker in text:
        if not all(item in text for item in required):
            raise SystemExit("STOP: existing single-process guard is incomplete.")
        return text

    tree = ast.parse(text)
    body = tree.body
    if not body:
        raise SystemExit("STOP: backend source has no module body.")

    insertion_line = 0
    index = 0
    if (
        isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        insertion_line = body[0].end_lineno
        index = 1

    while index < len(body):
        node = body[index]
        if isinstance(node, ast.ImportFrom) and node.module == "__future__":
            insertion_line = max(insertion_line, node.end_lineno)
            index += 1
            continue
        break

    if insertion_line == 0:
        insertion_line = body[0].lineno - 1

    lines = text.splitlines(keepends=True)
    offset = sum(len(line) for line in lines[:insertion_line])
    guard = _CONNECT_SINGLE_PROCESS_GUARD.strip()
    result = text[:offset] + "\n" + guard + "\n\n" + text[offset:]
    compile(result, "<connect-single-process-guard>", "exec")
    return result


def _remove_connect_startup_warmup(text: str) -> str:
    start_marker = "# Warmup\n"
    end_marker = "\ndef clean_transcript_enhanced"
    if start_marker not in text:
        return text
    if text.count(start_marker) != 1:
        raise SystemExit("STOP: ambiguous startup warmup block.")
    start = text.index(start_marker)
    end = text.find(end_marker, start)
    if end < 0:
        raise SystemExit("STOP: startup warmup end marker not found.")
    result = (
        text[:start]
        + "# RDT Connect: defer model loading until real transcription.\n"
        + text[end + 1:]
    )
    if "_warm_path" in result:
        raise SystemExit("STOP: synthetic warmup path remains after removal.")
    compile(result, "<connect-without-startup-warmup>", "exec")
    return result




def _apply_connect_tiny_download_model_id(text: str) -> str:
    """Align Connect's staged download CLI with the shared Tiny model-lock ID."""
    tree = ast.parse(text)
    matches = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr != "add_argument" or not node.args:
            continue
        arg = node.args[0]
        if isinstance(arg, ast.Constant) and arg.value == "--download-model":
            choices = next((kw.value for kw in node.keywords if kw.arg == "choices"), None)
            matches.append((node, choices))
    if len(matches) != 1 or not isinstance(matches[0][1], ast.List):
        raise SystemExit("STOP: expected one literal --download-model choices list.")

    node, choices = matches[0]
    old_id = "mlx-community/whisper-tiny"
    new_id = "mlx-community/whisper-tiny-mlx"
    values = [
        x.value for x in choices.elts
        if isinstance(x, ast.Constant) and isinstance(x.value, str)
    ]
    if new_id in values and old_id not in values:
        return text
    if values.count(old_id) != 1 or new_id in values:
        raise SystemExit("STOP: Tiny model IDs in CLI allowlist are ambiguous.")

    lines = text.splitlines(keepends=True)
    block = "".join(lines[node.lineno - 1:node.end_lineno])
    literal = '"mlx-community/whisper-tiny"'
    if block.count(literal) != 1:
        raise SystemExit("STOP: Tiny source literal is not unique.")
    lines[node.lineno - 1:node.end_lineno] = [
        block.replace(literal, '"mlx-community/whisper-tiny-mlx"', 1)
    ]
    result = "".join(lines)
    compile(result, "<connect-tiny-cli-adapter>", "exec")
    return result


def patch(source: pathlib.Path) -> str:
    text = source.read_text(encoding="utf-8")
    text = _apply_connect_tiny_download_model_id(text)
    text = _apply_connect_single_process_guard(text)
    text = _apply_connect_model_reliability(text)
    text = _remove_connect_startup_warmup(text)
    exchange_route = '@app.post("/v1/browser-handoff/exchange")'
    audio_route = '@app.post("/v1/audio/transcriptions")'
    if text.count(exchange_route) != 1 or text.count(audio_route) != 1:
        raise SystemExit("STOP: expected unique browser-handoff and transcription routes.")

    if "def _rdt_desktop_loopback_origin(" not in text:
        helper = r'''
def _rdt_desktop_loopback_origin(origin, host):
    if not origin or not host:
        return False
    try:
        from urllib.parse import urlsplit
        parsed = urlsplit(origin)
        request_host = urlsplit("//" + host)
        origin_name = (parsed.hostname or "").lower().strip("[]")
        request_name = (request_host.hostname or "").lower().strip("[]")
        loopbacks = {"127.0.0.1", "localhost", "::1"}
        return (
            parsed.scheme == "https"
            and not parsed.username and not parsed.password
            and not parsed.path and not parsed.query and not parsed.fragment
            and not request_host.username and not request_host.password
            and not request_host.path
            and origin_name in loopbacks
            and request_name in loopbacks
            and origin_name == request_name
            and parsed.port is not None
            and parsed.port == request_host.port
        )
    except (ValueError, TypeError):
        return False

'''
        text = text.replace(exchange_route, helper + exchange_route, 1)

    if '@app.get("/assets/{asset_path:path}")' not in text:
        app_anchor = "app = FastAPI()\n"
        if text.count(app_anchor) != 1:
            raise SystemExit("STOP: expected exactly one FastAPI app declaration.")
        text = text.replace(app_anchor, app_anchor + ASSET_ROUTE, 1)

    benchmark_routes = (
        '@app.get("/v1/benchmark/capabilities")',
        '@app.post("/v1/benchmark/lease")',
        '@app.post("/v1/benchmark/lease/renew")',
        '@app.post("/v1/benchmark/lease/release")',
    )
    if "# RDT Connect benchmark lease API v1." not in text:
        text = text.replace(audio_route, BENCHMARK_API + audio_route, 1)
    for route in benchmark_routes:
        if text.count(route) != 1:
            raise SystemExit(f"STOP: benchmark route missing or duplicated: {route}")

    tree = ast.parse(text)
    trans = next(
        (node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "trans"),
        None,
    )
    if trans is None:
        raise SystemExit("STOP: transcription handler not found.")

    argument_names = {arg.arg for arg in trans.args.args}
    missing = []
    if "save_recording" not in argument_names:
        missing.append("save_recording: Optional[bool]=Form(default=None)")
    if "benchmark_session" not in argument_names:
        missing.append('benchmark_session: Optional[str]=Header(None, alias="RDT-Benchmark-Session")')
    if missing:
        lines = text.splitlines(keepends=True)
        signature_line = lines[trans.lineno - 1]
        if not signature_line.lstrip().startswith("def trans("):
            raise SystemExit("STOP: transcription signature anchor is not the expected function.")
        signature = lines[trans.lineno - 1]
        closing = signature.rfind("):")
        if closing < 0:
            raise SystemExit("STOP: cannot identify transcription signature end.")
        lines[trans.lineno - 1] = signature[:closing] + ", " + ", ".join(missing) + signature[closing:]
        text = "".join(lines)

    tree = ast.parse(text)
    trans = next(
        node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "trans"
    )
    trans_source = ast.get_source_segment(text, trans) or ""
    if "_rdt_require_benchmark_lease(benchmark_session, authorization, tailscale_user_login)" not in trans_source:
        lines = text.splitlines(keepends=True)
        auth_marker = "check_host(host); require_bearer(authorization, tailscale_user_login)"
        auth_lines = [
            index for index in range(trans.lineno - 1, trans.end_lineno)
            if auth_marker in lines[index]
        ]
        if len(auth_lines) != 1:
            raise SystemExit("STOP: transcription auth statement missing or ambiguous.")
        index = auth_lines[0]
        indent = lines[index][:len(lines[index]) - len(lines[index].lstrip())]
        child = indent
        guard = [
            child + "if benchmark_session:\n",
            child + "    _rdt_require_benchmark_lease(benchmark_session, authorization, tailscale_user_login)\n",
            child + "    if save_recording is not False:\n",
            child + '        raise HTTPException(status_code=400, detail="Benchmark requests must explicitly disable recording saves")\n',
            child + "    private_mode = True\n",
            child + "elif save_recording is False:\n",
            child + '    raise HTTPException(status_code=403, detail="No-save benchmark transcription requires an active lease")\n',
        ]
        lines[index + 1:index + 1] = guard
        text = "".join(lines)

    tree = ast.parse(text)
    exchange = next(
        (node for node in tree.body if isinstance(node, ast.FunctionDef)
         and node.name == "exchange_rdt_browser_handoff"),
        None,
    )
    if exchange is None:
        raise SystemExit("STOP: browser-handoff exchange handler not found.")
    original_guard = next(
        (node for node in exchange.body if isinstance(node, ast.If)
         and ast.unparse(node.test) == "origin != _RDT_OFFICIAL_BROWSER_ORIGIN"),
        None,
    )
    patched_guard = any(
        isinstance(node, ast.If) and "_rdt_desktop_loopback_origin" in ast.unparse(node.test)
        for node in exchange.body
    )
    if original_guard is not None:
        lines = text.splitlines(keepends=True)
        line = lines[original_guard.lineno - 1]
        indent = line[:len(line) - len(line.lstrip())]
        lines[original_guard.lineno - 1:original_guard.end_lineno] = [
            indent + "if origin != _RDT_OFFICIAL_BROWSER_ORIGIN and not _rdt_desktop_loopback_origin(origin, host):\n",
            indent + '    raise HTTPException(status_code=403, detail="Browser handoff must originate from the official RDT Voice website or the local RDT Connect interface.")\n',
        ]
        text = "".join(lines)
    elif not patched_guard:
        raise SystemExit("STOP: browser-handoff Origin guard not found.")

    ast.parse(text)
    compile(text, str(source), "exec")

    # RDT Connect benchmark inference contract v2.
    # Restore the verified 1D35 MLX selection and response fields.
    if "# RDT Connect benchmark inference contract v2" not in text:
        tree = ast.parse(text)
        trans = next((n for n in tree.body
                      if isinstance(n, ast.FunctionDef) and n.name == "trans"), None)
        if trans is None:
            raise SystemExit("STOP: transcription handler not found.")

        if "import mlx.core as mx" not in text:
            mlx_import = next((n for n in tree.body
                               if isinstance(n, ast.Import)
                               and any(a.name == "mlx_whisper" for a in n.names)), None)
            if mlx_import is None:
                raise SystemExit("STOP: mlx_whisper import not found.")
            lines = text.splitlines(keepends=True)
            lines.insert(mlx_import.end_lineno, "import mlx.core as mx\n")
            text = "".join(lines)

        if "def _rdt_configure_mlx_device" not in text:
            helper_lines = [
                "# RDT Connect benchmark inference contract v2",
                "RDT_MLX_DEVICE = os.environ.get('RDT_MLX_DEVICE', 'auto').strip().lower()",
                "if RDT_MLX_DEVICE not in {'auto', 'gpu', 'cpu'}:",
                "    raise RuntimeError('RDT_MLX_DEVICE must be auto, gpu, or cpu')",
                "",
                "ACTIVE_INFERENCE_PROVIDER = {'engine': 'mlx-whisper', 'provider': 'not-yet-selected', 'provider_verified': False, 'device': 'unknown', 'accelerated': False, 'requested_device': RDT_MLX_DEVICE, 'fallback_reason': None}",
                "",
                "def _rdt_configure_mlx_device():",
                "    global ACTIVE_INFERENCE_PROVIDER",
                "    requested = RDT_MLX_DEVICE",
                "    if requested == 'cpu':",
                "        mx.set_default_device(mx.cpu)",
                "        active = mx.default_device()",
                "        if active.type != mx.cpu or not bool(mx.is_available(active)):",
                "            raise RuntimeError(f'Requested MLX CPU device unavailable: {active}')",
                "        result = {'engine': 'mlx-whisper', 'provider': 'mlx-cpu-selected', 'provider_verified': True, 'device': str(active), 'accelerated': False, 'requested_device': requested, 'fallback_reason': None}",
                "        ACTIVE_INFERENCE_PROVIDER = result",
                "        return dict(result)",
                "    fallback_reason = None",
                "    try:",
                "        metal_available = bool(mx.metal.is_available())",
                "    except Exception as exc:",
                "        metal_available = False",
                "        fallback_reason = f'Metal availability check failed: {type(exc).__name__}'",
                "    if metal_available:",
                "        try:",
                "            mx.set_default_device(mx.gpu)",
                "            active = mx.default_device()",
                "            if active.type != mx.gpu or not bool(mx.is_available(active)):",
                "                raise RuntimeError(f'Selected MLX GPU device unavailable: {active}')",
                "            result = {'engine': 'mlx-whisper', 'provider': 'mlx-metal-gpu', 'provider_verified': True, 'device': str(active), 'accelerated': True, 'requested_device': requested, 'fallback_reason': None}",
                "            ACTIVE_INFERENCE_PROVIDER = result",
                "            return dict(result)",
                "        except Exception as exc:",
                "            fallback_reason = f'Metal device selection failed: {type(exc).__name__}'",
                "    if requested == 'gpu':",
                "        raise RuntimeError('GPU was explicitly selected, but MLX could not activate Metal: ' + (fallback_reason or 'Metal is unavailable.'))",
                "    mx.set_default_device(mx.cpu)",
                "    active = mx.default_device()",
                "    if active.type != mx.cpu or not bool(mx.is_available(active)):",
                "        raise RuntimeError(f'MLX CPU fallback device unavailable: {active}')",
                "    result = {'engine': 'mlx-whisper', 'provider': 'mlx-cpu-fallback', 'provider_verified': True, 'device': str(active), 'accelerated': False, 'requested_device': requested, 'fallback_reason': fallback_reason or 'MLX Metal is unavailable'}",
                "    ACTIVE_INFERENCE_PROVIDER = result",
                "    return dict(result)",
                "",
            ]
            helper = "\n".join(helper_lines) + "\n"
            tree = ast.parse(text)
            trans = next(n for n in tree.body
                         if isinstance(n, ast.FunctionDef) and n.name == "trans")
            insertion_line = min([d.lineno for d in trans.decorator_list] or [trans.lineno])
            lines = text.splitlines(keepends=True)
            lines.insert(insertion_line - 1, helper + "\n")
            text = "".join(lines)

        tree = ast.parse(text)
        trans = next(n for n in tree.body
                     if isinstance(n, ast.FunctionDef) and n.name == "trans")

        def is_mlx_call(node):
            return (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "transcribe"
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "mlx_whisper")

        assignments = [
            n for n in ast.walk(trans)
            if isinstance(n, (ast.Assign, ast.AnnAssign))
            and n.value is not None
            and any(is_mlx_call(child) for child in ast.walk(n.value))
        ]
        if len(assignments) != 1:
            raise SystemExit(f"STOP: expected one MLX transcription assignment; found {len(assignments)}.")

        parents = {child: parent for parent in ast.walk(trans)
                   for child in ast.iter_child_nodes(parent)}
        ancestor = assignments[0]
        locked = False
        while ancestor in parents:
            ancestor = parents[ancestor]
            if isinstance(ancestor, (ast.With, ast.AsyncWith)) and any(
                isinstance(item.context_expr, ast.Name)
                and item.context_expr.id == "INFERENCE_LOCK"
                for item in ancestor.items
            ):
                locked = True
                break
        if not locked:
            raise SystemExit("STOP: MLX call is not protected by INFERENCE_LOCK.")

        call = assignments[0]
        lines = text.splitlines(keepends=True)
        indent = lines[call.lineno - 1][:len(lines[call.lineno - 1]) - len(lines[call.lineno - 1].lstrip())]
        before = [
            indent + "if benchmark_session:\n",
            indent + "    _rdt_benchmark_provider = _rdt_configure_mlx_device()\n",
            indent + "    if _rdt_benchmark_provider.get('requested_device') not in ('gpu', 'cpu'):\n",
            indent + "        raise HTTPException(status_code=400, detail='Benchmark requires an explicit GPU or CPU candidate')\n",
            indent + "else:\n",
            indent + "    _rdt_benchmark_provider = None\n",
            indent + "_rdt_benchmark_inference_started = time.perf_counter() if benchmark_session else None\n",
        ]
        after = [
            indent + "_rdt_benchmark_inference_seconds = (\n",
            indent + "    round(time.perf_counter() - _rdt_benchmark_inference_started, 6)\n",
            indent + "    if _rdt_benchmark_inference_started is not None else None\n",
            indent + ")\n",
        ]
        lines[call.end_lineno:call.end_lineno] = after
        lines[call.lineno - 1:call.lineno - 1] = before
        text = "".join(lines)

        tree = ast.parse(text)
        trans = next(n for n in tree.body
                     if isinstance(n, ast.FunctionDef) and n.name == "trans")
        response_dicts = []
        for node in ast.walk(trans):
            if isinstance(node, ast.Return) and isinstance(node.value, ast.Dict):
                keys = {k.value for k in node.value.keys
                        if isinstance(k, ast.Constant) and isinstance(k.value, str)}
                if {"text", "segments"}.issubset(keys):
                    response_dicts.append(node.value)
        if len(response_dicts) != 1:
            raise SystemExit(f"STOP: expected one successful response dictionary; found {len(response_dicts)}.")

        response = response_dicts[0]
        response_keys = {k.value for k in response.keys
                         if isinstance(k, ast.Constant) and isinstance(k.value, str)}
        if response_keys.intersection({"inference_ms", "inference_provider", "metrics"}):
            raise SystemExit("STOP: conflicting benchmark response fields already exist.")

        lines = text.splitlines(keepends=True)
        response_indent = lines[response.lineno - 1][:len(lines[response.lineno - 1]) - len(lines[response.lineno - 1].lstrip())]
        entry_indent = response_indent + "    "
        entry = (
            entry_indent + '**({\n'
            + entry_indent + '    "inference_ms": round(_rdt_benchmark_inference_seconds * 1000.0, 3),\n'
            + entry_indent + '    "inference_provider": _rdt_benchmark_provider,\n'
            + entry_indent + '    "metrics": {"inference_seconds": _rdt_benchmark_inference_seconds},\n'
            + entry_indent + '} if benchmark_session else {}),\n'
        )
        lines.insert(response.end_lineno - 1, entry)
        text = "".join(lines)
        ast.parse(text)
        compile(text, str(source), "exec")

    return patch_socket_first_uvicorn_startup(text)



def patch_socket_first_uvicorn_startup(text: str) -> str:
    """Bind the Rust-selected listener before Uvicorn runs ASGI lifespan."""
    tree = ast.parse(text)
    parents = {
        child: parent
        for parent in ast.walk(tree)
        for child in ast.iter_child_nodes(parent)
    }
    launches = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "uvicorn"
        and node.func.attr == "run"
    ]
    if not launches:
        if "uvicorn.Server(_rdt_uvicorn_config).run(sockets=[_rdt_listen_socket])" in text:
            return text
        raise SystemExit("STOP: production Uvicorn launch was not found.")
    if len(launches) != 1:
        raise SystemExit(f"STOP: expected one production uvicorn.run call; found {len(launches)}.")

    call = launches[0]
    if len(call.args) != 1 or not isinstance(call.args[0], ast.Name) or call.args[0].id != "app":
        raise SystemExit("STOP: Uvicorn launch app argument changed.")
    keywords = {item.arg: item.value for item in call.keywords if item.arg is not None}
    has_kwargs = any(
        item.arg is None and isinstance(item.value, ast.Name) and item.value.id == "kwargs"
        for item in call.keywords
    )
    if set(keywords) != {"host", "port"} or not has_kwargs:
        raise SystemExit("STOP: Uvicorn host/port/TLS kwargs contract changed.")
    if ast.dump(keywords["host"]) != ast.dump(ast.Name(id="_bind_host", ctx=ast.Load())):
        raise SystemExit("STOP: Uvicorn bind-host source changed.")
    expected_port = ast.Attribute(value=ast.Name(id="_args", ctx=ast.Load()),
                                   attr="port", ctx=ast.Load())
    if ast.dump(keywords["port"]) != ast.dump(expected_port):
        raise SystemExit("STOP: Uvicorn port is no longer the Rust-selected CLI port.")

    ancestor = call
    in_main_guard = False
    while ancestor in parents:
        ancestor = parents[ancestor]
        if isinstance(ancestor, ast.If):
            test_node = ancestor.test
            if (isinstance(test_node, ast.Compare)
                and isinstance(test_node.left, ast.Name)
                and test_node.left.id == "__name__"
                and len(test_node.comparators) == 1
                and isinstance(test_node.comparators[0], ast.Constant)
                and test_node.comparators[0].value == "__main__"):
                in_main_guard = True
                break
    if not in_main_guard:
        raise SystemExit("STOP: Uvicorn launch is not inside the __main__ guard.")

    lines = text.splitlines(keepends=True)
    original = lines[call.lineno - 1]
    indent = original[:len(original) - len(original.lstrip())]
    block = [
        "# Bind before Uvicorn's ASGI lifespan can initialize the model/GPU.",
        "import socket as _rdt_socket",
        "_rdt_family = _rdt_socket.AF_INET6 if ':' in _bind_host else _rdt_socket.AF_INET",
        "_rdt_listen_socket = _rdt_socket.socket(_rdt_family, _rdt_socket.SOCK_STREAM)",
        "try:",
        "    if hasattr(_rdt_socket, 'SO_EXCLUSIVEADDRUSE'):",
        "        _rdt_listen_socket.setsockopt(_rdt_socket.SOL_SOCKET, _rdt_socket.SO_EXCLUSIVEADDRUSE, 1)",
        "    else:",
        "        _rdt_listen_socket.setsockopt(_rdt_socket.SOL_SOCKET, _rdt_socket.SO_REUSEADDR, 1)",
        "    try:",
        "        _rdt_listen_socket.bind((_bind_host, _args.port))",
        "        _rdt_listen_socket.listen(2048)",
        "    except OSError as _rdt_bind_error:",
        "        print(f'[RDT] listener bind failed on {_bind_host}:{_args.port}: {_rdt_bind_error}', file=sys.stderr, flush=True)",
        "        raise",
        "    _rdt_listen_socket.setblocking(False)",
        "    _rdt_uvicorn_config = uvicorn.Config(app, host=_bind_host, port=_args.port, **kwargs)",
        "    uvicorn.Server(_rdt_uvicorn_config).run(sockets=[_rdt_listen_socket])",
        "finally:",
        "    _rdt_listen_socket.close()",
    ]
    replacement = "\n".join(indent + item for item in block) + "\n"
    lines[call.lineno - 1:call.end_lineno] = [replacement]
    updated = "".join(lines)
    parsed = ast.parse(updated)
    compile(parsed, "<rdt-connect-staged-backend>", "exec")
    if "uvicorn.run(" in updated or "SO_REUSEPORT" in updated:
        raise SystemExit("STOP: unsafe or duplicate Uvicorn listener path remains.")
    if updated.count("uvicorn.Server(_rdt_uvicorn_config).run(sockets=[_rdt_listen_socket])") != 1:
        raise SystemExit("STOP: socket handoff was not emitted exactly once.")
    return updated


def main() -> int:
    if len(sys.argv) != 3:
        print("Usage: patch_desktop_backend.py SOURCE_SERVER.py STAGED_SERVER.py", file=sys.stderr)
        return 2
    source = pathlib.Path(sys.argv[1]).expanduser().resolve()
    target = pathlib.Path(sys.argv[2]).expanduser().resolve()
    if not source.is_file():
        print(f"ERROR: source server not found: {source}", file=sys.stderr)
        return 2
    staged = patch(source)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(staged, encoding="utf-8")
    print(f"Prepared Connect-only staged backend: {target}")
    print("Canonical RDT Voice backend was not modified.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
