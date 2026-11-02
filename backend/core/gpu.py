"""统一远端 GPU 锁：生图/视频/生歌串行；仅模型类型切换时 /free，OOM 释放后重试一次。"""
from __future__ import annotations

import asyncio
import contextlib

from backend.core.comfy import get_comfy_http_client
from backend.core.db import get_settings
from backend.core.runtime import rt


def gpu_lock() -> asyncio.Lock:
    """惰性创建锁，避免在 import 期就绑定到某个事件循环上。"""
    if rt.gpu_lock_obj is None:
        rt.gpu_lock_obj = asyncio.Lock()
    return rt.gpu_lock_obj


class gpu_slot:
    """串行化一次远端 GPU 作业，并在模型类型切换时先释放显存。

    可重入：同一个 asyncio 任务嵌套进入时只做模型检查，不会自己把自己锁死。
    """

    def __init__(self, model: str | None):
        self.model = model
        self._outermost = False

    async def __aenter__(self) -> "gpu_slot":
        current = asyncio.current_task()
        if rt.gpu_lock_owner is current:
            rt.gpu_lock_depth += 1
        else:
            await gpu_lock().acquire()
            rt.gpu_lock_owner = current
            rt.gpu_lock_depth = 1
            self._outermost = True
        if self._outermost and self.model is not None:
            await ensure_gpu_model(self.model)
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        if self._outermost:
            rt.gpu_lock_depth = 0
            rt.gpu_lock_owner = None
            gpu_lock().release()
        else:
            rt.gpu_lock_depth = max(0, rt.gpu_lock_depth - 1)
        return False


async def free_remote_gpu(reason: str = "") -> None:
    """让远端 ComfyUI 卸载模型并回收显存。"""
    rt.gpu_loaded_model = None
    settings = get_settings()
    client = get_comfy_http_client(settings["comfy_url"])
    with contextlib.suppress(Exception):
        await client.post(
            settings["comfy_url"].rstrip("/") + "/free",
            json={"unload_models": True, "free_memory": True},
            timeout=30.0,
        )
        if reason:
            print(f"[gpu] 已释放显存：{reason}")


async def ensure_gpu_model(model: str) -> None:
    """模型类型切换时才卸载旧模型；同一个模型连续生成不卸载。"""
    if rt.gpu_loaded_model == model:
        return
    if rt.gpu_loaded_model is not None:
        await free_remote_gpu(f"{rt.gpu_loaded_model} -> {model}")
    rt.gpu_loaded_model = model


def is_out_of_memory_error(exc: BaseException | str) -> bool:
    text = str(exc).lower()
    return "out of memory" in text or "outofmemory" in text or "cuda error: out of memory" in text


async def gpu_oom_retry(action, *, label: str):
    """遇到 OOM 时释放显存、等远端回收、重试一次；第二次仍失败才上抛。"""
    try:
        return await action()
    except Exception as exc:
        if not is_out_of_memory_error(exc):
            raise
        print(f"[gpu] {label} 遇到显存不足，释放显存后重试一次：{exc}")
        await free_remote_gpu(f"{label} OOM 重试")
        await asyncio.sleep(6.0)
        return await action()
