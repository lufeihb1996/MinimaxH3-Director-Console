"""视频生成：按模型适配器准备工作流、提交、监控进度、下载收尾、队列 worker 与恢复。"""
from __future__ import annotations

import asyncio
import json
import math
import re
import uuid
import websockets
from fastapi import HTTPException
from pathlib import Path
from typing import Any

from backend.core.comfy import ComfyClient, _interrupt_remote_prompt, comfy_prompt_presence, history_terminal_state, set_workflow_value, workflow_node_class, workflow_node_inputs, workflow_node_label
from backend.core.db import db, get_settings, read_json_file
from backend.core.gpu import gpu_oom_retry, gpu_slot
from backend.core.media import dims_from_mp, extract_last_frame, frame_count, media_duration_seconds, stabilize_continue_video, strip_video_metadata
from backend.core.ns import namespace
from backend.core.runtime import hub, job_queue, recovery_tasks, rt
from backend.core.tasks import _cancel_active_item, _remove_from_queue
from backend.models.video import select_final_video, video_adapter, video_model, video_workflow_paths
from backend.services.continuation import auto_wait_reason, resolve_auto_input
from backend.services.native_audio import REQUIRED_NODES as NATIVE_AUDIO_NODES, finish_native_video, native_snapshot, wire_native_audio
from backend.services.repo import ensure_shot_dir, get_shot, update_shot_state

REALISM_TRIGGER_WORD = "r34l1sm"


def ensure_realism_trigger(prompt: str) -> str:
    """把 fal Realism People 的触发词顶到 prompt 最前面；已经手写过就不重复加。"""
    text = (prompt or "").strip()
    if not text:
        return REALISM_TRIGGER_WORD
    if re.search(r"\br34l1sm\b", text, re.IGNORECASE):
        return text
    return f"{REALISM_TRIGGER_WORD}, {text}"


def compose_prompt(shot: dict[str, Any]) -> str:
    text = (shot.get("prompt") or "").strip()
    if not text:
        return ""
    if str(shot.get("mode") or "CUT").upper() != "T2V":
        text = re.sub(r"\bthe provided image\b", "<Picture 1>", text, flags=re.IGNORECASE)
        if not re.search(r"<\s*Picture\s*1\s*>", text, re.IGNORECASE):
            text = f"Keep the character's exact facial features, hair, body, and outfit strictly consistent with <Picture 1>. {text}"
    if not re.search(r"\bno\s+(?:subtitles|captions|on-screen\s+text)\b", text, re.IGNORECASE):
        text = text.rstrip(".") + ". No subtitles, no captions, no on-screen text."
    return text


async def prepare_workflow(shot: dict[str, Any], client: ComfyClient) -> dict[str, Any]:
    # 模型目录统一管理模式、参数与模板路径。
    settings = get_settings()
    is_continue = shot['mode'].upper() == 'CONTINUE'
    is_t2v = shot['mode'].upper() == 'T2V'
    wf_path, bindings_path = video_workflow_paths(shot, settings)
    template_dir = Path(shot["queued_input_path"]).parent if shot.get("queued_input_path") else None
    if template_dir:
        if not (template_dir / 'template.json').is_file() or not (template_dir / 'bindings.json').is_file():
            raise RuntimeError('排队任务的工作流快照缺失，请重新生成以建立新快照；不会改用当前模板')
        workflow = json.loads((template_dir / "template.json").read_text(encoding="utf-8"))
        bindings = json.loads((template_dir / "bindings.json").read_text(encoding="utf-8"))
    else:
        workflow = read_json_file(wf_path)
        bindings = read_json_file(bindings_path)
    field_bindings = bindings.get("fields", {})
    model = video_model(shot.get("video_model"))
    adapter = video_adapter(model["id"])
    adapter.prepare_template(workflow, shot, model)

    width, height = dims_from_mp(adapter.render_megapixels(shot, model), shot["aspect_ratio"])
    fps = int(shot.get("input_fps") or settings.get("fps", "24") or 24)
    requested_seconds = float(shot.get("target_duration") or 0) or float(shot["seconds"])
    length = frame_count(math.ceil(requested_seconds*fps)/fps if shot.get("native_song_audio") else requested_seconds, fps)

    prompt = compose_prompt(shot)
    if adapter.wants_realism_trigger(shot):
        prompt = ensure_realism_trigger(prompt)
    set_workflow_value(workflow, field_bindings.get("prompt"), prompt)
    set_workflow_value(workflow, field_bindings.get("width"), width)
    set_workflow_value(workflow, field_bindings.get("height"), height)
    set_workflow_value(workflow, field_bindings.get("length"), length)
    set_workflow_value(workflow, field_bindings.get("megapixels"), float(shot["megapixels"]))
    ratio_labels = {
        "9:16": "9:16 (Portrait)",
        "16:9": "16:9 (Widescreen)",
        "1:1": "1:1 (Square)",
    }
    set_workflow_value(workflow, field_bindings.get("aspect_ratio"), ratio_labels.get(shot["aspect_ratio"], shot["aspect_ratio"]))
    set_workflow_value(workflow, field_bindings.get("seconds"), requested_seconds)
    set_workflow_value(workflow, field_bindings.get("steps"), adapter.sampler_steps(shot, model))
    if int(shot["seed"]) > 0:
        set_workflow_value(workflow, field_bindings.get("seed"), int(shot["seed"]))

    adapter.finalize(workflow, shot)
    for node in workflow.values():
        if node.get('class_type') == 'CreateVideo':
            node['inputs']['fps'] = fps

    pass2_only_fn = getattr(adapter, 'prepare_single_video_pass2', None)
    if pass2_only_fn and await pass2_only_fn(workflow, shot, client, fps):
        final_output = (shot.get('video_profile') or video_model(shot.get('video_model')))['output_node']
        if final_output not in workflow:
            raise RuntimeError(f'Workflow is missing the final output node: {final_output}')
        workflow[final_output].setdefault('_meta', {})['studio_final_output'] = True
        if shot.get('queued_input_path'):
            snapshot_dir = Path(shot['queued_input_path']).parent
            (snapshot_dir / 'workflow.json').write_text(json.dumps(workflow, ensure_ascii=False, indent=2), encoding='utf-8')
        return workflow

    uploads: dict[str, str] = {}
    image_fields = [] if is_t2v else (["ref1_path", "first_frame_path"] if is_continue else ["ref1_path", "ref2_path", "ref3_path", "ref4_path", "first_frame_path"])
    for field in image_fields:
        p = (shot.get(field) or "").strip()
        if p:
            local = Path(p)
            if local.exists():
                uploads[field] = await client.upload_image(local)

    if is_continue and "first_frame_path" not in uploads:
        raise RuntimeError("请先选择接续帧，或手动指定首帧；CONTINUE 不再盲取上一镜末帧")

    map_names = {
        "ref1_path": "ref1",
        "ref2_path": "ref2",
        "ref3_path": "ref3",
        "ref4_path": "ref4",
        "first_frame_path": "first_frame",
    }
    for src, dst in map_names.items():
        if src in uploads:
            set_workflow_value(workflow, field_bindings.get(dst), uploads[src])

    # CUT / Ref2VA 支持动态扩展多参考图：保留官方第一个 LoadImage，
    # ref2~ref4 在提交前临时克隆 LoadImage，并挂到 ReferenceToVideo 的递增插槽。
    dynamic_refs = bindings.get("dynamic_refs")
    if dynamic_refs:
        target_id = str(dynamic_refs.get("target_node", ""))
        base_load_id = str(dynamic_refs.get("base_load_node", ""))
        prefix = str(dynamic_refs.get("input_prefix", "ref_images.ref_image_"))
        if not is_continue and "ref1_path" not in uploads:
            raise RuntimeError("CUT / Ref2VA 至少需要上传 Ref1 角色或场景参考图")
        if target_id not in workflow or base_load_id not in workflow:
            raise RuntimeError("Ref2VA 动态参考图绑定配置不正确")
        if 'ref1_path' in uploads:
            workflow[base_load_id].setdefault('inputs', {})['image'] = uploads['ref1_path']
            workflow[target_id].setdefault('inputs', {})[prefix + '0'] = [base_load_id, 0]
            reference_index = 1
        elif is_continue and 'first_frame_path' in uploads:
            # CONTINUE without a separate Ref1 wires the starting frame (node 902) as <Picture 1> into
            # MiniMaxH3ReferenceToVideo so Qwen3-VL-32B and minimax_refs lock onto the exact previous-shot end frame.
            ff_node_id = str((field_bindings.get('first_frame') or {}).get('node') or '902')
            workflow[target_id].setdefault('inputs', {})[prefix + '0'] = [ff_node_id, 0]
            del workflow[base_load_id]
            reference_index = 1
        else:
            workflow[target_id].setdefault('inputs', {}).pop(prefix + '0', None)
            del workflow[base_load_id]
            reference_index = 0
        used_ids = {int(k) for k in workflow.keys() if str(k).isdigit()}
        next_id = max(used_ids or {900}) + 1
        for idx, src in enumerate(("ref2_path", "ref3_path", "ref4_path"), start=1):
            if src not in uploads:
                continue
            while next_id in used_ids:
                next_id += 1
            node_id = str(next_id)
            workflow[node_id] = {
                "class_type": "LoadImage",
                "inputs": {"image": uploads[src]},
                "_meta": {"title": f"AI Anime Studio Reference {idx + 1}"},
            }
            workflow[target_id]["inputs"][prefix + str(reference_index)] = [node_id, 0]
            reference_index += 1
            used_ids.add(next_id)
            next_id += 1

    if shot.get("native_song_audio"):
        await client.require_nodes(NATIVE_AUDIO_NODES)
        voice = await client.upload_input_file(Path(shot["native_vocals_path"]), "anime_studio/mv")
        wire_native_audio(workflow, voice)

    final_output = (shot.get('video_profile') or video_model(shot.get('video_model')))['output_node']
    if final_output not in workflow:
        raise RuntimeError(f'Workflow is missing the final output node: {final_output}')
    workflow[final_output].setdefault('_meta', {})['studio_final_output'] = True

    if shot.get("queued_input_path"):
        snapshot_dir = Path(shot["queued_input_path"]).parent
        (snapshot_dir / "workflow.json").write_text(json.dumps(workflow, ensure_ascii=False, indent=2), encoding="utf-8")
    return workflow


async def finalize_output(shot: dict[str, Any], client: ComfyClient, prompt_id: str, output_node: str | None = None) -> tuple[str, str]:
    update_shot_state(shot["id"], progress=98, current_node="Cloud finished; receiving video")
    await hub.broadcast({"type": "shot_progress", "shot_id": shot["id"], "progress": 98, "node": "Cloud finished; receiving video"})
    hist = await client.history(prompt_id)
    entry = hist.get(prompt_id) or next(iter(hist.values()), {})
    outputs = entry.get("outputs", {}) if isinstance(entry, dict) else {}
    # Recovery reads the exact workflow saved at submission, independent of later edits.
    if output_node is None:
        snapshot = str(shot.get('queued_input_path') or shot.get('completed_input_path') or '')
        workflow_path = Path(snapshot).parent / 'workflow.json' if snapshot else None
        saved_workflow = json.loads(workflow_path.read_text(encoding='utf-8')) if workflow_path and workflow_path.is_file() else {}
        output_node = next((key for key, node in saved_workflow.items()
                            if node.get('_meta', {}).get('studio_final_output')), None)
        if output_node is None and '92' in outputs:
            output_node = '92'
    video_meta = select_final_video(outputs, output_node)

    shot_dir = ensure_shot_dir(shot)
    ext = Path(video_meta.get("filename", "result.mp4")).suffix or ".mp4"
    version = 1
    conn = db()
    row = conn.execute("SELECT COALESCE(MAX(version),0) AS v FROM generations WHERE shot_id=?", (shot["id"],)).fetchone()
    if row:
        version = int(row["v"]) + 1
    conn.close()
    out_path = shot_dir / f"shot_{shot['shot_no']:04d}_v{version}{ext}"
    last_vid_dl_ts = 0.0

    async def report_video_download(received: int, total: int) -> None:
        nonlocal last_vid_dl_ts
        now_ts = asyncio.get_running_loop().time()
        if (now_ts - last_vid_dl_ts < 0.25) and (not total or received < total):
            return
        last_vid_dl_ts = now_ts
        if total > 0:
            ratio = max(0.0, min(1.0, received / total))
            progress = 98.0 + ratio * 1.5
            node = f"下载成片到 Windows · {received / 1048576:.2f}/{total / 1048576:.2f} MB"
        else:
            progress = 98.0
            node = f"下载成片到 Windows · {received / 1048576:.2f} MB"
        await hub.broadcast({"type": "shot_progress", "shot_id": shot["id"], "progress": progress, "node": node})

    await client.download_view(video_meta, out_path, progress_cb=report_video_download)

    # MV 原声驱动镜头：云端只出画面，下载后在本地配回冻结的完整原歌片段。
    snapshot = native_snapshot(shot)
    if snapshot:
        update_shot_state(int(shot["id"]), current_node="Muxing the full original song")
        await hub.broadcast({"type":"shot_progress", "shot_id":shot["id"], "progress":99, "node":"Muxing the full original song"})
        out_path = await asyncio.to_thread(finish_native_video, out_path, snapshot)
    else:
        out_path = await asyncio.to_thread(strip_video_metadata, out_path)

    from backend.models.adapters.video_b15 import find_pass1_video
    frame_src = (find_pass1_video(shot) or out_path) if bool(int(shot.get("second_pass_enabled") or 0)) else out_path
    last_path = shot_dir / f"shot_{shot['shot_no']:04d}_v{version}_last.png"
    last_frame_path = str(last_path) if extract_last_frame(frame_src, last_path) else ""

    conn = db()
    conn.execute(
        "INSERT INTO generations(shot_id,version,prompt_id,status,output_path,last_frame_path) VALUES(?,?,?,?,?,?)",
        (shot["id"], version, prompt_id, "DONE", str(out_path), last_frame_path),
    )
    conn.commit()
    conn.close()
    return str(out_path), last_frame_path


def video_progress_label(workflow: dict[str, Any], node: Any, value: float, maximum: float) -> str:
    label = workflow_node_label(workflow, node)
    if workflow_node_class(workflow, node) == 'SamplerCustomAdvanced' and maximum > 0:
        completed = max(0, min(int(value), int(maximum)))
        return f'{label} · {completed}/{int(maximum)}步'
    return label


def overall_progress_for_event(workflow: dict[str, Any], node: Any, value: float, maximum: float) -> float:
    """把 ComfyUI 节点内部进度映射为整个 Shot 的总进度。

    LTX-2.5 同时兼容旧视觉单阶段 KSampler 和两阶段插件工作流。
    当前 MV 默认走旧视觉单阶段路线；保留两阶段映射用于兼容其他 workflow。
    """
    ratio = max(0.0, min(1.0, value / (maximum or 1.0)))
    class_type = workflow_node_class(workflow, node)
    if 'b15_refine' in workflow:
        node_id = str(node)
        if 'b15_load_video' in workflow and '125' not in workflow:
            if node_id == 'b15_upscale':
                return 20.0 + ratio * 15.0
            if node_id == 'b15_refine':
                return 40.0 + ratio * 45.0
            return post_sampler_progress(workflow, node) or 0.0
        if node_id == '125':
            return 5.0 + ratio * 50.0
        if node_id == 'b15_refine':
            return 65.0 + ratio * 20.0
        if node_id == 'b15_upscale':
            return 56.0 + ratio * 8.0
        # Non-sampler progress must not masquerade as 0-85% sampling.
        return post_sampler_progress(workflow, node) or 0.0

    if class_type == "LTXV25KSampler":
        schedule = str(workflow_node_inputs(workflow, node).get("schedule") or "").lower()
        if "refine" in schedule:
            return 72.0 + ratio * 18.0  # 72% -> 90%
        return 10.0 + ratio * 45.0  # distilled: 10% -> 55%

    if class_type == "LTXV25LatentUpscale":
        return 62.0 + ratio * 8.0  # 62% -> 70%

    if class_type == "KSampler":
        return 10.0 + ratio * 75.0  # old visual route: 10% -> 85%

    if class_type == "SamplerCustomAdvanced":
        return 5.0 + ratio * 80.0  # 5% -> 85%

    return min(85.0, ratio * 85.0)


def post_sampler_progress(workflow: dict[str, Any], node: Any) -> float | None:
    if 'b15_refine' in workflow:
        if 'b15_load_video' in workflow and '125' not in workflow:
            single_stages = {
                'b15_load_video': 5.0, 'b15_video_comp': 8.0, 'b15_prep_draft': 10.0,
                'b15_vae_encode': 14.0, 'b15_upscale': 20.0, 'b15_concat': 36.0,
                'b15_regen_lora': 38.0, 'b15_text_cond': 38.0, 'b15_guider': 38.0,
                'b15_sampler': 38.0, 'b15_noise': 38.0, 'b15_schedule': 38.0,
                'b15_refine': 40.0, '122': 90.0, 'b15_finish': 95.0, '130': 96.0, '92': 98.0,
            }
            if str(node) in single_stages:
                return single_stages[str(node)]
        stages = {'125': 5.0, 'b15_split': 55.0, 'b15_upscale': 56.0,
                  'b15_concat': 64.0, 'b15_regen_lora': 64.0, 'b15_guide': 64.0, 'b15_guider': 64.0, 'b15_sampler': 64.0, 'b15_noise': 64.0, 'b15_schedule': 64.0,
                  'b15_refine': 65.0, '122': 90.0, '121': 90.0, '130': 96.0, '92': 98.0}
        if str(node) in stages:
            return stages[str(node)]
    class_type = workflow_node_class(workflow, node)

    if class_type == "LTXV25KSampler":
        schedule = str(workflow_node_inputs(workflow, node).get("schedule") or "").lower()
        return 72.0 if "refine" in schedule else 10.0

    if class_type == "KSampler":
        return 10.0

    floors = {
        "LTXV25VidToVideo": 8.0,
        "LTXV25CropVideoGuide": 58.0,
        "LatentUpscaleModelLoader": 60.0,
        "LTXV25LatentUpscale": 62.0,
        "LTXV25AVDecode": 92.0,
        "VAEDecode": 90.0,
        "VAEDecodeAudio": 90.0,
        "CreateVideo": 96.0,
        "SaveVideo": 98.0,
    }
    return floors.get(class_type)


async def monitor_prompt(
    client: ComfyClient,
    client_id: str,
    prompt_id: str,
    shot_id: int,
    workflow: dict[str, Any],
) -> None:
    uri = client.ws_base + f"/ws?clientId={client_id}"
    deadline = asyncio.get_running_loop().time() + 900
    last_history_check = 0.0
    active_node = None
    last_progress = 0.0
    two_pass = 'b15_refine' in workflow

    async def check_history() -> bool:
        terminal, error = await history_terminal_state(client, prompt_id)
        if not terminal:
            return False
        if error:
            raise RuntimeError(error)
        return True

    try:
        async with websockets.connect(uri, max_size=32 * 1024 * 1024, open_timeout=20) as ws:
            while True:
                now = asyncio.get_running_loop().time()
                if now >= deadline:
                    raise RuntimeError("Timed out waiting for ComfyUI (15 min)")
                # Heartbeats/other events must not postpone completion checks forever.
                if now - last_history_check >= 3.0:
                    last_history_check = now
                    if await check_history():
                        return
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=1.5)
                except asyncio.TimeoutError:
                    if await check_history():
                        return
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
                    if data.get('prompt_id') and data['prompt_id'] != prompt_id:
                        continue
                    node = data.get('node') or active_node
                    value = float(data.get("value", 0))
                    maximum = float(data.get("max", 1) or 1)
                    progress = overall_progress_for_event(workflow, node, value, maximum)
                    if two_pass:
                        if progress < last_progress:
                            continue  # Delayed step events must not roll the step label back either.
                        last_progress = progress
                    node_label = video_progress_label(workflow, node, value, maximum)
                    update_shot_state(shot_id, progress=progress, current_node=node_label)
                    await hub.broadcast({"type": "shot_progress", "shot_id": shot_id, "progress": progress, "node": node_label})
                elif msg_type == "executing":
                    if data.get("prompt_id") == prompt_id:
                        node = data.get("node")
                        if node is None:
                            # executing(node=None) 可能比 /history 落盘更早，等 history 确认后再结束监控。
                            if await check_history():
                                return
                            continue
                        active_node = node
                        node_label = workflow_node_label(workflow, node)
                        stage_progress = post_sampler_progress(workflow, node)
                        if stage_progress is None:
                            update_shot_state(shot_id, current_node=node_label)
                            await hub.broadcast({"type": "shot_node", "shot_id": shot_id, "node": node_label})
                        else:
                            if two_pass:
                                stage_progress = max(last_progress, stage_progress)
                                last_progress = stage_progress
                            update_shot_state(shot_id, progress=stage_progress, current_node=node_label)
                            await hub.broadcast({"type": "shot_progress", "shot_id": shot_id, "progress": stage_progress, "node": node_label})
                elif msg_type in {"execution_error", "execution_interrupted"}:
                    if not data.get("prompt_id") or data.get("prompt_id") == prompt_id:
                        raise RuntimeError(data.get("exception_message") or data.get("message") or msg_type)
                elif msg_type == "execution_success" and data.get("prompt_id") == prompt_id:
                    # Cloudflare/ComfyUI 下 history 偶尔会比 WS success 晚一个瞬间，避免误判“无输出”。
                    if await check_history():
                        return
    except RuntimeError:
        raise
    except Exception:
        # WebSocket 断开后退回 history + queue 轮询。Colab Runtime 如果被替换，
        # 新 ComfyUI 的 history/queue 都不会再认识旧 prompt_id；连续确认 3 次后
        # 立即结束僵尸任务，不再让 Windows UI 卡 15 分钟 RUNNING。
        missing_checks = 0
        while asyncio.get_running_loop().time() < deadline:
            if await check_history():
                return
            presence = await comfy_prompt_presence(client, prompt_id)
            if presence is False:
                missing_checks += 1
                if missing_checks >= 3:
                    raise RuntimeError("Colab/ComfyUI restarted; the original video task is gone")
            elif presence is True:
                missing_checks = 0
            await asyncio.sleep(1.5)
        raise RuntimeError("Timed out waiting for ComfyUI (15 min)")


async def run_shot(shot_id: int) -> None:
    shot = get_shot(shot_id)
    # 重复点「重新生成」时旧排队记录已被摘掉；数据库里不再是 QUEUED 说明它是过期记录。
    if str(shot.get("status") or "").upper() != "QUEUED":
        print(f"[video-queue] skip stale shot {shot_id} (status={shot.get('status')})")
        return
    input_path = str(shot.get("queued_input_path") or "")
    if input_path and Path(input_path).is_file():
        shot = json.loads(Path(input_path).read_text(encoding="utf-8"))["shot"]
        shot["queued_input_path"] = input_path
    previous_output_path = str(shot.get("output_path") or "")
    previous_last_frame_path = str(shot.get("last_frame_path") or "")
    rt.current_shot_id = shot_id
    settings = get_settings()
    client = ComfyClient(settings["comfy_url"])
    update_shot_state(shot_id, status="PREPARING", progress=0, error="", current_node="")
    await hub.broadcast({"type": "shot_state", "shot_id": shot_id, "status": "PREPARING", "generation_started_at": get_shot(shot_id)["generation_started_at"], "generation_finished_at": 0, "generation_elapsed_seconds": 0})
    try:
        if shot.get("auto_source_shot_id"):
            shot = await asyncio.to_thread(resolve_auto_input, shot, namespace())
        from backend.services.video_backends import external_backend_for
        backend = external_backend_for(shot)
        if backend is not None:
            output_path, last_frame_path = await backend.generate(shot, namespace())
        else:
            workflow = await prepare_workflow(shot, client)
            client_id = str(uuid.uuid4())
            # Original ComfyUI execution remains isolated from external API backends.
            async with gpu_slot((shot.get("video_profile") or video_model(shot.get("video_model")))["gpu_key"]):
                async def _run_video_prompt() -> None:
                    pid = await client.submit(workflow, client_id)
                    update_shot_state(shot_id, status="RUNNING", prompt_id=pid)
                    await hub.broadcast({"type": "shot_state", "shot_id": shot_id, "status": "RUNNING", "prompt_id": pid})
                    await monitor_prompt(client, client_id, pid, shot_id, workflow)

                await gpu_oom_retry(_run_video_prompt, label=f"Shot {shot_id} 视频")
                prompt_id = str(get_shot(shot_id).get("prompt_id") or "")
            update_shot_state(shot_id, progress=98, current_node="Downloading the final cut to Windows")
            await hub.broadcast({"type": "shot_progress", "shot_id": shot_id, "progress": 98, "node": "Downloading the final cut to Windows"})
            shot = get_shot(shot_id)
            final_node = next((key for key, node in workflow.items() if node.get('_meta', {}).get('studio_final_output')), None)
            output_path, last_frame_path = await finalize_output(shot, client, prompt_id, final_node)
        update_shot_state(
            shot_id,
            status="DONE",
            progress=100,
            current_node="",
            output_path=output_path,
            completed_input_path=input_path,
            last_frame_path=last_frame_path,
            source_duration=media_duration_seconds(Path(output_path)),
            face_refined_path="",
            face_refined_last_frame_path="",
            lip_sync_status="DONE" if native_snapshot(shot) else "IDLE",
            lip_sync_progress=100 if native_snapshot(shot) else 0,
            lip_sync_audio_path="",
            lip_sync_output_path=output_path if native_snapshot(shot) else "",
            lip_sync_last_frame_path=last_frame_path if native_snapshot(shot) else "",
            lip_sync_error="",
        )
        await hub.broadcast({"type": "shot_state", "shot_id": shot_id, "status": "DONE", "output_path": output_path})
    except Exception as exc:
        # 重新生成失败时旧成片仍然是有效资产。尤其 Colab Runtime 被回收后，
        # 不应让 Shot 永久卡在 RUNNING/FAILED 而把旧视频也变得不可用。
        if previous_output_path and Path(previous_output_path).is_file():
            update_shot_state(
                shot_id,
                status="DONE",
                progress=100,
                error=f"Regeneration failed; previous video kept: {exc}",
                current_node="",
                output_path=previous_output_path,
                last_frame_path=previous_last_frame_path,
            )
            await hub.broadcast({
                "type": "shot_state",
                "shot_id": shot_id,
                "status": "DONE",
                "output_path": previous_output_path,
                "error": f"Regeneration failed; previous video kept: {exc}",
                "notice": f"重新生成未完成，已保留旧视频: {exc}",
            })
        else:
            update_shot_state(shot_id, status="FAILED", error=str(exc), current_node="")
            await hub.broadcast({"type": "shot_state", "shot_id": shot_id, "status": "FAILED", "error": str(exc)})
    finally:
        rt.current_shot_id = None


async def recover_running_shot(shot_id: int, prompt_id: str) -> None:
    """后端 reload/restart 后继续接管已提交任务，并识别 Colab 重启留下的僵尸 RUNNING。"""
    settings = get_settings()
    client = ComfyClient(settings["comfy_url"])
    rt.current_shot_id = shot_id
    try:
        # 如果上次已经下载并写入 generations，只差 shots 状态没来得及更新，直接恢复。
        conn = db()
        existing = conn.execute(
            "SELECT output_path,last_frame_path FROM generations WHERE shot_id=? AND prompt_id=? AND status='DONE' ORDER BY id DESC LIMIT 1",
            (shot_id, prompt_id),
        ).fetchone()
        conn.close()
        if existing and existing["output_path"] and Path(existing["output_path"]).is_file():
            update_shot_state(
                shot_id,
                status="DONE",
                progress=100,
                current_node="",
                prompt_id="",
                error="",
                output_path=existing["output_path"],
                last_frame_path=existing["last_frame_path"] or "",
            )
            await hub.broadcast({"type": "shot_state", "shot_id": shot_id, "status": "DONE", "output_path": existing["output_path"]})
            return

        from backend.services.video_backends import external_backend_for
        backend = external_backend_for(get_shot(shot_id))
        if backend is not None:
            output_path, last_frame_path = await backend.recover(get_shot(shot_id), prompt_id, namespace())
            update_shot_state(shot_id, status="DONE", progress=100, current_node="", error="",
                              output_path=output_path, last_frame_path=last_frame_path,
                              completed_input_path=get_shot(shot_id).get("queued_input_path") or "",
                              source_duration=media_duration_seconds(Path(output_path)))
            await hub.broadcast({"type": "shot_state", "shot_id": shot_id, "status": "DONE", "output_path": output_path})
            return

        deadline = asyncio.get_running_loop().time() + 900
        missing_checks = 0
        while asyncio.get_running_loop().time() < deadline:
            terminal, error = await history_terminal_state(client, prompt_id)
            if terminal:
                if error:
                    raise RuntimeError(error)
                shot = get_shot(shot_id)
                output_path, last_frame_path = await finalize_output(shot, client, prompt_id)
                update_shot_state(
                    shot_id,
                    status="DONE",
                    progress=100,
                    current_node="",
                    prompt_id="",
                    error="",
                    output_path=output_path,
                    last_frame_path=last_frame_path,
                )
                await hub.broadcast({"type": "shot_state", "shot_id": shot_id, "status": "DONE", "output_path": output_path})
                return

            presence = await comfy_prompt_presence(client, prompt_id)
            if presence is False:
                missing_checks += 1
                if missing_checks >= 3:
                    shot = get_shot(shot_id)
                    old_output = Path(str(shot.get("output_path") or ""))
                    if old_output.is_file():
                        # 这是重新生成被 Colab 重启打断：保留旧成片并恢复为可操作状态。
                        update_shot_state(
                            shot_id,
                            status="DONE",
                            progress=100,
                            current_node="",
                            prompt_id="",
                            error="Colab restarted; regeneration incomplete, previous video kept",
                        )
                        await hub.broadcast({
                            "type": "shot_state",
                            "shot_id": shot_id,
                            "status": "DONE",
                            "output_path": str(old_output),
                            "error": "Colab restarted; regeneration incomplete, previous video kept",
                            "notice": "Colab 已重启，未完成的重新生成已取消，旧视频已保留",
                        })
                        return
                    raise RuntimeError("Colab/ComfyUI restarted; the original video task is gone")
            elif presence is True:
                missing_checks = 0
            await asyncio.sleep(1.5)
        raise RuntimeError("应用重启后等待原 ComfyUI 任务超时（15 分钟）")
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        shot = get_shot(shot_id)
        old_output = Path(str(shot.get("output_path") or ""))
        if old_output.is_file():
            update_shot_state(shot_id, status="DONE", progress=100, prompt_id="", error=f"Task recovery failed; previous video kept: {exc}", current_node="")
            await hub.broadcast({
                "type": "shot_state",
                "shot_id": shot_id,
                "status": "DONE",
                "output_path": str(old_output),
                "error": f"Task recovery failed; previous video kept: {exc}",
                "notice": f"恢复任务失败，已保留旧视频: {exc}",
            })
        else:
            update_shot_state(shot_id, status="FAILED", prompt_id="", error=f"Task recovery failed: {exc}", current_node="")
            await hub.broadcast({"type": "shot_state", "shot_id": shot_id, "status": "FAILED", "error": f"Task recovery failed: {exc}"})
    finally:
        if rt.current_shot_id == shot_id:
            rt.current_shot_id = None


async def recover_pending_jobs() -> None:
    """恢复 reload/restart 前还留在数据库里的排队或运行任务。"""
    conn = db()
    rows = conn.execute(
        "SELECT id,status,prompt_id FROM shots WHERE status IN ('QUEUED','PREPARING','RUNNING') ORDER BY scene_id,shot_no"
    ).fetchall()
    conn.close()

    for row in rows:
        shot_id = int(row["id"])
        status = str(row["status"])
        prompt_id = str(row["prompt_id"] or "").strip()
        if status == "RUNNING" and prompt_id:
            task = asyncio.create_task(recover_running_shot(shot_id, prompt_id), name=f"shot-recovery:{shot_id}")
            recovery_tasks.add(task)
            task.add_done_callback(recovery_tasks.discard)
        else:
            try:
                saved = str(get_shot(shot_id).get("queued_input_path") or "")
                update_shot_state(shot_id, status="QUEUED", progress=0, current_node="", queued_input_path=saved if saved and Path(saved).is_file() else "")
                await job_queue.put(shot_id)
            except HTTPException as exc:
                update_shot_state(shot_id, status="FAILED", error=str(exc.detail), current_node="")


async def queue_worker() -> None:
    while True:
        shot_id = await job_queue.get()
        try:
            # 视频、Scene 生图、FaceRefine 共用同一套远端 GPU；避免同时提交造成显存抢占。
            # 如果图片正在跑，等当前图片完成；之后视频优先，图片 worker 会看到 job_queue 后让行。
            while rt.current_scene_image_job_id is not None:
                await asyncio.sleep(0.5)
            # 单独起子任务，这样"重新生成"能取消一个卡死的生成，而不会把 worker 自己也带走。
            if get_shot(shot_id)["status"] == "CANCELLED":
                continue
            queued = get_shot(shot_id)
            saved = str(queued.get('queued_input_path') or '')
            if queued['status'] == 'QUEUED' and saved and Path(saved).is_file():
                frozen = json.loads(Path(saved).read_text(encoding='utf-8'))['shot']
                reason = auto_wait_reason(frozen, namespace())
                if reason:
                    if queued.get('current_node') != reason:
                        update_shot_state(shot_id, current_node=reason)
                        await hub.broadcast({'type': 'shot_progress', 'shot_id': shot_id, 'progress': 0, 'node': reason})
                    await job_queue.put(shot_id)
                    await asyncio.sleep(0.5)
                    continue
            task = asyncio.create_task(run_shot(shot_id))
            rt.active_shot_task = task
            try:
                # 用 wait 而不是直接 await：被取消时不会把 CancelledError 冒到 worker 上。
                await asyncio.wait({task})
            except asyncio.CancelledError:
                task.cancel()
                raise
            finally:
                if rt.active_shot_task is task:
                    rt.active_shot_task = None
            if not task.cancelled():
                failure = task.exception()
                if failure:
                    raise failure
        except Exception as exc:
            # 删除 Scene/Shot 与队列取任务存在极小竞态时，不能让整个 worker 因一个失效 ID 退出。
            print(f"[video-queue] skip shot {shot_id}: {exc}")
        finally:
            job_queue.task_done()


async def prepare_shot_regeneration(shot_id: int) -> dict[str, Any]:
    """「重新生成」某个 Shot 的第 1~5 步：清本地旧协程、旧排队、旧状态、旧远端任务。

    成功产物（output_path / last_frame_path）一律保留：新视频成功前它仍是当前版本。
    """
    conn = db()
    try:
        row = conn.execute("SELECT prompt_id,output_path,last_frame_path FROM shots WHERE id=?", (shot_id,)).fetchone()
    finally:
        conn.close()
    if not row:
        raise HTTPException(404, "Shot not found")

    old_prompt_id = str(row["prompt_id"] or "")
    from backend.services.video_backends import external_backend_for
    backend = external_backend_for(get_shot(shot_id))
    if backend is not None:
        interrupted = await backend.cancel(get_shot(shot_id), old_prompt_id) if old_prompt_id else False
    else:
        client = ComfyClient(get_settings()["comfy_url"])
        interrupted = await _interrupt_remote_prompt(client, old_prompt_id)

    rt.current_shot_id = await _cancel_active_item(rt.active_shot_task, rt.current_shot_id, shot_id)
    removed = await _remove_from_queue(job_queue, shot_id)

    update_shot_state(
        shot_id,
        status="QUEUED",
        progress=0,
        current_node="Waiting in queue",
        prompt_id="",
        error="",
        output_path=str(row["output_path"] or ""),
        last_frame_path=str(row["last_frame_path"] or ""),
    )
    return {"removed_from_queue": removed, "interrupted_remote": interrupted}
