"""系统接口：模型目录、设置、状态、首页与 WebSocket。"""
from __future__ import annotations

import asyncio
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse

from backend.core.comfy import ComfyClient
from backend.core.pika import PikaClient
from backend.core.db import get_settings, save_settings
from backend.core.paths import FRONTEND_DIR
from backend.core.runtime import hub, job_queue, rt, scene_image_queue
from backend.core.schemas import SettingsIn
from backend.models.registry import catalog as model_catalog
from backend.models.video import video_catalog

router = APIRouter()


@router.get('/api/video-models')
async def video_models_list():
    return video_catalog()


@router.get('/api/models')
async def models_list():
    """Unified image / video / audio catalog from config/models.json."""
    return model_catalog()


@router.get("/")
async def index():
    return FileResponse(FRONTEND_DIR / "index.html", headers={"Cache-Control": "no-store, max-age=0"})


@router.get("/favicon.ico", include_in_schema=False)
async def favicon():
    fav_ico = FRONTEND_DIR / "favicon.ico"
    if fav_ico.is_file():
        return FileResponse(fav_ico, media_type="image/x-icon")
    fav_png = FRONTEND_DIR / "favicon.png"
    if fav_png.is_file():
        return FileResponse(fav_png, media_type="image/png")
    return FileResponse(FRONTEND_DIR / "index.html", headers={"Cache-Control": "no-store, max-age=0"})


@router.get("/api/settings")
async def settings_get():
    return get_settings()


@router.put("/api/settings")
async def settings_put(payload: SettingsIn):
    values = payload.model_dump(exclude_unset=True)
    if values.get("pika_url"):
        try:
            PikaClient(values["pika_url"])
        except ValueError as exc:
            from fastapi import HTTPException
            raise HTTPException(400, str(exc)) from exc
    return save_settings(values)


@router.get("/api/status")
async def status():
    s = get_settings()
    client = ComfyClient(s["comfy_url"])
    async def pika_health():
        if not s.get("pika_url"):
            return {"online": False, "endpoint": "", "error": "尚未配置 Pika 地址"}
        try:
            return await PikaClient(s["pika_url"]).health()
        except ValueError as exc:
            return {"online": False, "endpoint": s["pika_url"], "error": str(exc)}
    h, pika = await asyncio.gather(client.health(), pika_health())
    h["pika"] = pika
    h.update({
        "queue_size": job_queue.qsize(),
        "current_shot_id": rt.current_shot_id,
        "image_queue_size": scene_image_queue.qsize(),
        "current_scene_image_job_id": rt.current_scene_image_job_id,
        "gpu_model": rt.gpu_loaded_model or "",
        "song_running": bool(rt.active_song_task is not None and not rt.active_song_task.done()),
    })
    return h


@router.websocket("/ws/app")
async def ws_app(ws: WebSocket):
    await hub.connect(ws)
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        hub.disconnect(ws)
