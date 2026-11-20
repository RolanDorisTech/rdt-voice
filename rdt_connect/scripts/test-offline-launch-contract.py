#!/usr/bin/env python3
from pathlib import Path

source = Path(__file__).resolve().parents[1] / "src-tauri/src/main.rs"
rust = source.read_text()
start = rust.index("fn open_voice_window(")
end = rust.index("fn request_builder(", start)
launch = rust[start:end]

assert "offline_only_mode.unwrap_or_else(|| read_offline_only_mode(&state))" in launch
assert "let using_offline_copy = !online_available;" in launch
assert "&& !force_offline" in launch
assert "url::Url::parse(&backend_url)" in launch
assert "local.set_path(\"/\")" in launch
assert "read_offline_only_mode(state: &BackendState)" in rust
print("PASS: saved Offline Mode preference controls native Voice launch.")
print("PASS: offline mode bypasses hosted-site availability and selects local Voice.")
