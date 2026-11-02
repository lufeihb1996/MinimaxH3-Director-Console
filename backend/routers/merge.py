"""合并与成片接口。"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path
from fastapi import APIRouter, Body, HTTPException
from fastapi.responses import FileResponse

from backend.core.db import db
from backend.core.paths import PROJECTS_DIR
from backend.core.runtime import project_merge_progress
from backend.services.merge import build_project_master, build_scene_master

router = APIRouter()
_scene_merge_locks: dict[int, asyncio.Lock] = {}
_koubo_render_progress: dict[int, dict] = {}
REMOTION_DIR = Path(r"D:\ai\Remotion_Koubo_Studio")


@router.post("/api/scenes/{scene_id}/merge")
async def scene_merge(scene_id: int):
    lock = _scene_merge_locks.setdefault(scene_id, asyncio.Lock())
    summary: dict = {}
    try:
        async with lock:
            output = await asyncio.to_thread(build_scene_master, scene_id, completed_only=True, summary=summary)
    except RuntimeError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {
        "ok": True,
        "path": str(output),
        "media_url": f"/api/scenes/{scene_id}/master",
        "download_url": f"/api/scenes/{scene_id}/master/download",
        **summary,
    }


@router.get("/api/scenes/{scene_id}/master")
async def scene_master_media(scene_id: int):
    conn = db()
    scene = conn.execute("SELECT project_id FROM scenes WHERE id=?", (scene_id,)).fetchone()
    conn.close()
    if not scene:
        raise HTTPException(404, "Scene not found")
    p = PROJECTS_DIR / f"project_{int(scene['project_id']):03d}" / f"scene_{scene_id:03d}" / f"scene_{scene_id:03d}_master.mp4"
    if not p.exists():
        raise HTTPException(404, "This Scene has no merged output yet")
    return FileResponse(p, media_type="video/mp4")


@router.get("/api/scenes/{scene_id}/master/download")
async def scene_master_download(scene_id: int):
    conn = db()
    scene = conn.execute("SELECT project_id FROM scenes WHERE id=?", (scene_id,)).fetchone()
    conn.close()
    if not scene:
        raise HTTPException(404, "Scene not found")
    p = PROJECTS_DIR / f"project_{int(scene['project_id']):03d}" / f"scene_{scene_id:03d}" / f"scene_{scene_id:03d}_master.mp4"
    if not p.exists():
        raise HTTPException(404, "This Scene has no merged output yet")
    return FileResponse(p, media_type="video/mp4", filename=p.name)


@router.get("/api/scenes/{scene_id}/master/info")
async def scene_master_info(scene_id: int):
    conn = db()
    scene = conn.execute("SELECT project_id FROM scenes WHERE id=?", (scene_id,)).fetchone()
    conn.close()
    if not scene:
        return {"exists": False}
    p = PROJECTS_DIR / f"project_{int(scene['project_id']):03d}" / f"scene_{scene_id:03d}" / f"scene_{scene_id:03d}_master.mp4"
    if not p.exists() or p.stat().st_size == 0:
        return {"exists": False}
    return {
        "exists": True,
        "size": p.stat().st_size,
        "media_url": f"/api/scenes/{scene_id}/master",
        "download_url": f"/api/scenes/{scene_id}/master/download",
        "path": str(p),
        "filename": p.name,
    }


@router.get("/api/projects/{project_id}/merge/progress")
async def project_merge_progress_get(project_id: int):
    return project_merge_progress.get(project_id, {"status": "IDLE", "progress": 0, "stage": "", "started_at": 0})


@router.post("/api/projects/{project_id}/merge")
async def project_merge(project_id: int):
    existing = project_merge_progress.get(project_id, {})
    if existing.get("status") == "RUNNING":
        raise HTTPException(409, "当前项目正在合并，请等待完成")
    item = {"status": "RUNNING", "progress": 0, "stage": "Preparing merge", "started_at": time.time()}
    project_merge_progress[project_id] = item
    def report(pct, stage):
        item.update(progress=round(pct, 1), stage=stage)
    try:
        output = await asyncio.to_thread(build_project_master, project_id, report)
        item.update(status="DONE", progress=100, stage="Merge complete", finished_at=time.time())
    except Exception as exc:
        item.update(status="FAILED", stage="合并失败", error=str(exc), finished_at=time.time())
        raise HTTPException(400, str(exc)) from exc
    return {
        "ok": True, "path": str(output),
        "media_url": f"/api/projects/{project_id}/master",
        "download_url": f"/api/projects/{project_id}/master/download",
    }


@router.get("/api/projects/{project_id}/master")
async def project_master_media(project_id: int):
    p = PROJECTS_DIR / f"project_{project_id:03d}" / f"project_{project_id:03d}_master.mp4"
    if not p.exists():
        raise HTTPException(404, "This Project has not been merged yet")
    return FileResponse(p, media_type="video/mp4")


@router.get("/api/projects/{project_id}/master/download")
async def project_master_download(project_id: int):
    p = PROJECTS_DIR / f"project_{project_id:03d}" / f"project_{project_id:03d}_master.mp4"
    if not p.exists():
        raise HTTPException(404, "This Project has not been merged yet")
    return FileResponse(p, media_type="video/mp4", filename=p.name)


@router.get("/api/projects/{project_id}/master/info")
async def project_master_info(project_id: int):
    p = PROJECTS_DIR / f"project_{project_id:03d}" / f"project_{project_id:03d}_master.mp4"
    if not p.exists() or p.stat().st_size == 0:
        return {"exists": False}
    return {
        "exists": True,
        "size": p.stat().st_size,
        "media_url": f"/api/projects/{project_id}/master",
        "download_url": f"/api/projects/{project_id}/master/download",
        "path": str(p),
        "filename": p.name,
    }


# ==================== 口播包装工作台 (Remotion 16:9 实时预览与渲染导出) ====================

def _scene_paths(scene_id: int):
    conn = db()
    scene = conn.execute("SELECT id, project_id FROM scenes WHERE id=?", (scene_id,)).fetchone()
    conn.close()
    if not scene:
        raise HTTPException(404, "Scene not found")
    pid = int(scene["project_id"])
    proj_dir = PROJECTS_DIR / f"project_{pid:03d}"
    sc_dir = proj_dir / f"scene_{scene_id:03d}"
    sc_dir.mkdir(parents=True, exist_ok=True)
    return pid, proj_dir, sc_dir


@router.get("/api/scenes/{scene_id}/koubo-pack")
async def koubo_pack_get(scene_id: int):
    pid, proj_dir, sc_dir = _scene_paths(scene_id)
    sc_cfg_path = sc_dir / "cards169.json"
    rem_cfg_path = REMOTION_DIR / "src" / "cards169.json"
    cfg_path = sc_cfg_path if sc_cfg_path.exists() else rem_cfg_path
    cfg_data = json.loads(cfg_path.read_text(encoding="utf-8")) if cfg_path.exists() else {}

    rem_tl_path = REMOTION_DIR / "src" / "tl.json"
    tl_data = json.loads(rem_tl_path.read_text(encoding="utf-8")) if rem_tl_path.exists() else {"total": 60, "segs": []}

    master_p = sc_dir / f"scene_{scene_id:03d}_master.mp4"
    packaged_p = sc_dir / f"scene_{scene_id:03d}_packaged_169.mp4"
    rem_out_p = REMOTION_DIR / "out_169.mp4"
    if not packaged_p.exists() and rem_out_p.exists():
        try:
            shutil.copy2(rem_out_p, packaged_p)
        except Exception:
            pass

    has_packaged = packaged_p.exists() and packaged_p.stat().st_size > 0
    return {
        "scene_id": scene_id,
        "project_id": pid,
        "config": cfg_data,
        "timeline": tl_data,
        "has_master": master_p.exists() and master_p.stat().st_size > 0,
        "master_url": f"/api/scenes/{scene_id}/master",
        "has_packaged": has_packaged,
        "packaged_size": packaged_p.stat().st_size if has_packaged else 0,
        "packaged_mtime": int(packaged_p.stat().st_mtime) if has_packaged else 0,
        "packaged_url": f"/api/scenes/{scene_id}/koubo-pack/video",
        "download_url": f"/api/scenes/{scene_id}/koubo-pack/download",
        "render_status": _koubo_render_progress.get(
            scene_id,
            {"status": "IDLE", "progress": 0, "stage": "Ready", "rendered": 0, "total": 0},
        ),
    }


@router.put("/api/scenes/{scene_id}/koubo-pack")
async def koubo_pack_save(scene_id: int, payload: dict = Body(...)):
    _, _, sc_dir = _scene_paths(scene_id)
    cfg_data = payload.get("config") if "config" in payload else payload
    if not isinstance(cfg_data, dict) or "slides" not in cfg_data:
        raise HTTPException(400, "无效的 cards169 配置格式")
    txt = json.dumps(cfg_data, ensure_ascii=False, indent=2)
    (sc_dir / "cards169.json").write_text(txt, encoding="utf-8")
    (REMOTION_DIR / "src" / "cards169.json").write_text(txt, encoding="utf-8")
    return {"ok": True, "config": cfg_data}


@router.get("/api/scenes/{scene_id}/koubo-pack/progress")
async def koubo_pack_progress(scene_id: int):
    _, _, sc_dir = _scene_paths(scene_id)
    packaged_p = sc_dir / f"scene_{scene_id:03d}_packaged_169.mp4"
    has_packaged = packaged_p.exists() and packaged_p.stat().st_size > 0
    st = _koubo_render_progress.get(
        scene_id,
        {"status": "IDLE", "progress": 0, "stage": "Ready", "rendered": 0, "total": 0},
    )
    return {
        **st,
        "has_packaged": has_packaged,
        "packaged_mtime": int(packaged_p.stat().st_mtime) if has_packaged else 0,
        "packaged_url": f"/api/scenes/{scene_id}/koubo-pack/video",
        "download_url": f"/api/scenes/{scene_id}/koubo-pack/download",
    }


@router.post("/api/scenes/{scene_id}/koubo-pack/render")
async def koubo_pack_render(scene_id: int, payload: dict = Body(default={})):
    pid, proj_dir, sc_dir = _scene_paths(scene_id)
    cur = _koubo_render_progress.get(scene_id, {})
    if cur.get("status") == "RUNNING":
        raise HTTPException(409, "当前场景正在渲染包装成片，请稍候")

    if isinstance(payload, dict) and isinstance(payload.get("config"), dict):
        txt = json.dumps(payload["config"], ensure_ascii=False, indent=2)
        (sc_dir / "cards169.json").write_text(txt, encoding="utf-8")
        (REMOTION_DIR / "src" / "cards169.json").write_text(txt, encoding="utf-8")
    elif (sc_dir / "cards169.json").exists():
        shutil.copy2(sc_dir / "cards169.json", REMOTION_DIR / "src" / "cards169.json")

    master_p = sc_dir / f"scene_{scene_id:03d}_master.mp4"
    if not master_p.exists() or master_p.stat().st_size == 0:
        await asyncio.to_thread(build_scene_master, scene_id, completed_only=True)

    shutil.copy2(master_p, REMOTION_DIR / "public" / "main.mp4")

    state = {
        "status": "RUNNING",
        "progress": 2.0,
        "stage": "正在打包画布与启动渲染引擎...",
        "rendered": 0,
        "total": 0,
        "started_at": time.time(),
    }
    _koubo_render_progress[scene_id] = state

    def _worker():
        try:
            tmp_dir = REMOTION_DIR / ".tmp"
            tmp_dir.mkdir(parents=True, exist_ok=True)
            env = os.environ.copy()
            env["TEMP"] = str(tmp_dir)
            env["TMP"] = str(tmp_dir)
            cmd = [
                "node",
                str(REMOTION_DIR / "node_modules" / "@remotion" / "cli" / "remotion-cli.js"),
                "render",
                "src/index.ts",
                "Tutorial169",
                "out_169.mp4",
                "--image-format=jpeg",
                "--jpeg-quality=92",
                "--concurrency=3",
                "--offthreadvideo-cache-size-in-bytes=268435456",
                "--browser-executable=C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
            ]
            proc = subprocess.Popen(
                cmd,
                cwd=str(REMOTION_DIR),
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            re_render = re.compile(r"Rendered\s+(\d+)/(\d+)")
            re_encode = re.compile(r"Encoded\s+(\d+)/(\d+)")
            for line in proc.stdout or []:
                m1 = re_render.search(line)
                if m1:
                    done, tot = int(m1.group(1)), max(1, int(m1.group(2)))
                    pct = min(88.0, round(5.0 + (done / tot) * 83.0, 1))
                    state.update(progress=pct, rendered=done, total=tot, stage=f"正在渲染动态画面 ({done}/{tot} 帧)...")
                    continue
                m2 = re_encode.search(line)
                if m2:
                    done, tot = int(m2.group(1)), max(1, int(m2.group(2)))
                    pct = min(99.0, round(88.0 + (done / tot) * 11.0, 1))
                    state.update(progress=pct, rendered=done, total=tot, stage=f"正在合成音视频流 ({done}/{tot})...")
            rc = proc.wait()
            out_p = REMOTION_DIR / "out_169.mp4"
            if rc != 0 or not out_p.exists():
                raise RuntimeError(f"Remotion 渲染失败 (exit={rc})")
            dest_p = sc_dir / f"scene_{scene_id:03d}_packaged_169.mp4"
            shutil.copy2(out_p, dest_p)
            state.update(status="DONE", progress=100.0, stage="16:9 包装成片导出完成！", finished_at=time.time())
        except Exception as exc:
            state.update(status="FAILED", stage=f"渲染失败: {exc}", error=str(exc), finished_at=time.time())

    threading.Thread(target=_worker, daemon=True).start()
    return {"ok": True, "status": "RUNNING"}


@router.get("/api/scenes/{scene_id}/koubo-pack/video")
async def koubo_pack_video(scene_id: int):
    _, _, sc_dir = _scene_paths(scene_id)
    p = sc_dir / f"scene_{scene_id:03d}_packaged_169.mp4"
    if not p.exists():
        p = REMOTION_DIR / "out_169.mp4"
    if not p.exists():
        raise HTTPException(404, "No packaged cut generated yet")
    return FileResponse(p, media_type="video/mp4")


@router.get("/api/scenes/{scene_id}/koubo-pack/download")
async def koubo_pack_download(scene_id: int):
    _, _, sc_dir = _scene_paths(scene_id)
    p = sc_dir / f"scene_{scene_id:03d}_packaged_169.mp4"
    if not p.exists():
        p = REMOTION_DIR / "out_169.mp4"
    if not p.exists():
        raise HTTPException(404, "No packaged cut generated yet")
    return FileResponse(p, media_type="video/mp4", filename=f"scene_{scene_id:03d}_koubo_169.mp4")


@router.get("/api/koubo-pack/public/{file_path:path}")
async def koubo_pack_public_asset(file_path: str):
    target = (REMOTION_DIR / "public" / file_path).resolve()
    pub_root = (REMOTION_DIR / "public").resolve()
    if not str(target).startswith(str(pub_root)) or not target.exists() or not target.is_file():
        raise HTTPException(404, "素材不存在")
    return FileResponse(target)
