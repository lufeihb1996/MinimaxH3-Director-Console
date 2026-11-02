"""Krea 2 Turbo 文生图（四槽 Ref 素材）。"""
from __future__ import annotations

from typing import Any

from backend.models.base import ImageAdapter, node_class
from backend.models.registry import register

LORA_MODES = {"none", "accelerated", "style", "all", "legacy"}


def configure_image_loras(workflow: dict, mode: str) -> int:
    """Select adapters from the saved task; legacy preserves queued work."""
    if mode not in LORA_MODES:
        raise ValueError("无效的生图 LoRA 设置")
    if mode not in {"all", "legacy"}:
        if mode != "style":
            workflow.pop("11", None)
            workflow.pop("12", None)
        else:
            workflow["11"]["inputs"]["model"] = ["1", 0]
        if mode != "accelerated":
            workflow.pop("2", None)
        workflow["7"]["inputs"]["model"] = [{"none": "1", "accelerated": "2", "style": "12"}[mode], 0]
    steps = 4 if mode in {"accelerated", "all", "legacy"} else 8
    workflow["7"]["inputs"]["steps"] = steps
    return steps


@register("image", "krea2")
class Krea2(ImageAdapter):
    def required_nodes(self, lora_mode):
        nodes = ["UNETLoader", "CLIPLoader", "CLIPTextEncode", "ConditioningZeroOut",
                 "EmptyLatentImage", "KSampler", "VAELoader", "VAEDecode", "SaveImage"]
        if lora_mode != "none":
            nodes.append("LoraLoaderModelOnly")
        return nodes

    def build(self, workflow, model, *, prompt, width, height, seed, lora_mode, filename_prefix, steps: int = 0, cfg: float = 0.0):
        actual_cfg = float(cfg) if cfg and float(cfg) > 0 else float(model.get("cfg") or 1.0)
        workflow["4"].setdefault("inputs", {})["text"] = prompt
        workflow["6"].setdefault("inputs", {}).update({"width": width, "height": height, "batch_size": 1})
        workflow["7"].setdefault("inputs", {}).update({
            "seed": seed, "steps": 4, "cfg": actual_cfg,
            "sampler_name": "euler", "scheduler": "simple", "denoise": 1.0,
        })
        default_steps = configure_image_loras(workflow, lora_mode)
        actual_steps = int(steps) if steps and int(steps) > 0 else default_steps
        workflow["7"]["inputs"]["steps"] = actual_steps
        workflow["10"].setdefault("inputs", {})["filename_prefix"] = filename_prefix
        return actual_steps

    def stage_progress(self, workflow: dict[str, Any], node: Any, value=None, maximum=None) -> float:
        class_type = node_class(workflow, node)
        if class_type == "KSampler" and value is not None and maximum:
            return 15.0 + max(0.0, min(1.0, value / maximum)) * 70.0
        floors = {
            "UNETLoader": 4.0, "LoraLoaderModelOnly": 7.0, "CLIPLoader": 9.0, "CLIPTextEncode": 11.0,
            "ConditioningZeroOut": 12.0, "EmptyLatentImage": 13.0, "KSampler": 15.0,
            "VAELoader": 88.0, "VAEDecode": 91.0, "SaveImage": 96.0,
        }
        base = floors.get(class_type, 3.0)
        if value is not None and maximum:
            return min(97.0, base + max(0.0, min(1.0, value / maximum)) * 3.0)
        return base
