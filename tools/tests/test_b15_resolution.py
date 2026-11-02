"""Offline regression for first-pass versus final b15 resolution and pure single-video Pass 2."""
import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_video_profiles import namespace, Client
from backend.services.continuation import freeze_inputs
from backend.services.native_audio import wire_native_audio


class B15ResolutionTests(unittest.TestCase):
    def setUp(self):
        self.ns = namespace()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name)
        self.ref = self.folder / "ref.png"
        self.ref.write_bytes(b"offline-upload-fixture")
        self.ns["ensure_shot_dir"] = lambda shot: self.folder

    def shot(self, mode="CUT", ratio="9:16", **extra):
        return dict(id=1, scene_id=1, mode=mode, video_model="b15_49", steps=10,
                    seed=42, megapixels=0.4, aspect_ratio=ratio, seconds=5,
                    prompt="Test", ref1_path=str(self.ref), ref2_path="",
                    first_frame_path=str(self.ref), continuation_selection="",
                    **extra)

    def prepare(self, shot):
        return asyncio.run(self.ns["prepare_workflow"](shot, Client()))

    def assert_two_resolutions(self, workflow, ratio):
        inputs = workflow["131"]["inputs"]
        self.assertEqual((inputs["width"], inputs["height"]),
                         self.ns["dims_from_mp"](0.4, ratio))
        self.assertEqual(workflow["b15_upscale"]["inputs"]["mode.scale"], 2.0)
        self.assertEqual(workflow["124"]["inputs"]["steps"], 10)
        self.assertEqual(workflow["b15_schedule"]["class_type"], "OpenH3RegenSigmas")
        self.assertEqual(workflow["b15_schedule"]["inputs"]["steps"], 3)
        self.assertEqual(workflow["b15_schedule"]["inputs"]["start_sigma"], 0.78)
        self.assertEqual(workflow["b15_regen_lora"]["class_type"], "OpenH3RegenLora")
        self.assertEqual(workflow["b15_sampler"]["inputs"]["sampler_name"], "euler")
        self.assertEqual(workflow["b15_refine"]["inputs"]["latent_image"], ["b15_concat", 0])
        self.assertEqual(workflow["122"]["inputs"]["samples"], ["b15_refine", 0])
        for node in workflow.values():
            for value in node.get("inputs", {}).values():
                if isinstance(value, list) and len(value) == 2:
                    self.assertIn(str(value[0]), workflow)

    def test_enabled_and_legacy_two_pass_all_modes_and_ratios(self):
        for mode in ("CUT", "CONTINUE", "T2V"):
            for ratio in ("9:16", "16:9", "1:1"):
                for explicit in (False, True):
                    with self.subTest(mode=mode, ratio=ratio, explicit=explicit):
                        shot = self.shot(mode, ratio)
                        shot["megapixels"] = 0.7
                        if explicit:
                            shot["second_pass_enabled"] = 1
                        workflow = self.prepare(shot)
                        self.assert_two_resolutions(workflow, ratio)
                        if mode == "CONTINUE":
                            self.assertNotIn("b15_guide", workflow)
                            self.assertEqual(workflow["b15_guider"]["inputs"]["conditioning"], ["131", 0])

    def test_single_pass_keeps_requested_resolution(self):
        for mode in ("CUT", "CONTINUE", "T2V"):
            for mp in (0.4, 0.7, 1.0):
                with self.subTest(mode=mode, mp=mp):
                    shot = self.shot(mode, second_pass_enabled=0)
                    shot["megapixels"] = mp
                    workflow = self.prepare(shot)
                    inputs = workflow["131"]["inputs"]
                    self.assertEqual((inputs["width"], inputs["height"]),
                                     self.ns["dims_from_mp"](mp, "9:16"))
                    self.assertNotIn("b15_refine", workflow)
                    self.assertEqual(workflow["122"]["inputs"]["samples"], ["125", 0])

    def test_frozen_two_pass_template_uses_correct_first_resolution(self):
        shot = self.shot(second_pass_enabled=1)
        path = freeze_inputs(shot, self.ns)
        frozen = json.loads(Path(path).read_text(encoding="utf-8"))["shot"]
        frozen["queued_input_path"] = path
        workflow = self.prepare(frozen)
        self.assert_two_resolutions(workflow, "9:16")
        saved = json.loads((Path(path).parent / "workflow.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["131"]["inputs"]["width"], 480)
        self.assertEqual(saved["131"]["inputs"]["height"], 864)

    def test_single_video_pass2_when_pass1_video_exists(self):
        pass1_mp4 = self.folder / "pass1_draft.mp4"
        pass1_mp4.write_bytes(b"pass1-video-fixture")
        shot = self.shot(
            "CONTINUE",
            "16:9",
            second_pass_enabled=1,
            pass1_source_video_path=str(pass1_mp4),
        )
        shot.update(first_frame_path="", ref1_path="")
        workflow = self.prepare(shot)
        self.assertIn("b15_load_video", workflow)
        self.assertIn("b15_vae_encode", workflow)
        self.assertIn("b15_finish", workflow)
        self.assertNotIn("125", workflow)
        self.assertNotIn("902", workflow)
        self.assertNotIn("903", workflow)
        self.assertNotIn("b15_guide", workflow)
        self.assertEqual(workflow["b15_upscale"]["inputs"]["mode"], "scale by multiplier")
        self.assertEqual(workflow["b15_upscale"]["inputs"]["mode.scale"], 2.0)
        self.assertEqual(workflow["b15_schedule"]["inputs"]["start_sigma"], 0.78)
        self.assertEqual(workflow["b15_guider"]["inputs"]["conditioning"], ["b15_text_cond", 0])
        for node in workflow.values():
            for value in node.get("inputs", {}).values():
                if isinstance(value, list) and len(value) == 2:
                    self.assertIn(str(value[0]), workflow)

    def test_single_video_pass2_locks_mother_ref1_without_cross_shot_guide(self):
        pass1_mp4 = self.folder / "pass1_draft.mp4"
        pass1_mp4.write_bytes(b"pass1-video-fixture")
        shot = self.shot(
            "CONTINUE",
            "9:16",
            second_pass_enabled=1,
            pass1_source_video_path=str(pass1_mp4),
        )
        workflow = self.prepare(shot)
        self.assertIn("b15_load_video", workflow)
        self.assertIn("901", workflow)
        self.assertIn("131", workflow)
        self.assertNotIn("125", workflow)
        self.assertNotIn("902", workflow)
        self.assertNotIn("903", workflow)
        self.assertNotIn("b15_guide", workflow)
        self.assertEqual(workflow["131"]["inputs"]["ref_images.ref_image_0"], ["901", 0])
        self.assertEqual(workflow["b15_guider"]["inputs"]["conditioning"], ["131", 0])
        self.assertEqual(workflow["b15_schedule"]["inputs"]["start_sigma"], 0.78)
        for node in workflow.values():
            for value in node.get("inputs", {}).values():
                if isinstance(value, list) and len(value) == 2:
                    self.assertIn(str(value[0]), workflow)

    def test_native_audio_preserves_low_res_video_and_locked_refine_audio(self):
        for mode in ("CUT", "CONTINUE", "T2V"):
            with self.subTest(mode=mode):
                workflow = self.prepare(self.shot(mode, second_pass_enabled=1))
                first_latent = workflow["125"]["inputs"]["latent_image"][:]
                wire_native_audio(workflow, "frozen-voice.wav")
                self.assert_two_resolutions(workflow, "9:16")
                self.assertEqual(workflow["mv_initial"]["inputs"]["video_latent"], first_latent)
                self.assertEqual(workflow["b15_concat"]["inputs"]["audio_latent"], ["mv_lock", 0])


if __name__ == "__main__":
    unittest.main()
