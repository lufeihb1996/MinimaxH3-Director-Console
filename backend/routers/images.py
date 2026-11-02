"""生图工作台接口。"""
from __future__ import annotations

import uuid
from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pathlib import Path
from typing import Any

from backend.core.db import db
from backend.core.runtime import hub, rt, scene_image_queue
from backend.core.schemas import SceneImageApplyIn, SceneImageConfigIn, SceneImageQueueIn
from backend.models.registry import get_model
from backend.services.images import REF_ROLE_COLUMNS, apply_scene_image_to_matching_cut_shots, enqueue_scene_image_job, ensure_scene_image_configs, ensure_scene_image_dir, ensure_scene_image_slot, get_scene_image_configs, get_scene_image_job, prepare_image_job_regeneration, ref_owner_scene_id, scene_image_dashboard, scene_image_from_row, update_scene_image_job
from backend.services.repo import get_scene

router = APIRouter()


@router.get("/api/scenes/{scene_id}/images")
async def scene_images_list(scene_id: int):
    get_scene(scene_id)
    ensure_scene_image_configs(scene_id)
    conn = db()
    items = []
    for role in REF_ROLE_COLUMNS:
        owner = ref_owner_scene_id(scene_id, role)
        rows = conn.execute("SELECT * FROM scene_images WHERE scene_id=? AND role=? ORDER BY id DESC", (owner, role)).fetchall()
        items.extend(scene_image_from_row(row) for row in rows)
    conn.close()
    return sorted(items, key=lambda item: item["id"], reverse=True)


@router.get("/api/projects/{project_id}/image-dashboard")
async def project_image_dashboard(project_id: int):
    return scene_image_dashboard(project_id)


@router.get("/api/scenes/{scene_id}/image-configs")
async def scene_image_configs_get(scene_id: int):
    return get_scene_image_configs(scene_id)


@router.put("/api/scenes/{scene_id}/image-configs/{role}")
async def scene_image_config_put(scene_id: int, role: str, payload: SceneImageConfigIn):
    get_scene(scene_id)
    role = role.lower().strip()
    if role not in REF_ROLE_COLUMNS:
        raise HTTPException(400, "role must be ref1/ref2/ref3/ref4")
    if payload.ref_scope not in {"scene", "project"} or (payload.ref_scope == "project" and role not in {"ref1", "ref2"}):
        raise HTTPException(400, "仅人物1/2可选择项目共享，场景A/B独立")
    image_model = payload.image_model.lower().strip()
    try:
        model = get_model("image", image_model)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    aspect_ratio = payload.aspect_ratio.strip()
    if aspect_ratio not in (model.get("aspect_ratios") or {}):
        raise HTTPException(400, f"{model['label']} 图片比例只能是 " + " / ".join(model.get("aspect_ratios") or {}))
    if payload.lora_mode not in (model.get("lora_modes") or ["none"]):
        raise HTTPException(400, f"{model['label']} 不支持该 LoRA 设置")
    # Joining a shared slot reuses its existing definition rather than replacing it with an empty form.
    if payload.ref_scope == "project":
        conn = db()
        old = conn.execute("SELECT ref_scope FROM scene_image_configs WHERE scene_id=? AND role=?", (scene_id,role)).fetchone()
        owner = conn.execute("""SELECT cfg.* FROM scene_image_configs cfg JOIN scenes sc ON sc.id=cfg.scene_id
            WHERE sc.project_id=? AND cfg.role=? AND cfg.ref_scope='project' AND cfg.scene_id!=?
            ORDER BY sc.sort_order,sc.id LIMIT 1""", (get_scene(scene_id)["project_id"],role,scene_id)).fetchone()
        conn.close()
        if owner and (not old or old["ref_scope"] != "project"):
            payload = SceneImageConfigIn(**{**dict(owner), "ref_scope":"project"})
            aspect_ratio, image_model = payload.aspect_ratio, payload.image_model
    steps_val = max(0, int(payload.steps or 0))
    cfg_val = max(0.0, round(float(payload.cfg or 0.0), 2))
    if steps_val and not (1 <= steps_val <= 150):
        raise HTTPException(400, "生图步数范围需在 1 ~ 150 之间")
    if cfg_val and not (0.1 <= cfg_val <= 30.0):
        raise HTTPException(400, "生图 CFG 范围需在 0.1 ~ 30.0 之间")
    conn = db()
    conn.execute(
        """
        INSERT INTO scene_image_configs(scene_id,role,role_name,prompt,aspect_ratio,seed,steps,cfg,image_model,enabled,ref_scope,lora_mode,updated_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)
        ON CONFLICT(scene_id,role) DO UPDATE SET
          role_name=excluded.role_name,
          prompt=excluded.prompt,
          aspect_ratio=excluded.aspect_ratio,
          seed=excluded.seed,
          steps=excluded.steps,
          cfg=excluded.cfg,
          image_model=excluded.image_model,
          enabled=excluded.enabled,
          ref_scope=excluded.ref_scope,
          lora_mode=excluded.lora_mode,
          updated_at=CURRENT_TIMESTAMP
        """,
        (
            scene_id,
            role,
            payload.role_name.strip(),
            payload.prompt.strip(),
            aspect_ratio,
            int(payload.seed),
            steps_val,
            cfg_val,
            image_model,
            1 if payload.enabled else 0,
            payload.ref_scope,
            payload.lora_mode,
        ),
    )
    if payload.ref_scope == "project":
        conn.execute("""UPDATE scene_image_configs SET role_name=?,prompt=?,aspect_ratio=?,seed=?,steps=?,cfg=?,image_model=?,enabled=?,lora_mode=?,updated_at=CURRENT_TIMESTAMP
            WHERE role=? AND ref_scope='project' AND scene_id IN (SELECT id FROM scenes WHERE project_id=?)""",
            (payload.role_name.strip(),payload.prompt.strip(),aspect_ratio,int(payload.seed),steps_val,cfg_val,image_model,int(payload.enabled),payload.lora_mode,role,get_scene(scene_id)["project_id"]))
    conn.commit()
    row = conn.execute(
        "SELECT * FROM scene_image_configs WHERE scene_id=? AND role=?", (scene_id, role)
    ).fetchone()
    conn.close()
    return dict(row)


@router.post("/api/scenes/{scene_id}/images/queue")
async def scene_images_queue(scene_id: int, payload: SceneImageQueueIn):
    get_scene(scene_id)
    roles = [r.lower().strip() for r in (payload.roles or list(REF_ROLE_COLUMNS.keys()))]
    invalid = [r for r in roles if r not in REF_ROLE_COLUMNS]
    if invalid:
        raise HTTPException(400, "无效参考位: " + ", ".join(invalid))
    queued: list[int] = []
    skipped: list[str] = []
    skipped_reasons: dict[str, str] = {}
    existing_job_ids: list[int] = []
    for role in roles:
        job_id, reason = await enqueue_scene_image_job(
            scene_id, role,
            replace_existing=payload.replace_existing,
            only_missing=payload.only_missing,
        )
        if reason == "already_active" and not payload.only_missing and job_id is not None and get_scene_image_job(job_id)["scene_id"] == scene_id:
            # 单个参考位的「重新生成」：先清掉这一项旧的本地协程 / 排队记录 / 状态 /
            # 远端旧任务，再把同一个槽位重新排队。批量 only_missing=True 不受影响。
            await prepare_image_job_regeneration(job_id)
            job_id, reason = await enqueue_scene_image_job(
                scene_id, role,
                replace_existing=payload.replace_existing,
                only_missing=payload.only_missing,
            )
        if reason == "queued" and job_id is not None:
            queued.append(job_id)
        else:
            skipped.append(role)
            skipped_reasons[role] = reason
            if job_id is not None:
                existing_job_ids.append(job_id)
    return {
        "queued": len(queued),
        "job_ids": queued,
        "skipped_roles": skipped,
        "skipped_reasons": skipped_reasons,
        "existing_job_ids": existing_job_ids,
    }


@router.post("/api/projects/{project_id}/images/queue")
async def project_images_queue(project_id: int, payload: SceneImageQueueIn):
    conn = db()
    scenes = conn.execute(
        "SELECT id FROM scenes WHERE project_id=? ORDER BY sort_order,id", (project_id,)
    ).fetchall()
    conn.close()
    roles = [r.lower().strip() for r in (payload.roles or list(REF_ROLE_COLUMNS.keys()))]
    queued: list[int] = []
    skipped = 0
    skip_counts: dict[str, int] = {}
    for scene in scenes:
        for role in roles:
            job_id, reason = await enqueue_scene_image_job(
                int(scene["id"]), role,
                replace_existing=False,
                # “全剧生图”固定只补缺失：即使旧前端/缓存误传 only_missing=false，
                # 也绝不重新生成已有历史图的 Ref。
                only_missing=True,
            )
            if reason == "queued" and job_id is not None:
                queued.append(job_id)
            else:
                skipped += 1
                skip_counts[reason] = skip_counts.get(reason, 0) + 1
    return {"queued": len(queued), "job_ids": queued, "skipped": skipped, "skip_counts": skip_counts}


@router.post("/api/image-queue/slots/{scene_id}/{role}")
async def image_queue_slot_create(scene_id: int, role: str):
    try:
        job_id = ensure_scene_image_slot(scene_id, role)
    except RuntimeError as exc:
        raise HTTPException(400, str(exc)) from exc
    job = get_scene_image_job(job_id)
    return {"ok": True, "job": job}


@router.get("/api/image-queue/status")
async def image_queue_status(project_id: int | None = None):
    conn = db()
    params: list[Any] = []
    where = ""
    if project_id is not None:
        where = "WHERE sc.project_id=?"
        params.append(project_id)
    rows = conn.execute(
        f"""
        WITH latest_slot_job AS (
            SELECT scene_id, role, MAX(id) AS id
            FROM scene_image_jobs
            GROUP BY scene_id, role
        )
        SELECT j.*, sc.name AS scene_name, sc.project_id
        FROM latest_slot_job latest
        JOIN scene_image_jobs j ON j.id=latest.id
        JOIN scenes sc ON sc.id=j.scene_id
        {where}
        ORDER BY
            sc.sort_order ASC,
            sc.id ASC,
            CASE j.role
                WHEN 'ref1' THEN 1
                WHEN 'ref2' THEN 2
                WHEN 'ref3' THEN 3
                WHEN 'ref4' THEN 4
                ELSE 99
            END ASC
        LIMIT 100
        """,
        params,
    ).fetchall()
    if project_id is None:
        queue_size = scene_image_queue.qsize()
        current_id = rt.current_scene_image_job_id
    else:
        queue_size = conn.execute("""SELECT COUNT(*) FROM scene_image_jobs j JOIN scenes sc ON sc.id=j.scene_id
            WHERE sc.project_id=? AND j.status='QUEUED'""", (project_id,)).fetchone()[0]
        active = conn.execute("SELECT sc.project_id FROM scene_image_jobs j JOIN scenes sc ON sc.id=j.scene_id WHERE j.id=?", (rt.current_scene_image_job_id,)).fetchone()
        current_id = rt.current_scene_image_job_id if active and active["project_id"] == project_id else None
    conn.close()
    return {
        "queue_size": queue_size,
        "current_job_id": current_id,
        "jobs": [dict(r) for r in rows],
    }


@router.delete("/api/image-queue/slots/{scene_id}/{role}")
async def image_queue_slot_delete(scene_id: int, role: str):
    get_scene(scene_id)
    role = role.lower().strip()
    if role not in REF_ROLE_COLUMNS:
        raise HTTPException(400, "role must be ref1/ref2/ref3/ref4")
    conn = db()
    active = conn.execute(
        """
        SELECT id, status FROM scene_image_jobs
        WHERE scene_id=? AND role=? AND status IN ('QUEUED','PREPARING','RUNNING')
        ORDER BY id DESC LIMIT 1
        """,
        (scene_id, role),
    ).fetchone()
    if active:
        conn.close()
        raise HTTPException(409, "当前参考位正在排队或生成，不能删除；请先等待完成或中断当前任务")
    cur = conn.execute(
        "DELETE FROM scene_image_jobs WHERE scene_id=? AND role=?",
        (scene_id, role),
    )
    deleted = int(cur.rowcount or 0)
    conn.commit()
    conn.close()
    return {"ok": True, "deleted": deleted, "scene_id": scene_id, "role": role}


@router.post("/api/scenes/{scene_id}/images/upload")
async def scene_image_upload(scene_id: int, role: str = Form(...), file: UploadFile = File(...)):
    role = role.lower().strip()
    scene_id = ref_owner_scene_id(scene_id, role)
    scene = get_scene(scene_id)
    role = role.lower().strip()
    if role not in REF_ROLE_COLUMNS:
        raise HTTPException(400, "role must be ref1/ref2/ref3/ref4")
    image_dir = ensure_scene_image_dir(scene)
    suffix = Path(file.filename or "upload.png").suffix.lower() or ".png"
    out_path = image_dir / f"manual_{role}_{uuid.uuid4().hex[:10]}{suffix}"
    content = await file.read()
    out_path.write_bytes(content)

    conn = db()
    cur = conn.execute(
        """
        INSERT INTO scene_images(scene_id,role,prompt,aspect_ratio,width,height,steps,seed,prompt_id,output_path,applied_count)
        VALUES(?,?,?,?,?,?,?,?,?,?,0)
        """,
        (scene_id, role, "Manual reference upload", "custom", 1024, 1024, 0, 42, "", str(out_path)),
    )
    image_id = int(cur.lastrowid)
    config = conn.execute(
        "SELECT prompt FROM scene_image_configs WHERE scene_id=? AND role=?",
        (scene_id, role),
    ).fetchone()
    conn.commit()
    active = conn.execute(
        """
        SELECT id FROM scene_image_jobs
        WHERE scene_id=? AND role=? AND status IN ('QUEUED','PREPARING','RUNNING')
        ORDER BY id DESC LIMIT 1
        """,
        (scene_id, role),
    ).fetchone()
    conn.close()

    applied, applied_scene_ids = apply_scene_image_to_matching_cut_shots(
        scene_id, out_path, role, str(config["prompt"] or "") if config else "", replace_existing=True
    )
    conn = db()
    conn.execute("UPDATE scene_images SET applied_count=? WHERE id=?", (applied, image_id))
    conn.commit()
    row = conn.execute("SELECT * FROM scene_images WHERE id=?", (image_id,)).fetchone()
    conn.close()

    # 手动上传也要和中间的 Scene + Ref 槽位保持一致：没有运行任务时，
    # 把该槽位直接标成完成并指向最新上传图片；历史图片仍由 scene_images 独立保留。
    if not active:
        slot_job_id = ensure_scene_image_slot(scene_id, role)
        update_scene_image_job(
            slot_job_id,
            status="DONE",
            progress=100,
            current_node="Manual upload",
            error="",
            prompt_id="",
            image_id=image_id,
        )
        await hub.broadcast({
            "type": "scene_image_state",
            "job_id": slot_job_id,
            "scene_id": scene_id,
            "role": role,
            "status": "DONE",
            "progress": 100,
            "image_id": image_id,
            "node": "Manual upload",
            "applied_count": applied,
            "applied_scene_ids": applied_scene_ids,
        })
    result = scene_image_from_row(row)
    result["applied_count"] = applied
    result["applied_scene_ids"] = applied_scene_ids
    return result


@router.post("/api/scene-images/{image_id}/apply")
async def scene_image_apply(image_id: int, payload: SceneImageApplyIn):
    conn = db()
    row = conn.execute("SELECT * FROM scene_images WHERE id=?", (image_id,)).fetchone()
    conn.close()
    if not row:
        raise HTTPException(404, "Scene image not found")
    path = Path(str(row["output_path"] or ""))
    if not path.is_file():
        raise HTTPException(404, "Scene image file not found")
    role = payload.role.lower().strip()
    prompt = str(row["prompt"] or "").strip()
    if not prompt or prompt == "Manual reference upload":
        conn = db()
        config = conn.execute(
            "SELECT prompt FROM scene_image_configs WHERE scene_id=? AND role=?",
            (int(row["scene_id"]), role),
        ).fetchone()
        conn.close()
        prompt = str(config["prompt"] or "") if config else ""
    try:
        count, applied_scene_ids = apply_scene_image_to_matching_cut_shots(
            int(row["scene_id"]),
            path,
            role,
            prompt,
            replace_existing=payload.replace_existing,
        )
    except RuntimeError as exc:
        raise HTTPException(400, str(exc)) from exc
    conn = db()
    conn.execute(
        "UPDATE scene_images SET role=?, applied_count=? WHERE id=?",
        (role, count, image_id),
    )
    conn.commit()
    conn.close()
    return {
        "ok": True,
        "image_id": image_id,
        "role": role,
        "applied_count": count,
        "applied_scene_ids": applied_scene_ids,
    }


@router.get("/api/scene-images/{image_id}/media")
async def scene_image_media(image_id: int):
    conn = db()
    row = conn.execute("SELECT * FROM scene_images WHERE id=?", (image_id,)).fetchone()
    conn.close()
    if not row:
        raise HTTPException(404, "Scene image not found")
    path = Path(str(row["output_path"] or ""))
    if not path.is_file():
        raise HTTPException(404, "Scene image file not found")
    suffix = path.suffix.lower()
    media_type = "image/png"
    if suffix in {".jpg", ".jpeg"}:
        media_type = "image/jpeg"
    elif suffix == ".webp":
        media_type = "image/webp"
    return FileResponse(path, media_type=media_type)


@router.delete("/api/scene-images/{image_id}")
async def scene_image_delete(image_id: int):
    conn = db()
    row = conn.execute("SELECT * FROM scene_images WHERE id=?", (image_id,)).fetchone()
    if not row:
        conn.close()
        raise HTTPException(404, "Scene image not found")
    conn.execute("DELETE FROM scene_images WHERE id=?", (image_id,))
    conn.commit()
    conn.close()
    path = Path(str(row["output_path"] or ""))
    try:
        if path.is_file():
            path.unlink()
    except Exception:
        pass
    return {"ok": True}
