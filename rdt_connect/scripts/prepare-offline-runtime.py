#!/usr/bin/env python3
"""Install pinned local browser-runtime assets into a prepared RDT Voice web bundle."""
import argparse
import base64
import hashlib
import html
import os
import json
import pathlib
import re
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


def refresh_inline_script_csp(document):
    """Add CSP SHA-256 sources for every final inline script block."""
    meta_re = re.compile(
        r"<meta\b(?=[^>]*\bhttp-equiv\s*=\s*['\"]Content-Security-Policy['\"])[^>]*>",
        re.I,
    )
    metas = list(meta_re.finditer(document))
    if len(metas) != 1:
        fail(f"expected exactly one CSP meta tag, found {len(metas)}")

    scripts = re.findall(r"<script\b([^>]*)>(.*?)</script\s*>", document, re.I | re.S)
    hashes = set()
    for attrs, body in scripts:
        if re.search(r"\bsrc\s*=", attrs, re.I):
            continue
        typ = re.search(r"\btype\s*=\s*['\"]([^'\"]+)['\"]", attrs, re.I)
        if typ and typ.group(1).lower() in (
            "application/json", "application/ld+json", "text/template"
        ):
            continue
        digest = base64.b64encode(hashlib.sha256(body.encode("utf-8")).digest()).decode("ascii")
        hashes.add("'sha256-" + digest + "'")

    tag = metas[0].group(0)
    content = re.search(r"\bcontent\s*=\s*(['\"])(.*?)\1", tag, re.I | re.S)
    if not content:
        fail("CSP meta tag has no quoted content attribute")
    quote, policy = content.group(1), html.unescape(content.group(2))
    directives = [part.strip() for part in policy.split(";") if part.strip()]
    script_elem = next((i for i, d in enumerate(directives)
                        if d.split()[0].lower() == "script-src-elem"), None)
    script_src = next((i for i, d in enumerate(directives)
                       if d.split()[0].lower() == "script-src"), None)
    index = script_elem if script_elem is not None else script_src
    if index is None:
        directives.append("script-src 'self'")
        index = len(directives) - 1

    parts = directives[index].split()
    for digest in sorted(hashes):
        if digest not in parts:
            parts.append(digest)
    directives[index] = " ".join(parts)
    updated = "; ".join(directives)

    replacement = "content=" + quote + html.escape(
        updated, quote=(quote == '"')
    ) + quote
    tag2 = tag[:content.start()] + replacement + tag[content.end():]
    document = document[:metas[0].start()] + tag2 + document[metas[0].end():]

    # Verify each inline script against the effective element directive.
    effective = next((d.split()[1:] for d in directives
                      if d.split()[0].lower() == "script-src-elem"),
                     next((d.split()[1:] for d in directives
                           if d.split()[0].lower() == "script-src"), []))
    missing = hashes.difference(effective)
    if missing:
        fail(f"CSP inline script hashes remain missing: {len(missing)}")
    return document

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

    # Confirm the staged worker still matches the authoritative Alpha source
    # before applying the explicitly recorded offline-runtime transformation.
    embedded_manifest_path = web.parent / "rdt_voice_web_manifest.json"
    if not embedded_manifest_path.is_file():
        fail(f"embedded RDT Voice manifest is missing: {embedded_manifest_path}")
    embedded_manifest = json.loads(embedded_manifest_path.read_text(encoding="utf-8"))
    alpha_source_files = embedded_manifest.get("alpha_source_files")
    if not isinstance(alpha_source_files, dict):
        fail("embedded manifest lacks authoritative alpha_source_files hashes")
    source_index_sha256 = alpha_source_files.get("index.html")
    if not isinstance(source_index_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", source_index_sha256):
        fail("embedded manifest has no valid original Alpha index.html SHA-256")
    index_path = web / "index.html"
    if sha256(index_path) != source_index_sha256:
        fail("staged index.html does not match the verified Alpha source before transformation")

    # Apply the Connect-only offline marker to the hash-verified staged Alpha
    # index. Never substitute an older checked-in embedded HTML copy.
    index_html = index_path.read_text(encoding="utf-8")
    marker = '<meta name="rdt-connect-offline-easywhisper" content="disabled">'
    if index_html.count(marker) != 0 or index_html.count("</head>") != 1:
        fail("verified Alpha index already has the offline marker or has an invalid head section")
    for guard in (
        "RDT_CONNECT_EASYWHISPER_DISABLED",
        'setEasySetupChoice("server")',
        'easySetupPath === "easy"',
        "rolandoristech.github.io/rdt-voice/"
    ):
        if guard not in index_html:
            fail(f"verified Alpha index is missing Connect protection: {guard}")
    index_html = index_html.replace("</head>", marker + "\n</head>", 1)
    index_html = refresh_inline_script_csp(index_html)
    index_path.write_text(index_html, encoding="utf-8")

    source_worker_sha256 = alpha_source_files.get("worker.js")
    if not isinstance(source_worker_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", source_worker_sha256):
        fail("embedded manifest has no valid original Alpha worker.js SHA-256")
    worker_path = web / "worker.js"
    if sha256(worker_path) != source_worker_sha256:
        fail("staged worker.js does not match the verified Alpha source before transformation")
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

    # The Alpha source hashes above remain immutable. The embedded files map,
    # however, must describe the final transformed bundle actually shipped.
    final_files = {}
    for staged_file in sorted(web.rglob("*")):
        if staged_file.is_file() and staged_file.name != ".DS_Store":
            relative = staged_file.relative_to(web).as_posix()
            final_files[relative] = sha256(staged_file)

    embedded_manifest["files"] = final_files
    embedded_manifest["file_count"] = len(final_files)
    embedded_manifest["transformed_files"] = {
        "worker.js": {
            "transform": "rdt-offline-runtime-worker-v1",
            "source_sha256": source_worker_sha256,
            "staged_sha256": sha256(worker_path),
        },
        "index.html": {
            "transform": "rdt-connect-offline-easywhisper-v2",
            "source_sha256": source_index_sha256,
            "staged_sha256": sha256(index_path),
        },
    }
    embedded_manifest["offline_runtime"] = {
        "schema": bundle_manifest["schema"],
        "manifest": "third_party_notices/rdt-offline-runtime/bundle-runtime-manifest.json",
    }
    manifest_tmp = embedded_manifest_path.with_name(
        embedded_manifest_path.name + ".tmp"
    )
    manifest_tmp.write_text(
        json.dumps(embedded_manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(manifest_tmp, embedded_manifest_path)

    # Fail closed: verify every deployed runtime entry and reject extras.
    runtime_manifest_path = notice_destination / "bundle-runtime-manifest.json"
    saved_runtime_manifest = json.loads(runtime_manifest_path.read_text(encoding="utf-8"))
    entries = saved_runtime_manifest.get("deployed_files")
    if not isinstance(entries, list) or len(entries) != len(deployed) or not entries:
        fail("deployed runtime manifest is empty or has an unexpected entry count")

    expected_runtime = {}
    for entry in entries:
        relative = pathlib.PurePosixPath(entry.get("path", ""))
        if relative.is_absolute() or ".." in relative.parts:
            fail(f"unsafe deployed runtime path: {relative}")
        name = relative.as_posix()
        if name in expected_runtime:
            fail(f"duplicate deployed runtime path: {name}")
        expected_runtime[name] = entry

    actual_runtime = {}
    for staged_file in sorted(destination.rglob("*")):
        if staged_file.is_file():
            name = staged_file.relative_to(web).as_posix()
            actual_runtime[name] = staged_file

    if set(actual_runtime) != set(expected_runtime):
        missing = sorted(set(expected_runtime) - set(actual_runtime))
        extra = sorted(set(actual_runtime) - set(expected_runtime))
        fail(f"runtime inventory mismatch; missing={missing[:5]}, extra={extra[:5]}")

    for name, entry in expected_runtime.items():
        staged_file = web / name
        if staged_file.stat().st_size != entry.get("bytes"):
            fail(f"deployed runtime size mismatch: {name}")
        if sha256(staged_file) != entry.get("sha256"):
            fail(f"deployed runtime SHA-256 mismatch: {name}")

    if sha256(worker_path) != saved_runtime_manifest.get("worker_sha256"):
        fail("transformed worker hash differs from runtime bundle manifest")

    # Verify the complete embedded web-file inventory, not only runtime files.
    final_manifest = json.loads(embedded_manifest_path.read_text(encoding="utf-8"))
    recorded_files = final_manifest.get("files")
    if not isinstance(recorded_files, dict) or not recorded_files:
        fail("final embedded manifest has no file hashes")
    actual_files = {}
    for staged_file in sorted(web.rglob("*")):
        if staged_file.is_file() and staged_file.name != ".DS_Store":
            actual_files[staged_file.relative_to(web).as_posix()] = sha256(staged_file)
    if actual_files != recorded_files:
        fail("final embedded manifest file inventory or hashes do not match staged web tree")
    transformed = final_manifest.get("transformed_files", {})
    if transformed.get("worker.js", {}).get("staged_sha256") != sha256(worker_path):
        fail("embedded transformed worker hash does not match staged worker")
    index_transform = transformed.get("index.html", {})
    if index_transform.get("staged_sha256") != sha256(index_path):
        fail("embedded transformed index hash does not match staged index")
    if index_path.read_text(encoding="utf-8").count('<meta name="rdt-connect-offline-easywhisper" content="disabled">') != 1:
        fail("offline EasyWhisper marker must occur exactly once in staged index.html")

    print(f"PASS: verified all {len(entries)} deployed runtime files and hashes")
    print(f"PASS: verified all {len(actual_files)} embedded web files and hashes")
    print("PASS: transformed worker hash matches both manifests")

    total = sum(item["bytes"] for item in deployed)
    print(f"PASS: prepared {len(deployed)} pinned runtime files ({total:,} bytes)")
    print("PASS: worker and runtime JavaScript syntax checks")
    print("PASS: no CDN references in staged worker/runtime JavaScript")
    print(f"Runtime assets: {destination}")
    print(f"Notices/manifest: {notice_destination}")

if __name__ == "__main__":
    main()
