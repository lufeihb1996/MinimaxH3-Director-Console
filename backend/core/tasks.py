"""队列与后台任务的取消/摘除工具（重新生成用）。"""
from __future__ import annotations

import asyncio
import contextlib


async def _cancel_child_task(task: asyncio.Task | None) -> None:
    """取消子任务并等它真正结束；用 wait 收尾，避免 CancelledError 冒到请求处理上。"""
    if task is None or task.done():
        return
    task.cancel()
    with contextlib.suppress(Exception):
        await asyncio.wait({task})


async def _remove_from_queue(q: asyncio.Queue, target: int) -> int:
    """把队列里指定的一项摘掉，其余任务保持原有顺序，返回摘掉的个数。

    重复点击「重新生成」时用它清掉旧排队记录，避免同一个 Shot / 参考位在本地
    内存队列里排两份、被跑两次。
    """
    kept: list[int] = []
    removed = 0
    while True:
        try:
            item = q.get_nowait()
        except asyncio.QueueEmpty:
            break
        q.task_done()
        if int(item) == int(target):
            removed += 1
        else:
            kept.append(int(item))
    for item in kept:
        q.put_nowait(item)
    return removed


async def _cancel_active_item(active: asyncio.Task | None, current, target: int):
    """取消承载目标的那个生成协程（如果确实是它），返回取消后的 current 标志位。"""
    if current is not None and int(current) == int(target):
        await _cancel_child_task(active)
        return None
    return current
