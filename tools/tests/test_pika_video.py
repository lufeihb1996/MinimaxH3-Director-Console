"""Pika contract tests: fake HTTP only, no production DB or GPU submissions."""
import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from backend.core.pika import PikaClient
from backend.models.video import normalize_video_parameters, video_adapter, video_model
from backend.services.continuation import freeze_inputs
from backend.services.video_backends import external_backend_for
from test_video_profiles import namespace


class PikaParametersTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.ref = self.root / "ref.png"
        self.ref.write_bytes(b"reference")
        self.ns = namespace()
        self.ns["get_settings"] = lambda: {"pika_url": "http://pika.test", "fps": "24"}
        self.ns["ensure_shot_dir"] = lambda shot: self.root

    def shot(self, mode="CUT"):
        return dict(id=1, scene_id=1, mode=mode, video_model="pika_h3", steps=6, seed=42,
                    seconds=5, megapixels=0.4, aspect_ratio="9:16", prompt="Test prompt",
                    ref1_path=str(self.ref), ref2_path="", ref3_path=str(self.ref), ref4_path="",
                    first_frame_path=str(self.ref), continuation_selection="")

    def test_modes_and_both_resolutions_build_api_requests_not_graphs(self):
        template = json.loads((ROOT / "workflows/video/pika_h3_request.json").read_text())
        for mode, expected in [("CUT", "ref2va"), ("CONTINUE", "i2va"), ("T2V", "t2va")]:
            for mp, resolution in [(0.4, "480p"), (1.0, "768p")]:
                with self.subTest(mode=mode, mp=mp):
                    shot = dict(self.shot(mode), megapixels=mp)
                    body, files = video_adapter("pika_h3").build_request(shot, template)
                    self.assertEqual((body["task"], body["resolution"], body["duration"]), (expected, resolution, 5))
                    self.assertEqual([f for f, _ in files], ["ref1_path", "ref3_path"] if mode == "CUT" else ["first_frame_path"] if mode == "CONTINUE" else [])
                    self.assertFalse(body["references"])

    def test_invalid_duration_resolution_and_mv_are_rejected(self):
        for values in [{"seconds": 3}, {"seconds": 5.5}, {"seconds": 16}, {"megapixels": .7}, {"lip_sync_enabled": True}]:
            with self.subTest(values=values), self.assertRaises(ValueError):
                normalize_video_parameters(values, self.shot())
        normalized = normalize_video_parameters({"video_model": "pika_h3", "seconds": 5, "megapixels": .4,
                                                 "second_pass_enabled": True, "realism_lora_enabled": True})
        self.assertEqual(normalized["steps"], 6)
        self.assertFalse(normalized["second_pass_enabled"])
        self.assertFalse(normalized["realism_lora_enabled"])
        self.assertFalse(video_model("pika_h3")["supports_audio_lock"])

    def test_snapshot_freezes_endpoint_template_and_refs_and_routes_backend(self):
        shot = self.shot()
        path = freeze_inputs(shot, self.ns)
        data = json.loads(Path(path).read_text(encoding="utf-8"))["shot"]
        self.assertEqual(data["pika_endpoint"], "http://pika.test")
        self.assertTrue((Path(path).parent / "request_template.json").is_file())
        self.assertFalse((Path(path).parent / "template.json").exists())
        self.assertEqual(Path(data["ref1_path"]).read_bytes(), b"reference")
        # Switching the editor model cannot reroute a frozen running job.
        self.assertIsNotNone(external_backend_for({"video_model": "b15_49", "queued_input_path": path}))
        self.assertIsNone(external_backend_for({"video_model": "b15_49"}))


class PikaHttpTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.job_id = "a" * 32
        self.jobs = []
        self.posts = []
        self.content = b"\x00\x00\x00\x18ftypmp42" + b"video-fixture"
        self.client_http = httpx.AsyncClient(transport=httpx.MockTransport(self.handle))
        self.addAsyncCleanup(self.client_http.aclose)
        self.patcher = patch("backend.core.pika.get_comfy_http_client", return_value=self.client_http)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        self.client = PikaClient("http://pika.test")

    async def handle(self, request):
        path = request.url.path
        if path == "/api/health":
            return httpx.Response(200, json={"service": "pika-h3-flex-studio", "engine_ready": True, "authenticated": True})
        if path == "/api/jobs" and request.method == "GET":
            return httpx.Response(200, json={"jobs": self.jobs})
        if path == "/api/jobs" and request.method == "POST":
            self.posts.append(json.loads(request.content))
            # Simulate accepted POST with a lost response.
            self.jobs = [{"id": self.job_id, "status": "running"}]
            raise httpx.ReadError("lost reply")
        if path == "/api/uploads":
            self.posts.append((dict(request.url.params), request.content, request.headers["content-length"]))
            return httpx.Response(201, json={"id": self.job_id, "kind": "image"})
        if path.startswith("/media/"):
            return httpx.Response(200, content=self.content, headers={"Content-Type": "video/mp4"})
        if path.endswith("/cancel"):
            self.posts.append(path)
            return httpx.Response(200, json={"status": "cancelled"})
        return httpx.Response(404)

    async def test_health_upload_and_ambiguous_submit_do_not_duplicate(self):
        self.assertTrue((await self.client.health())["online"])
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "ref.png"
            path.write_bytes(b"fixture")
            self.assertEqual(await self.client.upload(path), self.job_id)
            self.assertEqual(self.posts[0], ({"name": "ref.png"}, b"fixture", "7"))
        self.assertEqual(await self.client.submit({"shot_id": self.job_id}), self.job_id)
        self.assertEqual(len([p for p in self.posts if isinstance(p, dict)]), 1)

    async def test_three_missing_checks_and_failed_jobs(self):
        reports = []
        async def report(*args): reports.append(args)
        with self.assertRaisesRegex(RuntimeError, "连续三次"):
            await self.client.wait(self.job_id, report, timeout=1, interval=0)
        self.jobs = [{"id": self.job_id, "status": "failed", "error": "fixture failure"}]
        with self.assertRaisesRegex(RuntimeError, "fixture failure"):
            await self.client.wait(self.job_id, report, timeout=1, interval=0)

    async def test_download_length_checks_and_old_artifact_protection(self):
        self.jobs = [{"id": self.job_id, "status": "done", "version": 1, "video_bytes": len(self.content)}]
        history = await self.client.history(self.job_id)
        meta = history[self.job_id]["outputs"][self.client.output_key]["videos"][0]
        with tempfile.TemporaryDirectory() as temp:
            dest = Path(temp) / "result.mp4"
            await self.client.download_view(meta, dest)
            self.assertEqual(dest.read_bytes(), self.content)
            dest.write_bytes(b"old-success")
            with self.assertRaisesRegex(RuntimeError, "长度不一致"):
                await self.client.download_view(dict(meta, pika_bytes=999), dest)
            self.assertEqual(dest.read_bytes(), b"old-success")
            self.assertFalse(dest.with_suffix(".mp4.part").exists())

    async def test_cancel_only_targets_requested_job(self):
        self.jobs = [{"id": self.job_id, "status": "running"}]
        self.assertTrue(await self.client.cancel(self.job_id))
        self.assertEqual(self.posts[-1], f"/api/jobs/{self.job_id}/cancel")


if __name__ == "__main__":
    unittest.main()
