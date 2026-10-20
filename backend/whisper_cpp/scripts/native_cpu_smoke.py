#!/usr/bin/env python3
"""Download a pinned multilingual Tiny GGML artifact and test real native inference.

This is run only on native GitHub-hosted architecture runners. It fails closed if
immutable metadata, candidate SHA-256, full downloaded byte count, worker handshake,
segment text, timestamps, or the architecture report do not match expectations.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import platform
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request

REPO = "ggerganov/whisper.cpp"
REVISION = "fb5ed9369c26eaf08392504d8446da431e0fffb9"
MODEL_FILE = "ggml-tiny-q8_0.bin"
EXPECTED_SHA256 = "c2085835d3f50733e2ff6e4b41ae8a2b8d8110461e18821b09a15c40c42d1cca"
MAX_BYTES = 150_000_000
MAX_LINE = 64 * 1024


def request_json(url: str) -> object:
    request = urllib.request.Request(url, headers={"User-Agent": "RDT-Connect-native-CPU-smoke/0D53", "Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=30) as response:
        if response.status != 200:
            raise RuntimeError(f"metadata_http_{response.status}")
        data = response.read(2 * 1024 * 1024 + 1)
    if len(data) > 2 * 1024 * 1024:
        raise RuntimeError("metadata_response_too_large")
    return json.loads(data.decode("utf-8"))


def verified_model(dest: Path) -> Path:
    query = urllib.parse.urlencode({"recursive": "true", "expand": "true"})
    metadata_url = f"https://huggingface.co/api/models/{REPO}/tree/{REVISION}?{query}"
    listing = request_json(metadata_url)
    if not isinstance(listing, list):
        raise RuntimeError("unexpected_metadata_schema")
    entry = next((x for x in listing if isinstance(x, dict) and x.get("path") == MODEL_FILE), None)
    if not entry:
        raise RuntimeError("pinned_tiny_artifact_missing")
    lfs = entry.get("lfs") or {}
    digest = str(lfs.get("oid", "")).removeprefix("sha256:").lower()
    if len(digest) != 64:
        digest = str(lfs.get("sha256", "")).lower()
    if digest != EXPECTED_SHA256:
        raise RuntimeError("immutable_metadata_sha256_does_not_match_approved_candidate")
    try:
        expected_size = int(entry["size"])
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError("immutable_metadata_missing_file_size") from exc
    if expected_size <= 0 or expected_size > MAX_BYTES:
        raise RuntimeError("immutable_metadata_file_size_out_of_range")

    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.is_file() and dest.stat().st_size == expected_size and _sha256(dest) == EXPECTED_SHA256:
        print(f"MODEL_CACHE_HIT bytes={expected_size} sha256={EXPECTED_SHA256}", flush=True)
        return dest
    part = dest.with_name(dest.name + ".part")
    part.unlink(missing_ok=True)
    url = f"https://huggingface.co/{REPO}/resolve/{REVISION}/{MODEL_FILE}"
    request = urllib.request.Request(url, headers={"User-Agent": "RDT-Connect-native-CPU-smoke/0D53"})
    digest_stream = hashlib.sha256()
    downloaded = 0
    with urllib.request.urlopen(request, timeout=60) as response, part.open("xb") as output:
        if response.status != 200:
            raise RuntimeError(f"model_http_{response.status}")
        content_length = response.headers.get("Content-Length")
        if content_length and content_length.isdigit() and int(content_length) != expected_size:
            raise RuntimeError("model_content_length_mismatch")
        while True:
            chunk = response.read(1024 * 1024)
            if not chunk:
                break
            downloaded += len(chunk)
            if downloaded > expected_size or downloaded > MAX_BYTES:
                raise RuntimeError("model_download_exceeded_metadata_size")
            digest_stream.update(chunk)
            output.write(chunk)
        output.flush()
        os.fsync(output.fileno())
    actual_sha = digest_stream.hexdigest()
    if downloaded != expected_size:
        part.unlink(missing_ok=True)
        raise RuntimeError(f"model_size_mismatch expected={expected_size} actual={downloaded}")
    if actual_sha != EXPECTED_SHA256:
        part.unlink(missing_ok=True)
        raise RuntimeError("downloaded_model_sha256_mismatch")
    os.replace(part, dest)
    print(f"MODEL_VERIFIED bytes={downloaded} sha256={actual_sha} revision={REVISION}", flush=True)
    return dest


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def native_transcription(worker: Path, model: Path, fixture: Path) -> None:
    if not worker.is_file() or not fixture.is_file():
        raise RuntimeError("worker_or_fixture_missing")
    proc = subprocess.Popen([str(worker), "--model", str(model)], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
    lines: queue.Queue[bytes | None | BaseException] = queue.Queue(maxsize=64)
    stderr_buffer = bytearray()
    stderr_lock = threading.Lock()

    def read_stderr() -> None:
        assert proc.stderr is not None
        while True:
            chunk = proc.stderr.read(4096)
            if not chunk:
                return
            with stderr_lock:
                stderr_buffer.extend(chunk)
                if len(stderr_buffer) > 12000:
                    del stderr_buffer[:-12000]

    def stderr_summary() -> str:
        with stderr_lock:
            raw = bytes(stderr_buffer)
        return raw.decode("utf-8", "replace").strip()[-3000:]

    stderr_reader = threading.Thread(target=read_stderr, daemon=True)
    stderr_reader.start()

    def read_stdout() -> None:
        assert proc.stdout is not None
        try:
            while True:
                line = proc.stdout.readline(MAX_LINE + 1)
                if not line:
                    lines.put(None)
                    return
                if len(line) > MAX_LINE or not line.endswith(b"\n"):
                    lines.put(RuntimeError("worker_line_too_long_or_unterminated"))
                    return
                lines.put(line)
        except BaseException as exc:
            lines.put(exc)

    thread = threading.Thread(target=read_stdout, daemon=True)
    thread.start()

    def next_line(timeout: float = 120) -> str:
        try:
            item = lines.get(timeout=timeout)
        except queue.Empty as exc:
            raise RuntimeError("worker_response_timeout") from exc
        if item is None:
            try:
                return_code = proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                return_code = "still-running"
            stderr_reader.join(timeout=2)
            detail = stderr_summary()
            suffix = f"; exit_code={return_code}"
            if detail:
                suffix += f"; stderr_tail={detail}"
            raise RuntimeError("worker_exited_before_protocol_complete" + suffix)
        if isinstance(item, BaseException):
            raise RuntimeError("worker_stdout_protocol_error") from item
        return item.decode("utf-8", "strict").rstrip("\r\n")

    try:
        ready = next_line(timeout=120).split("\t")
        if len(ready) != 3 or ready[0] != "READY" or ready[2] != "whisper.cpp/v1.9.5":
            raise RuntimeError(f"worker_handshake_invalid: {ready[:3]}")
        arch = ready[1]
        machine = (os.environ.get("PROCESSOR_ARCHITECTURE", "") or platform.machine()).lower()
        if machine in {"arm64", "aarch64"}:
            expected = "arm64"
        elif machine in {"amd64", "x86_64", "x64"}:
            expected = "x64"
        else:
            raise RuntimeError(f"runner_architecture_unrecognized: {machine}")
        if arch != expected:
            raise RuntimeError(f"worker_architecture_mismatch expected={expected} actual={arch}")
        print(f"WORKER_ARCHITECTURE={arch}", flush=True)
        request_id = "native-smoke-0d53"
        language = _b64(b"auto")
        audio_path = _b64(os.fsencode(fixture.resolve()))
        assert proc.stdin is not None
        proc.stdin.write(f"TRANSCRIBE\t{request_id}\t{language}\t{audio_path}\n".encode("utf-8"))
        proc.stdin.flush()
        segments: list[tuple[float, float, str]] = []
        language_name: str | None = None
        deadline = time.monotonic() + 300
        while time.monotonic() < deadline:
            line = next_line(timeout=max(1, deadline - time.monotonic()))
            parts = line.split("\t")
            if len(parts) == 5 and parts[0] == "SEGMENT" and parts[1] == request_id:
                start_cs, end_cs = int(parts[2]), int(parts[3])
                text = base64.urlsafe_b64decode(parts[4] + "=" * (-len(parts[4]) % 4)).decode("utf-8", "strict")
                if start_cs < 0 or end_cs < start_cs:
                    raise RuntimeError("worker_segment_timestamp_invalid")
                if text.strip():
                    segments.append((start_cs / 100.0, end_cs / 100.0, text))
            elif len(parts) == 3 and parts[0] == "DONE" and parts[1] == request_id:
                if parts[2] != "-":
                    language_name = base64.urlsafe_b64decode(parts[2] + "=" * (-len(parts[2]) % 4)).decode("utf-8", "strict")
                break
            elif len(parts) >= 3 and parts[0] == "ERROR" and parts[1] == request_id:
                raise RuntimeError(f"worker_inference_error:{parts[2][:64]}")
            else:
                raise RuntimeError("worker_response_schema_mismatch")
        else:
            raise RuntimeError("worker_transcription_timeout")
        if not segments:
            raise RuntimeError("transcription_returned_no_nonempty_segments")
        print(f"TRANSCRIPTION_OK segments={len(segments)} language={language_name or 'unknown'}", flush=True)
        for start, end, text in segments:
            if not (isinstance(start, float) and isinstance(end, float)):
                raise RuntimeError("segment_times_not_numeric")
            print(f"SEGMENT start={start:.2f}s end={end:.2f}s text={text[:100]!r}", flush=True)
    finally:
        try:
            if proc.poll() is None and proc.stdin:
                proc.stdin.write(b"SHUTDOWN\n")
                proc.stdin.flush()
        except Exception:
            pass
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
        stderr_reader.join(timeout=2)
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            if stream:
                stream.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--worker", type=Path, required=True)
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--model", type=Path, required=True)
    args = parser.parse_args()
    try:
        model = verified_model(args.model)
        native_transcription(args.worker.resolve(), model.resolve(), args.fixture.resolve())
    except Exception as exc:
        print(f"NATIVE_CPU_SMOKE_FAILED: {type(exc).__name__}: {str(exc)[:240]}", file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
