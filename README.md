RDT Voice — Single-File Browser UI for Local Whisper + YouTube Exports

Record in your browser. Send audio to your own Whisper backend. Export captions, transcripts, YouTube chapters, and descriptions.

No desktop app install. No account. No RDT cloud transcription. No build step.

⬇ Download rdt-voice.html

MIT · Single HTML · Local-first · OpenAI-compatible STT · MLX Ready
What it is

RDT Voice is a single HTML file — no build step — for local Whisper transcription. It is a browser UI that talks to a Whisper backend you already run. Record from your browser mic, send audio to your own server at 127.0.0.1:8090, and export SRT, VTT, CSV, JSON, YouTube time ranges, chapters, and a description draft.

Audio can stay entirely local. The current UI loads React, Tailwind, and Google Fonts from public CDNs — a self-contained build is on the roadmap.
Quick start

You need two things: this file, and a Whisper server running on your machine.

1. Start a backend. MLX on Apple Silicon is the fastest path:
bash

pip install mlx-whisper
python -m mlx_whisper.server --model mlx-community/whisper-large-v3-mlx --port 8090

It also works with other OpenAI-compatible Whisper servers. Enter the server's host and port as the backend URL; RDT Voice appends /v1/audio/transcriptions.

2. Serve the file over HTTP. Don't double-click it — serve it over localhost for the most reliable microphone permissions, backend CORS, and cross-browser behavior.
bash

python3 -m http.server 8000

3. Open http://localhost:8000/rdt-voice.html in your browser.

4. Tap the mic. Talk. Export.

The 56-bar visualizer animates as you speak. Live mode chunks audio every 2.5 seconds so the transcript builds in real time. Batch mode records until you stop.
What you get

    Browser recording — no desktop app install, no account
    Timestamped segments you can edit, add to, delete, and reorder
    Live mode and batch mode
    SRT for YouTube, DaVinci Resolve, and Premiere
    VTT for web captions
    CSV, JSON, TXT for data and archiving
    YouTube time ranges with millisecond precision, and chapter titles ready to paste
    Description draft, keywords, and hashtags pulled from what you actually said
    Filler radar — flags every um, uh, like, and you know
    WPM and reading time for pacing
    Search and replace before export

Your history stays in your browser, capped at 150 entries. Reload the page and your current work is still there.
Why I built this

Two things kept breaking for me.

Open WebUI's Slim image removes the local machine-learning stack, including local Whisper. That's intentional: the Slim image is roughly 89% smaller, and voice input requires an external speech-to-text engine.

Separately, Open WebUI has had STT MIME/Audio-settings regressions. Issue #20087 documents an audio transcription failure in v0.6.42 where the saved STT MIME-type list became [''], causing valid audio such as audio/wav to be rejected.

A later issue, #30207, found that saving the Audio settings could silently replace the speech-to-text extension allowlist with an empty list. That was fixed in PR #30208, merged September 19, 2026, ahead of v0.11.4.

I wanted a fallback that doesn't depend on Docker image variants or an application's settings UI: one HTML file that talks directly to an OpenAI-compatible /v1/audio/transcriptions endpoint. That's RDT Voice.
Backend you need

    Default URL: http://127.0.0.1:8090
    Default model: mlx-community/whisper-large-v3-mlx
    Endpoint: /v1/audio/transcriptions with verbose_json

Your backend needs to:

    Accept what your browser records. RDT Voice negotiates a supported MediaRecorder MIME type at runtime. Common results are WebM/Opus on Chromium browsers, MP4 on Safari, and OGG/WebM depending on Firefox/platform.
    Allow CORS from wherever you serve this file
    Return text plus timestamped segments

Docker tip: On Docker Desktop for Mac or Windows, localhost inside the container isn't your Mac. Use host.docker.internal. On Linux Docker, use your LAN IP.
Privacy

No account. No RDT cloud transcription server. No telemetry.

When you transcribe, your browser sends audio to the backend URL you configure. If that URL is 127.0.0.1:8090, the audio stays on your machine. If you set a LAN or remote URL, it goes there instead. RDT Voice doesn't decide — you do, in Settings.

The UI loads React and Tailwind from public CDNs on first load, plus Google Fonts. Your IP is visible to those CDNs, but no audio is involved.
Browser compatibility

Tested on Chrome, Firefox, Safari on macOS, Edge on Windows, and Safari on iOS.

RDT Voice negotiates a supported MediaRecorder MIME type at runtime. Common results are WebM/Opus on Chromium browsers, MP4 on Safari, and OGG/WebM depending on Firefox/platform.

File URLs have inconsistent origin handling across browsers. Private browsing can disable localStorage. showDirectoryPicker is Chrome-only.
Troubleshooting

Microphone permission denied
Serve via http://localhost:8000 rather than opening the file directly. Localhost provides a predictable origin for microphone permissions and backend CORS.

Backend connection failed / CORS blocked
Your backend must send Access-Control-Allow-Origin for the frontend's origin. Open the browser network tab to see the exact failing request.

HTTPS frontend → local HTTP backend
Depending on browser and Local Network Access/security policy, an HTTPS page may block or require permission for a request to http://127.0.0.1:8090.

For the simplest setup, serve RDT Voice from http://localhost:8000 while the Whisper backend runs on http://127.0.0.1:8090, and configure CORS on the backend.

Docker localhost doesn't reach the backend
Use host.docker.internal on Docker Desktop.

Safari recording rejected
The backend only allows webm. Allow audio/mp4 and audio/ogg too.

"Oops! file format not supported" / STT MIME Types empty
This is an Open WebUI issue. #20087 documents the earlier v0.6.42 MIME-type failure; #30207 documents the later Audio-settings allowlist overwrite, which was fixed in PR #30208 ahead of v0.11.4. For reference, current Open WebUI documentation lists audio/*,video/webm as its default STT content types. Workaround: set allowed types to audio/* or audio/mpeg audio/wav audio/ogg audio/x-m4a.

400 Invalid audio file extension
Add your browser's MIME type to the backend's allowed extensions.

WHISPER_COMPUTE_TYPE=int8 error
Common on Maxwell and Pascal Nvidia GPUs. Set WHISPER_COMPUTE_TYPE to float16 or float32.

MediaRecorder didn't start in a normal window but works in private
Extension or permission conflict. Works-in-private confirms it.

No timestamped segments
The backend didn't return verbose_json. Check that your model supports timestamps.
When to use what

RDT Voice if you want a portable browser UI for a Whisper backend you control, plus creator exports in one file.

Buzz, Vibe, MacWhisper, Aiko, Whisper Web if you want a packaged desktop app that includes its own inference.

Cloud services if you need collaboration or human review and you're fine with audio leaving your device.
Roadmap

    Optional PWA packaging while preserving the downloadable single-HTML release
    Fully self-contained build with React and Tailwind inlined
    Word-level timestamps where the backend supports it
    Better mobile layout

Contributing

PRs welcome. Keep it single-file. No npm install. Test in Chrome, Safari, and Firefox at http://localhost:8000 before submitting.
License

MIT — Copyright (c) 2026 Rolan & Doris Tech. See LICENSE. If you share the file, keep the header that links back to this repo and to https://youtube.com/@RolanDorisTech

Built by Rolan & Doris Tech.
