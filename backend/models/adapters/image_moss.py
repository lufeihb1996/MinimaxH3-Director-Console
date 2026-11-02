"""OpenMOSS MOSS-Image 5B SFT 文生图（SLG=22 防人体折叠工作流 + CFG 2.8 去塑料感）。"""
from __future__ import annotations

from typing import Any

from backend.models.base import ImageAdapter, node_class
from backend.models.registry import register

DEFAULT_NEGATIVE_PROMPT = (
    "plastic skin, waxy skin, airbrushed, beauty filter, smooth porcelain skin, "
    "CGI, 3D render, doll, overly symmetrical face, studio glamour retouching, "
    "artificial bokeh, worst quality, bad anatomy, extra limbs, fused body, folded body, "
    "duplicate person, multiple people, disembodied legs, mutated hands, "
    "extra fingers, watermark, jpeg artifact"
)


@register("image", "moss_image")
class MOSSImage(ImageAdapter):
    def required_nodes(self, lora_mode: str) -> list[str]:
        return [
            "UNETLoader",
            "SkipLayerGuidanceDiTSimple",
            "CLIPLoader",
            "VAELoader",
            "EmptyFlux2LatentImage",
            "CLIPTextEncode",
            "KSampler",
            "VAEDecode",
            "SaveImage",
        ]

    def build(
        self,
        workflow: dict[str, Any],
        model: dict[str, Any],
        *,
        prompt: str,
        width: int,
        height: int,
        seed: int,
        lora_mode: str,
        filename_prefix: str,
        steps: int = 0,
        cfg: float = 0.0,
    ) -> int:
        steps = int(steps) if steps and int(steps) > 0 else int(model.get("steps") or 35)
        cfg = float(cfg) if cfg and cfg > 0 else float(model.get("cfg") or 2.8)
        negative_prompt = str(model.get("negative_prompt") or DEFAULT_NEGATIVE_PROMPT)

        workflow["4"].setdefault("inputs", {}).update({
            "width": width,
            "height": height,
            "batch_size": 1,
        })
        workflow["6"].setdefault("inputs", {})["text"] = prompt
        workflow["7"].setdefault("inputs", {})["text"] = negative_prompt
        workflow["8"].setdefault("inputs", {}).update({
            "seed": seed,
            "steps": steps,
            "cfg": cfg,
            "sampler_name": "euler",
            "scheduler": "simple",
            "denoise": 1.0,
        })
        workflow["10"].setdefault("inputs", {})["filename_prefix"] = filename_prefix
        return steps

    def stage_progress(
        self,
        workflow: dict[str, Any],
        node: Any,
        value: float | None = None,
        value_max: float | None = None,
        maximum: float | None = None,
    ) -> float:
        max_v = maximum if maximum is not None else value_max
        class_type = node_class(workflow, node)
        if class_type == "KSampler" and value is not None and max_v:
            return 15.0 + max(0.0, min(1.0, value / max_v)) * 73.0
        floors = {
            "UNETLoader": 4.0,
            "SkipLayerGuidanceDiTSimple": 6.0,
            "CLIPLoader": 9.0,
            "CLIPTextEncode": 12.0,
            "EmptyFlux2LatentImage": 14.0,
            "KSampler": 15.0,
            "VAELoader": 90.0,
            "VAEDecode": 93.0,
            "SaveImage": 96.0,
        }
        base = floors.get(class_type, 3.0)
        if value is not None and max_v:
            return min(97.0, base + max(0.0, min(1.0, value / max_v)) * 3.0)
        return base
