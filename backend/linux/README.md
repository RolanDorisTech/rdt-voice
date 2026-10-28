# RDT Connect — Linux transcription server (Dev 11)

This source tree is an isolated Linux implementation derived from the available Dev 11 Windows implementation bundle at source revision `60e0aeca4b27c37c06e01191a73175b726811487`. The actual target checkout and Linux-specific source were not mounted in this execution environment, so this package is integration-ready source, not a verified in-place repository patch.

## Layout and shared interfaces

- `server.py` implements the API and recording/authentication behavior using the cross-platform GGML model registry and persistent `whisper.cpp` worker.
- `../whisper_cpp/model_registry.py` verifies the selected model's immutable registry revision, sidecar identity, exact byte size, and complete-file SHA-256 before inference.
- `../whisper_cpp/engine.py` starts one persistent native worker using the tab-delimited `READY` / `TRANSCRIBE` / `SEGMENT` / `DONE` / `ERROR` / `CANCEL` / `SHUTDOWN` protocol. Candidate selection is deterministic under `backend/whisper_cpp/bin`, with `RDT_WHISPER_WORKER_EXE` as an explicit, authoritative override. Failed candidate startup is retried against later available candidates.
- `../whisper_cpp/worker/rdt_whisper_worker.cpp` is the available Dev 11 worker source. This package does not include a prebuilt Linux executable or a built dependency tree.

## Requirements

Use Python 3.10+ with the dependencies already required by the service: FastAPI, Starlette, NumPy, Uvicorn, qrcode, and the multipart parser used by `UploadFile`/`Form`. Install FFmpeg/ffprobe and the approved native `whisper.cpp` dependencies/build inputs via the project's normal packaging process. This source delivery intentionally does not install packages, fetch source, or download model weights.

## Run

From this directory, run `./run-server.sh`. The default listener is `127.0.0.1:8888`; bearer authentication is enabled by default and a random token is stored at `~/.config/rdt-voice/server-token` with restrictive permissions. Configure `RDT_TOKEN` or `RDT_TOKEN_FILE` if the packaged host owns token provisioning. Download and verify a model with `./run-server.sh --download-model large-v3-turbo` before starting normal service mode.

For remote access, keep authentication enabled. Prefer Tailscale Serve or another authenticated, trusted TLS-terminating proxy to the loopback listener. When `RDT_TAILSCALE=1`, the server binds to `TAILSCALE_IP` only when explicitly set; otherwise it stays on loopback rather than silently binding to all interfaces. `--ssl-keyfile` and `--ssl-certfile` must be supplied together and point to regular files. An explicit `--host`/`WHISPER_HOST` non-loopback bind is allowed only with authentication enabled, and operators remain responsible for restricting ingress and providing TLS.

The server rejects transcription requests before multipart parsing when host/authentication fails or content length is missing, malformed, or over the configured bound. It admits at most two in-flight transcription requests before expensive upload parsing/decoding; overload returns HTTP 429. Other mutating routes (POST, PUT, PATCH, DELETE) also have a separate 64 KiB body cap, applied to both declared length and streamed bytes, including unauthenticated pairing JSON. Invalid/duplicate Content-Length on these routes returns HTTP 400; oversize returns HTTP 413. Transcription uploads still require Content-Length and return HTTP 411 when it is missing. Recordings are saved only after successful audio validation and are bounded by configured retention quotas. Language is validated as `auto` or a 2–3 letter ISO-639-style code before worker invocation.

## Worker packaging

The default shared engine looks for Linux workers in `backend/whisper_cpp/bin` (GPU variants first, ARM dot-product only when Linux CPU flags support it, then CPU). To build a variant from an already-supplied local whisper.cpp source tree, run `backend/whisper_cpp/scripts/build-linux-worker.sh cpu /path/to/whisper.cpp`; use `cuda`, `vulkan`, or `dotprod` only when that variant's toolchain/runtime is actually supported. The helper never downloads source or model weights. The ARM dot-product build checks compiler support and applies the ARMv8.2 flag to GGML's CPU target only; runtime selection remains gated by Linux CPU feature flags.

The build helper installs binaries using the exact shared-engine candidate names. Verify required shared libraries in the actual Linux packager and run a real model-backed inference qualification before advertising the selected provider as usable. To direct the engine at a custom packaged worker path, set `RDT_WHISPER_WORKER_EXE` to that exact executable; because this override is authoritative, it intentionally bypasses automatic candidate fallback.

## Qualification limits

The regression suite uses synthetic audio and mocked worker protocol/inference except for a real FFmpeg decode test where available. A passing suite does not claim a native worker build, actual transcription, a verified local model artifact, GPU use, or end-to-end performance. See `IMPLEMENTATION_REPORT.md` at the package root for exact checks and remaining blockers.
