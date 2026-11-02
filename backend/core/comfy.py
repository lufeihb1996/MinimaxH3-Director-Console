"""远端 ComfyUI 客户端：提交、上传、WebSocket/history 查询、下载与节点工具。"""
from __future__ import annotations

import asyncio
import contextlib
import httpx
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlparse

_comfy_http_clients: dict[str, httpx.AsyncClient] = {}

_verified_comfy_node_sets: set[tuple[str, tuple[str, ...]]] = set()


try:
    import h2 as _h2_mod  # noqa: F401
    _HTTP2_AVAILABLE = True
except ImportError:
    _HTTP2_AVAILABLE = False


def get_comfy_http_client(base_url: str) -> httpx.AsyncClient:
    key = base_url.rstrip("/")
    client = _comfy_http_clients.get(key)
    if client is None or client.is_closed:
        client = httpx.AsyncClient(
            trust_env=False,
            http2=_HTTP2_AVAILABLE,
            follow_redirects=True,
            timeout=httpx.Timeout(60.0, connect=8.0, read=60.0, write=300.0, pool=15.0),
            limits=httpx.Limits(max_connections=24, max_keepalive_connections=12, keepalive_expiry=120.0),
        )
        _comfy_http_clients[key] = client
    return client


async def close_comfy_http_clients() -> None:
    clients = list(_comfy_http_clients.values())
    _comfy_http_clients.clear()
    if clients:
        await asyncio.gather(*(client.aclose() for client in clients), return_exceptions=True)


class ComfyClient:
    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")
        parsed = urlparse(self.base_url)
        ws_scheme = "wss" if parsed.scheme == "https" else "ws"
        self.ws_base = f"{ws_scheme}://{parsed.netloc}"

    async def health(self) -> dict[str, Any]:
        client = get_comfy_http_client(self.base_url)
        for path in ("/system_stats", "/object_info"):
            try:
                # 隧道（Cloudflare）单次往返常常 5 秒以上。原先 4 秒的超时会让
                # 「在线/离线」徽标反复横跳，把好好的远端误报成掉线；放宽到 12 秒。
                r = await client.get(self.base_url + path, timeout=12.0)
                if r.is_success:
                    payload = r.json()
                    gpu_name = ""
                    vram_total = None
                    vram_free = None
                    if isinstance(payload, dict) and isinstance(payload.get("devices"), list) and payload["devices"]:
                        d = payload["devices"][0]
                        gpu_name = str(d.get("name", ""))
                        vram_total = d.get("vram_total")
                        vram_free = d.get("vram_free")
                    return {
                        "online": True,
                        "endpoint": self.base_url,
                        "gpu": gpu_name,
                        "vram_total": vram_total,
                        "vram_free": vram_free,
                    }
            except Exception:
                pass
        return {"online": False, "endpoint": self.base_url}

    async def get_json(self, path: str, timeout: float = 60.0) -> dict[str, Any]:
        client = get_comfy_http_client(self.base_url)
        response = await client.get(self.base_url + path, timeout=timeout)
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise RuntimeError(f"ComfyUI returned invalid JSON: {path}")
        return payload

    async def post_json(self, path: str, payload: dict[str, Any], timeout: float = 60.0) -> dict[str, Any]:
        client = get_comfy_http_client(self.base_url)
        response = await client.post(self.base_url + path, json=payload, timeout=timeout)
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict):
            raise RuntimeError(f"ComfyUI returned invalid JSON: {path}")
        return data

    async def upload_input_file(self, local_path: Path, subfolder: str = "anime_studio") -> str:
        if not local_path.exists():
            raise RuntimeError(f"本地文件不存在: {local_path}")
        client = get_comfy_http_client(self.base_url)
        with local_path.open("rb") as f:
            files = {"image": (local_path.name, f, "application/octet-stream")}
            data = {"overwrite": "true", "type": "input", "subfolder": subfolder}
            r = await client.post(self.base_url + "/upload/image", files=files, data=data, timeout=300.0)
        r.raise_for_status()
        payload = r.json()
        name = payload.get("name") or local_path.name
        sub = payload.get("subfolder") or subfolder
        return f"{sub}/{name}" if sub else name

    async def upload_image(self, local_path: Path, subfolder: str = "anime_studio") -> str:
        return await self.upload_input_file(local_path, subfolder)

    def nodes_verified(self, names: list[str]) -> bool:
        key = (self.base_url, tuple(sorted(set(names))))
        return key in _verified_comfy_node_sets

    async def require_nodes(self, names: list[str]) -> None:
        key = (self.base_url, tuple(sorted(set(names))))
        if key in _verified_comfy_node_sets:
            return

        missing: list[str] = []
        client = get_comfy_http_client(self.base_url)
        for name in names:
            try:
                r = await client.get(self.base_url + f"/object_info/{name}", timeout=20.0)
                payload = r.json() if r.is_success else {}
                if name not in payload:
                    missing.append(name)
            except Exception:
                missing.append(name)
        if missing:
            raise RuntimeError(
                "ComfyUI 环境缺少节点: " + ", ".join(missing)
                + "。请运行对应的最新版 Colab 启动脚本并等待 ComfyUI 重启；"
                  "如果缺少 YuE2GenerateABC / YuE2GenerateMusic，请确认 ComfyUI 已更新到包含原生 YuE2 的 master。"
            )
        _verified_comfy_node_sets.add(key)

    async def submit(self, workflow: dict[str, Any], client_id: str) -> str:
        client = get_comfy_http_client(self.base_url)
        r = await client.post(self.base_url + "/prompt", json={"prompt": workflow, "client_id": client_id}, timeout=60.0)
        if not r.is_success:
            detail = ""
            try:
                payload = r.json()
                error = payload.get("error") if isinstance(payload, dict) else None
                if isinstance(error, dict):
                    message = str(error.get("message") or "").strip()
                    details = str(error.get("details") or "").strip()
                    detail = " · ".join(part for part in (message, details) if part)
                if not detail and isinstance(payload, dict):
                    detail = str(payload.get("message") or payload.get("detail") or "").strip()
            except Exception:
                detail = (r.text or "").strip()[:2000]
            raise RuntimeError(f"ComfyUI 拒绝工作流 ({r.status_code})" + (f"：{detail}" if detail else ""))
        payload = r.json()
        prompt_id = payload.get("prompt_id")
        if not prompt_id:
            raise RuntimeError(f"ComfyUI 未返回 prompt_id: {payload}")
        return prompt_id

    async def history(self, prompt_id: str) -> dict[str, Any]:
        client = get_comfy_http_client(self.base_url)
        r = await client.get(self.base_url + f"/history/{prompt_id}", timeout=30.0)
        r.raise_for_status()
        return r.json()

    async def queued_prompt_ids(self) -> set[str]:
        """Return prompt ids currently known by ComfyUI's running/pending queue."""
        client = get_comfy_http_client(self.base_url)
        r = await client.get(self.base_url + "/queue", timeout=15.0)
        r.raise_for_status()
        payload = r.json()
        ids: set[str] = set()
        for key in ("queue_running", "queue_pending"):
            items = payload.get(key, []) if isinstance(payload, dict) else []
            if not isinstance(items, list):
                continue
            for item in items:
                if isinstance(item, (list, tuple)) and len(item) > 1 and item[1]:
                    ids.add(str(item[1]))
                elif isinstance(item, dict):
                    prompt_id = item.get("prompt_id") or item.get("id")
                    if prompt_id:
                        ids.add(str(prompt_id))
        return ids

    async def _probe_view_range_support(self, client: httpx.AsyncClient, url: str) -> tuple[int, bool]:
        try:
            r = await client.head(url, timeout=httpx.Timeout(10.0, connect=6.0))
            if r.is_success:
                raw_len = r.headers.get("content-length") or ""
                total = int(raw_len) if raw_len.isdigit() else 0
                accept_ranges = "bytes" in (r.headers.get("accept-ranges") or "").lower()
                return total, accept_ranges
        except Exception:
            pass
        return 0, False

    async def _download_view_parallel(
        self,
        client: httpx.AsyncClient,
        url: str,
        dest: Path,
        total: int,
        progress_cb=None,
    ) -> Path:
        if total < 2 * 1048576:
            num_parts = 3
        elif total < 8 * 1048576:
            num_parts = 4
        else:
            num_parts = 6

        part_size = (total + num_parts - 1) // num_parts
        ranges: list[tuple[int, int, int]] = []
        for idx in range(num_parts):
            start = idx * part_size
            end = min(total - 1, (idx + 1) * part_size - 1)
            if start <= end:
                ranges.append((idx, start, end))

        part_paths = [dest.with_name(f"{dest.name}.part{idx}") for idx, _, _ in ranges]
        part_received = [0] * len(ranges)

        async def notify_progress() -> None:
            if not progress_cb:
                return
            current = sum(part_received)
            res = progress_cb(current, total)
            if asyncio.iscoroutine(res):
                await res

        async def fetch_part(slot: int, start: int, end: int) -> None:
            p_path = part_paths[slot]
            if p_path.exists():
                p_path.unlink()
            expected_len = end - start + 1
            got = 0
            last_exc: Exception | None = None
            timeout = httpx.Timeout(75.0, connect=8.0, read=45.0, write=30.0, pool=15.0)

            for attempt in range(4):
                cur_start = start + got
                if cur_start > end:
                    break
                try:
                    headers = {"Range": f"bytes={cur_start}-{end}"}
                    async with client.stream("GET", url, headers=headers, timeout=timeout) as resp:
                        if resp.status_code != 206:
                            raise RuntimeError(f"Range not honored ({resp.status_code})")
                        mode = "ab" if got > 0 else "wb"
                        with p_path.open(mode) as fp:
                            async for chunk in resp.aiter_bytes(128 * 1024):
                                if not chunk:
                                    continue
                                fp.write(chunk)
                                got += len(chunk)
                                part_received[slot] = got
                                await notify_progress()
                    if got >= expected_len:
                        return
                except Exception as exc:
                    last_exc = exc
                    if "Range not honored" in str(exc):
                        raise
                    if attempt < 3:
                        await asyncio.sleep(0.5 * (attempt + 1))
            raise last_exc or RuntimeError(f"分片 {slot} 下载不完整 ({got}/{expected_len})")

        temp_merged = dest.with_name(dest.name + ".part")
        try:
            await asyncio.gather(*(fetch_part(i, s, e) for i, (_, s, e) in enumerate(ranges)))
            with temp_merged.open("wb") as out_fp:
                for p_path in part_paths:
                    with p_path.open("rb") as in_fp:
                        while True:
                            buf = in_fp.read(1024 * 1024)
                            if not buf:
                                break
                            out_fp.write(buf)
            temp_merged.replace(dest)
            return dest
        finally:
            for p_path in part_paths:
                with contextlib.suppress(Exception):
                    if p_path.exists():
                        p_path.unlink()
            with contextlib.suppress(Exception):
                if temp_merged.exists():
                    temp_merged.unlink()

    async def download_view(self, meta: dict[str, Any], dest: Path, progress_cb=None) -> Path:
        params = {
            "filename": meta.get("filename", ""),
            "subfolder": meta.get("subfolder", ""),
            "type": meta.get("type", "output"),
        }
        url = self.base_url + "/view?" + urlencode(params)
        client = get_comfy_http_client(self.base_url)
        dest.parent.mkdir(parents=True, exist_ok=True)

        # 大于等于 512KB (0.5MB) 且支持 Range 时启用多线程并发分片拉取（覆盖所有生图、视频与歌曲）
        probed_total, supports_range = await self._probe_view_range_support(client, url)
        if probed_total >= 512 * 1024 and supports_range:
            with contextlib.suppress(Exception):
                return await self._download_view_parallel(client, url, dest, probed_total, progress_cb=progress_cb)

        # 单流下载（支持 Range 断点续传：网络抖动时不删除已下载的 .part，直接接着下）
        temp_path = dest.with_name(dest.name + ".part")
        if temp_path.exists():
            temp_path.unlink()
        last_error: Exception | None = None
        total = probed_total
        received = 0

        for attempt in range(4):
            try:
                timeout = httpx.Timeout(75.0, connect=8.0, read=45.0, write=30.0, pool=15.0)
                headers = {"Range": f"bytes={received}-"} if (received > 0 and supports_range) else {}
                async with client.stream("GET", url, headers=headers, timeout=timeout) as response:
                    if response.status_code == 206 and received > 0:
                        file_mode = "ab"
                    else:
                        response.raise_for_status()
                        file_mode = "wb"
                        received = 0
                        raw_total = response.headers.get("content-length")
                        if raw_total and raw_total.isdigit():
                            total = int(raw_total)
                        if "bytes" in (response.headers.get("accept-ranges") or "").lower():
                            supports_range = True
                    with temp_path.open(file_mode) as f:
                        async for chunk in response.aiter_bytes(512 * 1024):
                            if not chunk:
                                continue
                            f.write(chunk)
                            received += len(chunk)
                            if progress_cb:
                                maybe_awaitable = progress_cb(received, total)
                                if asyncio.iscoroutine(maybe_awaitable):
                                    await maybe_awaitable
                    if received <= 0:
                        raise RuntimeError("ComfyUI /view returned an empty file")
                    if total > 0 and received < total:
                        raise RuntimeError(f"下载流提前中断 ({received}/{total} bytes)")
                temp_path.replace(dest)
                return dest
            except httpx.HTTPStatusError as exc:
                last_error = exc
                if exc.response.status_code not in {408, 425, 429, 500, 502, 503, 504}:
                    break
            except (httpx.TimeoutException, httpx.NetworkError, RuntimeError) as exc:
                last_error = exc
            if attempt < 3:
                await asyncio.sleep(0.6 * (attempt + 1))

        if temp_path.exists():
            temp_path.unlink()
        if last_error:
            raise last_error
        raise RuntimeError("Downloading ComfyUI output failed")

    async def interrupt(self) -> None:
        client = get_comfy_http_client(self.base_url)
        await client.post(self.base_url + "/interrupt", timeout=15.0)


def set_workflow_value(workflow: dict[str, Any], binding: dict[str, Any] | None, value: Any) -> None:
    if not binding:
        return
    node_id = str(binding.get("node", ""))
    input_name = str(binding.get("input", ""))
    if not node_id or not input_name:
        return
    node = workflow.get(node_id)
    if not isinstance(node, dict):
        raise RuntimeError(f"绑定节点不存在: {node_id}")
    inputs = node.setdefault("inputs", {})
    inputs[input_name] = value


def image_meta_from_history(history: dict[str, Any], prompt_id: str) -> dict[str, Any] | None:
    entry = history.get(prompt_id) if isinstance(history, dict) else None
    if not isinstance(entry, dict):
        return None
    outputs = entry.get("outputs", {}) if isinstance(entry.get("outputs"), dict) else {}
    for node_out in outputs.values():
        if not isinstance(node_out, dict):
            continue
        items = node_out.get("images")
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict) or not item.get("filename"):
                continue
            suffix = Path(str(item.get("filename", ""))).suffix.lower()
            if suffix in {".png", ".jpg", ".jpeg", ".webp"}:
                return item
    return None


def audio_meta_from_history(history: dict[str, Any], prompt_id: str) -> dict[str, Any] | None:
    """Extract a file reference emitted by ComfyUI SaveAudio/SaveAudioAdvanced."""
    entry = history.get(prompt_id) if isinstance(history, dict) else None
    if not isinstance(entry, dict):
        return None
    outputs = entry.get("outputs", {}) if isinstance(entry.get("outputs"), dict) else {}
    for node_out in outputs.values():
        if not isinstance(node_out, dict):
            continue
        for key in ("audio", "audios"):
            items = node_out.get(key)
            if not isinstance(items, list):
                continue
            for item in items:
                if not isinstance(item, dict) or not item.get("filename"):
                    continue
                suffix = Path(str(item.get("filename", ""))).suffix.lower()
                if suffix in {".flac", ".mp3", ".wav", ".opus", ".m4a", ".aac"}:
                    return item
    return None


def workflow_node_label(workflow: dict[str, Any], node: Any) -> str:
    if node is None:
        return ""
    node_id = str(node)
    info = workflow.get(node_id) if isinstance(workflow, dict) else None
    if isinstance(info, dict):
        meta = info.get("_meta") if isinstance(info.get("_meta"), dict) else {}
        title = str(meta.get("title") or info.get("class_type") or "").strip()
        if title:
            return f"{title} · Node {node_id}"
    return f"Node {node_id}"


def workflow_node_class(workflow: dict[str, Any], node: Any) -> str:
    if node is None:
        return ""
    info = workflow.get(str(node)) if isinstance(workflow, dict) else None
    return str(info.get("class_type") or "") if isinstance(info, dict) else ""


def workflow_node_inputs(workflow: dict[str, Any], node: Any) -> dict[str, Any]:
    info = workflow.get(str(node)) if isinstance(workflow, dict) else None
    inputs = info.get("inputs") if isinstance(info, dict) else None
    return inputs if isinstance(inputs, dict) else {}


async def history_terminal_state(client: ComfyClient, prompt_id: str) -> tuple[bool, str]:
    try:
        hist = await client.history(prompt_id)
    except Exception:
        return False, ""
    entry = hist.get(prompt_id) if isinstance(hist, dict) else None
    if not isinstance(entry, dict):
        return False, ""
    status = entry.get("status") if isinstance(entry.get("status"), dict) else {}
    if status.get("completed") and str(status.get("status_str", "")).lower() == "success":
        return True, ""

    detail = ""
    messages = status.get("messages")
    if isinstance(messages, list):
        for message in reversed(messages):
            if not isinstance(message, (list, tuple)) or len(message) < 2:
                continue
            event, data = message[0], message[1]
            if event in {"execution_error", "execution_interrupted"} and isinstance(data, dict):
                detail = str(data.get("exception_message") or data.get("message") or event)
                break
    if detail:
        return True, detail
    if status.get("completed"):
        return True, f"ComfyUI 任务结束但状态不是 success: {status.get('status_str', 'unknown')}"
    # 历史里一旦有条目，任务就已经离开执行队列。被中断的任务 completed=false，
    # 但它同样是终态；当成"还在运行"会让 Shot 永久卡在生成中（远端重启/手点中断后必然发生）。
    return True, "ComfyUI 任务已被中断"


async def comfy_prompt_presence(client: ComfyClient, prompt_id: str) -> bool | None:
    """Check whether a prompt still exists in the current ComfyUI runtime.

    True  = present in history or running/pending queue.
    False = both endpoints are reachable and the prompt is absent.
    None  = ComfyUI cannot be queried reliably right now.

    The False case is important after Colab runtime replacement: the Windows app may
    still remember a RUNNING prompt id that the new ComfyUI process has never seen.
    """
    history_ok = False
    queue_ok = False
    try:
        hist = await client.history(prompt_id)
        history_ok = True
        if isinstance(hist, dict) and prompt_id in hist:
            return True
    except Exception:
        pass

    try:
        queued_ids = await client.queued_prompt_ids()
        queue_ok = True
        if prompt_id in queued_ids:
            return True
    except Exception:
        pass

    if history_ok and queue_ok:
        return False
    return None


def decode_comfy_progress_text(raw: bytes) -> tuple[str, str] | None:
    """解析 ComfyUI 的 caption 二进制帧，返回 (node_id, 文本)。

    ComfyUI 的 PromptServer.send_progress_text() 走的是 BinaryEventTypes.TEXT(=3)
    二进制帧，不是 JSON。帧格式（全部大端）：
        >I  事件号（3 = TEXT）
        >I  node_id 的字节长度
        n   node_id（UTF-8）
        -   caption 文本（UTF-8，直到帧尾）
    后端原先收到 bytes 直接 continue，所以自定义节点（例如 YuE2 优化版）上报的
    "score / acoustic / semantic tokens" 这类分阶段文案全被丢掉了。
    """
    if len(raw) < 9:
        return None
    if int.from_bytes(raw[0:4], "big") != 3:  # protocol.BinaryEventTypes.TEXT
        return None
    id_len = int.from_bytes(raw[4:8], "big")
    if id_len <= 0 or 8 + id_len > len(raw):
        return None
    node_id = raw[8:8 + id_len].decode("utf-8", "replace")
    text = raw[8 + id_len:].decode("utf-8", "replace").strip()
    if not text:
        return None
    return node_id, text


async def _remote_prompt_still_alive(client: ComfyClient, prompt_id: str) -> bool:
    """远端是否仍然认识这个 prompt_id；远端已重启/任务已丢失时返回 False。"""
    prompt_id = str(prompt_id or "").strip()
    if not prompt_id:
        return False
    return await comfy_prompt_presence(client, prompt_id) is True


async def _interrupt_remote_prompt(client: ComfyClient, prompt_id: str) -> bool:
    """旧远程任务确实还在时终止它；远端已重启则直接忽略。"""
    if not await _remote_prompt_still_alive(client, prompt_id):
        return False
    with contextlib.suppress(Exception):
        await client.interrupt()
        return True
    return False
