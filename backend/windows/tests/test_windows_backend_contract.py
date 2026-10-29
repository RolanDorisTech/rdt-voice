"""Static regression contracts for the isolated non-MLX desktop backend.

These complement (not replace) native OS runtime/inference qualification.
They intentionally test public protocol/security contracts, not whole-file hashes.
"""
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[3]
SERVER = (ROOT / "backend/windows/server.py").read_text(encoding="utf-8")
REGISTRY = (ROOT / "backend/whisper_cpp/model_registry.py").read_text(encoding="utf-8")
ENGINE = (ROOT / "backend/whisper_cpp/engine.py").read_text(encoding="utf-8")
WORKER = (ROOT / "backend/whisper_cpp/worker/rdt_whisper_worker.cpp").read_text(encoding="utf-8")


class WindowsBackendContractTests(unittest.TestCase):
    def test_arm64_msvc_worker_disables_build_host_optional_features(self):
        cmake = (ROOT / "backend/whisper_cpp/worker/CMakeLists.txt").read_text(encoding="utf-8")
        self.assertIn('set(GGML_NATIVE ON CACHE BOOL "" FORCE)', cmake)
        self.assertIn("foreach(feature dotprod i8mm sve sme)", cmake)
        self.assertIn('set("GGML_MACHINE_SUPPORTS_${feature}" OFF CACHE BOOL "" FORCE)', cmake)

    def test_browser_api_and_response_fields_are_preserved(self):
        for route in (
            '@app.get("/health")', '@app.get("/v1/models")',
            '@app.post("/v1/browser-handoff")', '@app.post("/v1/browser-handoff/exchange")',
            '@app.post("/v1/audio/transcriptions")', '@app.get("/v1/transcription/progress/{job_id}")',
            '@app.get("/v1/recordings/list")', '@app.get("/v1/recordings/{recording_path:path}")',
            '@app.delete("/v1/recordings/{recording_path:path}")',
        ):
            self.assertIn(route, SERVER, route)
        for field in ('"segments"', '"text"', '"duration_original"', '"duration_filtered"'):
            self.assertIn(field, SERVER)
        self.assertIn('"engine_ready":bool(engine_status.get("ready"))', SERVER)

    def test_health_separates_worker_liveness_from_inference_qualification(self):
        self.assertIn("_ENGINE_INFERENCE_VERIFIED = False", SERVER)
        self.assertIn("def _mark_engine_inference_verified(engine: Any) -> None:", SERVER)

        health_start = SERVER.index('@app.get("/health")')
        health_end = SERVER.index("# RDT Connect browser handoff", health_start)
        health = SERVER[health_start:health_end]
        self.assertIn('engine_status["inference_verified"] = inference_verified', health)
        self.assertIn('if _ENGINE is not None and not inference_verified:', health)
        self.assertIn('provider="unknown", worker_variant="unknown"', health)
        self.assertIn('"engine_ready":bool(engine_status.get("ready"))', health)

        route_start = SERVER.index('@app.post("/v1/audio/transcriptions")')
        route = SERVER[route_start:]
        inference = route.index("result = engine.transcribe(")
        verified = route.index("_mark_engine_inference_verified(engine)", inference)
        failure_handler = route.index("except Exception as e:", verified)
        self.assertLess(inference, verified)
        self.assertLess(verified, failure_handler)

    def test_windows_transcription_disconnect_is_request_scoped_and_cleans_up(self):
        start = SERVER.index('@app.post("/v1/audio/transcriptions")')
        sync_start = SERVER.index("def _transcribe_sync(", start)
        wrapper = SERVER[start:sync_start]
        sync_end = SERVER.index("# Duration cache", sync_start)
        sync = SERVER[sync_start:sync_end]

        self.assertIn("async def trans(", wrapper)
        self.assertIn("await request.is_disconnected()", wrapper)
        self.assertIn("cancel_event.set()", wrapper)
        self.assertIn("await asyncio.to_thread(_cancel_worker_request, worker_request_id)", wrapper)
        self.assertIn("worker_request_id=worker_request_id", wrapper)
        self.assertIn("def _cancel_worker_request(request_id: str) -> bool:", SERVER)
        self.assertIn("request_id=worker_request_id", sync)
        self.assertIn("_run_ffmpeg_bounded(cmd, cancel_event, FFMPEG_TIMEOUT_SECONDS)", sync)
        self.assertIn("process.terminate()", SERVER)
        self.assertIn("process.kill()", SERVER)
        self.assertIn("_acquire_inference_lock(job_id, cancel_event)", sync)
        self.assertIn("if lock_acquired:\n                INFERENCE_LOCK.release()", sync)

        inference = sync.index("result = engine.transcribe(")
        cancelled_check = sync.index("_check_transcription_cancelled(job_id, cancel_event)", inference)
        verified = sync.index("_mark_engine_inference_verified(engine)", cancelled_check)
        self.assertLess(inference, cancelled_check)
        self.assertLess(cancelled_check, verified)
        self.assertIn("if cancel_event.is_set():", sync)

    def test_download_only_does_not_eagerly_import_worker(self):
        self.assertIn('if _args.download_model:', SERVER)
        self.assertIn('_download_model_only(_args.download_model)', SERVER)
        self.assertIn('from engine import PersistentWhisperEngine', SERVER)
        self.assertIn('The downloader imports only this registry', SERVER)
        self.assertLess(SERVER.index('if _args.download_model:'), SERVER.index('uvicorn.run(app'))

    def test_model_download_is_pinned_verified_and_atomic(self):
        self.assertIn('REVISION = "fb5ed9369c26eaf08392504d8446da431e0fffb9"', REGISTRY)
        self.assertIn('/resolve/{REVISION}/{spec[\'file\']}', REGISTRY)
        self.assertIn('digest.hexdigest() != expected_sha', REGISTRY)
        self.assertIn('transferred != expected_size', REGISTRY)
        self.assertIn('os.replace(part, destination)', REGISTRY)
        self.assertIn('RDT_MODEL_DOWNLOAD_STAGE:checking', REGISTRY)
        self.assertIn('RDT_MODEL_DOWNLOAD_META:', REGISTRY)
        self.assertIn('RDT_MODEL_DOWNLOAD_PROGRESS:', REGISTRY)
        self.assertIn('RDT_MODEL_DOWNLOAD_ERROR:', REGISTRY)

    def test_recording_open_is_audio_allowlisted_and_reparse_aware(self):
        self.assertIn('_AUDIO_RECORDING_EXTENSIONS =', SERVER)
        self.assertIn('def _validate_openable_recording', SERVER)
        self.assertIn('target.suffix.lower() not in _AUDIO_RECORDING_EXTENSIONS', SERVER)
        self.assertIn('FILE_ATTRIBUTE_REPARSE_POINT', SERVER)
        # Both open endpoints validate files before handing them to OS file handlers.
        self.assertIn('target = _validate_openable_recording(target)', SERVER)
        self.assertIn('FFPROBE = shutil.which("ffprobe") or (str(pathlib.Path(FFMPEG).with_name(', SERVER)

    def test_worker_variant_is_reported_in_engine_and_fallback_health(self):
        for worker, variant in (
            ("rdt_whisper_worker_cuda", "cuda"),
            ("rdt_whisper_worker_vulkan", "vulkan"),
            ("rdt_whisper_worker_dotprod", "arm-dotprod"),
            ("rdt_whisper_worker_avx2", "x64-avx2"),
            ("rdt_whisper_worker", "cpu"),
        ):
            self.assertIn(f'"{worker}": "{variant}"', ENGINE)
        self.assertIn('"worker_variant": self.worker_variant', ENGINE)
        self.assertIn('"worker_variant": "unknown"', SERVER)
        self.assertIn('"engine":engine_status', SERVER)

    def test_worker_protocol_is_bounded_and_persistent(self):
        self.assertIn('MAX_IPC_LINE = 64 * 1024', ENGINE)
        self.assertIn('READY_TIMEOUT_SECONDS', ENGINE)
        self.assertIn('std::cout << "READY\\t"', WORKER)
        self.assertIn('whisper_init_from_file_with_params', WORKER)
        self.assertIn('whisper_full(ctx, params', WORKER)
        self.assertIn('params.abort_callback = abort_callback', WORKER)
        self.assertIn('whisper_free(ctx)', WORKER)
        self.assertIn('kMaxLine = 64 * 1024', WORKER)

    def test_windows_watchdog_does_not_use_posix_liveness_probe(self):
        start = SERVER.index('def _start_rdt_connect_parent_watchdog()')
        end = SERVER.index('\n_start_rdt_connect_parent_watchdog()', start)
        watchdog = SERVER[start:end]
        self.assertIn('if os.name == "nt":', watchdog)
        self.assertIn('return', watchdog)
        self.assertLess(watchdog.index('if os.name == "nt":'), watchdog.index('os.kill(parent_pid, 0)'))


if __name__ == "__main__":
    unittest.main(verbosity=2)
