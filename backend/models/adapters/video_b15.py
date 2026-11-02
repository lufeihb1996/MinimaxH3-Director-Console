"""H3 b15-49 采样适配器：支持仅一采（0.4MP/10步快速试片，默认）、纯单视频二采（基于已有 0.4MP 一采视频直接 2.0x 放大 + 3 步 Open-H3 1080p 精修，不引入上一镜末帧）与两轮连跑。"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from backend.core.db import db
from backend.core.media import media_has_audio, media_video_info
from backend.models.base import VideoAdapter, set_style_lora_strengths
from backend.models.registry import register
from backend.models.video import SECOND_PASS_RESOLUTIONS

B15_MODEL = "b15_49"
B15_FILENAME = "minimax_h3_hybrid_fl2va_ref2va_b15-49-int8.safetensors"
OPENH3_REGEN_LORA = "open_h3_regenerate_2k_lora.safetensors"
OPENH3_SHARP_CURVES = "sharp_curve_branches.safetensors"
OPENH3_CURVE_TABLE = "minimax_h3_ref2va_pruned_int8_convrot.safetensors"
OPENH3_OUTPUT_MEGAPIXELS = 2.07
OPENH3_REFINE_STEPS = 3
OPENH3_START_SIGMA = 0.78
OPENH3_PASS2_SINGLE_START_SIGMA = 0.78
PASS1_MAX_PIXELS = 1_000_000


def _ensure_picture1_prompt(prompt: str) -> str:
    text = (prompt or "").strip()
    if not text:
        return "Keep the character's exact facial features, hair, body, and outfit strictly consistent with <Picture 1>."
    text = re.sub(r"\bthe provided image\b", "<Picture 1>", text, flags=re.IGNORECASE)
    if not re.search(r"<\s*Picture\s*1\s*>", text, re.IGNORECASE):
        text = f"Keep the character's exact facial features, hair, body, and outfit strictly consistent with <Picture 1>. {text}"
    return text


def find_scene_mother_refs(shot: dict[str, Any] | None) -> dict[str, str]:
    """查找当前镜头或同场景前序镜头（如 #001 CUT）的高清角色/场景母图 ref1_path~ref4_path，防止 CONTINUE 与单视频二采丢失人物长相。"""
    if not shot:
        return {}
    ref_fields = ("ref1_path", "ref2_path", "ref3_path", "ref4_path")
    own_ref1 = str(shot.get("ref1_path") or "").strip()
    if own_ref1 and Path(own_ref1).is_file():
        return {
            f: str(shot.get(f) or "").strip()
            for f in ref_fields
            if str(shot.get(f) or "").strip() and Path(str(shot.get(f) or "").strip()).is_file()
        }
    scene_id = shot.get("scene_id")
    shot_no = shot.get("shot_no")
    shot_id = shot.get("id")
    if not scene_id or shot_no is None:
        return {}
    try:
        conn = db()
        try:
            rows = conn.execute(
                "SELECT ref1_path, ref2_path, ref3_path, ref4_path FROM shots "
                "WHERE scene_id=? AND id!=? AND ref1_path!='' "
                "ORDER BY CASE WHEN shot_no < ? THEN 0 ELSE 1 END, shot_no DESC",
                (int(scene_id), int(shot_id or 0), int(shot_no)),
            ).fetchall()
        finally:
            conn.close()
        for r in rows:
            r1 = str(r["ref1_path"] or "").strip()
            if r1 and Path(r1).is_file():
                out: dict[str, str] = {}
                for f in ref_fields:
                    val = str(r[f] or "").strip()
                    if val and Path(val).is_file():
                        out[f] = val
                return out
    except Exception:
        pass
    return {}


def _is_pass1_video_file(path: Path) -> bool:
    if not path.is_file():
        return False
    try:
        info = media_video_info(path)
        w = int(info.get("width") or 0)
        h = int(info.get("height") or 0)
        return 0 < (w * h) <= PASS1_MAX_PIXELS
    except Exception:
        return False


def find_pass1_video(shot: dict[str, Any] | None) -> Path | None:
    """查找当前 Shot 已生成的一采（<=1.0MP）视频文件：优先使用当前选中的版本 (output_path)，其次取最新一采版本。"""
    if not shot:
        return None
    explicit = str(shot.get("pass1_source_video_path") or "").strip()
    if explicit:
        p = Path(explicit)
        if p.is_file():
            return p
    raw_out = str(shot.get("output_path") or "").strip()
    if raw_out and _is_pass1_video_file(Path(raw_out)):
        return Path(raw_out)
    shot_id = shot.get("id")
    if shot_id:
        try:
            conn = db()
            try:
                rows = conn.execute(
                    "SELECT output_path FROM generations WHERE shot_id=? AND status='DONE' ORDER BY version DESC",
                    (int(shot_id),),
                ).fetchall()
            finally:
                conn.close()
            for r in rows:
                raw = str(r["output_path"] or "").strip()
                if raw and _is_pass1_video_file(Path(raw)):
                    return Path(raw)
        except Exception:
            pass
    return None


def apply_continue_refine_conditioning(workflow: dict[str, Any]) -> None:
    """二采只针对当前单个视频自身潜变量进行精修，彻底移除跨镜首帧 b15_guide，杜绝首帧闪跳与跨镜色偏累加。"""
    workflow.pop("b15_guide", None)
    if "b15_refine" not in workflow:
        return
    second_pass_model = ["b15_regen_lora", 0] if "b15_regen_lora" in workflow else workflow.get("126", {}).get("inputs", {}).get("model", ["142", 0])
    base_cond_ref = ["131", 0] if "131" in workflow else workflow.get("126", {}).get("inputs", {}).get("conditioning", ["131", 0])
    workflow["b15_guider"] = {
        "class_type": "BasicGuider",
        "inputs": {
            "model": second_pass_model,
            "conditioning": base_cond_ref,
        },
        "_meta": {"title": "第二轮条件 · Open-H3 1080p 单视频精修"},
    }
    workflow["b15_refine"]["inputs"]["guider"] = ["b15_guider", 0]


def apply_b15_single_pass(workflow: dict[str, Any]) -> None:
    """Strip second-pass upscale/refine nodes and wire VAEDecode/VAEDecodeAudio directly to first-pass sampler 125."""
    for key in (
        "b15_split",
        "b15_upscale",
        "b15_concat",
        "b15_regen_lora",
        "b15_sampler",
        "b15_noise",
        "b15_schedule",
        "b15_refine",
        "b15_guide",
        "b15_guider",
    ):
        workflow.pop(key, None)
    if "125" in workflow:
        workflow["125"]["_meta"] = {"title": "b15–49 一采 · 0.4MP / 10步"}
    if "122" in workflow:
        workflow["122"].setdefault("inputs", {})["samples"] = ["125", 0]
    if "121" in workflow:
        workflow["121"].setdefault("inputs", {})["samples"] = ["125", 0]
    if "92" in workflow:
        workflow["92"].setdefault("inputs", {})["filename_prefix"] = "anime_studio/videos/b15_49_one_pass"


def apply_b15_pass2_only(
    workflow: dict[str, Any],
    shot: dict[str, Any],
    uploaded_video: str,
    fps: int = 24,
    has_draft_audio: bool = True,
    uploaded_ref1: str | None = None,
) -> None:
    """将工作流转换为纯单视频二采模式：直接读取当前镜头已有的一采视频 -> VAEEncode -> 2.0x 潜变量放大 -> 3 步 Open-H3 精修。
    不引入任何上一镜末帧（无 902/903/b15_guide），若存在静态角色母图 ref1 则挂载到 131 锁死五官身份。"""
    apply_b15_profile(workflow)
    set_style_lora_strengths(workflow, shot)

    raw_prompt = str(workflow.get("1", {}).get("inputs", {}).get("value") or shot.get("prompt") or "").strip()
    clean_prompt = re.sub(r"<\s*(?:Picture|Subject)\s*\d+\s*>", "the character", raw_prompt, flags=re.IGNORECASE).strip()
    if not clean_prompt:
        clean_prompt = "same content and motion as the draft video, photorealistic, natural skin texture, fine detail, sharp focus"

    base_model_ref = workflow.get("b15_regen_lora", {}).get("inputs", {}).get("model", ["142", 0])
    seed = int(shot.get("seed") or workflow.get("b15_noise", {}).get("inputs", {}).get("noise_seed") or 42)
    use_ref_cond = bool(uploaded_ref1) and "131" in workflow

    remove_keys = {
        "104", "121", "124", "125", "126", "129", "135",
        "902", "903", "b15_split", "b15_guide",
    }
    if not use_ref_cond:
        remove_keys.update({"1", "131", "901"})

    for key in list(workflow.keys()):
        if key in remove_keys or (workflow[key].get("class_type") == "LoadImage" and (not use_ref_cond or key != "901")):
            workflow.pop(key, None)

    if use_ref_cond:
        workflow["1"] = {
            "class_type": "PrimitiveStringMultiline",
            "inputs": {"value": _ensure_picture1_prompt(raw_prompt)},
            "_meta": {"title": "PROMPT"},
        }
        workflow["901"] = {
            "class_type": "LoadImage",
            "inputs": {"image": uploaded_ref1},
            "_meta": {"title": "REFERENCE IMAGE 1 (<Picture 1>)"},
        }
        ref_inputs = workflow["131"].setdefault("inputs", {})
        for k in list(ref_inputs.keys()):
            if k.startswith("ref_images.ref_image_"):
                ref_inputs.pop(k, None)
        ref_inputs["prompt"] = ["1", 0]
        ref_inputs["ref_images.ref_image_0"] = ["901", 0]
        cond_ref = ["131", 0]
    else:
        workflow["b15_text_cond"] = {
            "class_type": "CLIPTextEncode",
            "inputs": {
                "clip": ["128", 0],
                "text": clean_prompt,
            },
            "_meta": {"title": "单视频二采文本条件"},
        }
        cond_ref = ["b15_text_cond", 0]

    use_draft_audio = bool(has_draft_audio) and not bool(shot.get("native_song_audio"))

    finish_inputs: dict[str, Any] = {
        "images": ["122", 0],
        "frame_count": ["b15_prep_draft", 1],
        "crop": "none",
    }
    create_inputs: dict[str, Any] = {
        "images": ["b15_finish", 0],
        "fps": fps,
        "bit_depth": 8,
    }
    if use_draft_audio:
        finish_inputs["audio"] = ["b15_video_comp", 1]
        create_inputs["audio"] = ["b15_finish", 1]

    workflow.update({
        "b15_load_video": {
            "class_type": "LoadVideo",
            "inputs": {"file": uploaded_video},
            "_meta": {"title": "加载当前镜头一采原片"},
        },
        "b15_video_comp": {
            "class_type": "GetVideoComponents",
            "inputs": {"video": ["b15_load_video", 0]},
            "_meta": {"title": "拆出一采画面与原声"},
        },
        "b15_prep_draft": {
            "class_type": "OpenH3RegenPrepareDraft",
            "inputs": {
                "images": ["b15_video_comp", 0],
                "size": "keep (snap to 32)",
                "max_frames": 0,
                "source_fps": 0.0,
            },
            "_meta": {"title": "一采帧对齐 · 保持原分辨率与全部帧"},
        },
        "b15_vae_encode": {
            "class_type": "VAEEncode",
            "inputs": {
                "pixels": ["b15_prep_draft", 0],
                "vae": ["119", 0],
            },
            "_meta": {"title": "一采单视频编码为潜变量"},
        },
        "b15_upscale": {
            "class_type": "MinimaxH3LatentUpscaler3D",
            "inputs": {
                "latent": ["b15_vae_encode", 0],
                "model_name": "minimax_h3_latent_upscaler_3d_conv_v1/minimax_h3_latent_upscaler_3d_conv_v1_fp16.safetensors",
                "mode": "scale by multiplier",
                "mode.scale": SECOND_PASS_RESOLUTIONS.get(str(shot.get("second_pass_resolution") or "1k").strip().lower(), 2.0),
                "align": 32,
                "enable_temporal_chunking": True,
                "force_unload": True,
                "device": "cuda",
                "precision": "fp16",
            },
            "_meta": {"title": f"潜变量 {SECOND_PASS_RESOLUTIONS.get(str(shot.get('second_pass_resolution') or '1k').strip().lower(), 2.0):g}x 等比放大 ({str(shot.get('second_pass_resolution') or '1k').upper()})"},
        },
        "b15_concat": {
            "class_type": "OpenH3RegenRefineLatent",
            "inputs": {
                "latent": ["b15_upscale", 0],
                "audio_mode": "silence (reference)",
                "audio_vae": ["120", 0],
            },
            "_meta": {"title": "准备单视频精修潜变量"},
        },
        "b15_regen_lora": {
            "class_type": "OpenH3RegenLora",
            "inputs": {
                "model": base_model_ref,
                "lora_name": OPENH3_REGEN_LORA,
                "curve_name": OPENH3_SHARP_CURVES,
                "curve_table": OPENH3_CURVE_TABLE,
                "strength": 1.0,
                "curve_strength": 0.65,
                "lora_dtype": "bf16",
            },
            "_meta": {"title": "Open-H3 精修 LoRA + Sharp 50层高频锐化曲线"},
        },
        "b15_guider": {
            "class_type": "BasicGuider",
            "inputs": {
                "model": ["b15_regen_lora", 0],
                "conditioning": cond_ref,
            },
            "_meta": {"title": "第二轮条件 · 纯单视频 Open-H3 1080p 精修"},
        },
        "b15_sampler": {
            "class_type": "KSamplerSelect",
            "inputs": {"sampler_name": "euler"},
            "_meta": {"title": "Open-H3 精修采样器 · Euler"},
        },
        "b15_noise": {
            "class_type": "RandomNoise",
            "inputs": {"noise_seed": seed},
        },
        "b15_schedule": {
            "class_type": "OpenH3RegenSigmas",
            "inputs": {
                "steps": OPENH3_REFINE_STEPS,
                "start_sigma": OPENH3_PASS2_SINGLE_START_SIGMA,
            },
            "_meta": {"title": f"Open-H3 精修调度 · 3步 / start_sigma={OPENH3_PASS2_SINGLE_START_SIGMA}"},
        },
        "b15_refine": {
            "class_type": "SamplerCustomAdvanced",
            "inputs": {
                "noise": ["b15_noise", 0],
                "guider": ["b15_guider", 0],
                "sampler": ["b15_sampler", 0],
                "sigmas": ["b15_schedule", 0],
                "latent_image": ["b15_concat", 0],
            },
            "_meta": {"title": f"Open-H3 单视频 1080p 精修 · 3步 / start_sigma={OPENH3_PASS2_SINGLE_START_SIGMA}"},
        },
        "122": {
            "class_type": "VAEDecode",
            "inputs": {
                "samples": ["b15_refine", 0],
                "vae": ["119", 0],
            },
        },
        "b15_finish": {
            "class_type": "OpenH3RegenFinish",
            "inputs": finish_inputs,
            "_meta": {"title": "裁齐帧数并接回一采原声"},
        },
        "130": {
            "class_type": "CreateVideo",
            "inputs": create_inputs,
        },
    })
    workflow["92"].setdefault("inputs", {})["filename_prefix"] = "anime_studio/videos/b15_49_pass2_single"


def apply_b15_profile(workflow: dict[str, Any]) -> None:
    # Frozen templates that already contain the complete Open-H3 refine pipeline stay intact.
    if "b15_refine" in workflow and "b15_regen_lora" in workflow:
        if "b15_upscale" in workflow:
            up_in = workflow["b15_upscale"].setdefault("inputs", {})
            up_in.pop("mode.megapixels", None)
            up_in["mode"] = "scale by multiplier"
            up_in["mode.scale"] = 2.0
            up_in["enable_temporal_chunking"] = True
            up_in["force_unload"] = True
        if "b15_regen_lora" in workflow:
            workflow["b15_regen_lora"].setdefault("inputs", {})["curve_strength"] = 0.65
        apply_continue_refine_conditioning(workflow)
        return
    # Remove the Turbo loader entirely: strength zero still requires its file.
    turbo = {k: n["inputs"]["model"] for k, n in workflow.items()
             if n.get("class_type") == "LoraLoaderModelOnly"
             and "turbo_" in n.get("inputs", {}).get("lora_name", "").lower()}
    for node in workflow.values():
        for key, value in list(node.get("inputs", {}).items()):
            while isinstance(value, list) and len(value) == 2 and str(value[0]) in turbo:
                value = turbo[str(value[0])]
            node["inputs"][key] = value
    for key in turbo:
        del workflow[key]
    for node_id, node in workflow.items():
        if str(node_id).startswith("b15_"):
            continue
        kind, inputs = node.get("class_type"), node.get("inputs", {})
        if kind == "UNETLoader":
            inputs["unet_name"] = B15_FILENAME
        elif kind == "CLIPLoader":
            inputs["clip_name"] = "qwen3vl_32b_minimax_h3_int8_convrot.safetensors"
        elif kind == "MiniMaxH3SigmaShift":
            inputs.update(shift_video=12.0, shift_audio=3.0)
        elif kind == "KSamplerSelect":
            inputs["sampler_name"] = "res_multistep"
    # Existing mode templates retain their own conditioning, refs and first-frame guide.
    required = {"125": "SamplerCustomAdvanced", "124": "BasicScheduler",
                "129": "RandomNoise", "122": "VAEDecode", "121": "VAEDecodeAudio"}
    for key, kind in required.items():
        if workflow.get(key, {}).get("class_type") != kind:
            raise RuntimeError(f"b15-49 两轮工作流模板不兼容：缺少 {key} / {kind}")
    workflow["125"]["_meta"] = {"title": "b15–49 首轮 · 0.4MP / 10步"}
    workflow["124"]["inputs"].update(scheduler="simple", steps=10, denoise=1.0)

    def node(kind, **inputs):
        return {"class_type": kind, "inputs": inputs}

    base_model_ref = workflow["124"]["inputs"]["model"]
    base_cond_ref = ["131", 0] if "131" in workflow else workflow.get("126", {}).get("inputs", {}).get("conditioning", ["131", 0])

    workflow.update({
        "b15_split": node("LTXVSeparateAVLatent", av_latent=["125", 1]),
        "b15_upscale": node(
            "MinimaxH3LatentUpscaler3D",
            latent=["b15_split", 0],
            model_name="minimax_h3_latent_upscaler_3d_conv_v1/minimax_h3_latent_upscaler_3d_conv_v1_fp16.safetensors",
            mode="scale by multiplier",
            **{"mode.scale": 2.0},
            align=32,
            enable_temporal_chunking=True,
            force_unload=True,
            device="cuda",
            precision="fp16",
        ),
        "b15_concat": node("LTXVConcatAVLatent", video_latent=["b15_upscale", 0], audio_latent=["b15_split", 1]),
        "b15_regen_lora": node(
            "OpenH3RegenLora",
            model=base_model_ref,
            lora_name=OPENH3_REGEN_LORA,
            curve_name=OPENH3_SHARP_CURVES,
            curve_table=OPENH3_CURVE_TABLE,
            strength=1.0,
            curve_strength=0.65,
            lora_dtype="bf16",
        ),
        "b15_guider": node(
            "BasicGuider",
            model=["b15_regen_lora", 0],
            conditioning=base_cond_ref,
        ),
        "b15_sampler": node("KSamplerSelect", sampler_name="euler"),
        "b15_noise": node("RandomNoise", noise_seed=workflow["129"]["inputs"]["noise_seed"]),
        "b15_schedule": node(
            "OpenH3RegenSigmas",
            steps=OPENH3_REFINE_STEPS,
            start_sigma=OPENH3_START_SIGMA,
        ),
        "b15_refine": node(
            "SamplerCustomAdvanced",
            noise=["b15_noise", 0],
            guider=["b15_guider", 0],
            sampler=["b15_sampler", 0],
            sigmas=["b15_schedule", 0],
            latent_image=["b15_concat", 0],
        ),
    })
    workflow["122"]["inputs"]["samples"] = ["b15_refine", 0]
    workflow["121"]["inputs"]["samples"] = ["b15_refine", 0]
    workflow["92"]["inputs"]["filename_prefix"] = "anime_studio/videos/b15_49_two_pass"

    workflow["b15_upscale"]["_meta"] = {"title": "潜变量放大至 1080p (2.07MP)"}
    workflow["b15_regen_lora"]["_meta"] = {"title": "Open-H3 精修 LoRA + Sharp 50层高频锐化曲线"}
    workflow["b15_guider"]["_meta"] = {"title": "第二轮条件 · Open-H3 1080p 单视频精修"}
    workflow["b15_sampler"]["_meta"] = {"title": "Open-H3 精修采样器 · Euler"}
    workflow["b15_schedule"]["_meta"] = {"title": "Open-H3 精修调度 · 3步 / start_sigma=0.78"}
    workflow["b15_refine"]["_meta"] = {"title": "Open-H3 1080p 精修 · 3步 / start_sigma=0.78"}

    apply_continue_refine_conditioning(workflow)


@register("video", "b15_two_pass")
class B15TwoPass(VideoAdapter):
    def prepare_template(self, workflow, shot, model):
        if str(shot.get("mode") or "CUT").upper() != "T2V" and not str(shot.get("ref1_path") or "").strip():
            for k, v in find_scene_mother_refs(shot).items():
                if not str(shot.get(k) or "").strip():
                    shot[k] = v
        apply_b15_profile(workflow)
        if "b15_upscale" in workflow:
            res_tier = str(shot.get("second_pass_resolution") or "1k").strip().lower()
            scale = SECOND_PASS_RESOLUTIONS.get(res_tier, 2.0)
            workflow["b15_upscale"].setdefault("inputs", {})["mode.scale"] = scale
            workflow["b15_upscale"]["_meta"] = {"title": f"潜变量 {scale:g}x 等比放大 ({res_tier.upper()})"}
        if "second_pass_enabled" in shot and not bool(int(shot.get("second_pass_enabled") or 0)):
            apply_b15_single_pass(workflow)
        set_style_lora_strengths(workflow, shot)

    async def prepare_single_video_pass2(self, workflow: dict[str, Any], shot: dict[str, Any], client: Any, fps: int) -> bool:
        if not bool(int(shot.get("second_pass_enabled") or 0)):
            return False
        draft_path = find_pass1_video(shot)
        if draft_path is None or not draft_path.is_file():
            return False
        upload_fn = getattr(client, "upload_input_file", None) or getattr(client, "upload_image", None)
        if upload_fn is None:
            return False
        try:
            uploaded = await upload_fn(draft_path, "anime_studio/drafts")
        except TypeError:
            uploaded = await upload_fn(draft_path)
        uploaded_ref1: str | None = None
        if str(shot.get("mode") or "CUT").upper() != "T2V":
            mother_refs = find_scene_mother_refs(shot)
            ref1_raw = str(shot.get("ref1_path") or mother_refs.get("ref1_path") or "").strip()
            if ref1_raw and Path(ref1_raw).is_file():
                img_upload_fn = getattr(client, "upload_image", None) or upload_fn
                if img_upload_fn is not None:
                    uploaded_ref1 = await img_upload_fn(Path(ref1_raw))
        apply_b15_pass2_only(
            workflow,
            shot,
            uploaded_video=uploaded,
            fps=fps,
            has_draft_audio=media_has_audio(draft_path),
            uploaded_ref1=uploaded_ref1,
        )
        return True

    def finalize(self, workflow, shot):
        if "b15_noise" in workflow and "129" in workflow:
            workflow["b15_noise"].setdefault("inputs", {})["noise_seed"] = workflow["129"]["inputs"]["noise_seed"]

    def render_megapixels(self, shot, model):
        # These dimensions feed the first sampler's latent, not the final output.
        # The upscale node alone sets output_megapixels for the second pass.
        if "second_pass_enabled" not in shot or bool(int(shot.get("second_pass_enabled") or 0)):
            return float(model["default_megapixels"])
        return float(shot["megapixels"])

    def sampler_steps(self, shot, model):
        return 10
