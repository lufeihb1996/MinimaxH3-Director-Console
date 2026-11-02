"""AI Anime Studio 后端入口：创建 FastAPI 应用、挂载前端、注册各功能路由、启动队列 worker。

目录分层（上层可以引用下层，反之不行）：
  core/      基础设施：路径、数据库、运行状态、ComfyUI 客户端、GPU 锁、FFmpeg 工具、请求体
  models/    模型目录与适配器（生图 / 视频 / 歌曲，config/models.json）
  services/  业务流程：视频生成、生图、生歌与分析、合并字幕、接续、原声驱动
  routers/   HTTP 接口
"""
from __future__ import annotations

import asyncio
import contextlib
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from backend.core.comfy import close_comfy_http_clients
from backend.core.db import init_db
from backend.core.ns import namespace
from backend.core.paths import DB_PATH, FRONTEND_DIR  # noqa: F401  (DB_PATH re-exported for tests)
from backend.core.runtime import hub, recovery_tasks, rt
from backend.routers import audio, images, merge, projects, shots, system, voices
from backend.services.continuation import install_routes
from backend.services.drama3_migrate import migrate_drama3_state
from backend.services.images import recover_scene_image_jobs, scene_image_queue_worker
from backend.services.songs import recover_interrupted_mv_jobs
from backend.services.video import queue_worker, recover_pending_jobs
from backend.services.voice_jobs import recover_voice_jobs, voice_queue_worker

# 建表/迁移在导入时完成，直接调用 API（含测试）不依赖 lifespan。
init_db()


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    migrate_drama3_state()
    rt.worker_task = asyncio.create_task(queue_worker())
    rt.scene_image_worker_task = asyncio.create_task(scene_image_queue_worker())
    rt.voice_worker_task = asyncio.create_task(voice_queue_worker())
    await recover_pending_jobs()
    await recover_scene_image_jobs()
    await recover_voice_jobs()
    await recover_interrupted_mv_jobs()
    try:
        yield
    finally:
        for ws in list(hub.clients):
            with contextlib.suppress(Exception):
                await ws.close()
            hub.disconnect(ws)
        for task in list(recovery_tasks):
            task.cancel()
        if recovery_tasks:
            await asyncio.gather(*list(recovery_tasks), return_exceptions=True)
        for task in (rt.worker_task, rt.scene_image_worker_task, rt.voice_worker_task):
            if task:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        await close_comfy_http_clients()


app = FastAPI(title="AI Anime Studio", version="0.2.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")


@app.middleware("http")
async def frontend_cache_policy(request, call_next):
    response = await call_next(request)
    if request.url.path.startswith("/static/") and Path(request.url.path).suffix in {".js", ".css", ".json"}:
        response.headers["Cache-Control"] = "no-cache"
    elif request.url.path in {"/api/video-models", "/api/models"}:
        response.headers["Cache-Control"] = "no-store"
    return response


for module in (system, projects, images, audio, voices, shots, merge):
    app.include_router(module.router)
install_routes(app, namespace())
