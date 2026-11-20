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


def patch(source: pathlib.Path) -> str:
    text = source.read_text(encoding="utf-8")
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

    return text


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
