"""歌曲：按歌曲模型适配器生成、清理派生文件、本地分析调度。"""
from __future__ import annotations

import asyncio
import json
import time
import uuid
import websockets
from fastapi import HTTPException
from pathlib import Path
from typing import Any

from backend.core.comfy import ComfyClient, _interrupt_remote_prompt, audio_meta_from_history, comfy_prompt_presence, decode_comfy_progress_text, history_terminal_state, workflow_node_label
from backend.core.db import db, get_settings, read_json_file
from backend.core.gpu import gpu_oom_retry, gpu_slot
from backend.core.media import media_duration_seconds
from backend.core.paths import PROJECTS_DIR
from backend.core.runtime import hub, rt
from backend.core.tasks import _cancel_child_task
from backend.models.registry import get_adapter, get_model
from backend.services.audio_analysis import run_local_analysis
from backend.services.repo import _mv_audio_dir, _mv_update_project, ensure_mv_project

_audio_analysis_projects: set[int] = set()


async def analyze_project_audio(project_id: int) -> dict[str, Any]:
    mv = ensure_mv_project(project_id)
    audio = Path(str(mv.get("audio_path") or ""))
    lyrics = str(mv.get("lyrics") or "").strip()
    if not audio.is_file():
        raise RuntimeError("项目还没有音频，请先在音频台上传 / 生成，或在配音台点『设为本项目音频』")
    if not lyrics:
        raise RuntimeError("请先填写完整歌词")
    if project_id in _audio_analysis_projects:
        raise RuntimeError("当前项目歌曲正在分析，请等待完成")
    _audio_analysis_projects.add(project_id)
    try:
        # Local analysis shares scheduling, but never unloads the remote models.
        async with gpu_slot(None):
            return await _run_project_audio_analysis(project_id, audio, lyrics)
    finally:
        _audio_analysis_projects.discard(project_id)


async def _run_project_audio_analysis(project_id: int, audio: Path, lyrics: str) -> dict[str, Any]:
    # 新分析成功前保留旧时间轴与所有成功产物。
    source_signature = (audio.stat().st_size, audio.stat().st_mtime_ns)
    _mv_update_project(project_id, analysis_status="RUNNING", analysis_progress=2, analysis_error="")
    await hub.broadcast({
        "type": "mv_analysis_state",
        "project_id": project_id,
        "status": "RUNNING",
        "progress": 2,
        "node": "准备歌曲分析",
    })
    try:
        async def analysis_progress(progress: float, message: str) -> None:
            pct = max(2.0, min(99.0, progress))
            _mv_update_project(project_id, analysis_progress=pct)
            await hub.broadcast({
                "type": "mv_analysis_progress",
                "project_id": project_id,
                "status": "RUNNING",
                "progress": pct,
                "node": message or "歌曲分析中",
            })

        audio_dir = _mv_audio_dir(project_id)
        output_dir = audio_dir / ("analysis_" + uuid.uuid4().hex[:12])
        is_voice = str(ensure_mv_project(project_id).get("audio_source") or "") == "voice_job"
        timeline = await run_local_analysis(audio, lyrics, output_dir, analysis_progress, dry_vocals=is_voice)
        current = ensure_mv_project(project_id)
        if (str(current.get("audio_path") or "") != str(audio)
                or str(current.get("lyrics") or "").strip() != lyrics
                or (audio.stat().st_size, audio.stat().st_mtime_ns) != source_signature):
            raise RuntimeError("歌曲或歌词已改变，本次分析结果未应用，请重新分析")
        invalidate_project_song_timeline(project_id)
        timeline_path = output_dir / "timeline.json"
        srt_path = output_dir / "lyrics.srt"
        vocals_path = output_dir / "vocals.wav"
        no_vocals_path = output_dir / "no_vocals.wav"
        _mv_update_project(
            project_id,
            analysis_status="DONE",
            analysis_progress=100,
            analysis_error="",
            vocals_path=str(vocals_path),
            no_vocals_path=str(no_vocals_path) if no_vocals_path.is_file() else "",
            lyrics_timeline_path=str(timeline_path),
            lyrics_srt_path=str(srt_path),
        )
        await hub.broadcast({
            "type": "mv_analysis_state",
            "project_id": project_id,
            "status": "DONE",
            "progress": 100,
            "node": "歌曲分析完成",
        })
        return {"ok": True, "timeline": timeline}
    except Exception as exc:
        _mv_update_project(project_id, analysis_status="FAILED", analysis_progress=0, analysis_error=str(exc))
        await hub.broadcast({
            "type": "mv_analysis_state",
            "project_id": project_id,
            "status": "FAILED",
            "progress": 0,
            "node": "Song analysis failed",
            "error": str(exc),
        })
        raise


async def recover_interrupted_mv_jobs() -> None:
    """应用重启后清理无法继续接管的 MV 后台任务，避免页面永久卡在 RUNNING。"""
    conn = db()
    # 生歌任务走的是「路由直接 await」，没有全局标志位；一旦带 RUNNING 重启，
    # 旧协程已经不存在，页面就会永久卡在生成中。这里必须一起清掉。
    conn.execute(
        """
        UPDATE mv_projects
        SET song_status='FAILED',
            song_finished_at=CASE WHEN song_started_at>0 THEN CAST(strftime('%s','now') AS REAL) ELSE 0 END,
            song_elapsed_seconds=CASE WHEN song_started_at>0 THEN MAX(0,CAST(strftime('%s','now') AS REAL)-song_started_at) ELSE 0 END,
            song_progress=0,
            song_node='',
            song_error='应用重启，中断了上次生歌任务，请重新生成',
            updated_at=CURRENT_TIMESTAMP
        WHERE song_status='RUNNING'
        """
    )
    conn.execute(
        """
        UPDATE mv_projects
        SET analysis_status='FAILED',
            analysis_progress=0,
            analysis_error='应用重启，中断了上次歌曲分析，请重新分析',
            updated_at=CURRENT_TIMESTAMP
        WHERE analysis_status='RUNNING'
        """
    )
    conn.execute(
        """
        UPDATE shots
        SET lip_sync_status='FAILED',
            lip_sync_progress=0,
            lip_sync_error='应用重启，中断了上次对嘴任务，请重新执行',
            updated_at=CURRENT_TIMESTAMP
        WHERE lip_sync_status='RUNNING'
        """
    )
    conn.commit()
    conn.close()


def yue2_caption_label(workflow: dict[str, Any], node: Any, text: str) -> str:
    """给 caption 补上节点标题，免得只看到 "acoustic" 却不知道处在哪个阶段。"""
    info = workflow.get(str(node)) if isinstance(workflow, dict) else None
    meta = info.get("_meta") if isinstance(info, dict) else None
    title = str((meta or {}).get("title") or "").strip()
    return f"{title} · {text}" if title else text


async def monitor_yue2_prompt(
    client: ComfyClient,
    client_id: str,
    prompt_id: str,
    project_id: int,
    workflow: dict[str, Any],
    stage_progress,
) -> None:
    """Song monitor: persist + broadcast node/progress (stage mapping comes from the audio adapter)."""
    uri = client.ws_base + f"/ws?clientId={client_id}"
    deadline = asyncio.get_running_loop().time() + 7200

    last_progress = {"value": 2.0}

    async def emit(progress: float, node: str) -> None:
        progress = max(2.0, min(97.0, float(progress)))
        last_progress["value"] = progress
        _mv_update_project(project_id, song_progress=progress, song_node=node)
        await hub.broadcast({
            "type": "mv_song_progress",
            "project_id": project_id,
            "status": "RUNNING",
            "progress": progress,
            "node": node,
        })

    async def emit_caption(node: Any, text: str) -> None:
        """只换文字、不动百分比。

        节点的 caption（score / acoustic / semantic tokens...）和它的百分比是两条
        独立通道。如果这里也走 emit()，每次 caption 都会把进度条拉回该节点的底值，
        进度条会来回跳。
        """
        node_label = yue2_caption_label(workflow, node, text)
        _mv_update_project(project_id, song_node=node_label)
        await hub.broadcast({
            "type": "mv_song_progress",
            "project_id": project_id,
            "status": "RUNNING",
            "progress": last_progress["value"],
            "node": node_label,
        })

    async def check_history() -> bool:
        terminal, error = await history_terminal_state(client, prompt_id)
        if not terminal:
            return False
        if error:
            raise RuntimeError(error)
        return True

    # 远端被中断 / Colab 重启后，prompt 会从 history 和 queue 双双消失。视频与生图
    # 的监听都有这个兜底，YuE2 原先没有 —— 结果是一直干等到 120 分钟才报超时。
    # 隧道很慢，所以每约 9 秒才查一次，连续 3 次确认才判定任务已丢失。
    ticks = 0
    missing_checks = 0
    try:
        async with websockets.connect(uri, max_size=32 * 1024 * 1024, open_timeout=20) as ws:
            while True:
                if asyncio.get_running_loop().time() >= deadline:
                    raise RuntimeError("Timed out waiting for YuE2 song generation (120 min)")
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=1.5)
                except asyncio.TimeoutError:
                    if await check_history():
                        return
                    ticks += 1
                    if ticks % 6 == 0:
                        presence = await comfy_prompt_presence(client, prompt_id)
                        if presence is False:
                            missing_checks += 1
                            if missing_checks >= 3:
                                raise RuntimeError("Remote ComfyUI lost the song task (interrupted or restarted)")
                        elif presence is True:
                            missing_checks = 0
                    continue
                if isinstance(raw, bytes):
                    # caption 走二进制 TEXT 帧，只更新文案，不碰百分比。
                    caption = decode_comfy_progress_text(raw)
                    if caption is not None:
                        await emit_caption(caption[0], caption[1])
                    continue
                try:
                    msg = json.loads(raw)
                except Exception:
                    continue
                msg_type = msg.get("type")
                data = msg.get("data") or {}
                if msg_type == "progress":
                    value = float(data.get("value", 0))
                    maximum = float(data.get("max", 1) or 1)
                    node = data.get("node")
                    await emit(
                        stage_progress(workflow, node, value, maximum),
                        workflow_node_label(workflow, node),
                    )
                elif msg_type == "executing" and data.get("prompt_id") == prompt_id:
                    node = data.get("node")
                    if node is None:
                        if await check_history():
                            return
                        continue
                    await emit(
                        stage_progress(workflow, node),
                        workflow_node_label(workflow, node),
                    )
                elif msg_type in {"execution_error", "execution_interrupted"}:
                    if not data.get("prompt_id") or data.get("prompt_id") == prompt_id:
                        raise RuntimeError(data.get("exception_message") or data.get("message") or msg_type)
                elif msg_type == "execution_success" and data.get("prompt_id") == prompt_id:
                    if await check_history():
                        return
    except RuntimeError:
        raise
    except Exception:
        # WebSocket 断了就退回 history + queue 轮询。远端重启/中断后旧 prompt_id
        # 两边都不再认识，连续确认 3 次即结束僵尸任务，不再干等 120 分钟。
        missing_checks = 0
        while asyncio.get_running_loop().time() < deadline:
            if await check_history():
                return
            presence = await comfy_prompt_presence(client, prompt_id)
            if presence is False:
                missing_checks += 1
                if missing_checks >= 3:
                    raise RuntimeError("Remote ComfyUI lost the song task (interrupted or restarted)")
            elif presence is True:
                missing_checks = 0
            await asyncio.sleep(1.5)
        raise RuntimeError("Timed out waiting for YuE2 song generation (120 min)")


def _safe_unlink_project_media(project_id: int, raw_path: Any) -> None:
    raw = str(raw_path or "").strip()
    if not raw:
        return
    try:
        path = Path(raw).resolve()
        project_root = (PROJECTS_DIR / f"project_{project_id:03d}").resolve()
        path.relative_to(project_root)
    except Exception:
        return
    try:
        if path.is_file():
            path.unlink()
    except OSError:
        pass


def remove_project_song_files(project_id: int, *, include_audio: bool) -> None:
    """Delete only song-derived media inside this project's own directory."""
    mv = ensure_mv_project(project_id)
    if include_audio:
        _safe_unlink_project_media(project_id, mv.get("audio_path"))
        audio_dir = _mv_audio_dir(project_id)
        for stale_song in audio_dir.glob("song.*"):
            _safe_unlink_project_media(project_id, stale_song)
        for partial in audio_dir.glob(".song_downloading*"):
            _safe_unlink_project_media(project_id, partial)
    for key in ("vocals_path", "no_vocals_path", "lyrics_timeline_path", "lyrics_srt_path"):
        _safe_unlink_project_media(project_id, mv.get(key))

    conn = db()
    shot_rows = conn.execute(
        """
        SELECT sh.lip_sync_audio_path,sh.lip_sync_output_path,sh.lip_sync_last_frame_path
        FROM shots sh
        JOIN scenes sc ON sc.id=sh.scene_id
        WHERE sc.project_id=?
        """,
        (project_id,),
    ).fetchall()
    conn.close()
    for row in shot_rows:
        for key in ("lip_sync_audio_path", "lip_sync_output_path", "lip_sync_last_frame_path"):
            _safe_unlink_project_media(project_id, row[key])


def invalidate_project_song_timeline(project_id: int) -> None:
    """歌曲或歌词发生变化后，旧歌词时间轴和对嘴结果都不再可信。"""
    _mv_update_project(
        project_id,
        analysis_status="IDLE",
        analysis_progress=0,
        analysis_error="",
        vocals_path="",
        no_vocals_path="",
        lyrics_timeline_path="",
        lyrics_srt_path="",
        pipeline_status="IDLE",
        pipeline_progress=0,
        pipeline_node="",
        pipeline_error="",
        master_video_path="",
    )
    conn = db()
    conn.execute(
        """
        UPDATE shots
        SET timeline_start=0, timeline_end=0, target_duration=0, lyric_text='',
            lip_sync_status='IDLE', lip_sync_progress=0, lip_sync_audio_path='',
            lip_sync_output_path='', lip_sync_last_frame_path='', lip_sync_error='',
            updated_at=CURRENT_TIMESTAMP
        WHERE scene_id IN (SELECT id FROM scenes WHERE project_id=?)
        """,
        (project_id,),
    )
    conn.commit()
    conn.close()


async def generate_mv_song(project_id: int) -> dict[str, Any]:
    """Generate the project song through ComfyUI's native YuE2 nodes."""
    mv = ensure_mv_project(project_id)
    genre = str(mv.get("genre_prompt") or "").strip()
    lyrics = str(mv.get("lyrics") or "").strip()
    if not genre or not lyrics:
        raise RuntimeError("请先填写歌曲风格和歌词")

    try:
        model = get_model("audio", mv.get("song_model"))
    except ValueError:
        model = get_model("audio")
    adapter = get_adapter("audio", model["id"])
    old_audio_path = str(mv.get("audio_path") or "")
    # 「重新生成」时旧歌曲必须保留到新歌下载成功为止：先只清派生文件（人声 / 时间轴 /
    # 对嘴结果），不删旧的 song.*，失败时旧歌仍然可用。
    remove_project_song_files(project_id, include_audio=False)
    invalidate_project_song_timeline(project_id)
    _mv_update_project(
        project_id,
        song_status="RUNNING",
        song_progress=2,
        song_node="Preparing the YuE2 workflow",
        song_error="",
        song_prompt_id="",
    )
    await hub.broadcast({
        "type": "mv_song_state",
        "project_id": project_id,
        "status": "RUNNING",
        "progress": 2,
        "node": "Preparing the YuE2 workflow",
    })
    client = ComfyClient(get_settings()["comfy_url"])
    try:
        await client.require_nodes(adapter.required_nodes())
        target_duration = max(10, min(360, int(mv.get("target_duration") or 60)))
        seed = 42
        workflow = read_json_file(model["workflow"])
        adapter.build(
            workflow, model, style=genre, lyrics=lyrics, target_duration=target_duration, seed=seed,
            filename_prefix=f"{model.get('output_prefix') or 'anime_studio/music/' + model['id']}_{project_id:03d}",
        )

        client_id = str(uuid.uuid4())
        # 生歌与生图 / 视频串行共用同一块远程 GPU；模型类型切换时才卸载显存。
        async with gpu_slot(model["gpu_key"]):
            started_at = time.time()
            _mv_update_project(project_id, song_started_at=started_at)
            await hub.broadcast({"type": "mv_song_progress", "project_id": project_id,
                                 "song_started_at": started_at, "song_finished_at": 0,
                                 "song_elapsed_seconds": 0})
            async def _run_song_prompt() -> None:
                pid = await client.submit(workflow, client_id)
                _mv_update_project(
                    project_id, song_progress=3, song_node="YuE2 task submitted", song_prompt_id=pid
                )
                await hub.broadcast({
                    "type": "mv_song_state",
                    "project_id": project_id,
                    "status": "RUNNING",
                    "progress": 3,
                    "node": "YuE2 task submitted",
                    "prompt_id": pid,
                })
                await monitor_yue2_prompt(client, client_id, pid, project_id, workflow, adapter.stage_progress)

            await gpu_oom_retry(_run_song_prompt, label=f"Project {project_id} 生歌")

        conn = db()
        try:
            song_row = conn.execute(
                "SELECT song_prompt_id FROM mv_projects WHERE project_id=?", (project_id,)
            ).fetchone()
        finally:
            conn.close()
        prompt_id = str(song_row["song_prompt_id"] or "") if song_row else ""

        _mv_update_project(project_id, song_progress=97, song_node="读取 YuE2 输出")
        history = await client.history(prompt_id)
        meta = audio_meta_from_history(history, prompt_id)
        if not meta:
            raise RuntimeError("YuE2 已完成，但 ComfyUI history 中没有找到音频输出")

        suffix = Path(str(meta.get("filename") or "song.flac")).suffix.lower() or ".flac"
        audio_dir = _mv_audio_dir(project_id)
        target = audio_dir / f"song{suffix}"
        temp_target = audio_dir / f".song_downloading{suffix}"
        temp_target.unlink(missing_ok=True)

        last_download_ts = 0.0

        async def report_song_download(received: int, total: int) -> None:
            nonlocal last_download_ts
            now_ts = time.monotonic()
            if (now_ts - last_download_ts < 0.25) and (not total or received < total):
                return
            last_download_ts = now_ts
            if total > 0:
                ratio = max(0.0, min(1.0, received / total))
                progress = 98.0 + ratio * 1.8
                node = f"下载歌曲到 Windows · {received / 1048576:.1f}/{total / 1048576:.1f} MB"
            else:
                progress = 98.0
                node = f"下载歌曲到 Windows · {received / 1048576:.1f} MB"
            _mv_update_project(project_id, song_progress=progress, song_node=node)
            await hub.broadcast({
                "type": "mv_song_progress",
                "project_id": project_id,
                "status": "RUNNING",
                "progress": progress,
                "node": node,
            })

        await client.download_view(meta, temp_target, progress_cb=report_song_download)
        if not temp_target.is_file() or temp_target.stat().st_size <= 0:
            raise RuntimeError("YuE2 音频下载失败")
        if target.exists():
            target.unlink()
        temp_target.replace(target)
        if old_audio_path and Path(old_audio_path) != target:
            _safe_unlink_project_media(project_id, old_audio_path)
        for stale_song in audio_dir.glob("song.*"):
            if stale_song.resolve() != target.resolve():
                _safe_unlink_project_media(project_id, stale_song)

        duration = media_duration_seconds(target)
        _mv_update_project(
            project_id,
            audio_path=str(target),
            audio_duration=duration,
            song_status="DONE",
            song_progress=100,
            song_node="Generation complete",
            song_error="",
            audio_source="yue2",
            source_voice_job_id=0,
        )
        await hub.broadcast({
            "type": "mv_song_state",
            "project_id": project_id,
            "status": "DONE",
            "progress": 100,
            "node": "Generation complete",
            "audio_url": f"/api/projects/{project_id}/mv/audio",
            "audio_duration": duration,
        })
        return {
            "ok": True,
            "audio_path": str(target),
            "duration": duration,
            "prompt_id": prompt_id,
            "engine": "comfyui-native-yue2",
        }
    except Exception as exc:
        # 生成失败时旧歌曲仍然有效：音乐台继续保留旧 audio_path 可听可用。
        _mv_update_project(
            project_id,
            song_status="FAILED",
            song_progress=0,
            song_node="",
            song_error=str(exc),
            song_prompt_id="",
            audio_path=old_audio_path,
        )
        await hub.broadcast({
            "type": "mv_song_state",
            "project_id": project_id,
            "status": "FAILED",
            "progress": 0,
            "node": "",
            "error": str(exc),
        })
        raise


async def prepare_song_regeneration(project_id: int) -> dict[str, Any]:
    """「重新生成」当前项目歌曲的第 1~5 步。

    必须核对当前项目 ID：不能在项目 B 打断或覆盖项目 A 的歌曲。
    旧歌曲（audio_path）在新歌下载成功前一律保留。
    """
    ensure_mv_project(project_id)

    if rt.current_song_project_id is not None and int(rt.current_song_project_id) != int(project_id):
        raise HTTPException(409, "当前有另一个项目正在生歌，请先等它结束再重新生成本项目歌曲")

    conn = db()
    try:
        row = conn.execute(
            "SELECT song_prompt_id,audio_path FROM mv_projects WHERE project_id=?",
            (project_id,),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        raise HTTPException(404, "Project not found")

    old_prompt_id = str(row["song_prompt_id"] or "")
    client = ComfyClient(get_settings()["comfy_url"])
    interrupted = await _interrupt_remote_prompt(client, old_prompt_id)

    if rt.active_song_task is not None and not rt.active_song_task.done():
        await _cancel_child_task(rt.active_song_task)
    rt.current_song_project_id = None

    _mv_update_project(
        project_id,
        song_status="IDLE",
        song_progress=0,
        song_node="",
        song_error="",
        song_prompt_id="",
        audio_path=str(row["audio_path"] or ""),
    )
    return {"removed_from_queue": 0, "interrupted_remote": interrupted}
