"""镜头接口：增删改查、生成、媒体与参考图。"""
from __future__ import annotations

import json
import uuid
from fastapi import APIRouter, Body, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from pathlib import Path

from backend.core.db import db
from backend.core.ns import namespace
from backend.core.runtime import hub, job_queue, rt
from backend.core.schemas import ShotIn, ShotPatch, ShotPlanIn
from backend.core.tasks import _remove_from_queue
from backend.models.video import DEFAULT_VIDEO_MODEL, normalize_video_parameters, video_model
from backend.services.continuation import freeze_inputs
from backend.services.images import inherit_mother_refs
from backend.services.repo import ensure_mv_project, ensure_shot_dir, get_scene, get_shot, previous_shot, update_shot_state
from backend.services.shot_planner import plan_shots
from backend.services.video import prepare_shot_regeneration

router = APIRouter()


@router.post("/api/scenes/{scene_id}/second-pass")
async def scene_set_second_pass(scene_id: int, payload: dict):
    enabled = 1 if bool(payload.get("second_pass_enabled")) else 0
    conn = db()
    models = conn.execute("SELECT id,video_model FROM shots WHERE scene_id=?", (scene_id,)).fetchall()
    for row in models:
        value = enabled if video_model(row["video_model"]).get("supports_second_pass", True) else 0
        conn.execute("UPDATE shots SET second_pass_enabled=?, updated_at=CURRENT_TIMESTAMP WHERE id=?", (value, row["id"]))
    conn.commit()
    rows = conn.execute("SELECT * FROM shots WHERE scene_id=? ORDER BY shot_no", (scene_id,)).fetchall()
    conn.close()
    return {"ok": True, "second_pass_enabled": enabled, "shots": [dict(r) for r in rows]}


@router.get("/api/scenes/{scene_id}/shots")
async def shots_list(scene_id: int):
    conn = db()
    rows = conn.execute("SELECT * FROM shots WHERE scene_id=? ORDER BY shot_no", (scene_id,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def create_shot(scene_id: int, payload: ShotIn) -> dict:
    try:
        payload = ShotIn(**normalize_video_parameters(payload.model_dump(exclude_unset=True)))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    mode = payload.mode.upper()
    if mode not in {"CUT", "CONTINUE", "T2V"}:
        raise HTTPException(400, "mode must be CUT or CONTINUE")
    enhance_mode = "none"
    video_model_id = payload.video_model
    conn = db()
    next_no = conn.execute("SELECT COALESCE(MAX(shot_no),0)+1 AS n FROM shots WHERE scene_id=?", (scene_id,)).fetchone()["n"]
    cur = conn.execute(
        """
        INSERT INTO shots(scene_id,shot_no,title,mode,prompt,dialogue,seconds,megapixels,aspect_ratio,steps,seed,video_model,enhance_mode)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (scene_id, int(next_no), payload.title, mode, payload.prompt, payload.dialogue, payload.seconds, payload.megapixels, payload.aspect_ratio, payload.steps, payload.seed, video_model_id, enhance_mode),
    )
    shot_id = cur.lastrowid
    conn.commit()
    conn.close()
    t_start = max(0.0, float(payload.timeline_start or 0.0))
    t_end = max(0.0, float(payload.timeline_end or 0.0))
    t_dur = round(t_end - t_start, 4) if t_end > t_start else max(0.0, float(payload.target_duration or 0.0))
    update_shot_state(
        int(shot_id),
        second_pass_enabled=int(payload.second_pass_enabled),
        second_pass_resolution=str(payload.second_pass_resolution or "1k").lower(),
        realism_lora_enabled=int(payload.realism_lora_enabled),
        naughty_lora_enabled=int(payload.naughty_lora_enabled),
        timeline_start=t_start,
        timeline_end=t_end,
        target_duration=t_dur,
        lyric_text=str(payload.lyric_text or ""),
        conform_mode=str(payload.conform_mode or "auto").lower(),
        lip_sync_enabled=1 if bool(payload.lip_sync_enabled) else 0,
    )
    return inherit_mother_refs(int(shot_id))


@router.post("/api/scenes/{scene_id}/shots")
async def shots_create(scene_id: int, payload: ShotIn):
    return create_shot(scene_id, payload)


@router.patch("/api/shots/{shot_id}")
async def shots_patch(shot_id: int, payload: ShotPatch):
    values = {k: v for k, v in payload.model_dump().items() if v is not None}
    current = get_shot(shot_id)
    try:
        values = normalize_video_parameters(values, current)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if "mode" in values:
        values["mode"] = str(values["mode"]).upper()
        if values["mode"] not in {"CUT", "CONTINUE", "T2V"}:
            raise HTTPException(400, "mode must be CUT or CONTINUE")
    if "conform_mode" in values:
        values["conform_mode"] = str(values["conform_mode"]).lower()
        if values["conform_mode"] not in {"auto", "trim", "speed", "regenerate", "loop"}:
            raise HTTPException(400, "conform_mode must be auto / trim / speed / regenerate / loop")
    if "second_pass_enabled" in values:
        values["second_pass_enabled"] = 1 if bool(values["second_pass_enabled"]) else 0
    if "lip_sync_enabled" in values:
        values["lip_sync_enabled"] = 1 if bool(values["lip_sync_enabled"]) else 0
    if "timeline_start" in values:
        values["timeline_start"] = max(0.0, float(values["timeline_start"]))
    if "timeline_end" in values:
        values["timeline_end"] = max(0.0, float(values["timeline_end"]))
    if "target_duration" in values:
        values["target_duration"] = max(0.0, float(values["target_duration"]))
    timeline_changed = False
    if "timeline_start" in values or "timeline_end" in values:
        start = float(values.get("timeline_start", current.get("timeline_start") or 0))
        end = float(values.get("timeline_end", current.get("timeline_end") or 0))
        if end and end <= start:
            raise HTTPException(400, "歌曲结束时间必须大于开始时间")
        if end > start:
            values["target_duration"] = round(end - start, 4)
        timeline_changed = (
            abs(start - float(current.get("timeline_start") or 0)) > 0.0005
            or abs(end - float(current.get("timeline_end") or 0)) > 0.0005
        )
    conform_changed = (
        "conform_mode" in values
        and str(values["conform_mode"]) != str(current.get("conform_mode") or "auto")
    )
    if timeline_changed or conform_changed:
        values["lip_sync_status"] = "IDLE"
        values["lip_sync_progress"] = 0
        values["lip_sync_audio_path"] = ""
        values["lip_sync_output_path"] = ""
        values["lip_sync_last_frame_path"] = ""
        values["lip_sync_error"] = ""
    if "mode" in values and values["mode"] != current["mode"] and values["mode"] in {"CONTINUE", "T2V"}:
        for ref_col in ("ref1_path", "ref2_path", "ref3_path", "ref4_path"):
            values.setdefault(ref_col, "")
    update_shot_state(shot_id, **values)
    return inherit_mother_refs(shot_id) if "mode" in values and values["mode"] != current["mode"] else get_shot(shot_id)


@router.get("/api/shots/{shot_id}")
async def shots_get(shot_id: int):
    return get_shot(shot_id)


@router.post("/api/shots/{shot_id}/upload")
async def shots_upload(shot_id: int, slot: str = Form(...), file: UploadFile = File(...)):
    allowed = {"ref1": "ref1_path", "ref2": "ref2_path", "ref3": "ref3_path", "ref4": "ref4_path", "first_frame": "first_frame_path"}
    if slot not in allowed:
        raise HTTPException(400, "slot must be ref1/ref2/ref3/ref4/first_frame")
    shot = get_shot(shot_id)
    if str(shot.get("mode") or "").upper() == "CONTINUE" and slot != "first_frame":
        raise HTTPException(400, "CONTINUE 模式仅使用上一镜接续首帧，不使用 Ref1~Ref4")
    shot_dir = ensure_shot_dir(shot) / "refs"
    shot_dir.mkdir(parents=True, exist_ok=True)
    ext = Path(file.filename or "image.png").suffix or ".png"
    dest = shot_dir / f"{slot}_{uuid.uuid4().hex}{ext}"
    with dest.open("wb") as f:
        while chunk := await file.read(1024 * 1024):
            f.write(chunk)
    update_shot_state(shot_id, **({allowed[slot]: str(dest), "continuation_selection": ""} if slot == "first_frame" else {allowed[slot]: str(dest)}))
    return {"ok": True, "slot": slot, "path": str(dest)}


@router.post("/api/shots/{shot_id}/generate")
async def shots_generate(shot_id: int):
    """列表项上的「生成 / 重新生成」。

    已经处于 QUEUED / PREPARING / RUNNING 时不再报 409：这正是「重新生成」的入口，
    先取消旧的本地等待协程、清掉旧排队记录和旧状态、必要时终止仍然存在的远端旧任务，
    然后重新提交同一个 Shot。旧视频在新视频成功前保留。
    """
    shot = get_shot(shot_id)
    frozen_path = freeze_inputs(shot, namespace())
    if str(shot.get("status", "")).upper() in {"QUEUED", "PREPARING", "RUNNING"}:
        await prepare_shot_regeneration(shot_id)
    update_shot_state(shot_id, status="QUEUED", progress=0, error="", current_node="Waiting in queue", prompt_id="", queued_input_path=frozen_path)
    await job_queue.put(shot_id)
    await hub.broadcast({"type": "shot_state", "shot_id": shot_id, "status": "QUEUED"})
    return {"queued": True, "shot_id": shot_id, "queue_size": job_queue.qsize()}


@router.post("/api/scenes/{scene_id}/generate")
async def scene_generate(scene_id: int, payload: dict | None = None):
    conn = db()
    rows = conn.execute("SELECT id,status,second_pass_enabled FROM shots WHERE scene_id=? ORDER BY shot_no", (scene_id,)).fetchall()
    conn.close()

    include_done = bool((payload or {}).get("include_done")) or any(int(r["second_pass_enabled"] or 0) == 1 for r in rows)
    skip_statuses = {"QUEUED", "PREPARING", "RUNNING"} if include_done else {"DONE", "QUEUED", "PREPARING", "RUNNING"}
    ids = [int(r["id"]) for r in rows if str(r["status"]).upper() not in skip_statuses]
    skipped = [int(r["id"]) for r in rows if str(r["status"]).upper() in skip_statuses]
    completed = [] if include_done else [int(r["id"]) for r in rows if str(r["status"]).upper() == "DONE"]
    ids_set = set(ids)
    snapshots = {}
    for shot_id in ids:
        shot = get_shot(shot_id)
        if shot["mode"].upper() == "CONTINUE" and not bool(int(shot.get("second_pass_enabled") or 0)):
            prev = previous_shot(shot)
            if prev and int(prev["id"]) in ids_set and shot.get("continuation_selection"):
                update_shot_state(shot_id, first_frame_path="", continuation_selection="")
                shot = get_shot(shot_id)
        snapshots[shot_id] = freeze_inputs(shot, namespace())
    for shot_id in ids:
        update_shot_state(shot_id, status="QUEUED", progress=0, error="", queued_input_path=snapshots[shot_id])
        await job_queue.put(shot_id)
    await hub.broadcast({
        "type": "scene_queued",
        "scene_id": scene_id,
        "count": len(ids),
        "skipped": len(skipped),
        "completed_skipped": len(completed),
    })
    return {
        "queued": len(ids),
        "shot_ids": ids,
        "skipped": len(skipped),
        "skipped_shot_ids": skipped,
        "completed_skipped": len(completed),
        "completed_shot_ids": completed,
    }


@router.post("/api/projects/{project_id}/generate")
async def project_generate(project_id: int):
    conn = db()
    project = conn.execute("SELECT id,name FROM projects WHERE id=?", (project_id,)).fetchone()
    if not project:
        conn.close()
        raise HTTPException(404, "Project not found")
    rows = conn.execute(
        """
        SELECT sh.id, sh.status, sh.scene_id, sc.sort_order, sh.shot_no
        FROM shots sh
        JOIN scenes sc ON sc.id=sh.scene_id
        WHERE sc.project_id=?
        ORDER BY sc.sort_order, sc.id, sh.shot_no
        """,
        (project_id,),
    ).fetchall()
    conn.close()

    # 全剧出片与单 Scene 批量规则一致：DONE 与已经在队列/运行中的镜头不重复提交。
    skip_statuses = {"DONE", "QUEUED", "PREPARING", "RUNNING"}
    ids = [int(r["id"]) for r in rows if str(r["status"]).upper() not in skip_statuses]
    skipped = [int(r["id"]) for r in rows if str(r["status"]).upper() in skip_statuses]
    completed = [int(r["id"]) for r in rows if str(r["status"]).upper() == "DONE"]
    touched_scene_ids = sorted({int(r["scene_id"]) for r in rows if int(r["id"]) in ids})

    snapshots = {shot_id: freeze_inputs(get_shot(shot_id), namespace()) for shot_id in ids}
    for shot_id in ids:
        update_shot_state(shot_id, status="QUEUED", progress=0, error="", queued_input_path=snapshots[shot_id])
        await job_queue.put(shot_id)

    await hub.broadcast({
        "type": "project_queued",
        "project_id": project_id,
        "count": len(ids),
        "skipped": len(skipped),
        "completed_skipped": len(completed),
        "scene_ids": touched_scene_ids,
    })
    return {
        "queued": len(ids),
        "shot_ids": ids,
        "skipped": len(skipped),
        "skipped_shot_ids": skipped,
        "completed_skipped": len(completed),
        "completed_shot_ids": completed,
        "scene_ids": touched_scene_ids,
        "queue_size": job_queue.qsize(),
    }


@router.get("/api/shots/{shot_id}/media")
async def shot_media(shot_id: int):
    shot = get_shot(shot_id)
    p = Path(shot.get("output_path") or "")
    if not p.exists():
        raise HTTPException(404, "当前 Shot 还没有本地输出视频")
    return FileResponse(p)


@router.get("/api/shots/{shot_id}/ref/{slot}")
async def shot_ref_image(shot_id: int, slot: str):
    allowed = {
        "ref1": "ref1_path",
        "ref2": "ref2_path",
        "ref3": "ref3_path",
        "ref4": "ref4_path",
        "first_frame": "first_frame_path",
        "last_frame": "last_frame_path",
    }
    if slot not in allowed:
        raise HTTPException(400, "slot 必须是 ref1/ref2/ref3/ref4/first_frame/last_frame")
    shot = get_shot(shot_id)
    if slot == "last_frame" and shot.get("face_refined_last_frame_path"):
        p = Path(str(shot.get("face_refined_last_frame_path") or ""))
    else:
        p = Path(shot.get(allowed[slot]) or "")
    if not p.exists():
        raise HTTPException(404, f"{slot} 图片不存在")
    return FileResponse(p)


@router.delete("/api/shots/{shot_id}/ref/{slot}")
async def shot_delete_ref(shot_id: int, slot: str):
    allowed = {
        "ref1": "ref1_path",
        "ref2": "ref2_path",
        "ref3": "ref3_path",
        "ref4": "ref4_path",
        "first_frame": "first_frame_path",
    }
    if slot not in allowed:
        raise HTTPException(400, "slot must be ref1/ref2/ref3/ref4/first_frame")
    get_shot(shot_id)
    update_shot_state(shot_id, **({allowed[slot]: "", "continuation_selection": ""} if slot == "first_frame" else {allowed[slot]: ""}))
    return {"ok": True, "slot": slot}


@router.post("/api/shots/{shot_id}/clone")
async def shot_clone(shot_id: int):
    shot = get_shot(shot_id)
    conn = db()
    next_no = conn.execute(
        "SELECT COALESCE(MAX(shot_no),0)+1 AS n FROM shots WHERE scene_id=?",
        (shot["scene_id"],),
    ).fetchone()["n"]
    cur = conn.execute(
        """
        INSERT INTO shots(scene_id,shot_no,title,mode,prompt,dialogue,seconds,megapixels,aspect_ratio,steps,seed,video_model,enhance_mode,ref1_path,ref2_path,ref3_path,ref4_path)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            shot["scene_id"],
            int(next_no),
            (shot["title"] or "镜头") + " (副本)",
            shot["mode"],
            shot["prompt"],
            shot["dialogue"],
            shot["seconds"],
            shot["megapixels"],
            shot["aspect_ratio"],
            shot["steps"],
            shot["seed"],
            shot.get("video_model") or "b15_49",
            "none",
            shot["ref1_path"],
            shot["ref2_path"],
            shot["ref3_path"],
            shot["ref4_path"],
        ),
    )
    new_id = cur.lastrowid
    conn.commit()
    row = conn.execute("SELECT * FROM shots WHERE id=?", (new_id,)).fetchone()
    conn.close()
    return dict(row)


async def _delete_shot_internal(shot_id: int, *, broadcast: bool = True) -> dict:
    shot = get_shot(shot_id)
    if (
        shot_id == rt.current_shot_id
        or str(shot.get("status") or "").upper() in {"PREPARING", "RUNNING"}
    ):
        raise HTTPException(409, "当前 Shot 正在生成，暂不能删除；排队或等待中的 Shot 可以删除")
    conn = db()
    conn.execute("DELETE FROM shots WHERE id=?", (shot_id,))
    conn.commit()
    conn.close()
    removed = await _remove_from_queue(job_queue, shot_id)
    if broadcast:
        await hub.broadcast({"type": "shot_deleted", "shot_id": shot_id, "scene_id": shot["scene_id"]})
    return {"ok": True, "id": shot_id, "removed_from_queue": removed}


@router.delete("/api/shots/{shot_id}")
async def shot_delete(shot_id: int):
    return await _delete_shot_internal(shot_id, broadcast=True)


@router.post("/api/scenes/{scene_id}/shots/from-timeline")
async def shots_from_timeline(
    scene_id: int,
    payload: ShotPlanIn = Body(default_factory=ShotPlanIn),
):
    scene = get_scene(scene_id)
    project_id = int(scene["project_id"])
    mv = ensure_mv_project(project_id)

    timeline_path = Path(str(mv.get("lyrics_timeline_path") or ""))
    if str(mv.get("analysis_status") or "").upper() != "DONE" or not timeline_path.is_file():
        raise HTTPException(400, "请先分析音频")

    try:
        timeline = json.loads(timeline_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise HTTPException(400, f"读取音频时间轴失败: {exc}") from exc

    lines = timeline.get("lines") or []
    if not lines:
        raise HTTPException(400, "请先分析音频（时间轴没有歌词行）")

    bad_lines = [
        {
            "index": int(ln.get("index") or (idx + 1)),
            "text": str(ln.get("text") or ""),
            "aligned_ratio": float(ln.get("aligned_ratio", 0.0)),
        }
        for idx, ln in enumerate(lines)
        if float(ln.get("aligned_ratio", 1.0)) < 1.0
    ]
    if bad_lines:
        desc = "；".join(f"第 {b['index']} 行 ({b['aligned_ratio']*100:.0f}%): {b['text']}" for b in bad_lines)
        return JSONResponse(
            status_code=400,
            content={
                "detail": f"部分台词未完全对齐（要求 aligned_ratio == 1.0）：{desc}",
                "bad_lines": bad_lines,
            },
        )

    conn = db()
    existing_shots = [
        dict(r)
        for r in conn.execute("SELECT * FROM shots WHERE scene_id=? ORDER BY shot_no", (scene_id,)).fetchall()
    ]
    conn.close()

    if not payload.dry_run:
        for sh in existing_shots:
            if int(sh["id"]) == rt.current_shot_id or str(sh.get("status") or "").upper() in {"QUEUED", "PREPARING", "RUNNING"}:
                raise HTTPException(409, "当前 Scene 中有镜头处于排队或生成中，请先等待完成或取消")
        if existing_shots and not payload.replace:
            return JSONResponse(
                status_code=409,
                content={
                    "code": "SHOTS_EXIST",
                    "count": len(existing_shots),
                    "detail": f"当前 Scene 已有 {len(existing_shots)} 个镜头",
                },
            )

    words = [
        w
        for seg in (timeline.get("segments") or [])
        if isinstance(seg, dict)
        for w in (seg.get("words") or [])
    ]
    vocals_path = str(mv.get("vocals_path") or "")
    audio_duration = float(timeline.get("duration") or mv.get("audio_duration") or 0.0)

    try:
        planned = plan_shots(lines, words, vocals_path, audio_duration, payload)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc

    if not payload.dry_run and payload.initial_shots and len(payload.initial_shots) != len(planned["shots"]):
        # 提示词按镜号一一对应：分镜数量变了（如重新分析过音频）就必须重新 dry_run，防止提示词错位
        return JSONResponse(
            status_code=409,
            content={
                "code": "PLAN_CHANGED",
                "planned_count": len(planned["shots"]),
                "initial_shots_count": len(payload.initial_shots),
                "detail": f"当前分镜为 {len(planned['shots'])} 镜，但传入了 {len(payload.initial_shots)} 镜的提示词，请重新 dry_run 后再落库",
            },
        )

    if not payload.dry_run:
        default_vm = video_model(DEFAULT_VIDEO_MODEL)
        template = existing_shots[0] if existing_shots else {}
        inherit_video_model = str(template.get("video_model") or DEFAULT_VIDEO_MODEL)
        inherit_aspect_ratio = str(template.get("aspect_ratio") or default_vm["default_aspect_ratio"])
        # 重建分镜时沿用原 Scene 的生成设置（步数、像素、二次采样、LoRA 开关、种子），避免静默丢失
        inherit_extra: dict = {}
        if template:
            for key in ("steps", "megapixels", "seed"):
                if template.get(key) is not None:
                    inherit_extra[key] = template[key]
            for key in ("second_pass_enabled", "realism_lora_enabled", "naughty_lora_enabled"):
                inherit_extra[key] = bool(int(template.get(key) or 0))

        if payload.replace and existing_shots:
            for sh in existing_shots:
                await _delete_shot_internal(int(sh["id"]), broadcast=False)

        created_shots: list[dict] = []
        overrides = payload.initial_shots or []
        for idx_ps, ps in enumerate(planned["shots"]):
            ov = overrides[idx_ps] if idx_ps < len(overrides) and isinstance(overrides[idx_ps], dict) else {}
            created = create_shot(
                scene_id,
                ShotIn(
                    title=str(ov.get("title") or ps["title"]),
                    mode=str(ov.get("mode") or ps["mode"]),
                    prompt=str(ov.get("prompt") or ""),
                    dialogue=str(ov.get("dialogue") or ps["dialogue"]),
                    lyric_text=str(ov.get("lyric_text") or ps["dialogue"]),
                    timeline_start=ps["timeline_start"],
                    timeline_end=ps["timeline_end"],
                    target_duration=ps["target_duration"],
                    seconds=ps["seconds"],
                    video_model=str(ov.get("video_model") or inherit_video_model),
                    aspect_ratio=str(ov.get("aspect_ratio") or inherit_aspect_ratio),
                    **inherit_extra,
                ),
            )
            created_shots.append({**created, **ps, "id": created["id"], "ref1_path": created.get("ref1_path", "")})

        await hub.broadcast({"type": "scene_shots_replaced", "scene_id": scene_id})
        planned["shots"] = created_shots

    return {
        "dry_run": bool(payload.dry_run),
        "audio_duration": planned["audio_duration"],
        "shots": planned["shots"],
        "warnings": planned["warnings"],
    }


@router.get("/api/shots/{shot_id}/continuity")
async def shot_continuity(shot_id: int):
    shot = get_shot(shot_id)
    prev = previous_shot(shot)
    if not prev:
        return {"has_previous": False}
    has_last_frame = bool(prev.get("last_frame_path") and Path(prev["last_frame_path"]).is_file())
    has_video = bool(prev.get("output_path") and Path(prev["output_path"]).is_file())
    return {
        "has_previous": True,
        "previous_shot_id": prev["id"],
        "previous_shot_no": prev["shot_no"],
        "has_last_frame": has_last_frame,
        "has_video": has_video,
    }


@router.get("/api/shots/{shot_id}/log")
async def shot_terminal_log(shot_id: int):
    import json
    import re
    from backend.core.pika import PikaClient
    from backend.services.repo import get_settings

    shot = get_shot(shot_id)
    settings = get_settings()
    pika_url = str(settings.get("pika_url") or "").strip().rstrip("/")
    job_id = str(shot.get("prompt_id") or "").strip()
    endpoint = pika_url

    # Also check execution.json in queued/completed input folders or latest inputs folder
    shot_dir = ensure_shot_dir(shot)
    exec_records = []
    for key in ("queued_input_path", "completed_input_path"):
        raw_p = str(shot.get(key) or "").strip()
        if raw_p:
            ep = Path(raw_p).parent / "execution.json"
            if ep.is_file():
                exec_records.append(ep)
    inputs_root = shot_dir / "inputs"
    if inputs_root.is_dir():
        exec_records.extend(sorted(inputs_root.glob("*/execution.json"), key=lambda p: p.stat().st_mtime, reverse=True))
    for ep in exec_records:
        try:
            rec = json.loads(ep.read_text(encoding="utf-8"))
            if rec.get("backend") == "pika" and rec.get("job_id"):
                if not re.fullmatch(r"[0-9a-f]{32}", job_id):
                    job_id = str(rec["job_id"])
                if rec.get("endpoint"):
                    endpoint = str(rec["endpoint"]).strip().rstrip("/")
                break
        except Exception:
            pass

    is_pika_model = str(shot.get("video_model") or "") == "pika_h3"
    if (re.fullmatch(r"[0-9a-f]{32}", job_id) or is_pika_model) and endpoint:
        client = PikaClient(endpoint)
        target_job_id = job_id if re.fullmatch(r"[0-9a-f]{32}", job_id) else ""
        if not target_job_id:
            try:
                health = await client.health()
                active_id = str(health.get("active") or "")
                if re.fullmatch(r"[0-9a-f]{32}", active_id):
                    target_job_id = active_id
            except Exception:
                pass
        if target_job_id:
            try:
                raw_log = await client.log(target_job_id)
                header = (
                    f"=== Shot #{int(shot['shot_no']):03d} · Pika CLI 实时终端日志 ===\n"
                    f"任务 ID: {target_job_id} | 状态: {shot['status']} ({round(float(shot.get('progress') or 0))}%) | 阶段: {shot.get('current_node') or '运行中'}\n"
                    f"{'-' * 72}\n"
                )
                return {
                    "ok": True,
                    "shot_id": shot["id"],
                    "shot_no": shot["shot_no"],
                    "backend": "pika",
                    "job_id": target_job_id,
                    "log": header + (raw_log or "等待 Colab Pika CLI 输出日志…"),
                }
            except Exception as exc:
                return {
                    "ok": False,
                    "shot_id": shot["id"],
                    "shot_no": shot["shot_no"],
                    "backend": "pika",
                    "job_id": target_job_id,
                    "log": f"读取远端 Pika 日志失败：{exc}\n当前本地记录状态：{shot['status']} · {shot.get('current_node') or ''}",
                }

    # Fallback / ComfyUI diagnostic terminal log
    lines = [
        f"=== Shot #{int(shot['shot_no']):03d} · 任务运行状态日志 ===",
        f"模型: {shot.get('video_model')} | 模式: {shot.get('mode')} | 二采: {'开启 (' + str(shot.get('second_pass_resolution') or '1k').upper() + ')' if int(shot.get('second_pass_enabled') or 0) else '仅一采'}",
        f"状态: {shot.get('status')} | 进度: {round(float(shot.get('progress') or 0), 1)}% | 耗时: {round(float(shot.get('generation_elapsed_seconds') or 0), 1)}s",
        f"当前节点: {shot.get('current_node') or '等待中'}",
        f"任务 ID (prompt_id): {job_id or '尚未分配'}",
    ]
    if shot.get("error"):
        lines.extend(["-" * 72, "【报错信息】", str(shot["error"])])
    elif is_pika_model and not endpoint:
        lines.extend(["-" * 72, "提示：请先在右上角「设置」中填写 Pika 服务地址。"])
    elif is_pika_model:
        lines.extend(["-" * 72, "当前镜头尚未提交到 Pika CLI 队列，点击「生成 / 重新生成」后此处将实时滚动显示 Colab 终端输出。"])
    return {
        "ok": True,
        "shot_id": shot["id"],
        "shot_no": shot["shot_no"],
        "backend": "pika" if is_pika_model else "comfyui",
        "job_id": job_id,
        "log": "\n".join(lines),
    }
