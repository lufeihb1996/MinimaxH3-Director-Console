"""进程内运行状态：WebSocket 广播、视频/生图队列、当前任务句柄、GPU 锁状态。"""
from __future__ import annotations

import asyncio
from fastapi import WebSocket
from typing import Any


class SocketHub:
    def __init__(self) -> None:
        self.clients: set[WebSocket] = set()

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self.clients.add(ws)

    def disconnect(self, ws: WebSocket) -> None:
        self.clients.discard(ws)

    async def broadcast(self, event: dict[str, Any]) -> None:
        stale: list[WebSocket] = []
        for ws in list(self.clients):
            try:
                await ws.send_json(event)
            except Exception:
                stale.append(ws)
        for ws in stale:
            self.disconnect(ws)


class Runtime:
    """可变运行状态集中在这里，各模块通过 rt.xxx 读写，避免跨模块 global。"""

    def __init__(self) -> None:
        self.worker_task: asyncio.Task | None = None
        self.current_shot_id: int | None = None
        # 当前正在执行的视频协程句柄；「重新生成」可取消本地等待/监控。
        self.active_shot_task: asyncio.Task | None = None
        self.scene_image_worker_task: asyncio.Task | None = None
        self.current_scene_image_job_id: int | None = None
        self.active_scene_image_task: asyncio.Task | None = None
        self.voice_worker_task: asyncio.Task | None = None
        self.current_voice_job_id: int | None = None
        self.active_voice_task: asyncio.Task | None = None
        # 生歌协程句柄：「重新生成」能真正取消，也能判断本地是否仍在跑。
        self.active_song_task: asyncio.Task | None = None
        self.current_song_project_id: int | None = None
        # 统一远端 GPU 锁（可重入）与当前显存里的模型类型（gpu_key）。
        self.gpu_lock_obj: asyncio.Lock | None = None
        self.gpu_lock_owner: asyncio.Task | None = None
        self.gpu_lock_depth = 0
        self.gpu_loaded_model: str | None = None


rt = Runtime()
hub = SocketHub()
job_queue: asyncio.Queue[int] = asyncio.Queue()
scene_image_queue: asyncio.Queue[int] = asyncio.Queue()
voice_job_queue: asyncio.Queue[int] = asyncio.Queue()
recovery_tasks: set[asyncio.Task] = set()
project_merge_progress: dict[int, dict] = {}
