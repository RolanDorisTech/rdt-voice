from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[3]


class LinuxNativeWorkerBuildContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.worker_cmake = (
            ROOT / "backend/whisper_cpp/worker/CMakeLists.txt"
        ).read_text(encoding="utf-8")
        cls.workflow = (
            ROOT / ".github/workflows/linux-native-qualification.yml"
        ).read_text(encoding="utf-8")

    def test_native_x64_worker_forces_portable_x86_64_v1_features(self):
        cmake = self.worker_cmake
        self.assertRegex(
            cmake,
            r'elseif\(CMAKE_SYSTEM_NAME STREQUAL "Linux"\s+'
            r'AND CMAKE_SYSTEM_PROCESSOR MATCHES '
            r'"\^\(x86_64\|amd64\|AMD64\)\$"\)',
        )
        self.assertIn('set(GGML_NATIVE OFF CACHE BOOL "" FORCE)', cmake)
        features = (
            "SSE42", "AVX", "AVX_VNNI", "AVX2", "BMI2", "FMA", "F16C",
            "AVX512", "AVX512_VBMI", "AVX512_VNNI", "AVX512_BF16",
            "AMX_TILE", "AMX_INT8", "AMX_BF16",
        )
        loop = re.search(
            r"foreach\(feature(?P<body>.*?)\)\s+"
            r'set\("GGML_\$\{feature\}" OFF CACHE BOOL "" FORCE\)',
            cmake,
            re.S,
        )
        self.assertIsNotNone(loop, "Explicit portable GGML feature loop is missing")
        for feature in features:
            self.assertRegex(loop.group("body"), rf"\b{feature}\b")
        self.assertIn("RDT portable x86-64-v1 baseline worker", cmake)

    def test_linux_vulkan_worker_installs_spirv_headers_development_files(self):
        step_start = self.workflow.index(
            "- name: Build packaged CPU and Vulkan worker variants for first-start benchmark"
        )
        step_end = self.workflow.find("\n      - name:", step_start + 1)
        step = self.workflow[step_start:] if step_end < 0 else self.workflow[step_start:step_end]
        self.assertIn("libvulkan-dev", step)
        self.assertIn("glslc", step)
        self.assertIn("spirv-headers", step)


if __name__ == "__main__":
    unittest.main()
