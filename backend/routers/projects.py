"""Project / Scene 接口。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from backend.core.db import db
from backend.core.schemas import ProjectIn, SceneIn
from backend.services.repo import get_scene

router = APIRouter()


@router.get("/api/projects")
async def projects_list():
    conn = db()
    rows = conn.execute("SELECT * FROM projects ORDER BY id DESC").fetchall()
    conn.close()
    return [dict(r) for r in rows]


@router.post("/api/projects")
async def projects_create(payload: ProjectIn):
    conn = db()
    cur = conn.execute("INSERT INTO projects(name) VALUES(?)", (payload.name.strip(),))
    pid = cur.lastrowid
    conn.commit()
    row = conn.execute("SELECT * FROM projects WHERE id=?", (pid,)).fetchone()
    conn.close()
    return dict(row)


@router.get("/api/projects/{project_id}/scenes")
async def scenes_list(project_id: int):
    conn = db()
    rows = conn.execute("SELECT * FROM scenes WHERE project_id=? ORDER BY sort_order,id", (project_id,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@router.post("/api/projects/{project_id}/scenes")
async def scenes_create(project_id: int, payload: SceneIn):
    conn = db()
    max_order = conn.execute("SELECT COALESCE(MAX(sort_order),0) AS m FROM scenes WHERE project_id=?", (project_id,)).fetchone()["m"]
    cur = conn.execute("INSERT INTO scenes(project_id,name,sort_order) VALUES(?,?,?)", (project_id, payload.name.strip(), int(max_order) + 1))
    sid = cur.lastrowid
    conn.commit()
    row = conn.execute("SELECT * FROM scenes WHERE id=?", (sid,)).fetchone()
    conn.close()
    return dict(row)


@router.delete("/api/scenes/{scene_id}")
async def scene_delete(scene_id: int):
    get_scene(scene_id)
    conn = db()
    active_shot = conn.execute(
        "SELECT id FROM shots WHERE scene_id=? AND status IN ('QUEUED','PREPARING','RUNNING') LIMIT 1",
        (scene_id,),
    ).fetchone()
    active_image = conn.execute(
        "SELECT id FROM scene_image_jobs WHERE scene_id=? AND status IN ('QUEUED','PREPARING','RUNNING') LIMIT 1",
        (scene_id,),
    ).fetchone()
    if active_shot or active_image:
        conn.close()
        raise HTTPException(409, "当前 Scene 还有视频、生图或修脸任务在运行/排队，不能删除")
    conn.execute("DELETE FROM scenes WHERE id=?", (scene_id,))
    conn.commit()
    conn.close()
    return {"ok": True, "id": scene_id}


@router.delete("/api/projects/{project_id}")
async def project_delete(project_id: int):
    conn = db()
    project = conn.execute("SELECT id FROM projects WHERE id=?", (project_id,)).fetchone()
    if not project:
        conn.close()
        raise HTTPException(404, "项目不存在")
    active_shot = conn.execute(
        """
        SELECT sh.id FROM shots sh
        JOIN scenes sc ON sc.id=sh.scene_id
        WHERE sc.project_id=? AND sh.status IN ('QUEUED','PREPARING','RUNNING')
        LIMIT 1
        """,
        (project_id,),
    ).fetchone()
    active_image = conn.execute(
        """
        SELECT j.id FROM scene_image_jobs j
        JOIN scenes sc ON sc.id=j.scene_id
        WHERE sc.project_id=? AND j.status IN ('QUEUED','PREPARING','RUNNING')
        LIMIT 1
        """,
        (project_id,),
    ).fetchone()
    if active_shot or active_image:
        conn.close()
        raise HTTPException(409, "当前项目还有视频、生图或修脸任务在运行/排队，不能删除")
    conn.execute("DELETE FROM projects WHERE id=?", (project_id,))
    conn.commit()
    conn.close()
    return {"ok": True, "id": project_id}
