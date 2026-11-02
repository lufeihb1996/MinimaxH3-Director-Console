"""Optional API execution backends; ComfyUI keeps its existing workflow path."""
import json
import uuid
from pathlib import Path

from backend.core.gpu import gpu_slot
from backend.core.pika import PikaClient
from backend.models.video import video_adapter, video_model


def execution_snapshot(shot):
    path = Path(str(shot.get("queued_input_path") or ""))
    if path.is_file():
        result = json.loads(path.read_text(encoding="utf-8"))["shot"]
        result["queued_input_path"] = str(path)
        return result
    return shot


def external_backend_for(shot):
    shot = execution_snapshot(shot)
    model = shot.get("video_profile") or video_model(shot.get("video_model"))
    backend = model.get("execution_backend", "comfyui")
    if backend == "comfyui":
        return None
    backends = {"pika": PikaVideoBackend}
    if backend not in backends:
        raise ValueError(f"未知视频执行后端：{backend}")
    return backends[backend]()


class PikaVideoBackend:
    @staticmethod
    def metadata(shot):
        shot = execution_snapshot(shot)
        folder = Path(shot["queued_input_path"]).parent
        return shot, folder

    async def generate(self, shot, ns):
        shot, folder = self.metadata(shot)
        client = PikaClient(shot["pika_endpoint"])
        health = await client.health()
        if not health["online"]:
            raise RuntimeError("Pika 服务未就绪：" + health.get("error", "请检查 Colab"))
        template = json.loads((folder / "request_template.json").read_text(encoding="utf-8"))
        if template.get("schema_version") != 1 or template.get("backend") != "pika":
            raise RuntimeError("Pika 请求快照版本不兼容，请重新生成建立新快照")
        body, files = video_adapter(shot["video_model"]).build_request(shot, template)
        for field, filename in files:
            if field == "source_video":
                body["source_video"] = await client.upload(filename, expected_kind="video")
            elif field == "first_frame_path":
                body["first_image"] = await client.upload(filename, expected_kind="image")
            else:
                body["references"].append({"kind": "image", "id": await client.upload(filename, expected_kind="image")})
        # Unique remote IDs isolate every local version, even after server restarts.
        job_id = uuid.uuid4().hex
        body.update(shot_id=job_id, version=1)
        record = {"backend": "pika", "endpoint": client.base_url, "job_id": job_id, "request": body}
        (folder / "execution.json").write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")

        async with gpu_slot(None):
            # Persist before sending: an accepted POST whose reply is lost can be recovered.
            ns["update_shot_state"](shot["id"], status="RUNNING", prompt_id=job_id, current_node="Pika · 提交官方任务")
            await ns["hub"].broadcast({"type": "shot_state", "shot_id": shot["id"], "status": "RUNNING", "prompt_id": job_id})
            await client.submit(body)
            return await self.finish(shot, client, job_id, ns)

    async def finish(self, shot, client, job_id, ns):
        async def report(progress, label):
            ns["update_shot_state"](shot["id"], progress=progress, current_node=label)
            await ns["hub"].broadcast({"type": "shot_progress", "shot_id": shot["id"], "progress": progress, "node": label})
        await client.wait(job_id, report)
        return await ns["finalize_output"](ns["get_shot"](shot["id"]), client, job_id, client.output_key)

    async def recover(self, shot, job_id, ns):
        shot, folder = self.metadata(shot)
        record = json.loads((folder / "execution.json").read_text(encoding="utf-8"))
        if record["job_id"] != job_id:
            raise RuntimeError("Pika 恢复任务 ID 与输入快照不一致")
        client = PikaClient(record["endpoint"])
        async with gpu_slot(None):
            return await self.finish(shot, client, job_id, ns)

    async def cancel(self, shot, job_id):
        shot, folder = self.metadata(shot)
        record_path = folder / "execution.json"
        if not record_path.is_file():
            return False
        record = json.loads(record_path.read_text(encoding="utf-8"))
        if record.get("job_id") != job_id:
            return False
        return await PikaClient(record["endpoint"]).cancel(job_id)
