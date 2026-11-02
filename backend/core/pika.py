"""Client for the user's Pika Studio API. Does not submit on construction/health."""
import asyncio
import re
from pathlib import Path
from urllib.parse import urlparse

from backend.core.comfy import get_comfy_http_client


class PikaClient:
    output_key = "pika_video"

    def __init__(self, base_url):
        self.base_url = str(base_url or "").strip().rstrip("/")
        parsed = urlparse(self.base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.query or parsed.fragment:
            raise ValueError("Pika 地址需要有效的 http(s) 服务地址")

    @staticmethod
    def validate_id(job_id):
        if not re.fullmatch(r"[0-9a-f]{32}", str(job_id)):
            raise ValueError("Invalid Pika task ID")
        return job_id

    async def request(self, method, path, **kwargs):
        response = await get_comfy_http_client(self.base_url).request(method, self.base_url + path, **kwargs)
        if not response.is_success:
            try:
                detail = response.json().get("error") or response.text[:400]
            except (ValueError, AttributeError):
                detail = response.text[:400]
            raise RuntimeError(f"Pika HTTP {response.status_code}: {detail}")
        return response

    async def health(self):
        try:
            data = (await self.request("GET", "/api/health", timeout=8)).json()
            valid = data.get("service") == "pika-h3-flex-studio"
            ready = valid and data.get("engine_ready") and data.get("authenticated", True)
            return {"online": bool(ready), "endpoint": self.base_url, "service": data.get("service", ""),
                    "version": data.get("version", ""), "active": data.get("active"),
                    "error": "" if ready else "Pika 未就绪、需要登录，或地址指向了其他服务"}
        except Exception as exc:
            return {"online": False, "endpoint": self.base_url, "error": str(exc)}

    async def jobs(self):
        data = (await self.request("GET", "/api/jobs", timeout=20)).json()
        if not isinstance(data.get("jobs"), list):
            raise RuntimeError("Pika 返回的任务列表无效")
        return data["jobs"]

    async def job(self, job_id):
        self.validate_id(job_id)
        return next((job for job in await self.jobs() if job.get("id") == job_id), None)

    async def upload(self, local_path, expected_kind="image"):
        path = Path(local_path)
        if not path.is_file():
            raise ValueError(f"Pika 素材不存在：{path.name}")
        size = path.stat().st_size
        if not 0 < size <= 200 * 1024**2:
            raise ValueError("Pika 单个素材需为 1 字节至 200MiB")
        # Stream the file in bounded chunks; do not load a reference into RAM.
        async def chunks():
            with path.open("rb") as stream:
                while block := stream.read(1024**2):
                    yield block
        response = await self.request("POST", "/api/uploads", params={"name": path.name}, content=chunks(),
                                      headers={"Content-Length": str(size), "Content-Type": "application/octet-stream"}, timeout=300)
        data = response.json()
        self.validate_id(data.get("id"))
        if data.get("kind") != expected_kind:
            raise ValueError(f"当前视频参考槽需要{expected_kind}")
        return data["id"]

    async def submit(self, body):
        job_id = self.validate_id(body["shot_id"])
        try:
            result = (await self.request("POST", "/api/jobs", json=body, timeout=60)).json()
        except Exception:
            # A POST response can be lost after the server accepted the job.
            # Reconcile the same ID; never blindly create a second generation.
            result = await self.job(job_id)
            if not result:
                raise
        if result.get("id") != job_id:
            raise RuntimeError("Pika 返回的任务 ID 与提交 ID 不一致")
        return job_id

    async def cancel(self, job_id):
        self.validate_id(job_id)
        job = await self.job(job_id)
        if job and job.get("status") in {"queued", "running", "cancelling"}:
            await self.request("POST", f"/api/jobs/{job_id}/cancel", json={}, timeout=20)
            return True
        return False

    async def log(self, job_id):
        self.validate_id(job_id)
        data = (await self.request("GET", f"/api/jobs/{job_id}/log", timeout=15)).json()
        return str(data.get("log") or "")

    @staticmethod
    def progress(job):
        info = job.get("progress") or {}
        phase = info.get("phase", "")
        pct = {"starting": 2, "assets": 4, "conditioning": 8, "upscaling": 14, "loading": 18,
               "decoding": 90, "encoding": 95, "done": 97}.get(phase, 1)
        label = str(job.get("stage") or info.get("message") or "等待官方程序")
        if phase == "sampling":
            total = max(1, int(info.get("total") or 6))
            completed = max(0, min(total, int(info.get("completed") or 0)))
            pct = 20 + completed / total * 65
            if total <= 4:
                label = f"Pika 1080p 二采精修 · {completed}/{total}步 · start_sigma=0.78"
            else:
                label = f"Pika 官方一采 · {completed}/{total}步"
        return pct, label if label.startswith("Pika") else "Pika · " + label

    async def wait(self, job_id, report, timeout=1800, interval=3):
        deadline = asyncio.get_running_loop().time() + timeout
        missing = 0
        last_pct = 0
        while asyncio.get_running_loop().time() < deadline:
            try:
                job = await self.job(job_id)
            except Exception:
                # Transport failure does not prove the remote job disappeared.
                await asyncio.sleep(interval)
                continue
            if job is None:
                missing += 1
                if missing >= 3:
                    raise RuntimeError("Pika 连续三次确认任务不存在，远端可能已重启；不会自动重复生成")
            else:
                missing = 0
                status = job.get("status")
                if status in {"failed", "cancelled"}:
                    raise RuntimeError(str(job.get("error") or job.get("stage") or "Pika 任务失败"))
                pct, label = self.progress(job)
                last_pct = max(last_pct, pct)
                await report(last_pct, label)
                if status == "done":
                    return job
            await asyncio.sleep(interval)
        raise RuntimeError("等待 Pika 结果超时（30 分钟），请检查远端任务；未自动重复提交")

    async def history(self, job_id):
        """Normalize terminal metadata for the common local artifact finalizer."""
        job = await self.job(job_id)
        if not job or job.get("status") not in {"done", "failed", "cancelled"}:
            return {}
        success = job["status"] == "done"
        video = {"filename": "pika.mp4", "pika_job_id": job_id,
                 "pika_version": int(job.get("version") or 1), "pika_bytes": int(job.get("video_bytes") or 0)}
        return {job_id: {"status": {"completed": success, "status_str": "success" if success else "error",
                "messages": [] if success else [["execution_error", {"message": job.get("error") or job.get("stage")}]]},
                "outputs": {self.output_key: {"videos": [video]}} if success else {}}}

    async def download_view(self, metadata, dest, progress_cb=None):
        job_id = self.validate_id(metadata["pika_job_id"])
        dest = Path(dest)
        part = dest.with_suffix(dest.suffix + ".part")
        received = 0
        http = get_comfy_http_client(self.base_url)
        try:
            async with http.stream("GET", self.base_url + f"/media/{job_id}", params={"v": metadata["pika_version"]},
                                   headers={"Accept-Encoding": "identity"}, timeout=300) as response:
                response.raise_for_status()
                total = int(response.headers.get("Content-Length") or 0)
                expected = metadata.get("pika_bytes", 0)
                if expected and total and expected != total:
                    raise RuntimeError("Pika 视频版本或长度不一致")
                with part.open("wb") as out:
                    async for block in response.aiter_bytes(1024**2):
                        out.write(block)
                        received += len(block)
                        if progress_cb:
                            await progress_cb(received, total)
                if not received or (total and total != received) or (expected and expected != received):
                    raise RuntimeError("Pika 视频下载不完整")
            # Ensure an HTML error page cannot become a successful MP4 artifact.
            with part.open("rb") as stream:
                if stream.read(8)[4:8] != b"ftyp":
                    raise RuntimeError("Pika 返回的文件不是有效的 MP4 容器")
            part.replace(dest)
        finally:
            part.unlink(missing_ok=True)
