"""Focused regression tests for the Linux transcription server and shared worker recovery.

These tests use deterministic synthetic PCM and mocked subprocess IPC. They do not
claim to exercise real whisper.cpp inference or platform-specific native process behavior.
"""
from __future__ import annotations

import importlib
import io
import asyncio
import atexit
import time
import httpx
import os
import queue
import subprocess
import sys
import wave
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest import mock

import numpy as np
from fastapi import HTTPException
from starlette.datastructures import Headers, UploadFile
from starlette.requests import Request

def shutil_which(command: str) -> str | None:
    import shutil
    return shutil.which(command)


ROOT = Path(__file__).resolve().parents[3]
TEST_STATE = tempfile.TemporaryDirectory(prefix="rdt-linux-server-tests-")
atexit.register(TEST_STATE.cleanup)
STATE_ROOT = Path(TEST_STATE.name)
os.environ["RDT_DATA_ROOT"] = str(STATE_ROOT / "recordings")
os.environ["RDT_TOKEN_FILE"] = str(STATE_ROOT / "server-token")
os.environ["RDT_AUTH_DB"] = str(STATE_ROOT / "auth.sqlite3")
os.environ["RDT_AUTH"] = "off"
os.environ["RDT_TAILSCALE_AUTH"] = "off"

sys.path.insert(0, str(ROOT / "backend" / "whisper_cpp"))
sys.path.insert(0, str(ROOT / "backend" / "linux"))
_server_spec = importlib.util.spec_from_file_location("rdt_linux_server", ROOT / "backend" / "linux" / "server.py")
if _server_spec is None or _server_spec.loader is None:
    raise RuntimeError("unable to load Linux server module for tests")
server = importlib.util.module_from_spec(_server_spec)
sys.modules[_server_spec.name] = server
_server_spec.loader.exec_module(server)
engine_module = importlib.import_module("engine")


class LinuxModelIdCompatibilityTests(unittest.TestCase):
    def test_existing_desktop_model_ids_map_to_verified_linux_logical_ids(self):
        expected = {
            "mlx-community/whisper-tiny-mlx": "tiny",
            "mlx-community/whisper-base-mlx": "base",
            "mlx-community/whisper-small-mlx": "small",
            "mlx-community/whisper-large-v3-turbo": "large-v3-turbo",
            "mlx-community/whisper-large-v3-mlx": "large-v3",
        }
        self.assertEqual(server.LINUX_MODEL_ALIASES, expected)
        for desktop_id, linux_id in expected.items():
            with self.subTest(desktop_id=desktop_id):
                self.assertEqual(server.normalize_linux_model_id(desktop_id), linux_id)
                self.assertIn(linux_id, server.LINUX_MODELS)

    def test_native_linux_ids_remain_accepted_and_unknown_ids_fail_closed(self):
        for model_id in server.LINUX_MODELS:
            with self.subTest(model_id=model_id):
                self.assertEqual(server.normalize_linux_model_id(model_id), model_id)
        with self.assertRaisesRegex(ValueError, "unsupported_linux_model_id"):
            server.normalize_linux_model_id("untrusted/model")

    def test_download_cli_accepts_existing_desktop_model_id(self):
        with mock.patch.object(sys, "argv", ["server.py", "--download-model", "mlx-community/whisper-large-v3-turbo"]):
            args = server._parse_args()
        self.assertEqual(server.normalize_linux_model_id(args.download_model), "large-v3-turbo")


class LinuxAudioProcessingTests(unittest.TestCase):
    def test_frame_rms_matches_reference_for_overlapping_integer_frames(self):
        rng = np.random.default_rng(731)
        samples = rng.integers(-30000, 30000, size=16000, dtype=np.int16)
        actual, starts = server.calculate_frame_rms(samples, 16000)
        expected_starts = np.arange(0, len(samples) - 320 + 1, 160, dtype=np.int64)
        expected = np.asarray([
            np.sqrt(np.mean(samples[start:start + 320].astype(np.float64) ** 2))
            for start in expected_starts
        ])
        np.testing.assert_array_equal(starts, expected_starts)
        np.testing.assert_allclose(actual, expected, rtol=1e-12, atol=1e-9)

    def test_frame_rms_handles_short_and_empty_inputs(self):
        self.assertEqual(server.calculate_frame_rms(np.array([], dtype=np.int16), 16000)[0].size, 0)
        rms, starts = server.calculate_frame_rms(np.asarray([100, -100, 100, -100], dtype=np.int16), 16000)
        self.assertEqual(starts.tolist(), [0])
        self.assertAlmostEqual(float(rms[0]), 100.0)
        with self.assertRaisesRegex(ValueError, "sample_rate_invalid"):
            server.calculate_frame_rms(np.ones(500, dtype=np.int16), 0)

    def test_silence_and_quiet_speech_edges(self):
        silence = np.zeros(8000, dtype=np.int16)
        kept, cuts, info = server.detect_long_silences(silence, 16000)
        self.assertEqual(kept, [])
        self.assertEqual(cuts, [])
        self.assertTrue(info["gate_rejected"])

        # Quiet but consistently present signal should not be classified as silence
        # merely because the peak level is low.
        t = np.arange(8000, dtype=np.float64) / 16000.0
        quiet = np.asarray(60 * np.sin(2 * np.pi * 220 * t), dtype=np.int16)
        kept, _, info = server.detect_long_silences(quiet, 16000)
        self.assertFalse(info.get("gate_rejected", False))
        self.assertEqual(kept, [(0, len(quiet))])

    def test_trimmed_segments_map_back_to_original_timeline(self):
        mapping = [
            {"orig_start": 0.0, "orig_end": 1.0, "trimmed_start": 0.0, "trimmed_end": 1.0},
            {"orig_start": 4.0, "orig_end": 5.0, "trimmed_start": 1.0, "trimmed_end": 2.0},
        ]
        mapped = server._map_trimmed_segments_to_original(
            [
                {"text": "first", "start": 0.2, "end": 0.8},
                {"text": "second", "start": 1.2, "end": 1.7},
                {"text": "join", "start": 0.8, "end": 1.2},
                {"text": "bad", "start": float("nan"), "end": 1.0},
            ],
            mapping,
        )
        self.assertEqual(len(mapped), 3)
        self.assertEqual(mapped[0], {"text": "first", "start": 0.2, "end": 0.8})
        self.assertAlmostEqual(mapped[1]["start"], 4.2)
        self.assertAlmostEqual(mapped[1]["end"], 4.7)
        self.assertAlmostEqual(mapped[2]["start"], 0.8)
        self.assertAlmostEqual(mapped[2]["end"], 4.2)

    def test_mp4_suffix_matches_the_existing_supported_mime_mapping(self):
        self.assertIn(".mp4", server.ALLOWED_AUDIO_SUFFIXES)
        self.assertEqual(server._input_suffix("recording.mp4", "video/mp4"), ".mp4")

    def test_decoded_audio_bounds_are_applied_before_pcm_read(self):
        source = (ROOT / "backend" / "linux" / "server.py").read_text(encoding="utf-8")
        self.assertIn('"-t",f"{decode_limit:.3f}"', source)
        self.assertIn("frames / float(sr) > MAX_DECODED_AUDIO_SECONDS", source)
        self.assertIn("frames * channels * sample_width > MAX_DECODED_PCM_BYTES", source)
        self.assertGreater(server.MAX_DECODED_AUDIO_SECONDS, 0)


class LinuxTranscriptionRouteTests(unittest.TestCase):
    @unittest.skipUnless(server.FFMPEG and Path(server.FFMPEG).exists(), "FFmpeg unavailable in this environment")
    def test_real_ffmpeg_decode_and_trimmed_timestamp_response_with_mocked_inference(self):
        sample_rate = 16000
        t1 = np.arange(int(0.75 * sample_rate), dtype=np.float64) / sample_rate
        t2 = np.arange(int(0.75 * sample_rate), dtype=np.float64) / sample_rate
        first = np.asarray(10000 * np.sin(2 * np.pi * 220 * t1), dtype="<i2")
        silence = np.zeros(int(3.5 * sample_rate), dtype="<i2")
        second = np.asarray(10000 * np.sin(2 * np.pi * 220 * t2), dtype="<i2")
        samples = np.concatenate((first, silence, second))
        wav_bytes = io.BytesIO()
        with wave.open(wav_bytes, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(sample_rate)
            wav.writeframes(samples.tobytes())
        upload = UploadFile(
            file=io.BytesIO(wav_bytes.getvalue()),
            filename="speech-with-silence.wav",
            headers=Headers({"content-type": "audio/wav"}),
        )

        class FakeEngine:
            def transcribe(self, path, language="auto", *, request_id=None):
                with wave.open(str(path), "rb") as filtered:
                    self.filtered_frames = filtered.getnframes()
                    self.filtered_rate = filtered.getframerate()
                self.language = language
                self.request_id = request_id
                return {
                    "text": " hello world",
                    "segments": [
                        {"text": "hello", "start": 0.20, "end": 0.30},
                        {"text": "world", "start": 0.80, "end": 1.00},
                    ],
                    "language": "en",
                }

        fake_engine = FakeEngine()
        created_temp_paths = []
        actual_named_tempfile = server.tempfile.NamedTemporaryFile

        def tracked_named_tempfile(*args, **kwargs):
            result = actual_named_tempfile(*args, **kwargs)
            created_temp_paths.append(result.name)
            return result

        with mock.patch.object(server, "_ensure_engine", return_value=fake_engine), \
             mock.patch.object(server.tempfile, "NamedTemporaryFile", side_effect=tracked_named_tempfile):
            result = server._transcribe_sync(
                file=upload, model="whisper-1", audio_cache_dir=str(server.ALLOWED_ROOT),
                language="en", job_id="route-regression-test", authorization=None,
                host="localhost:8888", origin=None, tailscale_user_login=None,
            )

        self.assertEqual(result["text"], "hello world")
        self.assertIn("duration_original", result)
        self.assertIn("duration_filtered", result)
        self.assertEqual(len(result["segments"]), 2)
        self.assertAlmostEqual(result["segments"][0]["start"], 0.20, delta=0.06)
        self.assertAlmostEqual(result["segments"][1]["start"], 4.40, delta=0.12)
        self.assertAlmostEqual(result["segments"][1]["end"], 4.60, delta=0.12)
        self.assertLess(result["duration_filtered"], result["duration_original"])
        self.assertEqual(fake_engine.language, "en")
        self.assertRegex(fake_engine.request_id, r"^[a-f0-9]{24}$")
        self.assertEqual(fake_engine.filtered_rate, 16000)
        saved = server.ALLOWED_ROOT / str(result.get("recording_name") or "")
        if result.get("recording_name"):
            saved.unlink(missing_ok=True)
        for temp_path in created_temp_paths:
            Path(temp_path).unlink(missing_ok=True)
            Path(temp_path + ".1.wav").unlink(missing_ok=True)
            Path(temp_path + ".2.wav").unlink(missing_ok=True)

    @unittest.skipUnless(server.FFMPEG and Path(server.FFMPEG).exists(), "FFmpeg unavailable in this environment")
    def test_decoded_audio_duration_limit_rejects_before_inference(self):
        sample_rate = 16000
        seconds = 60.2
        samples = np.zeros(int(seconds * sample_rate), dtype="<i2")
        wav_bytes = io.BytesIO()
        with wave.open(wav_bytes, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(sample_rate)
            wav.writeframes(samples.tobytes())
        upload = UploadFile(
            file=io.BytesIO(wav_bytes.getvalue()),
            filename="too-long.wav",
            headers=Headers({"content-type": "audio/wav"}),
        )
        created_temp_paths = []
        actual_named_tempfile = server.tempfile.NamedTemporaryFile

        def tracked_named_tempfile(*args, **kwargs):
            result = actual_named_tempfile(*args, **kwargs)
            created_temp_paths.append(result.name)
            return result

        try:
            with mock.patch.object(server, "MAX_DECODED_AUDIO_SECONDS", 60.0), \
                 mock.patch.object(server, "MAX_DECODED_PCM_BYTES", 60 * sample_rate * 2), \
                 mock.patch.object(server, "_ensure_engine") as ensure_engine, \
                 mock.patch.object(server.tempfile, "NamedTemporaryFile", side_effect=tracked_named_tempfile):
                with self.assertRaises(HTTPException) as caught:
                    server._transcribe_sync(
                        file=upload, model="whisper-1", audio_cache_dir=str(server.ALLOWED_ROOT),
                        language="auto", job_id="decoded-duration-limit-test", authorization=None,
                        host="localhost:8888", origin=None, tailscale_user_login=None,
                    )
            self.assertEqual(caught.exception.status_code, 413)
            ensure_engine.assert_not_called()
        finally:
            for temp_path in created_temp_paths:
                Path(temp_path).unlink(missing_ok=True)
                Path(temp_path + ".1.wav").unlink(missing_ok=True)
                Path(temp_path + ".2.wav").unlink(missing_ok=True)
            for path in server.ALLOWED_ROOT.glob("*_too-long.wav"):
                path.unlink(missing_ok=True)

    def test_invalid_audio_returns_415_and_removes_processing_temp_files(self):
        cache_before = {p.resolve() for p in server.ALLOWED_ROOT.glob("*_broken.wav")}
        upload = UploadFile(
            file=io.BytesIO(b"not a wave file" * 100),
            filename="broken.wav",
            headers=Headers({"content-type": "audio/wav"}),
        )
        created_temp_paths = []
        actual_named_tempfile = server.tempfile.NamedTemporaryFile

        def tracked_named_tempfile(*args, **kwargs):
            result = actual_named_tempfile(*args, **kwargs)
            created_temp_paths.append(result.name)
            return result

        try:
            with mock.patch.object(server.tempfile, "NamedTemporaryFile", side_effect=tracked_named_tempfile):
                with self.assertRaises(HTTPException) as caught:
                    server._transcribe_sync(
                        file=upload, model="whisper-1", audio_cache_dir=str(server.ALLOWED_ROOT),
                        language="auto", job_id="invalid-audio-regression-test", authorization=None,
                        host="localhost:8888", origin=None, tailscale_user_login=None,
                    )
            self.assertEqual(caught.exception.status_code, 415)
            cache_after = {p.resolve() for p in server.ALLOWED_ROOT.glob("*_broken.wav")}
            self.assertEqual(cache_after, cache_before, "invalid audio must not be retained in the recordings cache")
        finally:
            for temp_path in created_temp_paths:
                Path(temp_path).unlink(missing_ok=True)
                Path(temp_path + ".1.wav").unlink(missing_ok=True)
                Path(temp_path + ".2.wav").unlink(missing_ok=True)


class UploadGateAndRetentionTests(unittest.TestCase):
    def _call_gate(self, headers, *, auth_enabled=True):
        scope = {
            "type": "http", "http_version": "1.1", "method": "POST",
            "scheme": "http", "path": "/v1/audio/transcriptions",
            "raw_path": b"/v1/audio/transcriptions", "query_string": b"",
            "headers": [(k.lower().encode("latin-1"), v.encode("latin-1")) for k, v in headers.items()],
            "server": ("localhost", 8888), "client": ("127.0.0.1", 32123),
        }
        receive_calls = []
        async def receive():
            receive_calls.append(True)
            return {"type": "http.request", "body": b"must-not-be-read", "more_body": True}
        request = Request(scope, receive)
        reached_next = []
        async def call_next(_request):
            reached_next.append(True)
            from starlette.responses import JSONResponse
            return JSONResponse({"ok": True})
        old_auth = server.AUTH_ENABLED
        old_token = server.TOKEN
        server.AUTH_ENABLED = auth_enabled
        server.TOKEN = "test-token"
        try:
            response = asyncio.run(server.rdt_transcription_upload_gate(request, call_next))
            return response.status_code, reached_next, receive_calls
        finally:
            server.AUTH_ENABLED = old_auth
            server.TOKEN = old_token

    def test_auth_rejects_before_multipart_body_can_be_read(self):
        status, reached_next, receive_calls = self._call_gate({
            "host": "localhost:8888", "content-length": str(10 * 1024**3),
        })
        self.assertEqual(status, 401)
        self.assertEqual(reached_next, [])
        self.assertEqual(receive_calls, [])

    def test_authenticated_oversized_content_length_is_rejected_before_body_read(self):
        status, reached_next, receive_calls = self._call_gate({
            "host": "localhost:8888", "authorization": "Bearer test-token",
            "content-length": str(server.MAX_UPLOAD_BYTES + 1_048_577),
        })
        self.assertEqual(status, 413)
        self.assertEqual(reached_next, [])
        self.assertEqual(receive_calls, [])

    def test_unknown_content_length_is_rejected(self):
        status, reached_next, receive_calls = self._call_gate({
            "host": "localhost:8888", "authorization": "Bearer test-token",
        })
        self.assertEqual(status, 411)
        self.assertEqual(reached_next, [])
        self.assertEqual(receive_calls, [])

    def test_malformed_content_length_is_rejected_without_reading_body(self):
        status, reached_next, receive_calls = self._call_gate({
            "host": "localhost:8888", "authorization": "Bearer test-token",
            "content-length": "not-a-number",
        })
        self.assertEqual(status, 411)
        self.assertEqual(reached_next, [])
        self.assertEqual(receive_calls, [])

    def test_request_limit_is_two_and_full_capacity_returns_429(self):
        acquired = [server._TRANSCRIPTION_SLOTS.acquire(blocking=False) for _ in range(server.MAX_CONCURRENT_TRANSCRIPTIONS)]
        self.assertTrue(all(acquired))
        try:
            status, reached_next, receive_calls = self._call_gate({
                "host": "localhost:8888", "authorization": "Bearer test-token",
                "content-length": "100",
            })
            self.assertEqual(status, 429)
            self.assertEqual(reached_next, [])
            self.assertEqual(receive_calls, [])
        finally:
            for _ in acquired:
                server._TRANSCRIPTION_SLOTS.release()

    def test_session_middleware_wraps_upload_gate(self):
        names = []
        for item in server.app.user_middleware:
            if "dispatch" in item.kwargs:
                names.append(item.kwargs["dispatch"].__name__)
            else:
                names.append(item.cls.__name__)
        self.assertLess(
            names.index("rdt_browser_session_middleware"),
            names.index("RDTTranscriptionUploadGateMiddleware"),
        )

    def test_health_remains_responsive_while_two_uploads_are_processing(self):
        def slow_fake(**kwargs):
            time.sleep(0.60)
            return {"text": "ok", "segments": [], "job_id": kwargs.get("job_id")}
        async def exercise():
            transport = httpx.ASGITransport(app=server.app)
            async with httpx.AsyncClient(transport=transport, base_url="http://localhost:8888") as client:
                async def post_one(job_id):
                    return await client.post(
                        "/v1/audio/transcriptions",
                        headers={"host": "localhost:8888"},
                        data={"model": "whisper-1", "language": "auto", "job_id": job_id},
                        files={"file": ("request.wav", b"synthetic upload fixture" * 20, "audio/wav")},
                    )
                with mock.patch.object(server, "_transcribe_sync", side_effect=slow_fake):
                    jobs = [asyncio.create_task(post_one(f"health-load-{i}")) for i in range(2)]
                    await asyncio.sleep(0.05)
                    started = time.monotonic()
                    health = await asyncio.wait_for(client.get("/health", headers={"host": "localhost:8888"}), timeout=1.0)
                    elapsed = time.monotonic() - started
                    results = await asyncio.gather(*jobs)
                return health, elapsed, results
        health, elapsed, results = asyncio.run(exercise())
        self.assertEqual(health.status_code, 200)
        self.assertLess(elapsed, 1.0)
        self.assertEqual([r.status_code for r in results], [200, 200])

    def test_progress_label_reports_actual_worker_variant_and_provider(self):
        class FakeEngine:
            def status(self): return {"worker_variant": "vulkan", "provider": "Vulkan"}
        self.assertEqual(server._worker_progress_label(FakeEngine()), "Transcribing with whisper.cpp vulkan worker (Vulkan)")

    def test_recording_quota_bounds_file_count_and_bytes_without_deleting(self):
        with tempfile.TemporaryDirectory(prefix="rdt-recording-quota-") as td:
            root = Path(td)
            (root / "existing.wav").write_bytes(b"x" * 11)
            with mock.patch.object(server, "MAX_RECORDING_CACHE_BYTES", 20), \
                 mock.patch.object(server, "MAX_RECORDING_CACHE_FILES", 2):
                self.assertFalse(server._recording_quota_allows(root, 10))
                self.assertTrue(server._recording_quota_allows(root, 9))
            with mock.patch.object(server, "MAX_RECORDING_CACHE_BYTES", 100), \
                 mock.patch.object(server, "MAX_RECORDING_CACHE_FILES", 1):
                self.assertFalse(server._recording_quota_allows(root, 1))
            self.assertEqual((root / "existing.wav").read_bytes(), b"x" * 11)

    def test_cancellation_flag_stops_work_before_starting_worker(self):
        event = threading.Event()
        event.set()
        with self.assertRaises(server._TranscriptionCancelled):
            server._check_transcription_cancelled("cancel-before-decode", event)

    def test_ffmpeg_child_is_terminated_when_cancellation_arrives(self):
        event = threading.Event()
        event.set()
        class FakeProcess:
            def __init__(self):
                self.returncode = None
                self.terminated = False
                self.killed = False
            def poll(self): return self.returncode
            def terminate(self):
                self.terminated = True
                self.returncode = -15
            def kill(self):
                self.killed = True
                self.returncode = -9
            def communicate(self, timeout=None): return (None, "")
            def wait(self, timeout=None): return self.returncode
        child = FakeProcess()
        with mock.patch.object(server.subprocess, "Popen", return_value=child):
            with self.assertRaises(server._TranscriptionCancelled):
                server._run_ffmpeg_bounded(["ffmpeg", "-i", "input.wav"], event)
        self.assertTrue(child.terminated)
        self.assertFalse(child.killed)

    def test_async_route_cancels_targeted_worker_request_on_client_disconnect(self):
        class DisconnectedRequest:
            async def is_disconnected(self):
                return True
        upload = UploadFile(file=io.BytesIO(b"fixture"), filename="fixture.wav", headers=Headers({"content-type": "audio/wav"}))
        def blocking_fake(**kwargs):
            event = kwargs["cancel_event"]
            if not event.wait(timeout=2.0):
                raise AssertionError("disconnect did not signal cancellation")
            raise HTTPException(status_code=499, detail="cancelled")
        with mock.patch.object(server, "_transcribe_sync", side_effect=blocking_fake), \
             mock.patch.object(server, "_cancel_worker_request", return_value=True) as cancel_worker:
            response = asyncio.run(server.trans(
                request=DisconnectedRequest(), file=upload, model="whisper-1",
                audio_cache_dir=None, language="auto", job_id="disconnect-regression-test",
                authorization=None, host="localhost:8888", origin=None, tailscale_user_login=None,
            ))
        self.assertEqual(response.status_code, 499)
        self.assertTrue(cancel_worker.called)


class ServerEngineRecoveryTests(unittest.TestCase):
    def test_transient_engine_startup_error_can_recover_without_process_restart(self):
        class Lease:
            def __init__(self):
                self.released = False

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                self.released = True

        class FakeEngine:
            def __init__(self, model_path, model_id):
                self.path = model_path
                self.model_id = model_id
                self.stopped = False

            def start(self):
                return None

            def status(self):
                return {"ready": True}

            def stop(self):
                self.stopped = True

        lease = Lease()
        server._shutdown_engine()
        server._ENGINE_ERROR = "previous transient error"
        server._ENGINE_RETRY_AFTER = 0.0
        fake_engine_module = types.SimpleNamespace(PersistentWhisperEngine=FakeEngine)
        with mock.patch.object(server, "_linux_model_usage_lock", return_value=lease), \
             mock.patch.object(server, "_linux_model_path", return_value=STATE_ROOT / "verified-model.bin"), \
             mock.patch.dict(sys.modules, {"engine": fake_engine_module}):
            engine = server._ensure_engine()
        self.assertIsInstance(engine, FakeEngine)
        self.assertIsNone(server._ENGINE_ERROR)
        self.assertEqual(server._ENGINE_RETRY_AFTER, 0.0)
        self.assertFalse(lease.released)
        server._shutdown_engine()
        self.assertTrue(lease.released)

    def test_model_verification_failure_prevents_engine_start(self):
        class Lease:
            def __init__(self): self.released = False
            def __enter__(self): return self
            def __exit__(self, exc_type, exc, tb): self.released = True
        lease = Lease()
        server._shutdown_engine()
        server._ENGINE_ERROR = None
        server._ENGINE_RETRY_AFTER = 0.0
        fake_engine_module = types.SimpleNamespace(PersistentWhisperEngine=mock.Mock())
        try:
            with mock.patch.object(server, "_linux_model_usage_lock", return_value=lease), \
                 mock.patch.object(server, "_linux_model_path", side_effect=RuntimeError("model bytes are not fully verified")), \
                 mock.patch.dict(sys.modules, {"engine": fake_engine_module}):
                with self.assertRaisesRegex(RuntimeError, "Transcription engine is not ready"):
                    server._ensure_engine()
                fake_engine_module.PersistentWhisperEngine.assert_not_called()
            self.assertTrue(lease.released)
            self.assertIsNone(server._ENGINE)
        finally:
            server._ENGINE_ERROR = None
            server._ENGINE_RETRY_AFTER = 0.0


class _FakePipe:
    def __init__(self):
        self.writes = []
        self.closed = False

    def write(self, data):
        self.writes.append(data)
        return len(data)

    def flush(self):
        return None

    def close(self):
        self.closed = True


class _FakeProcess:
    def __init__(self):
        self.stdin = _FakePipe()
        self.stdout = None
        self.stderr = None
        self.returncode = None
        self.terminated = False

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        self.returncode = 0
        return 0

    def terminate(self):
        self.terminated = True
        self.returncode = -15

    def kill(self):
        self.terminated = True
        self.returncode = -9


class PersistentWorkerProtocolTests(unittest.TestCase):
    def setUp(self):
        self.wav = STATE_ROOT / "worker-test.wav"
        self.wav.write_bytes(b"RIFF" + b"\0" * 40)
        self.engine = engine_module.PersistentWhisperEngine(self.wav, "test-model")
        self.process = _FakeProcess()
        self.engine.process = self.process
        self.engine._lines = queue.Queue()

    def tearDown(self):
        self.engine.stop()
        self.wav.unlink(missing_ok=True)

    def call_with_id(self, records, function=None):
        request_id = "a" * 24
        for record in records:
            self.engine._lines.put(record.encode("utf-8") + b"\n")
        fake_uuid = types.SimpleNamespace(hex=request_id)
        patcher = mock.patch.object(engine_module.uuid, "uuid4", return_value=fake_uuid)
        with patcher:
            if function is None:
                return self.engine.transcribe(self.wav, "en")
            return function()

    def test_valid_segment_and_done_records(self):
        result = self.call_with_id([
            "SEGMENT\t" + "a" * 24 + "\t12\t45\tSGVsbG8gV29ybGQ=",
            "DONE\t" + "a" * 24 + "\tZW4=",
        ])
        self.assertEqual(result["text"], "Hello World")
        self.assertEqual(result["segments"], [{"text": "Hello World", "start": 0.12, "end": 0.45}])
        self.assertEqual(result["language"], "en")
        self.assertIs(self.engine.process, self.process)

    def test_request_id_mismatch_kills_worker_to_prevent_queue_contamination(self):
        with self.assertRaisesRegex(RuntimeError, "worker_protocol_response_mismatch"):
            self.call_with_id(["SEGMENT\t" + "b" * 24 + "\t0\t1\teA=="])
        self.assertIsNone(self.engine.process)

    def test_malformed_base64_kills_worker(self):
        with self.assertRaisesRegex(RuntimeError, "worker_protocol_base64_invalid"):
            self.call_with_id([
                "SEGMENT\t" + "a" * 24 + "\t0\t1\t%%%",
            ])
        self.assertIsNone(self.engine.process)

    def test_malformed_timestamp_kills_worker(self):
        with self.assertRaisesRegex(RuntimeError, "worker_protocol_segment_invalid"):
            self.call_with_id(["SEGMENT\t" + "a" * 24 + "\tNaN\t1\teA=="])
        self.assertIsNone(self.engine.process)

    def test_out_of_order_segments_are_rejected(self):
        with self.assertRaisesRegex(RuntimeError, "worker_timestamp_invalid"):
            self.call_with_id([
                "SEGMENT\t" + "a" * 24 + "\t80\t90\teA==",
                "SEGMENT\t" + "a" * 24 + "\t20\t30\teQ==",
            ])
        self.assertIsNone(self.engine.process)

    def test_well_formed_worker_error_does_not_discard_resident_process(self):
        with self.assertRaisesRegex(RuntimeError, "worker_inference_failed"):
            self.call_with_id(["ERROR\t" + "a" * 24 + "\tinference_failed"])
        self.assertIs(self.engine.process, self.process)

    def test_timeout_stops_worker(self):
        with mock.patch.object(engine_module, "TRANSCRIPTION_TIMEOUT_SECONDS", 0.01):
            with self.assertRaisesRegex(RuntimeError, "worker_transcription_timeout"):
                self.call_with_id([])
        self.assertIsNone(self.engine.process)

    def test_worker_eof_is_treated_as_crash_and_stops_process(self):
        self.engine._lines.put(None)
        with mock.patch.object(engine_module.uuid, "uuid4", return_value=types.SimpleNamespace(hex="a" * 24)):
            with self.assertRaisesRegex(RuntimeError, "persistent_worker_crashed"):
                self.engine.transcribe(self.wav, "en")
        self.assertIsNone(self.engine.process)
        self.assertTrue(self.process.stdin.closed)

    def test_shutdown_closes_worker_pipes(self):
        self.engine.stop()
        self.assertIsNone(self.engine.process)
        self.assertTrue(self.process.stdin.closed)

    def test_cpp_worker_preserves_cancel_before_request_activation(self):
        source = (ROOT / "backend" / "whisper_cpp" / "worker" /
                  "rdt_whisper_worker.cpp").read_text(encoding="utf-8")
        self.assertIn("g_known_request_ids.insert(command_fields[1])", source)
        self.assertIn("g_cancel_requested_ids.insert(fields[1]);", source)
        self.assertIn("g_cancel_requested_ids.erase(id) > 0", source)
        self.assertIn(
            "g_cancel.store(cancel_before_activation || shutting_down,",
            source,
        )

    def test_transcribe_is_not_active_before_initial_write_lock(self):
        threading = __import__("threading")
        request_id = "writer_race_123"
        self.engine._lines.put(
            f"DONE\t{request_id}\tZW4=".encode("utf-8") + b"\n"
        )
        entered = threading.Event()
        allow_write = threading.Event()

        class GateLock:
            def __init__(self):
                self.lock = threading.Lock()

            def __enter__(self):
                entered.set()
                if not allow_write.wait(4):
                    raise RuntimeError("test_write_gate_timeout")
                self.lock.acquire()
                return self

            def __exit__(self, exc_type, exc, tb):
                self.lock.release()

        self.engine._write_lock = GateLock()
        result = {}

        def invoke():
            try:
                result["value"] = self.engine.transcribe(
                    self.wav, "en", request_id=request_id
                )
            except BaseException as exc:
                result["error"] = exc

        thread = threading.Thread(target=invoke, daemon=True)
        thread.start()
        reached_writer = entered.wait(2)
        try:
            with self.engine._state_lock:
                active_while_waiting = self.engine._active_id
            early_cancel = self.engine.cancel(request_id)
        finally:
            allow_write.set()
        thread.join(timeout=5)

        self.assertTrue(reached_writer, "transcription never reached its writer")
        self.assertIsNone(active_while_waiting)
        self.assertFalse(early_cancel)
        self.assertFalse(thread.is_alive(), "transcription thread did not finish")
        self.assertNotIn("error", result, repr(result.get("error")))
        self.assertEqual(result["value"]["language"], "en")
        self.assertTrue(self.process.stdin.writes[0].startswith(
            b"TRANSCRIBE\t" + request_id.encode("ascii") + b"\t"
        ))

    def test_cancel_only_targets_current_request_and_sends_protocol_record(self):
        self.assertFalse(self.engine.cancel())
        with self.engine._state_lock:
            self.engine._active_id = "c" * 24
        self.assertTrue(self.engine.cancel("c" * 24))
        self.assertIn(b"CANCEL\t" + b"c" * 24 + b"\n", self.process.stdin.writes)
        self.assertIn("c" * 24, self.engine._cancel_requested_ids)
        self.assertFalse(self.engine.cancel("d" * 24))


    def test_explicit_request_id_is_used_for_targeted_server_cancellation(self):
        request_id = "serverjob_123"
        self.engine._lines.put(f"DONE\t{request_id}\tZW4=".encode() + b"\n")
        result = self.engine.transcribe(self.wav, "en", request_id=request_id)
        self.assertEqual(result["language"], "en")
        self.assertIn(f"TRANSCRIBE\t{request_id}\t".encode(), self.process.stdin.writes[0])

    def test_invalid_explicit_request_id_is_rejected_before_worker_start(self):
        self.engine.process = None
        with mock.patch.object(self.engine, "start") as start:
            with self.assertRaisesRegex(RuntimeError, "worker_request_id_invalid"):
                self.engine.transcribe(self.wav, "en", request_id="bad\tid")
        start.assert_not_called()

    def test_posix_worker_candidate_order_and_linux_arm_capability_are_explicit(self):
        with tempfile.TemporaryDirectory(prefix="rdt-worker-candidates-") as td:
            fake_file = Path(td) / "placeholder"
            fake_file.write_text("", encoding="utf-8")
            with mock.patch.dict(os.environ, {"RDT_WHISPER_WORKER_EXE": ""}), \
                 mock.patch.object(engine_module.sys, "frozen", False, create=True), \
                 mock.patch.object(engine_module.platform, "machine", return_value="x86_64"), \
                 mock.patch.object(engine_module.sys, "platform", "linux"):
                candidates = engine_module.PersistentWhisperEngine.worker_candidates()
            self.assertEqual([p.name for p in candidates], [
                "rdt_whisper_worker_cuda", "rdt_whisper_worker_vulkan", "rdt_whisper_worker",
            ])
            with mock.patch.dict(os.environ, {"RDT_WHISPER_WORKER_EXE": ""}), \
                 mock.patch.object(engine_module.sys, "frozen", False, create=True), \
                 mock.patch.object(engine_module.platform, "machine", return_value="aarch64"), \
                 mock.patch.object(engine_module.sys, "platform", "linux"), \
                 mock.patch.object(engine_module, "_linux_arm_dotprod_available", return_value=True):
                arm_candidates = engine_module.PersistentWhisperEngine.worker_candidates()
            self.assertEqual([p.name for p in arm_candidates], [
                "rdt_whisper_worker_cuda", "rdt_whisper_worker_vulkan",
                "rdt_whisper_worker_dotprod", "rdt_whisper_worker",
            ])

    def test_client_cancelled_non_cpu_crash_does_not_demote_candidate(self):
        gpu = Path("/tmp/rdt-whisper-fake-gpu-cancel-test")
        self.engine.worker_variant = "cuda"
        self.engine._active_worker_path = str(gpu)
        with self.engine._state_lock:
            self.engine._active_id = "cancelled-gpu-job"
            self.engine._cancel_requested_ids.add("cancelled-gpu-job")
        with mock.patch.object(self.engine, "stop") as stop:
            self.engine._demote_failed_non_cpu_worker(RuntimeError("persistent_worker_crashed"))
        stop.assert_not_called()
        self.assertNotIn(str(gpu.resolve()), self.engine._bad_worker_paths)

    def test_gpu_worker_startup_failure_falls_back_to_cpu_worker(self):
        with tempfile.TemporaryDirectory(prefix="rdt-worker-fallback-") as td:
            root = Path(td)
            gpu = root / "rdt_whisper_worker_cuda"
            cpu = root / "rdt_whisper_worker"
            gpu.write_bytes(b"test stub")
            cpu.write_bytes(b"test stub")
            class Reader:
                def __init__(self, lines): self.lines = list(lines)
                def readline(self, size=-1): return self.lines.pop(0) if self.lines else b""
                def close(self): pass
            processes = []
            def make_process(command, **kwargs):
                proc = _FakeProcess()
                proc.stdout = Reader(
                    [b"INVALID_READY\n"] if Path(command[0]).name.endswith("cuda")
                    else [b"READY\tx64\twhisper.cpp/v1.9.5\tCPU\tCPU\n"]
                )
                processes.append(Path(command[0]).name)
                return proc
            self.engine.process = None
            with mock.patch.object(self.engine, "worker_candidates", return_value=[gpu, cpu]), \
                 mock.patch.object(engine_module.subprocess, "Popen", side_effect=make_process):
                self.engine.start()
            self.assertEqual(processes, [gpu.name, cpu.name])
            self.assertEqual(self.engine.worker_variant, "cpu")
            self.engine.stop()

    def test_gpu_worker_inference_failure_demotes_candidate_then_cpu_succeeds(self):
        with tempfile.TemporaryDirectory(prefix="rdt-worker-inference-fallback-") as td:
            root = Path(td)
            gpu = root / "rdt_whisper_worker_cuda"
            cpu = root / "rdt_whisper_worker"
            gpu.write_bytes(b"fake GPU worker")
            cpu.write_bytes(b"fake CPU worker")
            class Reader:
                def __init__(self, lines): self.lines = list(lines)
                def readline(self, size=-1): return self.lines.pop(0) if self.lines else b""
                def close(self): return None
            processes = []
            def make_process(command, **kwargs):
                name = Path(command[0]).name
                proc = _FakeProcess()
                if name.endswith("_cuda"):
                    lines = [
                        b"READY\tx64\twhisper.cpp/v1.9.5@d1be6fde11ac6e0407606b4e42fe72d34add8037\tCUDA\tFake GPU\n",
                        b"ERROR\tgpu-request-1\tinference_failed\n",
                    ]
                else:
                    lines = [
                        b"READY\tx64\twhisper.cpp/v1.9.5@d1be6fde11ac6e0407606b4e42fe72d34add8037\tCPU\tCPU\n",
                        b"DONE\tcpu-request-2\tZW4=\n",
                    ]
                proc.stdout = Reader(lines)
                processes.append(name)
                return proc
            self.engine.process = None
            with mock.patch.object(self.engine, "worker_candidates", return_value=[gpu, cpu]), \
                 mock.patch.object(engine_module.subprocess, "Popen", side_effect=make_process):
                with self.assertRaisesRegex(RuntimeError, "worker_inference_failed"):
                    self.engine.transcribe(self.wav, "en", request_id="gpu-request-1")
                result = self.engine.transcribe(self.wav, "en", request_id="cpu-request-2")
            self.assertEqual(result["language"], "en")
            self.assertEqual(processes, [gpu.name, cpu.name])
            self.assertIn(str(gpu.resolve()), self.engine._bad_worker_paths)
            self.assertEqual(self.engine.worker_variant, "cpu")

    def test_native_worker_publishes_active_id_before_wav_load_and_polls_cancel(self):
        source = (ROOT / "backend" / "whisper_cpp" / "worker" / "rdt_whisper_worker.cpp").read_text(encoding="utf-8")
        active = source.index("g_active_id = id;", source.index("static bool transcribe("))
        read = source.index("read_pcm16_wav(wav, pcm, error)", source.index("static bool transcribe("))
        self.assertLess(active, read)
        self.assertIn("if (g_cancel.load(std::memory_order_relaxed)) { code = \"cancelled\"; samples.clear(); return false; }", source)

    def test_ready_handshake_accepts_complete_ready_record(self):
        with tempfile.TemporaryDirectory(prefix="rdt-worker-start-ok-") as td:
            exe = Path(td) / "rdt_whisper_worker.exe"
            exe.write_bytes(b"stub")
            self.engine.process = None
            fake = _FakeProcess()
            class Reader:
                def __init__(self):
                    self.lines = [b"READY\tx64\twhisper.cpp/v1.9.5\tCPU\tCPU\n", b""]
                def readline(self, size=-1):
                    return self.lines.pop(0) if self.lines else b""
                def close(self):
                    return None
            fake.stdout = Reader()
            with mock.patch.object(self.engine, "worker_candidates", return_value=[exe]), mock.patch.object(engine_module.subprocess, "Popen", return_value=fake):
                self.engine.start()
            self.assertEqual(self.engine.architecture, "x64")
            self.assertEqual(self.engine.provider, "CPU")
            self.assertEqual(self.engine.worker_variant, "cpu")
            self.engine.stop()

    def test_ready_timeout_fails_closed(self):
        with tempfile.TemporaryDirectory(prefix="rdt-worker-start-timeout-") as td:
            exe = Path(td) / "rdt_whisper_worker.exe"
            exe.write_bytes(b"stub")
            self.engine.process = None
            fake = _FakeProcess()
            fake.stdout = None
            with mock.patch.object(self.engine, "worker_candidates", return_value=[exe]), \
                 mock.patch.object(engine_module.subprocess, "Popen", return_value=fake), \
                 mock.patch.object(engine_module, "READY_TIMEOUT_SECONDS", 0.01), \
                 mock.patch.object(engine_module.queue.Queue, "get", side_effect=queue.Empty):
                with self.assertRaisesRegex(RuntimeError, "persistent_worker_ready_timeout"):
                    self.engine.start()
            self.assertIsNone(self.engine.process)

    def test_ready_handshake_rejects_malformed_shape(self):
        with tempfile.TemporaryDirectory(prefix="rdt-worker-start-") as td:
            exe = Path(td) / "rdt_whisper_worker.exe"
            exe.write_bytes(b"stub")
            self.engine.process = None
            fake = _FakeProcess()
            class Reader:
                def readline(self, size=-1):
                    if not hasattr(self, "done"):
                        self.done = True
                        return b"NOT_READY\tx64\twhisper.cpp/v1.9.5\n"
                    return b""
            fake.stdout = Reader()
            with mock.patch.object(self.engine, "worker_candidates", return_value=[exe]), mock.patch.object(engine_module.subprocess, "Popen", return_value=fake):
                with self.assertRaisesRegex(RuntimeError, "persistent_worker_handshake_invalid"):
                    self.engine.start()
            self.assertIsNone(self.engine.process)


class LinuxPlatformPolicyTests(unittest.TestCase):
    def test_asgi_unauthenticated_huge_declared_upload_is_rejected_without_body_read_or_recording(self):
        async def exercise():
            old_auth, old_token = server.AUTH_ENABLED, server.TOKEN
            server.AUTH_ENABLED = True
            server.TOKEN = "test-token"
            received = 0
            sent = []
            before = set(server.ALLOWED_ROOT.iterdir())

            async def receive():
                nonlocal received
                received += 1
                return {"type": "http.request", "body": b"must-not-be-consumed", "more_body": True}

            async def send(message):
                sent.append(message)

            scope = {
                "type": "http", "http_version": "1.1", "method": "POST",
                "scheme": "http", "path": "/v1/audio/transcriptions",
                "raw_path": b"/v1/audio/transcriptions", "query_string": b"",
                "headers": [
                    (b"host", b"localhost:8888"),
                    (b"content-type", b"multipart/form-data; boundary=boundary"),
                    (b"content-length", str(10 * 1024**3).encode("ascii")),
                ],
                "server": ("localhost", 8888), "client": ("127.0.0.1", 32125),
            }
            try:
                await server.app(scope, receive, send)
                status = next(m["status"] for m in sent if m["type"] == "http.response.start")
                after = set(server.ALLOWED_ROOT.iterdir())
                return status, received, before, after
            finally:
                server.AUTH_ENABLED, server.TOKEN = old_auth, old_token

        status, received, before, after = asyncio.run(exercise())
        self.assertEqual(status, 401)
        self.assertEqual(received, 0, "unauthenticated body must not be received or multipart-parsed")
        self.assertEqual(before, after, "unauthenticated upload must not create a retained recording")

    def test_pairing_mutation_rejects_10mb_content_length_before_reading_body(self):
        async def exercise():
            receive_calls = 0
            sent = []
            async def receive():
                nonlocal receive_calls
                receive_calls += 1
                return {"type": "http.request", "body": b"should-not-be-read", "more_body": True}
            async def send(message):
                sent.append(message)
            scope = {
                "type": "http", "http_version": "1.1", "method": "POST",
                "scheme": "https", "path": "/auth/pair/request",
                "raw_path": b"/auth/pair/request", "query_string": b"",
                "headers": [(b"host", b"localhost:8888"), (b"content-type", b"application/json"),
                            (b"content-length", str(10 * 1024 * 1024).encode("ascii"))],
                "server": ("localhost", 8888), "client": ("127.0.0.1", 32126),
            }
            await server.app(scope, receive, send)
            status = next(m["status"] for m in sent if m["type"] == "http.response.start")
            return status, receive_calls
        status, receive_calls = asyncio.run(exercise())
        self.assertEqual(status, 413)
        self.assertEqual(receive_calls, 0, "the oversized JSON body must be rejected before parsing")

    def test_pairing_mutation_stream_without_content_length_is_bounded(self):
        async def exercise():
            receive_calls = 0
            received = 0
            sent = []
            total = 10 * 1024 * 1024
            async def receive():
                nonlocal receive_calls, received
                receive_calls += 1
                remaining = total - received
                chunk = b"x" * min(32 * 1024, remaining) if remaining > 0 else b""
                received += len(chunk)
                return {"type": "http.request", "body": chunk, "more_body": received < total}
            async def send(message):
                sent.append(message)
            scope = {
                "type": "http", "http_version": "1.1", "method": "POST",
                "scheme": "https", "path": "/auth/pair/request",
                "raw_path": b"/auth/pair/request", "query_string": b"",
                "headers": [(b"host", b"localhost:8888"), (b"content-type", b"application/json")],
                "server": ("localhost", 8888), "client": ("127.0.0.1", 32127),
            }
            await server.app(scope, receive, send)
            status = next(m["status"] for m in sent if m["type"] == "http.response.start")
            return status, receive_calls, received, total
        status, receive_calls, received, total = asyncio.run(exercise())
        self.assertEqual(status, 413)
        self.assertLess(receive_calls, 10, "the gate should stop consuming after a small bounded prefix")
        self.assertLess(received, total)
        self.assertLessEqual(received, server.MAX_MUTATION_BODY_BYTES + 32 * 1024)

    def test_pairing_mutation_rejects_malformed_content_length(self):
        async def exercise():
            calls = 0
            sent = []
            async def receive():
                nonlocal calls
                calls += 1
                return {"type": "http.request", "body": b"{}", "more_body": False}
            async def send(message): sent.append(message)
            scope = {
                "type": "http", "http_version": "1.1", "method": "POST",
                "scheme": "https", "path": "/auth/pair/request",
                "raw_path": b"/auth/pair/request", "query_string": b"",
                "headers": [(b"host", b"localhost:8888"), (b"content-length", b"12x")],
                "server": ("localhost", 8888), "client": ("127.0.0.1", 32128),
            }
            await server.app(scope, receive, send)
            status = next(m["status"] for m in sent if m["type"] == "http.response.start")
            return status, calls
        status, calls = asyncio.run(exercise())
        self.assertEqual(status, 400)
        self.assertEqual(calls, 0)

    def test_invalid_language_is_rejected_before_worker_invocation(self):
        request = Request({
            "type": "http", "http_version": "1.1", "method": "POST", "scheme": "http",
            "path": "/v1/audio/transcriptions", "raw_path": b"/v1/audio/transcriptions",
            "query_string": b"", "headers": [], "server": ("localhost", 8888),
            "client": ("127.0.0.1", 12345),
        })
        upload = UploadFile(file=io.BytesIO(b"x" * 256), filename="invalid.wav",
                            headers=Headers({"content-type": "audio/wav"}))
        with mock.patch.object(server, "_ensure_engine") as ensure_engine:
            with self.assertRaises(HTTPException) as raised:
                asyncio.run(server.trans(request=request, file=upload, language="zz-invalid"))
        self.assertEqual(raised.exception.status_code, 400)
        ensure_engine.assert_not_called()

    def test_disconnected_waiter_abandons_inference_lock_within_one_second(self):
        cancel_event = threading.Event()
        self.assertTrue(server.INFERENCE_LOCK.acquire(blocking=False))
        try:
            timer = threading.Timer(0.05, cancel_event.set)
            started = time.monotonic()
            timer.start()
            try:
                with self.assertRaises(server._TranscriptionCancelled):
                    server._acquire_inference_lock("cancelled-lock-wait-test", cancel_event, poll_seconds=0.1)
            finally:
                timer.join(timeout=1)
            self.assertLess(time.monotonic() - started, 1.0)
        finally:
            server.INFERENCE_LOCK.release()

    def test_build_script_rejects_a_whisper_cpp_checkout_at_the_wrong_commit(self):
        if not shutil_which("git"):
            self.skipTest("git is not installed")
        script = ROOT / "backend" / "whisper_cpp" / "scripts" / "build-linux-worker.sh"
        with tempfile.TemporaryDirectory(prefix="rdt-wrong-whisper-revision-") as td:
            src = Path(td) / "whisper.cpp"
            src.mkdir()
            subprocess.run(["git", "init", "-q", str(src)], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(src), "config", "user.email", "test@example.invalid"], check=True)
            subprocess.run(["git", "-C", str(src), "config", "user.name", "RDT Test"], check=True)
            (src / "CMakeLists.txt").write_text("cmake_minimum_required(VERSION 3.20)\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(src), "add", "CMakeLists.txt"], check=True)
            subprocess.run(["git", "-C", str(src), "commit", "-q", "-m", "wrong revision"], check=True, capture_output=True)
            # Keep the production script Linux-only, but simulate Linux in
            # this test so macOS can exercise the pinned-revision guard.
            fake_bin = Path(td) / "fake-bin"
            fake_bin.mkdir()
            fake_uname = fake_bin / "uname"
            fake_uname.write_text("\n".join(["#!/bin/sh", "printf 'Linux\\n'", ""]), encoding="utf-8")
            fake_uname.chmod(0o755)
            env = dict(__import__("os").environ)
            env["PATH"] = str(fake_bin) + __import__("os").pathsep + env.get("PATH", "")
            result = subprocess.run(
                ["bash", str(script), "cpu", str(src)],
                capture_output=True, text=True, timeout=10, env=env,
            )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Required pinned commit", result.stderr)

    def test_cmake_rejects_a_non_pinned_whisper_cpp_revision(self):
        if not shutil_which("cmake"):
            self.skipTest("cmake is not installed")
        worker_cmake = ROOT / "backend" / "whisper_cpp" / "worker"
        with tempfile.TemporaryDirectory(prefix="rdt-cmake-unpinned-test-") as td:
            src = Path(td) / "src"
            src.mkdir()
            (src / "CMakeLists.txt").write_text("cmake_minimum_required(VERSION 3.24)\nproject(stub LANGUAGES C CXX)\n", encoding="utf-8")
            result = subprocess.run([
                "cmake", "-S", str(worker_cmake), "-B", str(Path(td) / "build"),
                f"-DWHISPER_CPP_SOURCE={src}", "-DRDT_WHISPER_CPP_COMMIT=not-a-pinned-commit",
            ], capture_output=True, text=True, timeout=30)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("RDT_WHISPER_CPP_COMMIT must equal the pinned", result.stdout + result.stderr)

    def test_tailscale_mode_defaults_to_loopback_without_an_explicit_address(self):
        self.assertEqual(
            server._resolve_bind_host("0.0.0.0", tailscale_enabled=True, tailscale_ip=""),
            "127.0.0.1",
        )
        self.assertEqual(
            server._resolve_bind_host("127.0.0.1", tailscale_enabled=True, tailscale_ip="100.64.0.12"),
            "100.64.0.12",
        )
        self.assertEqual(
            server._resolve_bind_host("127.0.0.1", tailscale_enabled=False, tailscale_ip="100.64.0.12"),
            "127.0.0.1",
        )
        with self.assertRaisesRegex(ValueError, "numeric Tailscale interface IP"):
            server._resolve_bind_host("127.0.0.1", tailscale_enabled=True, tailscale_ip="0.0.0.0/0")
        with self.assertRaisesRegex(ValueError, "wildcard or multicast"):
            server._resolve_bind_host("127.0.0.1", tailscale_enabled=True, tailscale_ip="0.0.0.0")

    def test_health_hides_provider_until_resident_worker_completes_inference(self):
        class ReadyButUnqualifiedEngine:
            def status(self):
                return {
                    "ready": True, "running": True, "engine": "whisper.cpp/v1.9.5",
                    "architecture": "x86_64", "provider": "CUDA",
                    "worker_variant": "cuda", "provider_reason": "CUDA device found",
                }
        old_engine = server._ENGINE
        old_verified = server._ENGINE_INFERENCE_VERIFIED
        try:
            server._ENGINE = ReadyButUnqualifiedEngine()
            server._ENGINE_INFERENCE_VERIFIED = False
            first = server.health(host="localhost:8888")
            self.assertTrue(first["engine_ready"])
            self.assertFalse(first["engine"]["inference_verified"])
            self.assertEqual(first["engine"]["provider"], "unknown")
            self.assertEqual(first["engine"]["worker_variant"], "unknown")

            server._ENGINE_INFERENCE_VERIFIED = True
            qualified = server.health(host="localhost:8888")
            self.assertTrue(qualified["engine"]["inference_verified"])
            self.assertEqual(qualified["engine"]["provider"], "CUDA")
            self.assertEqual(qualified["engine"]["worker_variant"], "cuda")
        finally:
            server._ENGINE = old_engine
            server._ENGINE_INFERENCE_VERIFIED = old_verified

    def test_tailscale_diagnostic_source_uses_linux_paths_only(self):
        self.assertEqual(server._tailscale_cli_source("/usr/bin/tailscale"), "/usr/bin")
        self.assertEqual(server._tailscale_cli_source("/bin/tailscale"), "/bin")
        self.assertEqual(server._tailscale_cli_source("/custom/safe/tailscale"), "PATH or custom location (full path hidden)")

    def test_linux_arm_dotprod_candidate_and_build_rule_are_aligned(self):
        cmake = (ROOT / "backend" / "whisper_cpp" / "worker" / "CMakeLists.txt").read_text(encoding="utf-8")
        engine_source = (ROOT / "backend" / "whisper_cpp" / "engine.py").read_text(encoding="utf-8")
        self.assertIn('CMAKE_SYSTEM_NAME STREQUAL "Linux"', cmake)
        self.assertIn("-march=armv8.2-a+dotprod", cmake)
        self.assertIn("GGML_MACHINE_SUPPORTS_dotprod ON", cmake)
        self.assertIn('"asimddp" in flags or "dotprod" in flags', engine_source)
        self.assertIn('root / "rdt_whisper_worker_dotprod"', engine_source)

    def test_loopback_recognition_handles_ipv4_ipv6_and_hostname(self):
        for host in ("localhost", "127.0.0.1", "::1", "[::1]"):
            self.assertTrue(server._is_loopback_bind_host(host), host)
        for host in ("0.0.0.0", "192.0.2.5", "example.invalid"):
            self.assertFalse(server._is_loopback_bind_host(host), host)

    def test_tls_key_and_certificate_must_be_a_pair(self):
        with self.assertRaisesRegex(ValueError, "both --ssl-keyfile and --ssl-certfile"):
            server._tls_kwargs_or_error("/tmp/key.pem", "")
        with self.assertRaisesRegex(ValueError, "both --ssl-keyfile and --ssl-certfile"):
            server._tls_kwargs_or_error("", "/tmp/cert.pem")

    def test_tls_files_must_exist_and_return_qualified_kwargs(self):
        with tempfile.TemporaryDirectory(prefix="rdt-linux-tls-test-") as td:
            key = Path(td) / "key.pem"
            cert = Path(td) / "cert.pem"
            key.write_text("test key placeholder", encoding="utf-8")
            cert.write_text("test certificate placeholder", encoding="utf-8")
            self.assertEqual(
                server._tls_kwargs_or_error(str(key), str(cert)),
                {"ssl_keyfile": str(key), "ssl_certfile": str(cert)},
            )
            with self.assertRaisesRegex(ValueError, "does not exist"):
                server._tls_kwargs_or_error(str(key), str(Path(td) / "missing.pem"))

    def test_unstarted_engine_status_does_not_claim_cpu_provider(self):
        old_engine = server._ENGINE
        try:
            server._ENGINE = None
            result = server.health(host="localhost:8888")
            self.assertEqual(result["engine"]["provider"], "unknown")
            self.assertEqual(result["engine"]["worker_variant"], "unknown")
            self.assertFalse(result["engine_ready"])
        finally:
            server._ENGINE = old_engine

    def test_asgi_receive_guard_stops_mismatched_length_body_before_full_stream(self):
        async def exercise():
            old_auth, old_token, old_max = server.AUTH_ENABLED, server.TOKEN, server.MAX_UPLOAD_BYTES
            server.AUTH_ENABLED = True
            server.TOKEN = "test-token"
            # The guard allows the configured file cap plus a fixed multipart allowance;
            # setting the file cap to zero makes this test small and deterministic.
            server.MAX_UPLOAD_BYTES = 0
            receive_calls = 0
            emitted_body_bytes = 0
            remaining = 3 * 1024 * 1024
            prefix = (
                b"--boundary\r\n"
                b'Content-Disposition: form-data; name="file"; filename="large.wav"\r\n'
                b"Content-Type: audio/wav\r\n\r\n"
            )
            try:
                async def receive():
                    nonlocal receive_calls, emitted_body_bytes, remaining
                    receive_calls += 1
                    if receive_calls == 1:
                        chunk = prefix
                    elif remaining > 0:
                        chunk = b"x" * min(64 * 1024, remaining)
                        remaining -= len(chunk)
                    else:
                        chunk = b"\r\n--boundary--\r\n"
                    emitted_body_bytes += len(chunk)
                    return {"type": "http.request", "body": chunk, "more_body": remaining > 0 or receive_calls == 1}

                sent = []
                async def send(message):
                    sent.append(message)

                scope = {
                    "type": "http", "http_version": "1.1", "method": "POST",
                    "scheme": "http", "path": "/v1/audio/transcriptions",
                    "raw_path": b"/v1/audio/transcriptions", "query_string": b"",
                    "headers": [
                        (b"host", b"localhost:8888"),
                        (b"authorization", b"Bearer test-token"),
                        (b"content-type", b"multipart/form-data; boundary=boundary"),
                        # Deliberately inconsistent with the actual streamed body.
                        (b"content-length", b"1"),
                    ],
                    "server": ("localhost", 8888), "client": ("127.0.0.1", 32124),
                }
                await server.app(scope, receive, send)
                status = next(m["status"] for m in sent if m["type"] == "http.response.start")
                return status, receive_calls, emitted_body_bytes
            finally:
                server.AUTH_ENABLED, server.TOKEN, server.MAX_UPLOAD_BYTES = old_auth, old_token, old_max

        status, receive_calls, emitted_body_bytes = asyncio.run(exercise())
        self.assertEqual(status, 413)
        self.assertLess(receive_calls, 60, "the server should stop consuming the 3 MiB body shortly after the configured limit")
        self.assertLess(emitted_body_bytes, 3 * 1024 * 1024)


if __name__ == "__main__":
    unittest.main(verbosity=2)
