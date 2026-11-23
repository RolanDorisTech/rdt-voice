import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
CONNECT = ROOT / "rdt_connect"
SCRIPT = CONNECT / "scripts/prepare-embedded-rdt-voice.mjs"

class EmbeddedVoiceResourcePreflightTests(unittest.TestCase):
    def test_linux_packager_runs_shared_preflight(self):
        source = (CONNECT / "scripts/prepare-linux-bundle.sh").read_text()
        self.assertIn("RDT_CONNECT_EMBEDDED_VOICE_PREFLIGHT", source)
        self.assertIn("prepare-embedded-rdt-voice.mjs", source)

    def test_windows_packager_runs_shared_preflight(self):
        source = (CONNECT / "scripts/prepare-windows-bundle.ps1").read_text()
        self.assertIn("RDT_CONNECT_EMBEDDED_VOICE_PREFLIGHT", source)
        self.assertIn("prepare-embedded-rdt-voice.mjs", source)

    def test_shared_preflight_passes_embedded_resource_contract(self):
        result = subprocess.run(
            ["node", str(SCRIPT)],
            cwd=CONNECT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(
            result.returncode, 0,
            msg=(result.stdout + "\n" + result.stderr)[-5000:],
        )

if __name__ == "__main__":
    unittest.main()
