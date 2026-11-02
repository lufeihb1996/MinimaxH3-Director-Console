"""配音任务 (voice_jobs) 与全局音色库 (voice_profiles) 服务：CRUD、队列 Worker、重启恢复、设为项目音频。"""
from __future__ import annotations

import asyncio
import json
import re
import shutil
import subprocess
import time
import uuid
import websockets
from fastapi import HTTPException, UploadFile
from fastapi.responses import JSONResponse
from pathlib import Path
from typing import Any

from backend.core.comfy import (
    ComfyClient,
    _interrupt_remote_prompt,
    audio_meta_from_history,
    comfy_prompt_presence,
    decode_comfy_progress_text,
    history_terminal_state,
    workflow_node_label,
)
from backend.core.db import db, get_settings
from backend.core.gpu import gpu_oom_retry, gpu_slot
from backend.core.media import media_duration_seconds
from backend.core.runtime import hub, rt, voice_job_queue
from backend.core.schemas import (
    UseAsProjectAudioIn,
    VoiceJobCreateIn,
    VoiceJobPatch,
    VoiceJobSpeakerIn,
    VoiceProfilePatch,
)
from backend.core.tasks import _cancel_active_item, _remove_from_queue
from backend.services.drama3 import (
    DRAMA3_OUTPUTS_DIR,
    DRAMA3_VOICES_DIR,
    _AUDIO_EXTS,
    _VIDEO_EXTS,
    _clean_single_speaker_script,
    _clean_uploaded_voice_sync,
    _drama3_stage_progress,
    build_drama3_workflow,
)
from backend.services.repo import _mv_update_project, ensure_mv_project, mv_project_dir
from backend.services.songs import invalidate_project_song_timeline, remove_project_song_files

DEFAULT_DRAMA3_PARAMS: dict[str, Any] = {
    "model_name": "drama3_w6a8experts_bf16.safetensors",
    "experts": "in RAM",
    "expert_cache_gb": 0.0,
    "temperature": 1.0,
    "top_p": 0.9,
    "top_k": 0,
    "max_frames": 800,
    "seed": 42,
    "speakers_mode": "one take per line",
    "split_long_text": True,
    "shorten_long_pauses": True,
    "asr_language": "auto",
}


def extract_spoken_lyrics(script_text: str) -> str:
    """从配音脚本提取纯台词，一行脚本对应一行歌词（供音频对齐分析使用）。"""
    out_lines: list[str] = []
    for raw_line in str(script_text or "").splitlines():
        line = re.sub(r"<\|[^|>]*\|>", "", raw_line)
        line = re.sub(r"\[[^\[\]]*\]", "", line)
        line = re.sub(r"[\s\u3000]+", " ", line).strip()
        if line:
            out_lines.append(line)
    return "\n".join(out_lines)


def get_drama3_defaults() -> dict[str, Any]:
    conn = db()
    try:
        row = conn.execute("SELECT value FROM settings WHERE key='drama3_defaults'").fetchone()
    finally:
        conn.close()
    defaults = dict(DEFAULT_DRAMA3_PARAMS)
    if row and row["value"]:
        try:
            parsed = json.loads(row["value"])
            if isinstance(parsed, dict):
                for k in DEFAULT_DRAMA3_PARAMS:
                    if k in parsed and parsed[k] is not None:
                        defaults[k] = parsed[k]
        except Exception:
            pass
    return defaults


def _save_drama3_defaults(params: dict[str, Any]) -> None:
    current = get_drama3_defaults()
    for k in DEFAULT_DRAMA3_PARAMS:
        if k in params and params[k] is not None:
            current[k] = params[k]
    conn = db()
    try:
        conn.execute(
            """
            INSERT INTO settings(key, value) VALUES('drama3_defaults', ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value
            """,
            (json.dumps(current, ensure_ascii=False),),
        )
        conn.commit()
    finally:
        conn.close()


# ============================================================================
# 2.2 音色库 (voice_profiles)
# ============================================================================

def _serialize_voice_profile(row: Any, used_by_jobs: int = 0) -> dict[str, Any]:
    pid = int(row["id"])
    return {
        "id": pid,
        "name": str(row["name"] or ""),
        "duration": float(row["duration"] or 0.0),
        "transcript": str(row["transcript"] or ""),
        "remote_name": str(row["remote_name"] or ""),
        "audio_url": f"/api/voices/{pid}/audio",
        "used_by_jobs": int(used_by_jobs),
    }


def list_voice_profiles() -> list[dict[str, Any]]:
    conn = db()
    try:
        rows = conn.execute(
            """
            SELECT vp.*, COUNT(DISTINCT vjs.job_id) AS used_by_jobs
            FROM voice_profiles vp
            LEFT JOIN voice_job_speakers vjs ON vjs.voice_profile_id = vp.id
            GROUP BY vp.id
            ORDER BY vp.id
            """
        ).fetchall()
    finally:
        conn.close()
    return [_serialize_voice_profile(r, int(r["used_by_jobs"] or 0)) for r in rows]


def get_voice_profile(profile_id: int) -> dict[str, Any]:
    conn = db()
    try:
        row = conn.execute("SELECT * FROM voice_profiles WHERE id=?", (profile_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Voice not found")
        cnt = conn.execute(
            "SELECT COUNT(DISTINCT job_id) AS c FROM voice_job_speakers WHERE voice_profile_id=?",
            (profile_id,),
        ).fetchone()["c"]
    finally:
        conn.close()
    return _serialize_voice_profile(row, int(cnt or 0))


async def create_voice_profile(file: UploadFile, name: str = "") -> dict[str, Any]:
    suffix = Path(file.filename or "voice.wav").suffix.lower() or ".wav"
    if suffix not in (_AUDIO_EXTS | _VIDEO_EXTS):
        raise HTTPException(400, f"不支持的文件格式: {suffix}（支持音频 wav/mp3/flac/m4a 及视频 mp4/mov/mkv/webm）")

    data = await file.read()
    if not data:
        raise HTTPException(400, "上传的文件为空")

    DRAMA3_VOICES_DIR.mkdir(parents=True, exist_ok=True)
    raw_stem = Path(file.filename or "voice").stem
    safe_stem = re.sub(r"[^a-zA-Z0-9_\-\u4e00-\u9fa5]+", "_", raw_stem)[:32] or "voice"
    uid = uuid.uuid4().hex[:6]

    ffmpeg = shutil.which("ffmpeg")
    temp_input = DRAMA3_VOICES_DIR / f".tmp_in_{uid}{suffix}"
    temp_raw_wav = DRAMA3_VOICES_DIR / f".tmp_raw_{uid}.wav"
    target_file = DRAMA3_VOICES_DIR / f"voice_{safe_stem}_{uid}.wav"
    temp_input.write_bytes(data)

    try:
        if ffmpeg:
            cmd = [
                ffmpeg, "-v", "error", "-y",
                "-i", str(temp_input),
                "-vn", "-t", "25",
                "-acodec", "pcm_s16le", "-ar", "44100", "-ac", "1",
                str(temp_raw_wav),
            ]
            proc = await asyncio.to_thread(subprocess.run, cmd, capture_output=True, text=True, timeout=60)
            if proc.returncode != 0 or not temp_raw_wav.is_file() or temp_raw_wav.stat().st_size <= 1024:
                raise HTTPException(400, "无法从该文件提取有效人声音轨，请上传带有人声的音频或视频")
        elif suffix in _VIDEO_EXTS:
            raise HTTPException(500, "未找到 FFmpeg，无法从视频中提取参考音轨")
        else:
            shutil.copy2(temp_input, temp_raw_wav)

        await asyncio.to_thread(_clean_uploaded_voice_sync, temp_raw_wav, target_file)
    finally:
        temp_input.unlink(missing_ok=True)
        temp_raw_wav.unlink(missing_ok=True)

    duration = media_duration_seconds(target_file)
    display_name = str(name or "").strip() or raw_stem or "参考干声"

    conn = db()
    try:
        cur = conn.execute(
            """
            INSERT INTO voice_profiles(name, ref_audio_path, remote_name, transcript, duration)
            VALUES (?, ?, '', '', ?)
            """,
            (display_name, str(target_file), float(duration)),
        )
        profile_id = int(cur.lastrowid)
        conn.commit()
    finally:
        conn.close()
    return get_voice_profile(profile_id)


def patch_voice_profile(profile_id: int, payload: VoiceProfilePatch) -> dict[str, Any]:
    conn = db()
    try:
        row = conn.execute("SELECT * FROM voice_profiles WHERE id=?", (profile_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Voice not found")
        updates: dict[str, Any] = {}
        if payload.name is not None:
            updates["name"] = str(payload.name).strip() or str(row["name"] or "")
        if payload.transcript is not None:
            updates["transcript"] = str(payload.transcript)
        if payload.remote_name is not None:
            updates["remote_name"] = str(payload.remote_name).strip()
        if updates:
            cols = ", ".join(f"{k}=?" for k in updates)
            conn.execute(
                f"UPDATE voice_profiles SET {cols}, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                [*updates.values(), profile_id],
            )
            conn.commit()
    finally:
        conn.close()
    return get_voice_profile(profile_id)


def delete_voice_profile(profile_id: int, force: bool = False) -> dict[str, Any]:
    conn = db()
    try:
        row = conn.execute("SELECT * FROM voice_profiles WHERE id=?", (profile_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Voice not found")
        cnt = int(
            conn.execute(
                "SELECT COUNT(DISTINCT job_id) AS c FROM voice_job_speakers WHERE voice_profile_id=?",
                (profile_id,),
            ).fetchone()["c"]
            or 0
        )
        if cnt > 0 and not force:
            raise HTTPException(409, f"该音色正被 {cnt} 条配音任务引用，不能直接删除")
        ref_path = str(row["ref_audio_path"] or "").strip()
        conn.execute("DELETE FROM voice_profiles WHERE id=?", (profile_id,))
        remaining = 0
        if ref_path:
            remaining = int(
                conn.execute(
                    "SELECT COUNT(*) AS c FROM voice_profiles WHERE ref_audio_path=?",
                    (ref_path,),
                ).fetchone()["c"]
                or 0
            )
        conn.commit()
    finally:
        conn.close()

    if ref_path and remaining == 0:
        try:
            target_p = Path(ref_path).resolve()
            target_p.relative_to(DRAMA3_VOICES_DIR.resolve())
            if target_p.is_file():
                target_p.unlink()
        except Exception:
            pass
    return {"ok": True}


def get_voice_profile_audio_file(profile_id: int) -> Path:
    conn = db()
    try:
        row = conn.execute("SELECT ref_audio_path FROM voice_profiles WHERE id=?", (profile_id,)).fetchone()
    finally:
        conn.close()
    if not row:
        raise HTTPException(404, "Voice not found")
    path = Path(str(row["ref_audio_path"] or ""))
    if not path.is_file():
        raise HTTPException(404, "音色参考音频文件不存在")
    return path


# ============================================================================
# 2.3 配音任务 (voice_jobs)
# ============================================================================

def _serialize_voice_job(
    row: Any,
    speakers: list[dict[str, Any]],
    project_mv_map: dict[int, tuple[str, int]],
) -> dict[str, Any]:
    jid = int(row["id"])
    pid = int(row["project_id"]) if row["project_id"] is not None else None
    audio_path = str(row["audio_path"] or "").strip()
    has_audio = bool(audio_path and Path(audio_path).is_file())
    is_project_audio = False
    if pid is not None and pid in project_mv_map:
        src_type, src_job_id = project_mv_map[pid]
        is_project_audio = (src_type == "voice_job" and src_job_id == jid)

    return {
        "id": jid,
        "project_id": pid,
        "sort_order": int(row["sort_order"] or 0),
        "title": str(row["title"] or ""),
        "script_text": str(row["script_text"] or ""),
        "params": {
            "model_name": str(row["model_name"] or DEFAULT_DRAMA3_PARAMS["model_name"]),
            "experts": str(row["experts"] or DEFAULT_DRAMA3_PARAMS["experts"]),
            "expert_cache_gb": float(row["expert_cache_gb"] or 0.0),
            "temperature": float(row["temperature"] if row["temperature"] is not None else 1.0),
            "top_p": float(row["top_p"] if row["top_p"] is not None else 0.9),
            "top_k": int(row["top_k"] or 0),
            "max_frames": int(row["max_frames"] or 800),
            "seed": int(row["seed"] if row["seed"] is not None else 42),
            "speakers_mode": str(row["speakers_mode"] or "one take per line"),
            "split_long_text": bool(int(row["split_long_text"]) if row["split_long_text"] is not None else 1),
            "shorten_long_pauses": bool(int(row["shorten_long_pauses"]) if row["shorten_long_pauses"] is not None else 1),
            "asr_language": str(row["asr_language"] or "auto"),
        },
        "speakers": speakers,
        "status": str(row["status"] or "PENDING"),
        "progress": float(row["progress"] or 0.0),
        "current_node": str(row["current_node"] or ""),
        "error": str(row["error"] or ""),
        "duration": float(row["duration"] or 0.0),
        "elapsed_seconds": float(row["elapsed_seconds"] or 0.0),
        "audio_url": f"/api/voice-jobs/{jid}/audio" if has_audio else "",
        "download_url": f"/api/voice-jobs/{jid}/download" if has_audio else "",
        "has_audio": has_audio,
        "is_project_audio": is_project_audio,
        "created_at": str(row["created_at"] or ""),
    }


def _fetch_jobs_serialized(where_sql: str, params: tuple[Any, ...]) -> list[dict[str, Any]]:
    conn = db()
    try:
        rows = conn.execute(
            f"SELECT * FROM voice_jobs WHERE {where_sql} ORDER BY sort_order, id",
            params,
        ).fetchall()
        if not rows:
            return []
        job_ids = [int(r["id"]) for r in rows]
        placeholders = ",".join("?" for _ in job_ids)
        spk_rows = conn.execute(
            f"""
            SELECT vjs.job_id, vjs.speaker_index, vjs.name, vjs.voice_profile_id,
                   vp.name AS voice_name, vp.duration AS voice_duration,
                   vp.transcript AS voice_transcript, vp.remote_name AS voice_remote_name
            FROM voice_job_speakers vjs
            LEFT JOIN voice_profiles vp ON vp.id = vjs.voice_profile_id
            WHERE vjs.job_id IN ({placeholders})
            ORDER BY vjs.job_id, vjs.speaker_index
            """,
            job_ids,
        ).fetchall()

        pids = sorted({int(r["project_id"]) for r in rows if r["project_id"] is not None})
        project_mv_map: dict[int, tuple[str, int]] = {}
        if pids:
            p_holders = ",".join("?" for _ in pids)
            mv_rows = conn.execute(
                f"SELECT project_id, audio_source, source_voice_job_id FROM mv_projects WHERE project_id IN ({p_holders})",
                pids,
            ).fetchall()
            for mr in mv_rows:
                project_mv_map[int(mr["project_id"])] = (
                    str(mr["audio_source"] or ""),
                    int(mr["source_voice_job_id"] or 0),
                )
    finally:
        conn.close()

    speakers_by_job: dict[int, list[dict[str, Any]]] = {jid: [] for jid in job_ids}
    for sr in spk_rows:
        jid = int(sr["job_id"])
        sidx = int(sr["speaker_index"])
        default_name = "Role 0 (lead)" if sidx == 0 else f"Role {sidx}"
        vpid = int(sr["voice_profile_id"]) if sr["voice_profile_id"] is not None else None
        speakers_by_job[jid].append({
            "speaker_index": sidx,
            "name": str(sr["name"] or default_name),
            "voice_profile_id": vpid,
            "voice_name": str(sr["voice_name"] or "") if vpid is not None else "",
        })

    for jid, spk_list in speakers_by_job.items():
        if not spk_list:
            spk_list.append({
                "speaker_index": 0,
                "name": "Role 0 (lead)",
                "voice_profile_id": None,
                "voice_name": "",
            })

    return [_serialize_voice_job(r, speakers_by_job[int(r["id"])], project_mv_map) for r in rows]


def get_voice_job(job_id: int) -> dict[str, Any]:
    items = _fetch_jobs_serialized("id=?", (job_id,))
    if not items:
        raise HTTPException(404, "Voice-over task not found")
    return items[0]


def list_project_voice_jobs(project_id: int) -> list[dict[str, Any]]:
    return _fetch_jobs_serialized("project_id=?", (project_id,))


def list_unassigned_voice_jobs() -> list[dict[str, Any]]:
    return _fetch_jobs_serialized("project_id IS NULL", ())


def create_project_voice_job(project_id: int, payload: VoiceJobCreateIn) -> dict[str, Any]:
    conn = db()
    try:
        proj = conn.execute("SELECT id FROM projects WHERE id=?", (project_id,)).fetchone()
        if not proj:
            raise HTTPException(404, "Project not found")

        params = get_drama3_defaults()
        speakers_to_create: list[tuple[int, str, int | None]] = [(0, "Role 0 (lead)", None)]

        if payload.from_job_id is not None:
            ref_row = conn.execute("SELECT * FROM voice_jobs WHERE id=?", (int(payload.from_job_id),)).fetchone()
            if ref_row:
                for k in DEFAULT_DRAMA3_PARAMS:
                    if ref_row[k] is not None:
                        if k in {"split_long_text", "shorten_long_pauses"}:
                            params[k] = bool(int(ref_row[k]))
                        else:
                            params[k] = ref_row[k]
                ref_spks = conn.execute(
                    "SELECT speaker_index, name, voice_profile_id FROM voice_job_speakers WHERE job_id=? ORDER BY speaker_index",
                    (int(payload.from_job_id),),
                ).fetchall()
                if ref_spks:
                    speakers_to_create = [
                        (
                            int(s["speaker_index"]),
                            str(s["name"] or ""),
                            int(s["voice_profile_id"]) if s["voice_profile_id"] is not None else None,
                        )
                        for s in ref_spks
                    ]

        if isinstance(payload.params, dict):
            for k in DEFAULT_DRAMA3_PARAMS:
                if k in payload.params and payload.params[k] is not None:
                    params[k] = payload.params[k]

        if isinstance(payload.speakers, list) and payload.speakers:
            custom_spks: list[tuple[int, str, int | None]] = []
            for idx, spk in enumerate(payload.speakers[:16]):
                if not isinstance(spk, dict):
                    continue
                sidx = max(0, min(15, int(spk.get("speaker_index", idx))))
                default_name = "Role 0 (lead)" if sidx == 0 else f"Role {sidx}"
                sname = str(spk.get("name") or default_name).strip() or default_name
                vpid = int(spk["voice_profile_id"]) if spk.get("voice_profile_id") else None
                custom_spks.append((sidx, sname, vpid))
            if custom_spks:
                speakers_to_create = custom_spks

        stat_row = conn.execute(
            "SELECT COUNT(*) AS cnt, COALESCE(MAX(sort_order), -1) + 1 AS next_sort FROM voice_jobs WHERE project_id=?",
            (project_id,),
        ).fetchone()
        next_no = int(stat_row["cnt"] or 0) + 1
        next_sort = int(stat_row["next_sort"] or 0)

        title = str(payload.title or "").strip() or f"配音任务 #{next_no:03d}"
        script_text = _clean_single_speaker_script(payload.script_text or "")
        current_node = "Parameters ready, not generated" if script_text else "Waiting for lines"

        cur = conn.execute(
            """
            INSERT INTO voice_jobs(
                project_id, sort_order, title, script_text,
                model_name, experts, expert_cache_gb, temperature, top_p, top_k,
                max_frames, seed, speakers_mode, split_long_text, shorten_long_pauses,
                asr_language, status, progress, current_node
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'PENDING', 0, ?)
            """,
            (
                project_id,
                next_sort,
                title,
                script_text,
                str(params["model_name"]),
                str(params["experts"]),
                float(params["expert_cache_gb"]),
                float(params["temperature"]),
                float(params["top_p"]),
                int(params["top_k"]),
                int(params["max_frames"]),
                int(params["seed"]),
                str(params["speakers_mode"]),
                1 if bool(params["split_long_text"]) else 0,
                1 if bool(params["shorten_long_pauses"]) else 0,
                str(params["asr_language"]),
                current_node,
            ),
        )
        job_id = int(cur.lastrowid)
        for sidx, sname, vpid in speakers_to_create:
            conn.execute(
                """
                INSERT OR REPLACE INTO voice_job_speakers(job_id, speaker_index, name, voice_profile_id)
                VALUES (?, ?, ?, ?)
                """,
                (job_id, sidx, sname, vpid),
            )
        conn.commit()
    finally:
        conn.close()

    return get_voice_job(job_id)


def patch_voice_job(job_id: int, payload: VoiceJobPatch) -> dict[str, Any]:
    conn = db()
    try:
        row = conn.execute("SELECT * FROM voice_jobs WHERE id=?", (job_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Voice-over task not found")
        status = str(row["status"] or "PENDING").upper()
        if status in {"QUEUED", "RUNNING", "PREPARING"}:
            raise HTTPException(409, "配音任务正在排队或生成中，不能修改")

        updates: dict[str, Any] = {}
        if "project_id" in payload.model_fields_set:
            if payload.project_id is None or int(payload.project_id) == 0:
                updates["project_id"] = None
            else:
                proj = conn.execute("SELECT id FROM projects WHERE id=?", (int(payload.project_id),)).fetchone()
                if not proj:
                    raise HTTPException(404, "目标 Project 不存在")
                updates["project_id"] = int(payload.project_id)
        if payload.title is not None:
            updates["title"] = str(payload.title).strip() or str(row["title"] or "")
        if payload.script_text is not None:
            cleaned = _clean_single_speaker_script(payload.script_text)
            updates["script_text"] = cleaned
            if status == "PENDING":
                updates["current_node"] = "Parameters ready, not generated" if cleaned else "Waiting for lines"

        param_updates: dict[str, Any] = {}
        if isinstance(payload.params, dict):
            for k in DEFAULT_DRAMA3_PARAMS:
                if k in payload.params and payload.params[k] is not None:
                    val = payload.params[k]
                    if k in {"split_long_text", "shorten_long_pauses"}:
                        updates[k] = 1 if bool(val) else 0
                        param_updates[k] = bool(val)
                    elif k in {"expert_cache_gb", "temperature", "top_p"}:
                        updates[k] = float(val)
                        param_updates[k] = float(val)
                    elif k in {"top_k", "max_frames", "seed"}:
                        updates[k] = int(val)
                        param_updates[k] = int(val)
                    else:
                        updates[k] = str(val)
                        param_updates[k] = str(val)

        if updates:
            set_sql = ", ".join(f"{k}=?" for k in updates)
            conn.execute(
                f"UPDATE voice_jobs SET {set_sql}, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                [*updates.values(), job_id],
            )
            conn.commit()
    finally:
        conn.close()

    if param_updates:
        _save_drama3_defaults(param_updates)
    return get_voice_job(job_id)


def put_voice_job_speaker(job_id: int, speaker_index: int, payload: VoiceJobSpeakerIn) -> dict[str, Any]:
    if speaker_index < 0 or speaker_index > 15:
        raise HTTPException(400, "speaker_index 必须在 0～15 之间")
    conn = db()
    try:
        job_row = conn.execute("SELECT id, status FROM voice_jobs WHERE id=?", (job_id,)).fetchone()
        if not job_row:
            raise HTTPException(404, "Voice-over task not found")
        if str(job_row["status"] or "").upper() in {"QUEUED", "RUNNING", "PREPARING"}:
            raise HTTPException(409, "Voice-over task is queued or running; roles cannot be changed")

        existing = conn.execute(
            "SELECT * FROM voice_job_speakers WHERE job_id=? AND speaker_index=?",
            (job_id, speaker_index),
        ).fetchone()
        default_name = "Role 0 (lead)" if speaker_index == 0 else f"角色 {speaker_index}"
        name = (
            str(payload.name).strip()
            if payload.name is not None
            else (str(existing["name"] or default_name) if existing else default_name)
        ) or default_name

        if "voice_profile_id" in payload.model_fields_set:
            vpid = int(payload.voice_profile_id) if payload.voice_profile_id else None
        else:
            vpid = int(existing["voice_profile_id"]) if (existing and existing["voice_profile_id"] is not None) else None

        if vpid is not None:
            vp = conn.execute("SELECT id FROM voice_profiles WHERE id=?", (vpid,)).fetchone()
            if not vp:
                raise HTTPException(404, "指定的音色不存在")

        conn.execute(
            """
            INSERT INTO voice_job_speakers(job_id, speaker_index, name, voice_profile_id)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(job_id, speaker_index) DO UPDATE SET
                name=excluded.name,
                voice_profile_id=excluded.voice_profile_id
            """,
            (job_id, speaker_index, name, vpid),
        )
        conn.commit()
    finally:
        conn.close()
    return get_voice_job(job_id)


def delete_voice_job_speaker(job_id: int, speaker_index: int) -> dict[str, Any]:
    if speaker_index <= 0:
        raise HTTPException(400, "至少保留 speaker 0")
    conn = db()
    try:
        job_row = conn.execute("SELECT id, status FROM voice_jobs WHERE id=?", (job_id,)).fetchone()
        if not job_row:
            raise HTTPException(404, "Voice-over task not found")
        if str(job_row["status"] or "").upper() in {"QUEUED", "RUNNING", "PREPARING"}:
            raise HTTPException(409, "Voice-over task is queued or running; roles cannot be changed")
        conn.execute(
            "DELETE FROM voice_job_speakers WHERE job_id=? AND speaker_index=?",
            (job_id, speaker_index),
        )
        conn.commit()
    finally:
        conn.close()
    return get_voice_job(job_id)


def _update_voice_job_fields(job_id: int, **fields: Any) -> None:
    if not fields:
        return
    conn = db()
    try:
        cols = ", ".join(f"{k}=?" for k in fields)
        conn.execute(
            f"UPDATE voice_jobs SET {cols}, updated_at=CURRENT_TIMESTAMP WHERE id=?",
            [*fields.values(), job_id],
        )
        conn.commit()
    finally:
        conn.close()


async def prepare_voice_job_regeneration(job_id: int) -> None:
    """仅取消本任务自身的旧排队记录或正在运行的协程/远端 prompt，绝不影响其他配音任务。"""
    conn = db()
    try:
        row = conn.execute("SELECT prompt_id FROM voice_jobs WHERE id=?", (job_id,)).fetchone()
    finally:
        conn.close()
    old_prompt_id = str(row["prompt_id"] or "") if row else ""
    await _remove_from_queue(voice_job_queue, job_id)
    if rt.current_voice_job_id is not None and int(rt.current_voice_job_id) == int(job_id):
        if old_prompt_id:
            client = ComfyClient(get_settings()["comfy_url"])
            await _interrupt_remote_prompt(client, old_prompt_id)
        rt.current_voice_job_id = await _cancel_active_item(
            rt.active_voice_task, rt.current_voice_job_id, job_id
        )


async def enqueue_voice_job(job_id: int) -> dict[str, Any]:
    job = get_voice_job(job_id)
    script_text = _clean_single_speaker_script(job.get("script_text") or "")
    if not script_text:
        raise HTTPException(400, "请先填写需要配音的台词内容")

    if str(job.get("status") or "").upper() in {"QUEUED", "RUNNING", "PREPARING"}:
        await prepare_voice_job_regeneration(job_id)

    _update_voice_job_fields(
        job_id,
        script_text=script_text,
        status="QUEUED",
        progress=0.0,
        current_node="Waiting in queue",
        prompt_id="",
        error="",
    )
    await voice_job_queue.put(job_id)
    await hub.broadcast({
        "type": "voice_job_state",
        "job_id": job_id,
        "project_id": job.get("project_id"),
        "status": "QUEUED",
        "error": "",
    })
    return {"queued": True, "job_id": job_id, "queue_size": voice_job_queue.qsize()}


async def cancel_voice_job(job_id: int) -> dict[str, Any]:
    job = get_voice_job(job_id)
    await prepare_voice_job_regeneration(job_id)
    _update_voice_job_fields(
        job_id,
        status="CANCELLED",
        progress=0.0,
        current_node="Cancelled",
        prompt_id="",
        error="",
    )
    await hub.broadcast({
        "type": "voice_job_state",
        "job_id": job_id,
        "project_id": job.get("project_id"),
        "status": "CANCELLED",
        "error": "",
    })
    return get_voice_job(job_id)


async def delete_voice_job(job_id: int) -> dict[str, Any]:
    conn = db()
    try:
        row = conn.execute("SELECT * FROM voice_jobs WHERE id=?", (job_id,)).fetchone()
    finally:
        conn.close()
    if not row:
        raise HTTPException(404, "Voice-over task not found")
    status = str(row["status"] or "").upper()
    if job_id == rt.current_voice_job_id or status in {"RUNNING", "PREPARING"}:
        raise HTTPException(409, "配音任务正在运行中，请先取消后再删除")

    await _remove_from_queue(voice_job_queue, job_id)
    audio_path = str(row["audio_path"] or "").strip()

    conn = db()
    try:
        conn.execute("DELETE FROM voice_jobs WHERE id=?", (job_id,))
        conn.commit()
    finally:
        conn.close()

    if audio_path:
        try:
            out_p = Path(audio_path).resolve()
            out_p.relative_to(DRAMA3_OUTPUTS_DIR.resolve())
            if out_p.is_file():
                out_p.unlink()
        except Exception:
            pass
    return {"ok": True, "id": job_id}


def get_voice_job_output_file(job_id: int) -> tuple[Path, str]:
    conn = db()
    try:
        row = conn.execute("SELECT title, audio_path FROM voice_jobs WHERE id=?", (job_id,)).fetchone()
    finally:
        conn.close()
    if not row:
        raise HTTPException(404, "Voice-over task not found")
    path = Path(str(row["audio_path"] or ""))
    if not path.is_file():
        raise HTTPException(404, "配音输出文件不存在")
    raw_title = str(row["title"] or f"voice_job_{job_id}").strip() or f"voice_job_{job_id}"
    safe_title = re.sub(r'[\\/:*?"<>|]+', "_", raw_title).strip(" .") or f"voice_job_{job_id}"
    suffix = path.suffix.lower() or ".flac"
    return path, f"{safe_title}{suffix}"


# ============================================================================
# 2.4 ⭐ 设为本项目音频
# ============================================================================

async def use_voice_job_as_project_audio(
    job_id: int,
    payload: UseAsProjectAudioIn,
) -> dict[str, Any] | JSONResponse:
    conn = db()
    try:
        job_row = conn.execute("SELECT * FROM voice_jobs WHERE id=?", (job_id,)).fetchone()
    finally:
        conn.close()
    if not job_row:
        raise HTTPException(404, "Voice-over task not found")

    audio_path = Path(str(job_row["audio_path"] or ""))
    if str(job_row["status"] or "").upper() != "DONE" or not audio_path.is_file():
        raise HTTPException(400, "配音还没生成完成")

    if job_row["project_id"] is None:
        raise HTTPException(400, "请先把任务移到某个项目")
    project_id = int(job_row["project_id"])

    mv = ensure_mv_project(project_id)
    if str(mv.get("analysis_status") or "").upper() == "RUNNING" or str(mv.get("song_status") or "").upper() == "RUNNING":
        raise HTTPException(409, "当前项目正在生歌或分析音频，请等待完成后再操作")

    existing_audio = str(mv.get("audio_path") or "").strip()
    existing_source = str(mv.get("audio_source") or "")
    existing_job_id = int(mv.get("source_voice_job_id") or 0)
    is_same_source = (existing_source == "voice_job" and existing_job_id == job_id)

    if existing_audio and not is_same_source and not payload.confirm_replace:
        return JSONResponse(
            status_code=409,
            content={
                "code": "AUDIO_EXISTS",
                "audio_source": existing_source or "upload",
                "source_voice_job_id": existing_job_id,
                "detail": "本项目已有音频，确认替换吗？",
            },
        )

    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise HTTPException(500, "未找到 FFmpeg，无法转码配音音频")

    audio_dir = mv_project_dir(project_id) / "audio"
    audio_dir.mkdir(parents=True, exist_ok=True)
    out_wav = audio_dir / f"voice_{job_id}.wav"
    # 先转码到临时文件：转码失败时项目原有音频保持不动
    tmp_wav = audio_dir / f".voice_{job_id}_converting.wav"
    tmp_wav.unlink(missing_ok=True)

    cmd = [
        ffmpeg, "-v", "error", "-y",
        "-i", str(audio_path),
        "-ar", "48000", "-ac", "2", "-c:a", "pcm_s16le",
        str(tmp_wav),
    ]
    proc = await asyncio.to_thread(subprocess.run, cmd, capture_output=True, text=True, timeout=120)
    if proc.returncode != 0 or not tmp_wav.is_file() or tmp_wav.stat().st_size <= 0:
        tmp_wav.unlink(missing_ok=True)
        raise HTTPException(500, f"FFmpeg 转码 48kHz WAV 失败: {proc.stderr[-500:]}")

    remove_project_song_files(project_id, include_audio=True)
    invalidate_project_song_timeline(project_id)
    tmp_wav.replace(out_wav)

    audio_duration = media_duration_seconds(out_wav)
    lyrics = extract_spoken_lyrics(str(job_row["script_text"] or ""))
    node_label = f"配音台 · 任务 #{job_id}"

    _mv_update_project(
        project_id,
        audio_path=str(out_wav),
        audio_duration=audio_duration,
        song_status="DONE",
        song_progress=100,
        song_node=node_label,
        song_error="",
        audio_source="voice_job",
        source_voice_job_id=job_id,
        lyrics=lyrics,
        audio_linked=1,
        native_audio_enabled=1,
        analysis_status="IDLE",
        analysis_progress=0,
        analysis_error="",
    )

    await hub.broadcast({
        "type": "mv_song_state",
        "project_id": project_id,
        "status": "DONE",
        "node": node_label,
    })

    lyrics_lines = [line for line in lyrics.splitlines() if line.strip()]
    return {
        "ok": True,
        "audio_duration": audio_duration,
        "lyrics_lines": lyrics_lines,
        "next": "analyze",
    }


# ============================================================================
# 2.7 配音队列 Worker 与重启恢复
# ============================================================================

async def _monitor_voice_job_prompt(
    client: ComfyClient,
    client_id: str,
    prompt_id: str,
    workflow: dict[str, Any],
    job_id: int,
    project_id: int | None,
) -> None:
    uri = client.ws_base + f"/ws?clientId={client_id}"
    deadline = asyncio.get_running_loop().time() + 3600
    last_progress = {"value": 4.0}

    async def emit(progress: float, node_label: str) -> None:
        pct = max(3.0, min(97.0, float(progress)))
        last_progress["value"] = pct
        _update_voice_job_fields(job_id, status="RUNNING", progress=pct, current_node=node_label)
        await hub.broadcast({
            "type": "voice_job_progress",
            "job_id": job_id,
            "project_id": project_id,
            "progress": pct,
            "node": node_label,
        })

    async def check_history() -> bool:
        terminal, error = await history_terminal_state(client, prompt_id)
        if not terminal:
            return False
        if error:
            raise RuntimeError(error)
        return True

    ticks = 0
    missing_checks = 0
    last_history_poll = 0.0
    try:
        async with websockets.connect(uri, max_size=32 * 1024 * 1024, open_timeout=20) as ws:
            while True:
                now_loop = asyncio.get_running_loop().time()
                if now_loop >= deadline:
                    raise RuntimeError("Timed out waiting for Drama 3 voice-over")
                if now_loop - last_history_poll >= 1.5:
                    last_history_poll = now_loop
                    if await check_history():
                        return
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=1.2)
                except asyncio.TimeoutError:
                    last_history_poll = asyncio.get_running_loop().time()
                    if await check_history():
                        return
                    ticks += 1
                    if ticks % 5 == 0:
                        presence = await comfy_prompt_presence(client, prompt_id)
                        if presence is False:
                            if await check_history():
                                return
                            missing_checks += 1
                            if missing_checks >= 3:
                                raise RuntimeError("Remote ComfyUI lost the voice-over task (restarted or interrupted)")
                        elif presence is True:
                            missing_checks = 0
                    continue
                if isinstance(raw, bytes):
                    caption = decode_comfy_progress_text(raw)
                    if caption is not None:
                        label = workflow_node_label(workflow, caption[0])
                        text = f"{label} · {caption[1]}" if label else caption[1]
                        _update_voice_job_fields(job_id, current_node=text)
                        await hub.broadcast({
                            "type": "voice_job_progress",
                            "job_id": job_id,
                            "project_id": project_id,
                            "progress": last_progress["value"],
                            "node": text,
                        })
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
                    label = workflow_node_label(workflow, node)
                    step_label = f"{label} ({int(value)}/{int(maximum)})" if maximum > 1 else label
                    await emit(_drama3_stage_progress(workflow, node, value, maximum), step_label)
                elif msg_type == "executing" and (not data.get("prompt_id") or data.get("prompt_id") == prompt_id):
                    node = data.get("node")
                    if node is None:
                        for _ in range(8):
                            if await check_history():
                                return
                            await asyncio.sleep(0.25)
                        continue
                    await emit(_drama3_stage_progress(workflow, node), workflow_node_label(workflow, node))
                elif msg_type in {"execution_error", "execution_interrupted"}:
                    if not data.get("prompt_id") or data.get("prompt_id") == prompt_id:
                        raise RuntimeError(data.get("exception_message") or data.get("message") or msg_type)
                elif msg_type in {"execution_success", "executed"} and (not data.get("prompt_id") or data.get("prompt_id") == prompt_id):
                    for _ in range(8):
                        if await check_history():
                            return
                        await asyncio.sleep(0.25)
    except RuntimeError:
        raise
    except Exception:
        missing_checks = 0
        while asyncio.get_running_loop().time() < deadline:
            if await check_history():
                return
            presence = await comfy_prompt_presence(client, prompt_id)
            if presence is False:
                missing_checks += 1
                if missing_checks >= 3:
                    raise RuntimeError("Remote ComfyUI lost the voice-over task (restarted or interrupted)")
            elif presence is True:
                missing_checks = 0
            await asyncio.sleep(1.5)
        raise RuntimeError("Timed out waiting for Drama 3 voice-over")


async def run_voice_job(job_id: int) -> dict[str, Any]:
    conn = db()
    try:
        job_row = conn.execute("SELECT * FROM voice_jobs WHERE id=?", (job_id,)).fetchone()
        if not job_row:
            raise RuntimeError(f"配音任务 #{job_id} 不存在")
        spk_rows = conn.execute(
            """
            SELECT vjs.speaker_index, vjs.name, vjs.voice_profile_id,
                   vp.ref_audio_path, vp.remote_name, vp.transcript, vp.duration
            FROM voice_job_speakers vjs
            LEFT JOIN voice_profiles vp ON vp.id = vjs.voice_profile_id
            WHERE vjs.job_id=?
            ORDER BY vjs.speaker_index
            """,
            (job_id,),
        ).fetchall()
    finally:
        conn.close()

    project_id = int(job_row["project_id"]) if job_row["project_id"] is not None else None
    script_text = _clean_single_speaker_script(job_row["script_text"] or "")
    if not script_text:
        raise RuntimeError("配音台词为空")

    max_spk_idx = max([int(r["speaker_index"]) for r in spk_rows], default=0)
    voices_list: list[dict[str, Any]] = [
        {
            "index": idx,
            "name": "Role 0 (lead)" if idx == 0 else f"Role {idx}",
            "local_path": "",
            "remote_name": "",
            "transcript": "",
            "duration": 0.0,
        }
        for idx in range(max_spk_idx + 1)
    ]
    active_voice_slots: list[tuple[int, dict[str, Any]]] = []
    for r in spk_rows:
        idx = int(r["speaker_index"])
        if idx < 0 or idx >= 16:
            continue
        local_p = str(r["ref_audio_path"] or "").strip() if r["voice_profile_id"] is not None else ""
        remote_n = str(r["remote_name"] or "").strip() if r["voice_profile_id"] is not None else ""
        transcript = str(r["transcript"] or "") if r["voice_profile_id"] is not None else ""
        duration = float(r["duration"] or 0.0) if r["voice_profile_id"] is not None else 0.0
        slot_dict = {
            "index": idx,
            "name": str(r["name"] or ("Role 0 (lead)" if idx == 0 else f"Role {idx}")),
            "local_path": local_p,
            "remote_name": remote_n,
            "transcript": transcript,
            "duration": duration,
        }
        voices_list[idx] = slot_dict
        if (local_p and Path(local_p).is_file()) or remote_n:
            active_voice_slots.append((idx, slot_dict))

    has_ref_audio = bool(active_voice_slots)
    now_ts = time.time()
    _update_voice_job_fields(
        job_id,
        script_text=script_text,
        status="RUNNING",
        progress=2.0,
        current_node="准备 Drama 3 配音工作流",
        error="",
        prompt_id="",
        started_at=now_ts,
        finished_at=0.0,
        elapsed_seconds=0.0,
    )
    await hub.broadcast({
        "type": "voice_job_state",
        "job_id": job_id,
        "project_id": project_id,
        "status": "RUNNING",
        "error": "",
    })
    await hub.broadcast({
        "type": "voice_job_progress",
        "job_id": job_id,
        "project_id": project_id,
        "progress": 2.0,
        "node": "准备 Drama 3 配音工作流",
    })

    client = ComfyClient(get_settings()["comfy_url"])
    try:
        required_nodes = [
            "Drama3Loader",
            "Drama3CodecLoader",
            "Drama3Generate",
            "Drama3Decode",
            "SaveAudio",
        ]
        if has_ref_audio:
            required_nodes.append("Drama3EncodeVoice")
            needs_asr = any(not bool(str(v.get("transcript") or "").strip()) for _, v in active_voice_slots)
            if needs_asr:
                required_nodes.extend(["Qwen3ASRLoader", "Qwen3ASRTranscribe"])
        await client.require_nodes(required_nodes)

        uploaded_map: dict[int, str] = {}
        for idx, v in active_voice_slots:
            local_p = str(v.get("local_path") or "").strip()
            remote_n = str(v.get("remote_name") or "").strip()
            role_label = str(v.get("name") or f"Role {idx}")
            if local_p and Path(local_p).is_file():
                node_msg = f"上传参考干声到云端 ComfyUI ({role_label})"
                _update_voice_job_fields(job_id, progress=3.0, current_node=node_msg)
                await hub.broadcast({
                    "type": "voice_job_progress",
                    "job_id": job_id,
                    "project_id": project_id,
                    "progress": 3.0,
                    "node": node_msg,
                })
                uploaded_map[idx] = await client.upload_input_file(Path(local_p), subfolder="anime_studio_drama3")
            elif remote_n:
                uploaded_map[idx] = remote_n

        take_uid = time.strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:6]
        filename_prefix = f"anime_studio/drama3/voice_job_{job_id}_{take_uid}"

        item_for_wf = {
            **dict(job_row),
            "script_text": script_text,
            "split_long_text": bool(int(job_row["split_long_text"]) if job_row["split_long_text"] is not None else 1),
            "shorten_long_pauses": bool(int(job_row["shorten_long_pauses"]) if job_row["shorten_long_pauses"] is not None else 1),
            "voices": voices_list,
        }
        workflow = build_drama3_workflow(item_for_wf, uploaded_map, filename_prefix)

        client_id = str(uuid.uuid4())
        async with gpu_slot("audio:drama3"):
            started_at = time.time()
            _update_voice_job_fields(job_id, started_at=started_at)

            async def _run_prompt() -> str:
                pid = await client.submit(workflow, client_id)
                _update_voice_job_fields(
                    job_id,
                    progress=5.0,
                    current_node="Drama 3 任务已提交云端",
                    prompt_id=pid,
                )
                await hub.broadcast({
                    "type": "voice_job_progress",
                    "job_id": job_id,
                    "project_id": project_id,
                    "progress": 5.0,
                    "node": "Drama 3 任务已提交云端",
                })
                await _monitor_voice_job_prompt(client, client_id, pid, workflow, job_id, project_id)
                return pid

            prompt_id = await gpu_oom_retry(_run_prompt, label=f"配音任务 #{job_id}")

        _update_voice_job_fields(job_id, progress=97.0, current_node="读取云端音频输出")
        history = await client.history(prompt_id)
        meta = audio_meta_from_history(history, prompt_id)
        if not meta:
            raise RuntimeError("Drama 3 已完成，但 ComfyUI history 中没有找到音频输出")

        suffix = Path(str(meta.get("filename") or "drama3.flac")).suffix.lower() or ".flac"
        DRAMA3_OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)
        out_file = DRAMA3_OUTPUTS_DIR / f"voice_job_{job_id}_{take_uid}{suffix}"
        temp_target = DRAMA3_OUTPUTS_DIR / f".downloading_job_{job_id}_{take_uid}{suffix}"
        temp_target.unlink(missing_ok=True)

        async def report_download(received: int, total: int) -> None:
            if total > 0:
                ratio = max(0.0, min(1.0, received / total))
                pct = 98.0 + ratio * 1.8
                node = f"下载 44.1kHz 配音到本地 · {received / 1048576:.1f}/{total / 1048576:.1f} MB"
            else:
                pct = 98.0
                node = f"下载 44.1kHz 配音到本地 · {received / 1048576:.1f} MB"
            _update_voice_job_fields(job_id, progress=pct, current_node=node)
            await hub.broadcast({
                "type": "voice_job_progress",
                "job_id": job_id,
                "project_id": project_id,
                "progress": pct,
                "node": node,
            })

        await client.download_view(meta, temp_target, progress_cb=report_download)
        if not temp_target.is_file() or temp_target.stat().st_size <= 0:
            raise RuntimeError("Drama 3 配音文件下载失败")
        temp_target.replace(out_file)

        old_audio = str(job_row["audio_path"] or "").strip()
        if old_audio and old_audio != str(out_file):
            try:
                old_p = Path(old_audio).resolve()
                old_p.relative_to(DRAMA3_OUTPUTS_DIR.resolve())
                if old_p.is_file():
                    old_p.unlink()
            except Exception:
                pass

        finished_at = time.time()
        conn = db()
        try:
            st_row = conn.execute("SELECT started_at FROM voice_jobs WHERE id=?", (job_id,)).fetchone()
        finally:
            conn.close()
        started_ts = float(st_row["started_at"] or now_ts) if st_row else now_ts
        elapsed = max(0.1, round(finished_at - started_ts, 1))
        duration = media_duration_seconds(out_file)

        _update_voice_job_fields(
            job_id,
            audio_path=str(out_file),
            duration=duration,
            elapsed_seconds=elapsed,
            status="DONE",
            progress=100.0,
            current_node="Generation complete · 44.1kHz",
            error="",
            prompt_id="",
            finished_at=finished_at,
        )
        if project_id is not None:
            try:
                # 自动同步只在「项目还没有音频」或「当前音频本来就来自配音台」时进行；
                # YuE2 歌曲 / 手动上传的音频绝不静默覆盖，需用户在配音台点「设为本项目音频」并确认替换。
                cur_mv = ensure_mv_project(project_id)
                cur_audio = str(cur_mv.get("audio_path") or "").strip()
                cur_source = str(cur_mv.get("audio_source") or "")
                if not cur_audio or cur_source == "voice_job":
                    await use_voice_job_as_project_audio(job_id, UseAsProjectAudioIn(confirm_replace=True))
                else:
                    print(f"[voice-job] job #{job_id} 未自动同步：项目已有 {cur_source or 'upload'} 音频")
            except Exception as sync_err:
                print(f"[voice-job] auto sync project audio skipped for job #{job_id}: {sync_err}")
        await hub.broadcast({
            "type": "voice_job_state",
            "job_id": job_id,
            "project_id": project_id,
            "status": "DONE",
            "error": "",
        })
        return get_voice_job(job_id)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        finished_at = time.time()
        conn = db()
        try:
            st_row = conn.execute("SELECT started_at FROM voice_jobs WHERE id=?", (job_id,)).fetchone()
        finally:
            conn.close()
        started_ts = float(st_row["started_at"] or now_ts) if st_row else now_ts
        elapsed = max(0.0, round(finished_at - started_ts, 1))
        _update_voice_job_fields(
            job_id,
            status="FAILED",
            progress=0.0,
            current_node="",
            error=str(exc),
            prompt_id="",
            finished_at=finished_at,
            elapsed_seconds=elapsed,
        )
        await hub.broadcast({
            "type": "voice_job_state",
            "job_id": job_id,
            "project_id": project_id,
            "status": "FAILED",
            "error": str(exc),
        })
        raise


async def recover_voice_jobs() -> None:
    """启动时恢复配音队列：RUNNING/PREPARING 改为 FAILED，QUEUED 按 id 顺序重新入队。"""
    conn = db()
    try:
        running = conn.execute(
            "SELECT id FROM voice_jobs WHERE status IN ('PREPARING', 'RUNNING')"
        ).fetchall()
        queued = conn.execute(
            "SELECT id FROM voice_jobs WHERE status='QUEUED' ORDER BY id"
        ).fetchall()
        for row in running:
            conn.execute(
                """
                UPDATE voice_jobs
                SET status='FAILED',
                    error='应用重启，中断了上次配音任务',
                    progress=0,
                    current_node='',
                    prompt_id='',
                    updated_at=CURRENT_TIMESTAMP
                WHERE id=?
                """,
                (int(row["id"]),),
            )
        conn.commit()
    finally:
        conn.close()

    for row in queued:
        await voice_job_queue.put(int(row["id"]))


async def voice_queue_worker() -> None:
    while True:
        job_id = await voice_job_queue.get()
        try:
            conn = db()
            try:
                row = conn.execute("SELECT id, status FROM voice_jobs WHERE id=?", (job_id,)).fetchone()
            finally:
                conn.close()
            if not row or str(row["status"] or "").upper() != "QUEUED":
                continue

            rt.current_voice_job_id = job_id
            task = asyncio.create_task(run_voice_job(job_id))
            rt.active_voice_task = task
            try:
                await asyncio.wait({task})
            except asyncio.CancelledError:
                task.cancel()
                raise
            finally:
                if rt.active_voice_task is task:
                    rt.active_voice_task = None
                if rt.current_voice_job_id == job_id:
                    rt.current_voice_job_id = None
            if not task.cancelled():
                failure = task.exception()
                if failure:
                    raise failure
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"[voice-queue] skip job {job_id}: {exc}")
        finally:
            voice_job_queue.task_done()
