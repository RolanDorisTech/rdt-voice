# RDT Connect Dev 11 — Linux Native Qualification

This is a **manual qualification workflow**, not an automatic release/publish job. It builds an x64 Linux CPU worker from the pinned whisper.cpp Git commit, validates the selected GGML model at its immutable upstream revision and by full-file checksum, builds the packaged backend and `.deb`, and makes one real transcription request through the packaged API.

## Existing frontend model IDs

The current desktop launcher passes its existing model ID through `WHISPER_MODEL_ID` and `--download-model`. Linux now accepts those five recognized UI IDs and maps them to logical Linux GGML IDs:

| Existing UI ID | Linux GGML registry ID |
|---|---|
| `mlx-community/whisper-tiny-mlx` | `tiny` |
| `mlx-community/whisper-base-mlx` | `base` |
| `mlx-community/whisper-small-mlx` | `small` |
| `mlx-community/whisper-large-v3-turbo` | `large-v3-turbo` |
| `mlx-community/whisper-large-v3-mlx` | `large-v3` |

This is a *logical-selection compatibility map*, not a claim that MLX and GGML weight files are interchangeable. Each mapping must still resolve to an artifact in the Linux registry and pass immutable metadata, byte-size, sidecar, and full-file SHA-256 verification.

## Build/package behavior

- `backend/whisper_cpp/scripts/build-linux-worker.sh cpu <clean-pinned-whisper.cpp-checkout>` compiles the actual native CPU worker. Test stubs are never accepted for release-worker qualification.
- `rdt_connect/scripts/prepare-linux-bundle.sh` uses PyInstaller `--onedir` and stages the API executable and real worker at `rdt_connect/src-tauri/resources/rdt_voice_backend/`.
- `rdt_connect/src-tauri/tauri.linux.conf.json` includes the staged backend, existing RDT Voice browser resources, and third-party notices. It disables updater artifacts and targets `.deb` only for this qualification.
- `.github/workflows/linux-native-qualification.yml` is triggered manually only. It does not commit, tag, publish or release anything.
- The workflow uploads logs, a worker executable copy, the resulting `.deb`, model-verification sidecar, stage timings and JSON report. It does not upload model weights.

## Real API smoke test

The workflow uses eSpeak NG to generate a repeatable English speech fixture, then FFmpeg converts it to 16 kHz mono PCM16 WAV. It sends one authenticated transcription request to the backend extracted from the built `.deb` and checks:

- a non-empty transcript with the expected `quick` and `fox` anchors;
- non-empty numeric segment timestamps;
- the desktop model ID remains exposed by `/v1/models` while resolving internally to Linux GGML;
- post-inference health reports a verified resident CPU worker;
- total latency, observed progress-stage timings, real-time factor and sampled backend/worker process-tree RSS.

The smoke test's HTTP listener is loopback-only. Production TLS configuration and certificate trust still require qualification on the native Linux app/test machine; a successful local HTTP smoke is not a TLS trust test.

## Release gate

A green unit suite is not release readiness. Require a native worker compiled from the pinned clean checkout, a verified model download, a successfully built `.deb`, the real packaged API inference pass, no silent HTTPS-to-HTTP fallback, and an install/launch test of the `.deb` in UTM. Any checksum, compiler, loader, transcription, TLS or packaging failure must remain a recorded blocker rather than being bypassed with a mock or alternate unverified model.


## Additional app-level release gate

The server/API qualification does not prove that the desktop Rust model manager handles the Linux GGML cache. The historical Rust model storage helpers used MLX/Hugging Face snapshot and `weights.npz` assumptions. Inspect the current `rdt_connect/src-tauri/src/main.rs`; if any such assumptions remain on Linux, add Linux-specific model download/status/available/delete handling based on the approved GGML manifest and `.rdt-verified.json` sidecar. Test the UI model-download lifecycle in the packaged app before release. Do not claim model download/status parity based only on `/v1/models` or a direct backend API smoke test.
