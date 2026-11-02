"""歌曲与分析接口。"""
from __future__ import annotations

import asyncio
import re
from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pathlib import Path
from typing import Any

from backend.core.db import db
from backend.core.media import media_duration_seconds
from backend.core.runtime import hub, rt
from backend.core.schemas import MVProjectPatch
from backend.models.registry import get_model
from backend.services.repo import _load_project_timeline, _mv_update_project, ensure_mv_project, mv_project_dir
from backend.services.songs import analyze_project_audio, generate_mv_song, invalidate_project_song_timeline, prepare_song_regeneration, remove_project_song_files

router = APIRouter()


@router.get("/api/projects/{project_id}/mv")
async def mv_project_get(project_id: int):
    mv = ensure_mv_project(project_id)
    result = dict(mv)
    has_audio = bool(str(mv.get("audio_path") or "").strip())
    result["audio_url"] = f"/api/projects/{project_id}/mv/audio" if has_audio else ""
    result["audio_download_url"] = f"/api/projects/{project_id}/mv/audio/download" if has_audio else ""
    result["master_url"] = f"/api/projects/{project_id}/mv/master" if str(mv.get("master_video_path") or "").strip() else ""
    return result


@router.put("/api/projects/{project_id}/mv")
async def mv_project_put(project_id: int, payload: MVProjectPatch):
    current_mv = ensure_mv_project(project_id)
    values = payload.model_dump(exclude_none=True)
    if "lyrics" in values and str(values["lyrics"]) != str(current_mv.get("lyrics") or ""):
        invalidate_project_song_timeline(project_id)
    fields: dict[str, Any] = {
        k: str(v) for k, v in values.items() if k in {"title", "genre_prompt", "lyrics"}
    }
    if "target_duration" in values:
        fields["target_duration"] = max(10, min(360, int(values["target_duration"])))
    if "audio_linked" in values:
        fields["audio_linked"] = 1 if values["audio_linked"] else 0
    if "native_audio_enabled" in values:
        fields["native_audio_enabled"] = 1 if values["native_audio_enabled"] else 0
    if "song_model" in values:
        try:
            fields["song_model"] = get_model("audio", values["song_model"])["id"]
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
    if fields:
        assignments = ",".join(f"{k}=?" for k in fields)
        conn = db()
        conn.execute(
            f"UPDATE mv_projects SET {assignments},updated_at=CURRENT_TIMESTAMP WHERE project_id=?",
            [*fields.values(), project_id],
        )
        conn.commit()
        conn.close()
    return await mv_project_get(project_id)


@router.post("/api/projects/{project_id}/mv/song/generate")
async def mv_song_generate(project_id: int):
    """音乐台的「生成歌曲 / 重新生成」：重新生成时先清掉本项目旧的等待协程与远端旧任务。

    这里必须核对项目 ID：项目 B 不能打断或覆盖项目 A 的歌曲。
    """
    running = rt.active_song_task
    if running is not None and not running.done():
        if rt.current_song_project_id is not None and int(rt.current_song_project_id) != int(project_id):
            raise HTTPException(409, "当前有另一个项目正在生歌，不能在本项目重新生成")
        # 同一个项目：先取消旧协程、清掉旧 prompt_id / 进度 / 错误，再重新提交。
        await prepare_song_regeneration(project_id)
    # 起成独立任务并留句柄：这样「重新生成」能真正取消它，
    # 对账器也能判断本地到底还有没有在跑（而不是只看数据库的 RUNNING）。
    task = asyncio.create_task(generate_mv_song(project_id))
    rt.active_song_task = task
    rt.current_song_project_id = project_id
    try:
        await asyncio.wait({task})
    except asyncio.CancelledError:
        task.cancel()
        raise
    finally:
        if rt.active_song_task is task:
            rt.active_song_task = None
            rt.current_song_project_id = None
    if task.cancelled():
        raise HTTPException(400, "生歌任务已取消")
    failure = task.exception()
    if failure:
        raise HTTPException(400, str(failure)) from failure
    return task.result()


@router.post("/api/projects/{project_id}/mv/audio")
async def mv_audio_upload(project_id: int, file: UploadFile = File(...)):
    ensure_mv_project(project_id)
    suffix = Path(file.filename or "song.mp3").suffix.lower()
    if suffix not in {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg"}:
        raise HTTPException(400, "只支持 mp3/wav/m4a/aac/flac/ogg 音频")
    # 当前项目只保留一首歌曲；上传新歌前清掉旧歌曲及其派生分析/对嘴结果。
    remove_project_song_files(project_id, include_audio=True)
    invalidate_project_song_timeline(project_id)

    audio_dir = mv_project_dir(project_id) / "audio"
    audio_dir.mkdir(parents=True, exist_ok=True)
    out_path = audio_dir / f"song{suffix}"
    with out_path.open("wb") as f:
        while chunk := await file.read(1024 * 1024):
            f.write(chunk)
    duration = media_duration_seconds(out_path)
    conn = db()
    conn.execute(
        """
        UPDATE mv_projects
        SET audio_path=?,audio_duration=?,song_status='DONE',song_progress=100,
            song_started_at=0,song_finished_at=0,song_elapsed_seconds=0,
            song_node='Manual upload',song_error='',audio_source='upload',source_voice_job_id=0,
            updated_at=CURRENT_TIMESTAMP
        WHERE project_id=?
        """,
        (str(out_path), duration, project_id),
    )
    conn.commit()
    conn.close()
    return {"ok": True, "duration": duration, "audio_url": f"/api/projects/{project_id}/mv/audio"}


@router.get("/api/projects/{project_id}/mv/audio")
async def mv_audio_file(project_id: int):
    mv = ensure_mv_project(project_id)
    path = Path(str(mv.get("audio_path") or ""))
    if not path.is_file():
        raise HTTPException(404, "还没有 MV 音频")
    return FileResponse(path)


@router.get("/api/projects/{project_id}/mv/audio/download")
async def mv_audio_download(project_id: int):
    mv = ensure_mv_project(project_id)
    path = Path(str(mv.get("audio_path") or ""))
    if not path.is_file():
        raise HTTPException(404, "还没有可下载的歌曲")
    raw_title = str(mv.get("title") or "YuE2 song").strip() or "YuE2 song"
    safe_title = re.sub(r'[\\/:*?"<>|]+', "_", raw_title).strip(" .") or "YuE2 song"
    return FileResponse(path, filename=f"{safe_title}{path.suffix.lower() or '.flac'}")


@router.delete("/api/projects/{project_id}/mv/audio")
async def mv_audio_delete(project_id: int):
    mv = ensure_mv_project(project_id)
    if str(mv.get("song_status") or "").upper() == "RUNNING":
        raise HTTPException(409, "歌曲正在生成中，完成或中断后再删除")

    remove_project_song_files(project_id, include_audio=True)
    invalidate_project_song_timeline(project_id)
    _mv_update_project(
        project_id,
        audio_path="",
        audio_duration=0,
        audio_linked=0,
        audio_source="",
        source_voice_job_id=0,
        song_status="PENDING",
        song_progress=0,
        song_node="",
        song_error="",
        master_video_path="",
    )
    await hub.broadcast({
        "type": "mv_song_state",
        "project_id": project_id,
        "status": "PENDING",
        "progress": 0,
        "node": "",
        "deleted": True,
    })
    return {"ok": True}


@router.post("/api/projects/{project_id}/audio/analyze")
async def project_audio_analyze(project_id: int):
    try:
        return await analyze_project_audio(project_id)
    except Exception as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get("/api/projects/{project_id}/audio/timeline")
async def project_audio_timeline(project_id: int):
    mv = ensure_mv_project(project_id)
    timeline = _load_project_timeline(project_id)
    conn = db()
    rows = conn.execute(
        """
        SELECT sh.id, sh.scene_id, sh.shot_no, sh.title, sh.timeline_start, sh.timeline_end,
               sh.target_duration, sh.lyric_text, sh.lip_sync_enabled, sh.lip_sync_status,
               sh.lip_sync_progress, sh.lip_sync_error, sh.source_duration, sh.conform_mode
        FROM shots sh
        JOIN scenes sc ON sc.id=sh.scene_id
        WHERE sc.project_id=?
        ORDER BY sc.sort_order, sc.id, sh.shot_no
        """,
        (project_id,),
    ).fetchall()
    conn.close()
    return {
        "analysis_status": str(mv.get("analysis_status") or "IDLE"),
        "analysis_progress": float(mv.get("analysis_progress") or 0),
        "analysis_error": str(mv.get("analysis_error") or ""),
        "audio_duration": float(mv.get("audio_duration") or 0),
        "timeline": timeline,
        "shots": [dict(row) for row in rows],
    }


@router.get("/api/projects/{project_id}/audio/lyrics.srt")
async def project_audio_srt(project_id: int):
    mv = ensure_mv_project(project_id)
    path = Path(str(mv.get("lyrics_srt_path") or ""))
    if not path.is_file():
        raise HTTPException(404, "还没有歌词 SRT，请先分析歌曲")
    return FileResponse(path, media_type="application/x-subrip", filename="lyrics.srt")
