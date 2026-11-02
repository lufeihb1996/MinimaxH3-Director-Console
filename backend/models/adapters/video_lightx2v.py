"""Singularity 奇点（fused_h3）单轮采样：无 LoRA 20 步 / Ref2V Turbo 4 步 / 8 步。"""
from __future__ import annotations

from typing import Any

from backend.models.base import VideoAdapter, bypass_all_loras, set_style_lora_strengths
from backend.models.registry import register

LIGHTX2V_PROFILES = {
    20: {"lora": None, "shift_video": 12.0, "shift_audio": 3.0, "sampler": "res_multistep"},
    4: {"lora": "minimax_h3_ref2v_turbo_4step_v0.1_comfyui_bf16.safetensors", "shift_video": 12.0, "shift_audio": 3.0},
    8: {"lora": "minimax_h3_ref2v_turbo_8step_v1.0_768p_comfyui_bf16.safetensors", "shift_video": 6.0, "shift_audio": 3.0},
}


def apply_lightx2v_profile(workflow: dict[str, Any], steps: int) -> None:
    if steps not in LIGHTX2V_PROFILES:
        raise RuntimeError("采样档位只支持无LoRA20步、Turbo4步或8步，请保存后重新生成")
    profile = LIGHTX2V_PROFILES[steps]
    changed = {"lora": steps == 20, "sigma": False, "scheduler": False, "sampler": False}
    for node in workflow.values():
        inputs = node.get("inputs", {})
        kind = node.get("class_type")
        name = str(inputs.get("lora_name", "")).lower()
        if kind == "LoraLoaderModelOnly" and "minimax_h3_" in name and "turbo_" in name:
            if profile["lora"]:
                inputs["lora_name"] = profile["lora"]
            inputs["strength_model"] = 0.0 if steps == 20 else 1.0
            node.setdefault("_meta", {})["title"] = f"{steps}-Step Sampling / " + ("No LoRA" if steps == 20 else "Turbo LoRA")
            changed["lora"] = True
        elif kind == "MiniMaxH3SigmaShift":
            inputs.update(shift_video=profile["shift_video"], shift_audio=profile["shift_audio"])
            changed["sigma"] = True
        elif kind == "BasicScheduler":
            inputs.update(scheduler="simple", steps=steps, denoise=1.0)
            changed["scheduler"] = True
        elif kind == "KSamplerSelect":
            inputs["sampler_name"] = profile.get("sampler", "euler")
            changed["sampler"] = True
    if not all(changed.values()):
        raise RuntimeError("当前视频工作流不能切换 LightX2V 档位，缺少：" + ", ".join(k for k, v in changed.items() if not v))


@register("video", "lightx2v")
class LightX2V(VideoAdapter):
    def prepare_template(self, workflow, shot, model):
        steps = int(shot["steps"])
        apply_lightx2v_profile(workflow, steps)
        if steps == 20:
            # 20 steps = base sampling: fully remove LoRA loaders, including frozen chains.
            bypass_all_loras(workflow)
        else:
            set_style_lora_strengths(workflow, shot)

    def wants_realism_trigger(self, shot):
        return int(shot["steps"]) != 20 and bool(shot.get("realism_lora_enabled", False))

    def frozen_profile(self, shot, model):
        return LIGHTX2V_PROFILES[int(shot["steps"])]
