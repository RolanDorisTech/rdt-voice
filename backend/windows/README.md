# Windows backend (Build 0D53)

This is a Windows-only fork of the current `backend/apple/server.py` API service. It does not import MLX or the Apple model lock. It uses five logical model IDs (`tiny`, `base`, `small`, `large-v3-turbo`, `large-v3`), immutable Hugging Face artifact URLs, full SHA-256/byte-size verification, `.part` staging and atomic promotion, and a persistent RDT-owned `whisper.cpp` CPU worker.

## Development layout

- `server.py`: Windows service entry point, API/auth/pairing/recordings/progress/transcription contract.
- `model_registry.py`: pinned model candidates and verified downloader/cache.
- `engine.py`: bounded worker IPC client, startup handshake and lifecycle.
- `worker/rdt_whisper_worker.cpp`: persistent native C API worker; CPU-only until GPU backends are separately qualified.
- `worker/CMakeLists.txt`: native worker build target.
- `requirements-windows.txt`: exact Python dependency version baseline. It is **not a hash-locked release lock** until native x64 and ARM64 wheel resolution/import tests pass.

Set `RDT_WINDOWS_MODEL_DIR` to the desired cache directory. In packaged mode Rust sets it under the active model-storage root. The model cache is directly reusable between Windows x64 and ARM64; the native worker and packaged Python runtime are architecture-specific.

## Source pin

`engine-pins.json` records `whisper.cpp` v1.9.5 at commit `d1be6fde11ac6e0407606b4e42fe72d34add8037`. The Windows CPU qualification workflow fetches that exact commit detached, verifies `HEAD`, initializes gitlinks and archives `git submodule status --recursive`. It never builds from a moving branch.

## Download protocol

Rust reads these existing lines from stdout; do not rename their prefixes or colon-delimited fields:

- `RDT_MODEL_DOWNLOAD_STAGE:checking|downloading|verifying|complete`
- `RDT_MODEL_DOWNLOAD_META:<total-bytes>:<file-count>:<is-estimate>`
- `RDT_MODEL_DOWNLOAD_PROGRESS:<downloaded-bytes>:<total-bytes>:<percent>`
- `RDT_MODEL_DOWNLOAD_ERROR:<safe-code>`

`--download-model <logical-id>` only imports the registry, verifies metadata and the complete downloaded file, then exits. It does not import `engine.py`, start the C++ worker or warm inference. Runtime/model metadata mismatch is a hard failure.

## Development and security limits

- Windows plaintext HTTP is accepted only on loopback. Remote HTTP binding is rejected. Windows native certificate trust is not implemented or release-qualified in this patch, so do not enable remote access/release use.
- `health` preserves `status: "ok"` and existing fields and adds `engine_ready` / `engine` status. ASGI startup fails if the verified model or native worker is missing, so listener readiness is not treated as inference readiness.
- Recording uploads and open operations accept only the explicit audio extension allowlist; reparse-point checks and regular-file containment are applied to recording paths. The server never opens client-selected arbitrary file types.
- The native C++ worker remains unbuilt/unexecuted here. Its actual architecture, cancellation and transcription behaviour must be demonstrated by the native Windows Actions jobs before a release claim.
