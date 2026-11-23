import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SOURCE = (ROOT / "rdt_connect/src/main.ts").read_text(encoding="utf-8")
LOCALE_DIR = ROOT / "rdt_connect/src/locales"
KEYS = (
    "msg.model_catalog_unavailable.1d47",
    "msg.model_catalog_status_unavailable.1d47",
    "msg.model_catalog_repair_guidance.1d47",
)


class ModelCatalogLocaleContract(unittest.TestCase):
    def test_all_locales_have_model_catalog_failure_copy(self):
        files = sorted(LOCALE_DIR.glob("*.json"))
        self.assertEqual(len(files), 26)
        for path in files:
            with self.subTest(locale=path.name):
                data = json.loads(path.read_text(encoding="utf-8"))
                for key in KEYS:
                    self.assertTrue(data.get(key), f"{path.name} missing {key}")

    def test_model_catalog_failure_uses_translation_keys(self):
        for key in KEYS:
            self.assertIn(f'translateKey("{key}")', SOURCE)
        self.assertNotIn('label.textContent = "Model catalog unavailable:"', SOURCE)
        self.assertNotIn('modelIdDetail.textContent = "Unavailable";', SOURCE)
        self.assertNotIn("The native model catalog could not be loaded.", SOURCE)


if __name__ == "__main__":
    unittest.main()
