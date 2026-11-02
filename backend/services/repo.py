"""Project/Scene/Shot/MV 数据读写的公共函数。"""
from __future__ import annotations

import json
import time
from fastapi import HTTPException
from pathlib import Path
from typing import Any

from backend.core.db import db, row_dict
from backend.core.ns import namespace
from backend.core.paths import PROJECTS_DIR
from backend.services.continuation import auto_wait_reason, freeze_inputs


def get_shot(shot_id: int) -> dict[str, Any]:
    conn = db()
    row = conn.execute(
        """
        SELECT sh.*, sc.name AS scene_name, sc.project_id, p.name AS project_name
        FROM shots sh
        JOIN scenes sc ON sc.id=sh.scene_id
        JOIN projects p ON p.id=sc.project_id
        WHERE sh.id=?
        """,
        (shot_id,),
    ).fetchone()
    conn.close()
    if not row:
        raise HTTPException(404, "Shot not found")
    return dict(row)


def previous_shot(shot: dict[str, Any]) -> dict[str, Any] | None:
    """Return the previous Shot in whole-project playback order, across Scene boundaries."""
    conn = db()
    row = conn.execute(
        """
        SELECT sh.*
        FROM shots sh
        JOIN scenes sc ON sc.id=sh.scene_id
        JOIN scenes current_sc ON current_sc.id=?
        WHERE sc.project_id=current_sc.project_id
          AND (
                sc.sort_order < current_sc.sort_order
                OR (sc.sort_order = current_sc.sort_order AND sc.id < current_sc.id)
                OR (
                    sc.sort_order = current_sc.sort_order
                    AND sc.id = current_sc.id
                    AND sh.shot_no < ?
                )
          )
        ORDER BY sc.sort_order DESC, sc.id DESC, sh.shot_no DESC
        LIMIT 1
        """,
        (shot["scene_id"], shot["shot_no"]),
    ).fetchone()
    conn.close()
    return row_dict(row)


def ensure_shot_dir(shot: dict[str, Any]) -> Path:
    safe_project = f"project_{shot['project_id']:03d}"
    safe_scene = f"scene_{shot['scene_id']:03d}"
    safe_shot = f"shot_{shot['shot_no']:04d}"
    path = PROJECTS_DIR / safe_project / safe_scene / safe_shot
    path.mkdir(parents=True, exist_ok=True)
    return path


def mv_project_dir(project_id: int) -> Path:
    path = PROJECTS_DIR / f"project_{project_id:03d}" / "mv"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _mv_audio_dir(project_id: int) -> Path:
    path = mv_project_dir(project_id) / "audio"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _load_project_timeline(project_id: int) -> dict[str, Any]:
    mv = ensure_mv_project(project_id)
    raw = str(mv.get("lyrics_timeline_path") or "").strip()
    path = Path(raw) if raw else _mv_audio_dir(project_id) / "lyrics_timeline.json"
    if not path.is_file():
        return {"duration": float(mv.get("audio_duration") or 0), "lines": [], "beats": [], "slots": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise RuntimeError(f"歌词时间轴读取失败: {exc}") from exc
    return data if isinstance(data, dict) else {"duration": 0, "lines": [], "beats": [], "slots": []}


def get_scene(scene_id: int) -> dict[str, Any]:
    conn = db()
    row = conn.execute(
        """
        SELECT sc.*, p.name AS project_name
        FROM scenes sc
        JOIN projects p ON p.id=sc.project_id
        WHERE sc.id=?
        """,
        (scene_id,),
    ).fetchone()
    conn.close()
    if not row:
        raise HTTPException(404, "Scene not found")
    return dict(row)


def update_shot_state(shot_id: int, **values: Any) -> None:
    if not values:
        return
    conn = db()
    try:
        status = str(values.get("status") or "").upper()
        now = time.time()
        if status == "QUEUED":
            values["queued_input_path"] = values.get("queued_input_path") or freeze_inputs(get_shot(shot_id), namespace())
            frozen = json.loads(Path(values["queued_input_path"]).read_text(encoding="utf-8"))["shot"]
            reason = auto_wait_reason(frozen, namespace())
            if reason:
                values["current_node"] = reason
            values.update(generation_started_at=0, generation_finished_at=0, generation_elapsed_seconds=0)
        elif status == "PREPARING":
            values.update(generation_started_at=now, generation_finished_at=0, generation_elapsed_seconds=0)
        elif status in {"DONE", "FAILED", "CANCELLED"}:
            row = conn.execute("SELECT generation_started_at,generation_finished_at FROM shots WHERE id=?", (shot_id,)).fetchone()
            if row and row["generation_started_at"] and not row["generation_finished_at"]:
                values.update(generation_finished_at=now, generation_elapsed_seconds=max(0, now - float(row["generation_started_at"])))
        keys = list(values.keys())
        sql = ", ".join(f"{k}=?" for k in keys) + ", updated_at=CURRENT_TIMESTAMP"
        conn.execute(f"UPDATE shots SET {sql} WHERE id=?", [values[k] for k in keys] + [shot_id])
        conn.commit()
    finally:
        conn.close()


def _mv_update_project(project_id: int, **fields: Any) -> None:
    if not fields:
        return
    conn = db()
    status = fields.get("song_status")
    if status == "RUNNING":
        fields.update(song_started_at=0, song_finished_at=0, song_elapsed_seconds=0)
    elif status in {"DONE", "FAILED", "CANCELLED"}:
        row = conn.execute("SELECT song_started_at,song_finished_at FROM mv_projects WHERE project_id=?", (project_id,)).fetchone()
        if row and row["song_started_at"] and not row["song_finished_at"]:
            now = time.time()
            fields.update(song_finished_at=now, song_elapsed_seconds=max(0, now - float(row["song_started_at"])))
    elif status in {"PENDING", "IDLE"}:
        fields.update(song_started_at=0, song_finished_at=0, song_elapsed_seconds=0)
    assignments = ",".join(f"{key}=?" for key in fields)
    conn.execute(
        f"UPDATE mv_projects SET {assignments},updated_at=CURRENT_TIMESTAMP WHERE project_id=?",
        [*fields.values(), project_id],
    )
    conn.commit()
    conn.close()


def ensure_mv_project(project_id: int) -> dict[str, Any]:
    conn = db()
    project = conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
    if not project:
        conn.close()
        raise HTTPException(404, "Project not found")
    conn.execute(
        "INSERT OR IGNORE INTO mv_projects(project_id,title) VALUES(?,?)",
        (project_id, f"{str(project['name'] or '').strip()} 歌曲"),
    )
    legacy_title = f"{str(project['name'] or '').strip()} MV"
    conn.execute(
        "UPDATE mv_projects SET title=?,updated_at=CURRENT_TIMESTAMP WHERE project_id=? AND title=?",
        (f"{str(project['name'] or '').strip()} 歌曲", project_id, legacy_title),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM mv_projects WHERE project_id=?", (project_id,)).fetchone()
    conn.close()
    return dict(row)
