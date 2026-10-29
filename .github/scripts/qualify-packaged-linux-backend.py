#!/usr/bin/env python3
"""Bounded real API transcription test against a packaged Linux backend.

Requires a previously verified GGML model, built CPU worker, FFmpeg and a fixed WAV
fixture. It does one actual transcription request and records observed progress-stage
timings. This is qualification evidence, not a substitute for installer/UI testing.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import secrets
import socket
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import httpx


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def process_tree_rss_kib(root_pid: int) -> int:
    """Sample RSS of backend plus descendants; this is a sampled approximation."""
    try:
        raw = subprocess.check_output(["ps", "-eo", "pid=,ppid=,rss="], text=True, timeout=2)
    except Exception:
        return 0
    parent: dict[int, int] = {}
    rss: dict[int, int] = {}
    for line in raw.splitlines():
        fields = line.split()
        if len(fields) != 3:
            continue
        try:
            pid, ppid, kb = map(int, fields)
        except ValueError:
            continue
        parent[pid] = ppid
        rss[pid] = kb
    selected = {root_pid}
    changed = True
    while changed:
        changed = False
        for pid, ppid in parent.items():
            if ppid in selected and pid not in selected:
                selected.add(pid)
                changed = True
    return sum(rss.get(pid, 0) for pid in selected)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--backend", type=Path, required=True, help="packaged rdt_voice_backend executable")
    parser.add_argument("--worker", type=Path, required=True, help="packaged rdt_whisper_worker executable beside backend")
    parser.add_argument("--frontend-dir", type=Path, required=True, help="packaged rdt_voice_web resource directory")
    parser.add_argument("--model-dir", type=Path, required=True, help="directory holding the verified model and sidecar")
    parser.add_argument("--fixture", type=Path, required=True, help="fixed 16 kHz mono PCM16 WAV")
    parser.add_argument("--output-json", type=Path, required=True)
    parser.add_argument("--model-id", default="mlx-community/whisper-large-v3-turbo", help="existing RDT Connect desktop model ID to test mapping")
    parser.add_argument("--timeout-seconds", type=int, default=900)
    parser.add_argument("--expected-worker-variant", default="cpu",
                        choices=("cpu", "x64-avx2"))
    args = parser.parse_args()

    backend = args.backend.resolve(strict=True)
    worker = args.worker.resolve(strict=True)
    frontend = args.frontend_dir.resolve(strict=True)
    model_dir = args.model_dir.resolve(strict=True)
    fixture = args.fixture.resolve(strict=True)
    for name, path in (("backend", backend), ("worker", worker), ("fixture", fixture),
                       ("frontend index", frontend / "index.html")):
        if not path.is_file():
            raise SystemExit(f"required_{name.replace(' ', '_')}_missing: {path}")
    if not os.access(backend, os.X_OK) or not os.access(worker, os.X_OK):
        raise SystemExit("packaged_backend_or_worker_not_executable")

    # Verify the exact approved model bytes and sidecar using the source registry.
    # Preserve the repository-root import setup used by this standalone script.
    repo_root = Path(__file__).resolve().parents[2]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))
    from backend.whisper_cpp.qualification_support import verify_model_in_directory
    model_id = "large-v3-turbo"
    spec, model_path, sidecar = verify_model_in_directory(model_id, model_dir)

    token = secrets.token_urlsafe(48)
    args.output_json.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="rdt-linux-package-smoke-") as td:
        temp = Path(td)
        port = free_port()
        log_path = args.output_json.with_suffix(".backend.log")
        report = {
            "schema": "rdt-linux-packaged-api-qualification-v1",
            "platform": platform.platform(),
            "machine": platform.machine(),
            "backend_path": str(backend),
            "backend_sha256": sha256(backend),
            "worker_path": str(worker),
            "worker_sha256": sha256(worker),
            "model_id_linux": model_id,
            "model_id_desktop_contract": args.model_id,
            "model_filename": spec["file"],
            "model_sha256": sha256(model_path),
            "model_size_bytes": model_path.stat().st_size,
            "model_revision": sidecar.get("revision"),
            "fixture_sha256": sha256(fixture),
            "fixture_size_bytes": fixture.stat().st_size,
            "timeout_seconds": args.timeout_seconds,
            "stage_observations": [],
            "elapsed_ms_total": None,
            "real_time_factor": None,
            "sampled_peak_backend_tree_rss_kib": 0,
            "sampled_peak_rss_note": "Sampled RSS of packaged backend and observed descendant processes; not an exact OS-wide peak.",
            "actual_inference_completed": False,
        }
        env = os.environ.copy()
        env.update({
            "RDT_AUTH": "on",
            "RDT_TOKEN": token,
            "RDT_TOKEN_FILE": str(temp / "server-token"),
            "RDT_AUTH_DB": str(temp / "auth.sqlite3"),
            "RDT_DATA_ROOT": str(temp / "recordings"),
            "RDT_GGML_MODEL_DIR": str(model_dir),
            "RDT_WHISPER_WORKER_EXE": str(worker),
            "RDT_VOICE_HTML": str(frontend / "index.html"),
            "RDT_TAILSCALE": "0",
            "WHISPER_MODEL_ID": args.model_id,
            "PYTHONUNBUFFERED": "1",
        })
        report["server_environment"] = {
            "RDT_AUTH": "on", "model_dir": str(model_dir), "tailscale": "off",
            "worker_override": str(worker), "tls": "loopback HTTP qualification only",
        }
        with log_path.open("wb") as log:
            proc = subprocess.Popen(
                [str(backend), "--host", "127.0.0.1", "--port", str(port)],
                stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                env=env, start_new_session=True,
            )
            report["backend_pid"] = proc.pid
            base = f"http://127.0.0.1:{port}"
            headers = {"Authorization": f"Bearer {token}", "Host": f"127.0.0.1:{port}"}
            started = time.monotonic()
            peak_rss = 0
            last_progress_key = None
            response_status = None
            try:
                with httpx.Client(timeout=httpx.Timeout(15.0, connect=2.0)) as client:
                    health = None
                    while time.monotonic() - started < 60:
                        if proc.poll() is not None:
                            raise RuntimeError(f"packaged_backend_exited_before_health code={proc.returncode}")
                        try:
                            resp = client.get(base + "/health", headers=headers)
                            if resp.status_code == 200:
                                health = resp.json()
                                break
                        except httpx.HTTPError:
                            pass
                        time.sleep(0.25)
                    if health is None:
                        raise RuntimeError("packaged_backend_health_timeout")
                    report["pre_inference_health"] = health
                    models_resp = client.get(base + "/v1/models", headers=headers)
                    if models_resp.status_code != 200:
                        raise RuntimeError(f"model_route_failed status={models_resp.status_code} body={models_resp.text[:200]}")
                    data = models_resp.json().get("data", [])
                    if not any(item.get("id") == args.model_id for item in data if isinstance(item, dict)):
                        raise RuntimeError("desktop_model_id_not_preserved_in_v1_models")
                    report["models_response"] = models_resp.json()

                    job_id = "linux-qual-" + secrets.token_hex(8)
                    request_start = time.monotonic()
                    def send_transcription():
                        with fixture.open("rb") as audio:
                            with httpx.Client(timeout=httpx.Timeout(float(args.timeout_seconds), connect=10.0)) as transcriber:
                                return transcriber.post(
                                    base + "/v1/audio/transcriptions",
                                    headers=headers,
                                    data={"model": "whisper-1", "language": "en", "job_id": job_id},
                                    files={"file": (fixture.name, audio, "audio/wav")},
                                )
                    with ThreadPoolExecutor(max_workers=1) as pool:
                        future = pool.submit(send_transcription)
                        deadline = request_start + args.timeout_seconds
                        while not future.done():
                            if proc.poll() is not None:
                                raise RuntimeError(f"backend_exited_during_inference code={proc.returncode}")
                            now = time.monotonic()
                            peak_rss = max(peak_rss, process_tree_rss_kib(proc.pid))
                            try:
                                pr = client.get(base + f"/v1/transcription/progress/{job_id}", headers=headers, timeout=2.0)
                                if pr.status_code == 200:
                                    state = pr.json()
                                    key = (state.get("stage"), state.get("label"))
                                    if key != last_progress_key:
                                        observation = {
                                            "elapsed_ms_observed": round((now - request_start) * 1000, 1),
                                            "stage": state.get("stage"),
                                            "label": state.get("label"),
                                            "percent": state.get("percent"),
                                        }
                                        report["stage_observations"].append(observation)
                                        print("STAGE " + json.dumps(observation, ensure_ascii=False), flush=True)
                                        last_progress_key = key
                            except httpx.HTTPError:
                                pass
                            if now > deadline:
                                raise RuntimeError("packaged_api_transcription_timeout")
                            time.sleep(0.4)
                        response = future.result()
                    request_end = time.monotonic()
                    response_status = response.status_code
                    report["api_status_code"] = response.status_code
                    report["elapsed_ms_total"] = round((request_end - request_start) * 1000, 1)
                    if response.status_code != 200:
                        # Response bodies can contain user-derived data; retain status only.
                        raise RuntimeError(f"transcription_http_{response.status_code}")
                    result = response.json()
                    text = str(result.get("text", "")).strip()
                    lower = text.casefold()
                    anchor_presence = {word: word in lower for word in ("quick", "fox")}
                    missing = [word for word, present in anchor_presence.items() if not present]
                    report["transcript_nonempty"] = bool(text)
                    report["transcript_anchor_words_present"] = anchor_presence
                    if not text or missing:
                        raise RuntimeError(f"transcript_quality_anchor_missing={','.join(missing) or 'empty'}")
                    segments = result.get("segments")
                    if not isinstance(segments, list) or not segments:
                        raise RuntimeError("transcription_has_no_segments")
                    if not all(isinstance(seg.get("start"), (int, float)) and isinstance(seg.get("end"), (int, float)) and seg["end"] >= seg["start"] for seg in segments if isinstance(seg, dict)):
                        raise RuntimeError("transcription_segment_timestamps_invalid")
                    duration = float(result.get("duration_original") or 0)
                    if duration <= 0:
                        duration = 0.0
                    report["audio_duration_seconds"] = duration
                    report["segment_count"] = len(segments)
                    report["real_time_factor"] = round(report["elapsed_ms_total"] / 1000.0 / duration, 3) if duration else None
                    # Actual post-inference health must qualify the resident worker/provider.
                    final_health = client.get(base + "/health", headers=headers).json()
                    report["post_inference_health"] = final_health
                    eng = final_health.get("engine") or {}
                    if not eng.get("inference_verified"):
                        raise RuntimeError("health_does_not_mark_real_inference_verified")
                    provider = str(eng.get("provider") or "").casefold()
                    if "cpu" not in provider:
                        raise RuntimeError(f"qualified_provider_not_cpu: {provider or 'unknown'}")
                    variant = str(eng.get("worker_variant") or "").casefold()
                    if variant != args.expected_worker_variant:
                        raise RuntimeError(
                            f"qualified_worker_variant_mismatch: expected={args.expected_worker_variant}; "
                            f"actual={variant or 'unknown'}")
                    report["actual_inference_completed"] = True
                    report["sampled_peak_rss_note"] += " peak_kib is the maximum sampled while this one real API request was active."
                    report["sampled_peak_backend_tree_rss_kib"] = peak_rss
                    report["outcome"] = "PASS_REAL_PACKAGED_API_INFERENCE"
                    print("REAL_INFERENCE_OK " + json.dumps({
                        "model_id": args.model_id, "linux_model_id": model_id,
                        "transcript_nonempty": bool(text),
                        "transcript_anchor_words_present": anchor_presence,
                        "segments": len(segments),
                        "elapsed_ms_total": report["elapsed_ms_total"],
                        "audio_duration_seconds": duration,
                        "real_time_factor": report["real_time_factor"],
                        "sampled_peak_backend_tree_rss_kib": peak_rss,
                        "provider": eng.get("provider"),
                        "worker_variant": eng.get("worker_variant"),
                    }, ensure_ascii=False), flush=True)
            except Exception as exc:
                report["outcome"] = "FAIL"
                report["failure"] = f"{type(exc).__name__}: {str(exc)[:400]}"
                raise
            finally:
                report["sampled_peak_backend_tree_rss_kib"] = max(
                    peak_rss, report["sampled_peak_backend_tree_rss_kib"]
                )
                if proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(timeout=8)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait(timeout=5)
                report["backend_returncode_after_shutdown"] = proc.returncode
                log.flush()
                report["backend_log_tail"] = log_path.read_text(encoding="utf-8", errors="replace")[-12000:]
                # Preserve diagnostic evidence even when the qualification fails.
                args.output_json.parent.mkdir(parents=True, exist_ok=True)
                args.output_json.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        report["outcome"] = report.get("outcome", "PASS_REAL_PACKAGED_API_INFERENCE")
        args.output_json.parent.mkdir(parents=True, exist_ok=True)
        args.output_json.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"REPORT_JSON={args.output_json}", flush=True)
    return 0



if __name__ == "__main__":
    import traceback

    def persist_outer_failure(exc):
        # argparse has already accepted --output-json only inside main().
        # Recover its literal CLI value even when setup fails before report creation.
        try:
            idx = sys.argv.index("--output-json")
            report_path = Path(sys.argv[idx + 1])
        except (ValueError, IndexError):
            print("QUALIFICATION_REPORT_PATH_UNAVAILABLE", file=sys.stderr, flush=True)
            return

        try:
            report_path.parent.mkdir(parents=True, exist_ok=True)
            log_path = report_path.with_suffix(".backend.log")
            report = {}
            if report_path.is_file():
                try:
                    report = json.loads(report_path.read_text(encoding="utf-8"))
                except Exception:
                    report = {}

            report.update({
                "schema": report.get("schema", "rdt-linux-packaged-api-qualification-v1"),
                "outcome": "FAIL",
                "failure": f"{type(exc).__name__}: {str(exc)[:1000]}",
                "outer_failure_traceback": traceback.format_exc()[-20000:],
                "backend_log_path": str(log_path),
            })
            if log_path.is_file():
                report["backend_log_tail"] = log_path.read_text(
                    encoding="utf-8", errors="replace"
                )[-20000:]

            tmp = report_path.with_suffix(report_path.suffix + ".tmp")
            tmp.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n",
                           encoding="utf-8")
            tmp.replace(report_path)
            print(f"REPORT_JSON={report_path}", flush=True)
        except Exception as report_exc:
            print(
                f"QUALIFICATION_REPORT_WRITE_FAILED: "
                f"{type(report_exc).__name__}: {str(report_exc)[:300]}",
                file=sys.stderr, flush=True,
            )

    try:
        raise SystemExit(main())
    except SystemExit as exc:
        if exc.code in (None, 0):
            raise
        persist_outer_failure(exc)
        raise SystemExit(exc.code if isinstance(exc.code, int) else 1)
    except Exception as exc:
        print(
            f"LINUX_PACKAGED_QUALIFICATION_FAILED: "
            f"{type(exc).__name__}: {str(exc)[:400]}",
            file=sys.stderr, flush=True,
        )
        persist_outer_failure(exc)
        raise SystemExit(1)
