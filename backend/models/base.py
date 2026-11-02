"""Adapter base classes. Only model-specific workflow edits live in adapters."""
from __future__ import annotations

from typing import Any


def node_class(workflow: dict[str, Any], node: Any) -> str:
    info = workflow.get(str(node)) if isinstance(workflow, dict) else None
    return str(info.get("class_type") or "") if isinstance(info, dict) else ""


def set_style_lora_strengths(workflow: dict[str, Any], shot: dict[str, Any]) -> None:
    """Optional H3 style LoRAs shared by the current video models."""
    for node in workflow.values():
        inputs = node.get("inputs", {})
        name = str(inputs.get("lora_name", "")).lower()
        if "h3-realism-people" in name:
            inputs["strength_model"] = 0.5 if shot.get("realism_lora_enabled", False) else 0.0
        elif "naughtytimes" in name:
            inputs["strength_model"] = 0.85 if shot.get("naughty_lora_enabled", False) else 0.0


def bypass_all_loras(workflow: dict[str, Any]) -> None:
    """Remove every LoraLoaderModelOnly and reconnect its consumers to the base model."""
    bypass = {key: node["inputs"]["model"] for key, node in workflow.items()
              if node.get("class_type") == "LoraLoaderModelOnly"}

    def resolve(value):
        seen = set()
        while isinstance(value, list) and len(value) == 2 and str(value[0]) in bypass:
            key = str(value[0])
            if key in seen:
                raise RuntimeError("LoRA model connection cycle")
            seen.add(key)
            value = bypass[key]
        return value

    for node in workflow.values():
        for key, value in list(node.get("inputs", {}).items()):
            node["inputs"][key] = resolve(value)
    for key in bypass:
        del workflow[key]


class VideoAdapter:
    """视频模型：模板改法、渲染尺寸/步数与快照档位。"""

    def prepare_template(self, workflow: dict[str, Any], shot: dict[str, Any], model: dict[str, Any]) -> None:
        """Apply the model profile to a template. Must be idempotent (frozen templates are re-applied)."""
        raise NotImplementedError

    def render_megapixels(self, shot: dict[str, Any], model: dict[str, Any]) -> float:
        fixed = model.get("fixed_megapixels")
        return float(fixed) if fixed is not None else float(shot["megapixels"])

    def sampler_steps(self, shot: dict[str, Any], model: dict[str, Any]) -> int:
        return int(shot["steps"])

    def wants_realism_trigger(self, shot: dict[str, Any]) -> bool:
        return bool(shot.get("realism_lora_enabled", False))

    def finalize(self, workflow: dict[str, Any], shot: dict[str, Any]) -> None:
        """Last edits after all bindings were written."""

    def frozen_profile(self, shot: dict[str, Any], model: dict[str, Any]) -> dict[str, Any]:
        """Extra per-model parameters stored in the queued input snapshot."""
        return model

    def normalize_parameters(self, values, current, model):
        """Validate model-specific API parameters without changing other models."""
        return values

    def snapshot_files(self, shot, model, settings, read_json):
        """Backend-specific immutable request templates, stored beside input.json."""
        workflow_path, bindings_path = model["workflows"][shot["mode"].upper()]
        workflow = read_json(workflow_path)
        self.prepare_template(workflow, shot, model)
        return {"template.json": workflow, "bindings.json": read_json(bindings_path)}

    def snapshot_context(self, settings):
        return {}


class ImageAdapter:
    """生图模型：必需节点、尺寸、写入提示词/尺寸/种子/LoRA。"""

    def required_nodes(self, lora_mode: str) -> list[str]:
        raise NotImplementedError

    def image_size(self, model: dict[str, Any], aspect_ratio: str) -> tuple[int, int]:
        presets = model.get("aspect_ratios") or {}
        size = presets.get(aspect_ratio) or presets.get("9:16") or next(iter(presets.values()))
        return int(size[0]), int(size[1])

    def build(self, workflow: dict[str, Any], model: dict[str, Any], *, prompt: str, width: int, height: int,
              seed: int, lora_mode: str, filename_prefix: str, steps: int = 0, cfg: float = 0.0) -> int:
        """Write job values into the workflow; return the sampler steps actually used."""
        raise NotImplementedError

    def stage_progress(self, workflow: dict[str, Any], node: Any, value: float | None = None,
                       maximum: float | None = None) -> float:
        return 50.0 if value is None or not maximum else max(3.0, min(97.0, value / maximum * 94.0 + 3.0))


class AudioAdapter:
    """歌曲模型：必需节点、写入风格/歌词/时长，进度映射。"""

    def required_nodes(self) -> list[str]:
        raise NotImplementedError

    def build(self, workflow: dict[str, Any], model: dict[str, Any], *, style: str, lyrics: str,
              target_duration: int, seed: int, filename_prefix: str) -> None:
        raise NotImplementedError

    def stage_progress(self, workflow: dict[str, Any], node: Any, value: float | None = None,
                       maximum: float | None = None) -> float:
        return 50.0 if value is None or not maximum else max(2.0, min(97.0, value / maximum * 95.0 + 2.0))
