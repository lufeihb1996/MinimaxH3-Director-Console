"""四槽 Ref 生图：配置、共享范围、生图队列、应用到镜头。"""
from __future__ import annotations

import asyncio
import json
import shutil
import time
import sqlite3
import uuid
import websockets
from pathlib import Path
from typing import Any

from backend.core.comfy import ComfyClient, _interrupt_remote_prompt, comfy_prompt_presence, history_terminal_state, image_meta_from_history, workflow_node_label
from backend.core.db import db, get_settings, read_json_file
from backend.core.gpu import gpu_oom_retry, gpu_slot
from backend.core.paths import PROJECTS_DIR
from backend.core.runtime import hub, rt, scene_image_queue
from backend.core.tasks import _cancel_active_item, _remove_from_queue
from backend.models.registry import get_adapter, get_model
from backend.services.repo import ensure_shot_dir, get_scene, get_shot, previous_shot, update_shot_state

REF_ROLE_COLUMNS = {
    "ref1": "ref1_path",
    "ref2": "ref2_path",
    "ref3": "ref3_path",
    "ref4": "ref4_path",
}


def ensure_scene_image_configs(scene_id: int) -> None:
    conn = db()
    for role in REF_ROLE_COLUMNS:
        default_scope = "project" if role in {"ref1", "ref2"} else "scene"
        conn.execute(
            """
            INSERT OR IGNORE INTO scene_image_configs(scene_id,role,prompt,aspect_ratio,seed,image_model,enabled,ref_scope,lora_mode)
            VALUES(?,?,?,'9:16',42,'moss_image_5b',1,?,'all')
            """,
            (scene_id, role, "", default_scope),
        )
    conn.commit()
    conn.close()


def get_scene_image_configs(scene_id: int) -> list[dict[str, Any]]:
    get_scene(scene_id)
    ensure_scene_image_configs(scene_id)
    conn = db()
    rows = conn.execute(
        "SELECT * FROM scene_image_configs WHERE scene_id=? ORDER BY role",
        (scene_id,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def update_scene_image_job(job_id: int, **values: Any) -> None:
    if not values:
        return
    conn = db()
    try:
        status = str(values.get("status") or "").upper()
        now = time.time()
        if status in {"DRAFT", "PENDING", "QUEUED"}:
            values.update(generation_started_at=0, generation_finished_at=0, generation_elapsed_seconds=0)
        elif status == "PREPARING":
            values.update(generation_started_at=now, generation_finished_at=0, generation_elapsed_seconds=0)
        elif status in {"DONE", "FAILED", "CANCELLED"}:
            row = conn.execute(
                "SELECT generation_started_at, generation_finished_at FROM scene_image_jobs WHERE id=?",
                (job_id,),
            ).fetchone()
            if row and row["generation_started_at"] and not row["generation_finished_at"]:
                values.update(
                    generation_finished_at=now,
                    generation_elapsed_seconds=max(0.0, now - float(row["generation_started_at"])),
                )
        keys = list(values.keys())
        sql = ", ".join(f"{k}=?" for k in keys) + ", updated_at=CURRENT_TIMESTAMP"
        conn.execute(f"UPDATE scene_image_jobs SET {sql} WHERE id=?", [values[k] for k in keys] + [job_id])
        conn.commit()
    finally:
        conn.close()


def get_scene_image_job(job_id: int) -> dict[str, Any]:
    conn = db()
    row = conn.execute(
        """
        SELECT j.*, sc.name AS scene_name, sc.project_id
        FROM scene_image_jobs j
        JOIN scenes sc ON sc.id=j.scene_id
        WHERE j.id=?
        """,
        (job_id,),
    ).fetchone()
    conn.close()
    if not row:
        raise RuntimeError("Image task not found")
    return dict(row)


def ensure_scene_image_dir(scene: dict[str, Any]) -> Path:
    path = (
        PROJECTS_DIR
        / f"project_{int(scene['project_id']):03d}"
        / f"scene_{int(scene['id']):03d}"
        / "generated_images"
    )
    path.mkdir(parents=True, exist_ok=True)
    return path


def scene_image_from_row(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    item = dict(row)
    image_id = int(item["id"])
    item["media_url"] = f"/api/scene-images/{image_id}/media"
    return item


def inherit_mother_refs(shot_id: int) -> dict[str, Any]:
    """Fill empty references for CUT shots only; CONTINUE and T2V never inherit Ref1..Ref4."""
    shot = get_shot(shot_id)
    if shot["mode"].upper() != "CUT" or shot["status"] in {"QUEUED", "PREPARING", "RUNNING"}:
        return shot
    previous = None
    conn = db()
    defaults = {r["role"]: r["image_path"] for r in conn.execute(
        "SELECT role,image_path FROM scene_default_refs WHERE scene_id=?", (shot["scene_id"],)
    )}
    # Existing projects predate default bindings: reuse established same-Scene references.
    siblings = [dict(r) for r in conn.execute(
        "SELECT * FROM shots WHERE scene_id=? AND id!=? AND UPPER(mode)!='T2V' ORDER BY shot_no",
        (shot["scene_id"], shot_id)
    )]
    conn.close()
    values = {}
    for i in range(1, 5):
        role = f"ref{i}"; field = role + "_path"
        if shot.get(field):
            continue
        owner = ref_owner_scene_id(int(shot["scene_id"]), role)
        if owner != int(shot["scene_id"]):
            conn = db()
            shared_default = conn.execute("SELECT image_path FROM scene_default_refs WHERE scene_id=? AND role=?", (owner,role)).fetchone()
            conn.close()
            if shared_default:
                defaults[role] = shared_default["image_path"]
        sources = ([previous.get(field)] if previous else []) + [defaults.get(role)] + [r.get(field) for r in siblings]
        source = next((Path(raw) for raw in sources if raw and Path(raw).is_file()), None)
        if source is None:
            continue
        dest = ensure_shot_dir(shot) / "refs" / f"{role}_{uuid.uuid4().hex}{source.suffix}"
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, dest)
        values[field] = str(dest)
    if values:
        update_shot_state(shot_id, **values)
    return get_shot(shot_id)


def apply_scene_image_to_cut_shots(scene_id: int, image_path: Path, role: str, replace_existing: bool = False) -> int:
    column = REF_ROLE_COLUMNS.get(role)
    if not column:
        raise RuntimeError("Image role must be ref1/ref2/ref3/ref4")
    if not image_path.is_file():
        raise RuntimeError("Scene image file not found")

    # A deliberate history-image application also establishes the default for future shots.
    scene = get_scene(scene_id)
    folder = PROJECTS_DIR / f"project_{int(scene['project_id']):03d}" / f"scene_{scene_id:03d}" / "mother_refs"
    folder.mkdir(parents=True, exist_ok=True)
    fixed = folder / f"{role}_{uuid.uuid4().hex}{image_path.suffix or '.png'}"
    shutil.copy2(image_path, fixed)
    conn = db()
    conn.execute("INSERT INTO scene_default_refs(scene_id,role,image_path) VALUES(?,?,?) ON CONFLICT(scene_id,role) DO UPDATE SET image_path=excluded.image_path", (scene_id, role, str(fixed)))
    conn.commit()
    rows = conn.execute(
        "SELECT id FROM shots WHERE scene_id=? AND UPPER(mode)='CUT' AND status NOT IN ('QUEUED','PREPARING','RUNNING') ORDER BY shot_no",
        (scene_id,),
    ).fetchall()
    conn.close()

    applied = 0
    for row in rows:
        shot = get_shot(int(row["id"]))
        current = str(shot.get(column) or "").strip()
        if current and not replace_existing:
            continue

        refs_dir = ensure_shot_dir(shot) / "refs"
        refs_dir.mkdir(parents=True, exist_ok=True)
        ext = image_path.suffix.lower() or ".png"
        dest = refs_dir / f"{role}_{uuid.uuid4().hex}{ext}"
        shutil.copy2(image_path, dest)
        update_shot_state(int(shot["id"]), **{column: str(dest)})
        applied += 1
    return applied


def shared_ref_scene_ids(scene_id: int, role: str) -> list[int]:
    """Explicit project sharing only; identical prompts never imply ownership."""
    scene = get_scene(scene_id)
    conn = db()
    config = conn.execute("SELECT ref_scope FROM scene_image_configs WHERE scene_id=? AND role=?", (scene_id, role)).fetchone()
    if role not in {"ref1", "ref2"} or not config or config["ref_scope"] != "project":
        conn.close()
        return [scene_id]
    rows = conn.execute("""SELECT sc.id FROM scenes sc JOIN scene_image_configs cfg ON cfg.scene_id=sc.id
        WHERE sc.project_id=? AND cfg.role=? AND cfg.ref_scope='project' ORDER BY sc.sort_order,sc.id""",
        (scene["project_id"], role)).fetchall()
    conn.close()
    return [int(row["id"]) for row in rows]


def ref_owner_scene_id(scene_id: int, role: str) -> int:
    return shared_ref_scene_ids(scene_id, role)[0]


def matching_project_scene_ids_for_ref(scene_id: int, role: str, prompt: str) -> list[int]:
    # The prompt argument is retained for callers, but no longer defines shared groups.
    return shared_ref_scene_ids(scene_id, role)


def apply_scene_image_to_matching_cut_shots(
    scene_id: int,
    image_path: Path,
    role: str,
    prompt: str,
    *,
    replace_existing: bool,
) -> tuple[int, list[int]]:
    """Apply one Scene image to same-prompt Ref slots across the Project.

    This is what makes a mother image useful: e.g. Scene 1 Ref1 Sun Wukong can automatically become
    Ref1 for later Scenes whose Ref1 config contains the exact same character prompt. Different
    prompts remain isolated, so an environment or supporting character will not leak into unrelated
    Scenes.
    """
    target_scene_ids = matching_project_scene_ids_for_ref(scene_id, role, prompt)
    applied = 0
    for target_scene_id in target_scene_ids:
        applied += apply_scene_image_to_cut_shots(
            target_scene_id,
            image_path,
            role,
            replace_existing=replace_existing,
        )
    return applied, target_scene_ids


async def monitor_scene_image_prompt(
    client: ComfyClient,
    client_id: str,
    prompt_id: str,
    job_id: int,
    workflow: dict[str, Any],
    stage_progress,
    model_label: str = "Image generation",
) -> None:
    uri = client.ws_base + f"/ws?clientId={client_id}"
    deadline = asyncio.get_running_loop().time() + 900

    async def emit(progress: float, node: str) -> None:
        update_scene_image_job(job_id, progress=progress, current_node=node)
        job = get_scene_image_job(job_id)
        await hub.broadcast({
            "type": "scene_image_progress",
            "job_id": job_id,
            "scene_id": job["scene_id"],
            "role": job["role"],
            "progress": progress,
            "node": node,
        })

    async def check_history() -> bool:
        terminal, error = await history_terminal_state(client, prompt_id)
        if not terminal:
            return False
        if error:
            raise RuntimeError(error)
        return True

    # WebSocket 断开不等于生成失败：远端 ComfyUI 可能还在跑。每隔几拍查一次
    # /history 和 /queue，连续三次都找不到才判定远端已重启、任务已丢失。
    ticks = 0
    missing_checks = 0
    active_node = None

    async def confirm_remote_loss() -> bool:
        """连续三次确认 history 和 queue 都不再认识这个 prompt 时返回 True。"""
        nonlocal missing_checks
        presence = await comfy_prompt_presence(client, prompt_id)
        if presence is False:
            missing_checks += 1
            return missing_checks >= 3
        if presence is True:
            missing_checks = 0
        return False

    try:
        async with websockets.connect(uri, max_size=32 * 1024 * 1024, open_timeout=20) as ws:
            while True:
                if asyncio.get_running_loop().time() >= deadline:
                    raise RuntimeError(f"Timed out waiting for {model_label} image generation (15 min)")
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=1.5)
                except asyncio.TimeoutError:
                    if await check_history():
                        return
                    ticks += 1
                    if ticks % 4 == 0 and await confirm_remote_loss():
                        raise RuntimeError("Colab/ComfyUI restarted; the original Scene image task is gone")
                    continue
                if isinstance(raw, bytes):
                    continue
                try:
                    msg = json.loads(raw)
                except Exception:
                    continue
                msg_type = msg.get("type")
                data = msg.get("data") or {}
                if msg_type == "progress":
                    if data.get("prompt_id") and data.get("prompt_id") != prompt_id:
                        continue
                    value = float(data.get("value", 0))
                    maximum = float(data.get("max", 1) or 1)
                    node = data.get("node") or active_node
                    base_label = workflow_node_label(workflow, node)
                    step_label = f"{base_label} · 第 {int(value)}/{int(maximum)} 步" if maximum > 0 else base_label
                    await emit(
                        stage_progress(workflow, node, value, maximum),
                        step_label,
                    )
                elif msg_type == "executing" and (not data.get("prompt_id") or data.get("prompt_id") == prompt_id):
                    node = data.get("node")
                    if node is None:
                        if await check_history():
                            return
                        continue
                    active_node = node
                    await emit(stage_progress(workflow, node), workflow_node_label(workflow, node))
                elif msg_type in {"execution_error", "execution_interrupted"}:
                    if not data.get("prompt_id") or data.get("prompt_id") == prompt_id:
                        raise RuntimeError(data.get("exception_message") or data.get("message") or msg_type)
                elif msg_type == "execution_success" and data.get("prompt_id") == prompt_id:
                    if await check_history():
                        return
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
                    raise RuntimeError("Colab/ComfyUI restarted; the original Scene image task is gone")
            elif presence is True:
                missing_checks = 0
            await asyncio.sleep(1.5)
        raise RuntimeError(f"Timed out waiting for {model_label} image generation (15 min)")


async def run_scene_image_job(job_id: int) -> None:
    job = get_scene_image_job(job_id)
    # 重复点「重新生成」时旧排队记录已被摘掉。如果数据库里这一项已经不再处于
    # 排队的 QUEUED 状态，说明它是过期记录，直接跳过，避免同一项被跑两次。
    if job.get("status") != "QUEUED":
        print(f"[image-queue] skip stale job {job_id} (status={job.get('status')})")
        return
    rt.current_scene_image_job_id = job_id
    scene_id = int(job["scene_id"])
    scene = get_scene(scene_id)
    role = str(job["role"]).lower().strip()
    aspect_ratio = str(job["aspect_ratio"] or "9:16")
    seed = max(0, int(job["seed"] or 42))
    # 历史记录里已撤除的模型（如 qwen21）回退到默认生图模型。
    try:
        model = get_model("image", job.get("image_model"))
    except ValueError:
        model = get_model("image")
    adapter = get_adapter("image", model["id"])
    image_model = model["id"]
    gpu_model = model["gpu_key"]
    model_label = model["label"]
    prompt = str(job["prompt"] or "").strip()
    client = ComfyClient(get_settings()["comfy_url"])
    try:
        if not prompt:
            raise RuntimeError(f"{scene['name']} / {role} 还没有填写 Prompt")
        required_nodes = adapter.required_nodes(job["lora_mode"])
        already_verified = client.nodes_verified(required_nodes)
        prepare_label = f"准备 {model_label} 工作流" if already_verified else f"首次检查 {model_label} 环境"
        update_scene_image_job(job_id, status="PREPARING", progress=1, error="", current_node=prepare_label)
        started_job = get_scene_image_job(job_id)
        await hub.broadcast({
            "type": "scene_image_state", "job_id": job_id, "scene_id": scene_id,
            "role": role, "status": "PREPARING", "progress": 1, "node": prepare_label,
            "generation_started_at": started_job.get("generation_started_at", 0),
            "generation_finished_at": 0, "generation_elapsed_seconds": 0,
        })
        if not already_verified:
            await client.require_nodes(required_nodes)
        width, height = adapter.image_size(model, aspect_ratio)
        workflow = read_json_file(model["workflow"])
        job_steps = int(job.get("steps") or 0)
        job_cfg = float(job.get("cfg") or 0.0)
        steps = adapter.build(
            workflow, model, prompt=prompt, width=width, height=height, seed=seed,
            lora_mode=job["lora_mode"],
            filename_prefix=f"{model.get('output_prefix') or 'anime_studio/images/' + image_model}_{scene_id:03d}_{role}",
            steps=job_steps, cfg=job_cfg,
        )
        actual_cfg = job_cfg if job_cfg > 0 else float(model.get("cfg") or 1.0)

        client_id = str(uuid.uuid4())
        # 生图、视频、生歌串行共用同一块远程 GPU；模型类型切换时才卸载显存。
        async with gpu_slot(gpu_model):
            async def _submit_image_prompt() -> str:
                return await client.submit(workflow, client_id)

            async def _run_image_prompt() -> None:
                pid = await _submit_image_prompt()
                update_scene_image_job(
                    job_id, status="RUNNING", progress=3, prompt_id=pid,
                    current_node=f"{model_label} task submitted",
                )
                await hub.broadcast({
                    "type": "scene_image_state", "job_id": job_id, "scene_id": scene_id,
                    "role": role, "status": "RUNNING", "progress": 3, "prompt_id": pid,
                    "node": f"{model_label} task submitted",
                })
                await monitor_scene_image_prompt(client, client_id, pid, job_id, workflow, adapter.stage_progress, model_label)

            await gpu_oom_retry(_run_image_prompt, label=f"Scene {scene_id} {role} 生图")
            prompt_id = str(get_scene_image_job(job_id).get("prompt_id") or "")

        update_scene_image_job(job_id, progress=98, current_node="Downloading image to Windows")
        await hub.broadcast({
            "type": "scene_image_progress", "job_id": job_id, "scene_id": scene_id,
            "role": role, "progress": 98, "node": "Downloading image to Windows",
        })

        history = await client.history(prompt_id)
        image_meta = image_meta_from_history(history, prompt_id)
        if not image_meta:
            raise RuntimeError(f"{model_label} 已完成，但 history 中没有找到图片输出")
        image_dir = ensure_scene_image_dir(scene)
        suffix = Path(str(image_meta.get("filename") or "image.png")).suffix.lower() or ".png"
        out_path = image_dir / f"{image_model}_{role}_{uuid.uuid4().hex[:10]}{suffix}"

        last_reported_download_ts = 0.0

        async def report_download(received: int, total: int) -> None:
            nonlocal last_reported_download_ts
            now_ts = time.monotonic()
            if (now_ts - last_reported_download_ts < 0.25) and (not total or received < total):
                return
            last_reported_download_ts = now_ts
            if total > 0:
                ratio = max(0.0, min(1.0, received / total))
                progress = 98.0 + ratio * 1.8
                node = f"下载图片到 Windows · {received / 1048576:.2f}/{total / 1048576:.2f} MB"
            else:
                progress = 98.0
                node = f"下载图片到 Windows · {received / 1048576:.2f} MB"
            await hub.broadcast({
                "type": "scene_image_progress", "job_id": job_id, "scene_id": scene_id,
                "role": role, "progress": progress, "node": node,
            })

        await client.download_view(image_meta, out_path, progress_cb=report_download)

        conn = db()
        cur = conn.execute(
            """
            INSERT INTO scene_images(scene_id,role,prompt,aspect_ratio,width,height,steps,cfg,seed,image_model,prompt_id,output_path,applied_count)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,0)
            """,
            (scene_id, role, prompt, aspect_ratio, width, height, steps, actual_cfg, seed, image_model, prompt_id, str(out_path)),
        )
        image_id = int(cur.lastrowid)
        conn.commit()
        conn.close()

        # Automatically apply newly generated image to matching non-running CUT shots.
        applied, applied_scene_ids = apply_scene_image_to_matching_cut_shots(
            scene_id, out_path, role, prompt, replace_existing=True
        )
        conn = db()
        conn.execute("UPDATE scene_images SET applied_count=? WHERE id=?", (applied, image_id))
        conn.commit()
        conn.close()
        update_scene_image_job(job_id, status="DONE", progress=100, current_node="", image_id=image_id)
        done_job = get_scene_image_job(job_id)
        conn = db()
        conn.execute(
            "UPDATE scene_images SET elapsed_seconds=? WHERE id=?",
            (float(done_job.get("generation_elapsed_seconds") or 0.0), image_id),
        )
        conn.commit()
        conn.close()
        await hub.broadcast({
            "type": "scene_image_state", "job_id": job_id, "scene_id": scene_id,
            "role": role, "status": "DONE", "progress": 100, "image_id": image_id,
            "applied_count": applied, "applied_scene_ids": applied_scene_ids,
            "generation_started_at": done_job.get("generation_started_at", 0),
            "generation_finished_at": done_job.get("generation_finished_at", 0),
            "generation_elapsed_seconds": done_job.get("generation_elapsed_seconds", 0),
        })
    except Exception as exc:
        update_scene_image_job(job_id, status="FAILED", error=str(exc), current_node="")
        failed_job = get_scene_image_job(job_id)
        await hub.broadcast({
            "type": "scene_image_state", "job_id": job_id, "scene_id": scene_id,
            "role": role, "status": "FAILED", "error": str(exc),
            "generation_started_at": failed_job.get("generation_started_at", 0),
            "generation_finished_at": failed_job.get("generation_finished_at", 0),
            "generation_elapsed_seconds": failed_job.get("generation_elapsed_seconds", 0),
        })
    finally:
        rt.current_scene_image_job_id = None


def ensure_scene_image_slot(scene_id: int, role: str) -> int:
    role = role.lower().strip()
    if role not in REF_ROLE_COLUMNS:
        raise RuntimeError("Image role must be ref1/ref2/ref3/ref4")
    get_scene(scene_id)
    ensure_scene_image_configs(scene_id)
    conn = db()
    latest = conn.execute(
        "SELECT id FROM scene_image_jobs WHERE scene_id=? AND role=? ORDER BY id DESC LIMIT 1",
        (scene_id, role),
    ).fetchone()
    if latest:
        conn.close()
        return int(latest["id"])
    config = conn.execute(
        "SELECT * FROM scene_image_configs WHERE scene_id=? AND role=?",
        (scene_id, role),
    ).fetchone()
    cur = conn.execute(
        """
        INSERT INTO scene_image_jobs(scene_id,role,prompt,aspect_ratio,seed,steps,cfg,image_model,lora_mode,replace_existing,status,progress,current_node,error)
        VALUES(?,?,?,?,?,?,?,?,?,0,'DRAFT',0,'等待填写参数','')
        """,
        (
            scene_id,
            role,
            str(config["prompt"] or "") if config else "",
            str(config["aspect_ratio"] or "9:16") if config else "9:16",
            int(config["seed"] or 42) if config else 42,
            int(config["steps"] or 0) if config else 0,
            float(config["cfg"] or 0.0) if config else 0.0,
            str(config["image_model"] or "moss_image_5b") if config else "moss_image_5b",
            str(config["lora_mode"]) if config else "all",
        ),
    )
    job_id = int(cur.lastrowid)
    conn.commit()
    conn.close()
    return job_id


async def enqueue_scene_image_job(
    scene_id: int,
    role: str,
    *,
    replace_existing: bool = False,
    only_missing: bool = False,
) -> tuple[int | None, str]:
    role = role.lower().strip()
    if role not in REF_ROLE_COLUMNS:
        raise RuntimeError("Image role must be ref1/ref2/ref3/ref4")
    get_scene(scene_id)
    ensure_scene_image_configs(scene_id)
    scene_id = ref_owner_scene_id(scene_id, role)
    conn = db()
    config = conn.execute(
        "SELECT * FROM scene_image_configs WHERE scene_id=? AND role=?",
        (scene_id, role),
    ).fetchone()
    if not config:
        conn.close()
        return None, "no_prompt"
    prompt = str(config["prompt"] or "").strip()
    if not prompt:
        conn.close()
        return None, "no_prompt"
    active = conn.execute(
        "SELECT id FROM scene_image_jobs WHERE scene_id=? AND role=? AND status IN ('QUEUED','PREPARING','RUNNING') ORDER BY id DESC LIMIT 1",
        (scene_id, role),
    ).fetchone()
    if active:
        conn.close()
        return int(active["id"]), "already_active"
    if only_missing:
        existing = conn.execute(
            "SELECT id FROM scene_images WHERE scene_id=? AND role=? ORDER BY id DESC LIMIT 1",
            (scene_id, role),
        ).fetchone()
        if existing:
            conn.close()
            return None, "existing_image"
    latest = conn.execute(
        "SELECT id,status FROM scene_image_jobs WHERE scene_id=? AND role=? ORDER BY id DESC LIMIT 1",
        (scene_id, role),
    ).fetchone()
    if latest:
        job_id = int(latest["id"])
        conn.execute(
            """
            UPDATE scene_image_jobs
            SET prompt=?, aspect_ratio=?, seed=?, steps=?, cfg=?, image_model=?, lora_mode=?, replace_existing=?, status='QUEUED', progress=0,
                current_node='Waiting in queue', prompt_id='', error='', image_id=NULL,
                generation_started_at=0, generation_finished_at=0, generation_elapsed_seconds=0, updated_at=CURRENT_TIMESTAMP
            WHERE id=?
            """,
            (
                prompt,
                str(config["aspect_ratio"] or "9:16"),
                int(config["seed"] or 42),
                int(config["steps"] or 0),
                float(config["cfg"] or 0.0),
                str(config["image_model"] or "moss_image_5b"),
                config["lora_mode"],
                1 if replace_existing else 0,
                job_id,
            ),
        )
    else:
        cur = conn.execute(
            """
            INSERT INTO scene_image_jobs(scene_id,role,prompt,aspect_ratio,seed,steps,cfg,image_model,lora_mode,replace_existing,status,progress,current_node)
            VALUES(?,?,?,?,?,?,?,?,?,?, 'QUEUED', 0, 'Waiting in queue')
            """,
            (
                scene_id, role, prompt, str(config["aspect_ratio"] or "9:16"),
                int(config["seed"] or 42), int(config["steps"] or 0), float(config["cfg"] or 0.0),
                str(config["image_model"] or "moss_image_5b"),
                config["lora_mode"],
                1 if replace_existing else 0,
            ),
        )
        job_id = int(cur.lastrowid)
    conn.commit()
    conn.close()
    await scene_image_queue.put(job_id)
    await hub.broadcast({
        "type": "scene_image_state", "job_id": job_id, "scene_id": scene_id,
        "role": role, "status": "QUEUED", "progress": 0,
    })
    return job_id, "queued"


def scene_image_dashboard(project_id: int) -> dict[str, Any]:
    conn = db()
    scenes = conn.execute(
        "SELECT * FROM scenes WHERE project_id=? ORDER BY sort_order,id",
        (project_id,),
    ).fetchall()
    result: list[dict[str, Any]] = []
    for scene_row in scenes:
        scene_id = int(scene_row["id"])
        conn.close()
        ensure_scene_image_configs(scene_id)
        conn = db()
        configs = [dict(r) for r in conn.execute(
            "SELECT * FROM scene_image_configs WHERE scene_id=? ORDER BY role", (scene_id,)
        ).fetchall()]
        roles: dict[str, Any] = {}
        for config in configs:
            role = str(config["role"])
            owner_id = ref_owner_scene_id(scene_id, role)
            config["source_scene_id"] = owner_id
            latest_image = conn.execute(
                "SELECT * FROM scene_images WHERE scene_id=? AND role=? ORDER BY id DESC LIMIT 1",
                (owner_id, role),
            ).fetchone()
            latest_job = conn.execute(
                "SELECT * FROM scene_image_jobs WHERE scene_id=? AND role=? ORDER BY id DESC LIMIT 1",
                (owner_id, role),
            ).fetchone()
            count = conn.execute(
                "SELECT COUNT(*) AS c FROM scene_images WHERE scene_id=? AND role=?",
                (owner_id, role),
            ).fetchone()["c"]
            roles[role] = {
                "config": config,
                "latest_image": scene_image_from_row(latest_image) if latest_image else None,
                "latest_job": dict(latest_job) if latest_job else None,
                "history_count": int(count),
            }
        result.append({**dict(scene_row), "roles": roles})
    conn.close()
    return {"project_id": project_id, "scenes": result}


async def recover_scene_image_jobs() -> None:
    conn = db()
    running = conn.execute(
        "SELECT id FROM scene_image_jobs WHERE status IN ('PREPARING','RUNNING')"
    ).fetchall()
    queued = conn.execute(
        "SELECT id FROM scene_image_jobs WHERE status='QUEUED' ORDER BY id"
    ).fetchall()
    for row in running:
        conn.execute(
            "UPDATE scene_image_jobs SET status='FAILED', error='应用重启，中断了上次生图任务', current_node='', updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (int(row["id"]),),
        )
    conn.commit()
    conn.close()
    for row in queued:
        await scene_image_queue.put(int(row["id"]))


async def scene_image_queue_worker() -> None:
    while True:
        job_id = await scene_image_queue.get()
        try:
            # A deferred CONTINUE in the video queue is not GPU work.
            # Only active work has priority; gpu_slot serializes actual submissions.
            while rt.current_shot_id is not None:
                await asyncio.sleep(0.5)
            task = asyncio.create_task(run_scene_image_job(job_id))
            rt.active_scene_image_task = task
            try:
                await asyncio.wait({task})
            except asyncio.CancelledError:
                task.cancel()
                raise
            finally:
                if rt.active_scene_image_task is task:
                    rt.active_scene_image_task = None
            if not task.cancelled():
                failure = task.exception()
                if failure:
                    raise failure
        except Exception as exc:
            # 队列中的槽位如果刚好被删除，跳过该失效 ID，worker 继续服务后续任务。
            print(f"[image-queue] skip job {job_id}: {exc}")
        finally:
            scene_image_queue.task_done()


async def prepare_image_job_regeneration(job_id: int) -> dict[str, Any]:
    """「重新生成」某个参考位的第 1~5 步。旧图片不会被删除。"""
    job = get_scene_image_job(job_id)
    old_prompt_id = str(job.get("prompt_id") or "")
    client = ComfyClient(get_settings()["comfy_url"])
    interrupted = await _interrupt_remote_prompt(client, old_prompt_id)

    rt.current_scene_image_job_id = await _cancel_active_item(
        rt.active_scene_image_task, rt.current_scene_image_job_id, job_id
    )
    removed = await _remove_from_queue(scene_image_queue, job_id)

    update_scene_image_job(
        job_id,
        status="PENDING",
        progress=0,
        current_node="准备重新提交",
        prompt_id="",
        error="",
        image_id=None,
    )
    return {"removed_from_queue": removed, "interrupted_remote": interrupted}
