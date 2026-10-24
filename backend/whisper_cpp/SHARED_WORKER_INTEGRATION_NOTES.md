# Shared worker source integration notes

- Base: Linux implementation bundle's shared worker sources.
- `engine.py`: preserves strict Linux IPC/request-ID/failover behavior and restores the Windows server's `timeout_seconds` parameter.
- `rdt_whisper_worker.cpp`: preserves Linux chunked WAV conversion and pinned READY identity, and restores bounded pending-CANCEL handling from the Windows implementation.
- `CMakeLists.txt`: preserves the pinned clean-source check and derives Git HEAD when the existing Windows build script omits the explicit CMake commit argument. The source must still match the exact pinned commit.
- No native build or real inference is implied by this source-only integration.
