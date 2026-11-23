import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
LOCALES = ROOT / "rdt_connect/src/locales"
RUST = ROOT / "rdt_connect/src-tauri/src/main.rs"
HTML = ROOT / "rdt_connect/src-tauri/resources/rdt_voice_web/index.html"
MESSAGE_KEY = "msg.native_server_transcription_out_of_memory.1d47"


class NativeServerOutOfMemoryUiContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rust = RUST.read_text(encoding="utf-8")
        cls.html = HTML.read_text(encoding="utf-8")

    def test_all_connect_locales_contain_message(self):
        files = sorted(LOCALES.glob("*.json"))
        self.assertEqual(len(files), 26)
        for path in files:
            with self.subTest(locale=path.stem):
                data = json.loads(path.read_text(encoding="utf-8"))
                self.assertTrue(data["messages"].get(MESSAGE_KEY))

    def test_rust_passes_selected_connect_translation_to_voice(self):
        self.assertIn("fn native_server_oom_message(locale: &str) -> String", self.rust)
        self.assertIn('.get("messages")?', self.rust)
        self.assertIn('.append_pair("rdt_connect_lang", selected_locale)', self.rust)
        self.assertIn('.append_pair("rdt_connect_native_oom_message", &native_oom_message)', self.rust)
        for locale in ("ar", "cs", "de", "en", "es", "fr", "hi", "id", "it", "ja", "ko", "lo", "ms", "nl", "no", "pl", "pt", "ru", "sv", "th", "tl", "tr", "uk", "vi", "zh-CN", "zh-TW"):
            self.assertIn(f'"{locale}" => include_str!', self.rust)

    def test_voice_reads_and_removes_connect_message_parameter(self):
        self.assertIn('params.get("rdt_connect_native_oom_message")', self.html)
        self.assertIn("RDT_CONNECT_NATIVE_OOM_MESSAGE = incomingNativeOomMessage", self.html)
        self.assertIn('params.delete("rdt_connect_native_oom_message")', self.html)

    def test_batch_maps_native_503_oom_to_connect_translation(self):
        start = self.html.index("async function transcribeBatch(blob, durEstimate, runOptions={}){")
        end = self.html.index("async function transcribeBatchEasyWhisper(", start)
        batch = self.html[start:end]
        self.assertIn('_httpStatus === 503 && /out of memory/i.test(_serverDetail)', batch)
        self.assertIn("rdtNativeServerOutOfMemoryMessage()", batch)

    def test_live_maps_native_503_oom_and_keeps_message_visible(self):
        start = self.html.index("async function transcribeLiveChunk(blob){")
        end = self.html.index("async function startBatchRec(", start)
        live = self.html[start:end]
        self.assertIn("rdtIsNativeServerOutOfMemory(resp.status,t)", live)
        self.assertIn("setError(rdtNativeServerOutOfMemoryMessage())", live)
        catch_start = live.rfind("}catch(e){")
        self.assertGreaterEqual(catch_start, 0)
        catch_body = live[catch_start:]
        ordinary_branch_start = catch_body.find("} else {")
        self.assertGreaterEqual(ordinary_branch_start, 0)
        oom_branch = catch_body[:ordinary_branch_start]
        ordinary_error_branch = catch_body[ordinary_branch_start:]
        self.assertIn("setError(rdtNativeServerOutOfMemoryMessage())", oom_branch)
        self.assertNotIn('setTimeout(()=>setError(""),2500)', oom_branch)
        self.assertIn('setTimeout(()=>setError(""),2500)', ordinary_error_branch)

    def test_voice_locale_catalogs_are_not_given_connect_key(self):
        voice_locales = ROOT / "rdt_connect/src-tauri/resources/rdt_voice_web/locales"
        for path in voice_locales.glob("*.json"):
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertNotIn(MESSAGE_KEY, data)


if __name__ == "__main__":
    unittest.main()
