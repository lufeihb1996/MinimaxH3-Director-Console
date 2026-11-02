"""Unified model catalog: every image/video/audio model resolves to an adapter and real workflow files."""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from backend.models import registry
from backend.models.base import VideoAdapter


class ModelRegistryTest(unittest.TestCase):
    def test_every_model_has_adapter_and_workflows(self):
        for kind in registry.KINDS:
            for model in registry.list_models(kind):
                with self.subTest(kind=kind, model=model["id"]):
                    self.assertIsNotNone(registry.get_adapter(kind, model["id"]))
                    paths = ([model["request_template"]] if "request_template" in model else
                             [model["workflow"]] if "workflow" in model else [p for pair in model["workflows"].values() for p in pair])
                    for path in paths:
                        self.assertTrue((ROOT / path).is_file(), path)
                    self.assertTrue(model["gpu_key"])

    def test_defaults_and_unknown_model(self):
        self.assertEqual(registry.default_model("video"), "b15_49")
        self.assertEqual(registry.default_model("image"), "moss_image_5b")
        self.assertEqual(registry.default_model("audio"), "yue2")
        with self.assertRaises(ValueError):
            registry.get_model("video", "missing")

    def test_audio_lock_is_a_catalog_capability(self):
        from backend.models.video import supports_audio_lock
        self.assertTrue(supports_audio_lock("b15_49"))

    def test_new_adapter_registers_without_touching_pipeline_code(self):
        @registry.register("video", "unit_test_adapter")
        class Dummy(VideoAdapter):
            def prepare_template(self, workflow, shot, model):
                workflow["touched"] = True
        registry._ensure_adapters()
        self.assertIsInstance(registry._ADAPTERS[("video", "unit_test_adapter")], Dummy)
        registry._ADAPTERS.pop(("video", "unit_test_adapter"))

    def test_catalog_returns_copies(self):
        data = registry.catalog()
        data["video"]["models"][0]["modes"].append("X")
        self.assertNotIn("X", registry.get_model("video", data["video"]["models"][0]["id"])["modes"])


if __name__ == "__main__":
    unittest.main()
