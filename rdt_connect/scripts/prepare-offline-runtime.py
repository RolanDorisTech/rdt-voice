#!/usr/bin/env python3
"""Install pinned local browser-runtime assets into a prepared RDT Voice web bundle."""
import argparse
import hashlib
import json
import pathlib
import shutil
import subprocess
import sys

FALLBACK = "https://cdn.jsdelivr.net/npm/@huggingface/transformers@${s.env.version}/dist/"
LOCAL_FALLBACK = '${new URL("./",import.meta.url).href}'
COMPAT_NAME = "rdt-easywhisper-compat-sequential-r71.min.js"

WORKER_REPLACEMENTS = [
    (
        'const TF_DIST = "https://cdn.jsdelivr.net/npm/@huggingface/transformers@3.7.1/dist/";',
        'const TF_DIST = new URL("./assets/rdt-offline-runtime/modern/", import.meta.url).href;',
    ),
    (
        'const ORT_WASM_DIST = "https://cdn.jsdelivr.net/npm/onnxruntime-web@1.22.0-dev.20250409-89f8206ba4/dist/";',
        'const ORT_WASM_DIST = new URL("./assets/rdt-offline-runtime/modern/", import.meta.url).href;',
    ),
    (
        'import { pipeline, env } from "https://cdn.jsdelivr.net/npm/@huggingface/transformers@3.7.1/dist/transformers.min.js";',
        'import { pipeline, env } from "./assets/rdt-offline-runtime/modern/transformers.min.js";',
    ),
    (
        'new URL("./rdt-easywhisper-compat-sequential-r71.min.js", import.meta.url).href',
        'new URL("./assets/rdt-offline-runtime/modern/rdt-easywhisper-compat-sequential-r71.min.js", import.meta.url).href',
    ),
    (
        '"https://cdn.jsdelivr.net/npm/onnxruntime-web@1.17.3/dist/esm/ort.wasm.min.js"',
        'new URL("./assets/rdt-offline-runtime/legacy/ort.wasm.min.js", import.meta.url).href',
    ),
    (
        '"https://cdn.jsdelivr.net/npm/onnxruntime-web@1.17.3/dist/ort-wasm.wasm"',
        'new URL("./assets/rdt-offline-runtime/legacy/ort-wasm.wasm", import.meta.url).href',
    ),
]

def fail(message):
    raise SystemExit("STOP: " + message)

def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

def patch_fallback(text, label):
    wrapped = f"[{FALLBACK}]({FALLBACK})"
    if text.count(wrapped) == 1:
        text = text.replace(wrapped, LOCAL_FALLBACK, 1)
    elif text.count(FALLBACK) == 1:
        text = text.replace(FALLBACK, LOCAL_FALLBACK, 1)
    else:
        fail(f"{label}: expected one CDN fallback, found {text.count(FALLBACK)}")
    if "cdn.jsdelivr.net" in text:
        fail(f"{label}: CDN reference remains after fallback patch")
    return text

def syntax_check(path, node):
    result = subprocess.run(
        [node, "--input-type=module", "--check"],
        input=path.read_text(encoding="utf-8"),
        text=True, capture_output=True,
    )
    if result.returncode:
        fail(f"JavaScript syntax check failed for {path.name}: {result.stderr.strip()}")

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", required=True)
    parser.add_argument("--web-root", required=True)
    parser.add_argument("--notices", required=True)
    args = parser.parse_args()

    repo = pathlib.Path(args.repo_root).resolve()
    web = pathlib.Path(args.web_root).resolve()
    notices = pathlib.Path(args.notices).resolve()
    vendor = repo / "rdt_connect/vendor/rdt-offline-runtime"
    runtime = vendor / "runtime-assets-v1"
    compat = vendor / COMPAT_NAME
    manifest_path = runtime / "runtime-manifest.json"
    destination = web / "assets/rdt-offline-runtime"
    notice_destination = notices / "rdt-offline-runtime"

    if not web.is_dir() or not (web / "worker.js").is_file():
        fail(f"prepared web directory or worker.js missing: {web}")
    if not (web / "assets").is_dir():
        fail("prepared web assets directory is missing")
    if not notices.is_dir():
        fail(f"third-party notices directory must already exist: {notices}")
    if destination.exists() or notice_destination.exists():
        fail("runtime destination already exists; nothing overwritten")
    if not manifest_path.is_file() or not compat.is_file():
        fail("pinned runtime manifest or compatibility module is missing")

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("schema") != "rdt-offline-runtime-v1":
        fail("unexpected runtime manifest schema")
    if sha256(compat) != manifest.get("compatibility_module_sha256"):
        fail("compatibility module SHA-256 differs from pinned manifest")

    for entry in manifest.get("runtime_files", []):
        relative = pathlib.PurePosixPath(entry["path"])
        if relative.is_absolute() or ".." in relative.parts:
            fail(f"unsafe runtime manifest path: {relative}")
        source = runtime.joinpath(*relative.parts).resolve()
        if runtime.resolve() not in source.parents or not source.is_file():
            fail(f"missing runtime file: {relative}")
        if source.stat().st_size != entry["bytes"] or sha256(source) != entry["sha256"]:
            fail(f"runtime file hash/size mismatch: {relative}")

    worker_path = web / "worker.js"
    worker = worker_path.read_text(encoding="utf-8")
    for old, new in WORKER_REPLACEMENTS:
        if worker.count(old) != 1:
            fail(f"worker patch expected one match, found {worker.count(old)}: {old[:90]}")
        worker = worker.replace(old, new, 1)

    modern_transformers = runtime / "modern/transformers.min.js"
    compat_text = compat.read_text(encoding="utf-8")
    transformer_text = modern_transformers.read_text(encoding="utf-8")
    transformer_text = patch_fallback(transformer_text, modern_transformers.name)
    compat_text = patch_fallback(compat_text, compat.name)

    for text, label in ((worker, "worker.js"),
                        (transformer_text, "transformers.min.js"),
                        (compat_text, COMPAT_NAME)):
        if "cdn.jsdelivr.net" in text:
            fail(f"runtime CDN dependency remains in {label}")

    node = shutil.which("node")
    if not node:
        fail("Node.js is required for syntax validation")

    # All inputs, hashes, and text substitutions are validated before creating output.
    destination.mkdir(parents=True)
    shutil.copytree(runtime / "modern", destination / "modern")
    shutil.copytree(runtime / "legacy", destination / "legacy")
    (destination / "modern" / COMPAT_NAME).write_text(compat_text, encoding="utf-8")
    (destination / "modern/transformers.min.js").write_text(
        transformer_text, encoding="utf-8"
    )
    worker_path.write_text(worker, encoding="utf-8")

    syntax_check(worker_path, node)
    syntax_check(destination / "modern/transformers.min.js", node)
    syntax_check(destination / "modern/ort.bundle.min.mjs", node)
    syntax_check(destination / "modern/ort-wasm-simd-threaded.jsep.mjs", node)
    syntax_check(destination / "modern/ort-wasm-simd-threaded.mjs", node)
    syntax_check(destination / "modern" / COMPAT_NAME, node)
    syntax_check(destination / "legacy/ort.wasm.min.js", node)

    js_files = [p for p in destination.rglob("*")
                if p.is_file() and p.suffix in (".js", ".mjs")]
    for p in js_files:
        text = p.read_text(encoding="utf-8", errors="replace")
        if any(host in text.lower() for host in
               ("cdn.jsdelivr.net", "cdnjs.cloudflare.com", "unpkg.com")):
            fail(f"runtime CDN reference remains in {p.relative_to(destination)}")

    shutil.copytree(runtime / "licenses", notice_destination / "licenses")
    shutil.copy2(manifest_path, notice_destination / "source-runtime-manifest.json")
    (notice_destination / "NOTICE.md").write_text(
        "# RDT Voice browser runtime bundled with RDT Connect\n\n"
        "- Transformers.js 3.7.1 — Apache-2.0; license included in `licenses/`.\n"
        "- ONNX Runtime Web 1.22.0-dev.20250409-89f8206ba4 — MIT; license included in `licenses/`.\n"
        "- ONNX Runtime Web 1.17.3 WASM-only fallback — MIT; license included in `licenses/`.\n\n"
        "The runtime JavaScript and WebAssembly files are served locally by the packaged backend.\n"
        "Whisper model weights are not included by this helper; a cold offline machine may still\n"
        "need a model already available locally before it can transcribe without network access.\n",
        encoding="utf-8",
    )

    deployed = []
    for p in sorted(destination.rglob("*")):
        if p.is_file():
            deployed.append({
                "path": p.relative_to(web).as_posix(),
                "bytes": p.stat().st_size,
                "sha256": sha256(p),
            })
    bundle_manifest = {
        "schema": "rdt-offline-runtime-bundle-v1",
        "source_manifest": manifest,
        "compatibility_module_sha256": sha256(destination / "modern" / COMPAT_NAME),
        "deployed_files": deployed,
        "worker_sha256": sha256(worker_path),
        "validation": {
            "javascript_syntax": "passed",
            "runtime_cdn_references": "none found in staged runtime JS/MJS",
        },
    }
    (notice_destination / "bundle-runtime-manifest.json").write_text(
        json.dumps(bundle_manifest, indent=2) + "\n", encoding="utf-8"
    )

    total = sum(item["bytes"] for item in deployed)
    print(f"PASS: prepared {len(deployed)} pinned runtime files ({total:,} bytes)")
    print("PASS: worker and runtime JavaScript syntax checks")
    print("PASS: no CDN references in staged worker/runtime JavaScript")
    print(f"Runtime assets: {destination}")
    print(f"Notices/manifest: {notice_destination}")

if __name__ == "__main__":
    main()
