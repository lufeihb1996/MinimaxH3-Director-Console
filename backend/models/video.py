"""Video catalog helpers shared by API, queue snapshots and UI."""
from __future__ import annotations

from typing import Any

from backend.models.registry import default_model, get_adapter, get_model, list_models

DEFAULT_VIDEO_MODEL = default_model("video")
SECOND_PASS_RESOLUTIONS: dict[str, float] = {
    "720p": 1.5,
    "1k": 2.0,
    "1.5k": 2.25,
    "2k": 2.5,
}


def video_model(model_id: str | None) -> dict[str, Any]:
    return get_model("video", model_id)


def video_adapter(model_id: str | None):
    return get_adapter("video", model_id)


def video_catalog() -> dict[str, Any]:
    return {"default_model": DEFAULT_VIDEO_MODEL, "models": list_models("video")}


def supports_audio_lock(model_id: str | None) -> bool:
    return bool(video_model(model_id).get("supports_audio_lock"))


def normalize_video_parameters(values: dict[str, Any], current: dict[str, Any] | None = None) -> dict[str, Any]:
    """Normalize an API create/patch without rewriting unrelated saved parameters."""
    result = dict(values)
    current = current or {}
    model = video_model(result.get("video_model", current.get("video_model")))
    if "video_model" in result or not current:
        result["video_model"] = model["id"]
    if not current:
        result.setdefault("steps", model["default_steps"])
        result.setdefault("megapixels", model["default_megapixels"])
        result.setdefault("aspect_ratio", model["default_aspect_ratio"])
        result.setdefault("second_pass_resolution", "1k")
    if "second_pass_resolution" in result:
        res_tier = str(result["second_pass_resolution"] or "1k").strip().lower()
        if res_tier not in SECOND_PASS_RESOLUTIONS:
            raise ValueError("二采分辨率请选择 720p / 1k / 1.5k / 2k")
        result["second_pass_resolution"] = res_tier
    if len(model["steps"]) == 1:
        result["steps"] = model["default_steps"]
    if model["fixed_megapixels"] is not None:
        result["megapixels"] = model["fixed_megapixels"]
    elif model["id"] != current.get("video_model") and current and "steps" not in result:
        result["steps"] = model["default_steps"]
    steps = result.get("steps", current.get("steps", model["default_steps"]))
    if steps not in model["steps"]:
        raise ValueError(f"{model['label']} 支持的采样步数：" + " / ".join(map(str, model["steps"])))
    mode = str(result.get("mode", current.get("mode", "CONTINUE"))).upper()
    if mode not in model["modes"]:
        raise ValueError(f"{model['label']} 不支持模式 {mode}")
    if "mode" in result:
        result["mode"] = mode
    return video_adapter(model["id"]).normalize_parameters(result, current, model)


def video_workflow_paths(shot: dict[str, Any], settings: dict[str, Any] | None = None) -> tuple[str, str]:
    model = video_model(shot.get("video_model"))
    mode = str(shot["mode"]).upper()
    if mode not in model["modes"] or mode not in model["workflows"]:
        raise ValueError(f"{model['label']} 不支持模式 {mode}")
    return tuple(model["workflows"][mode])


def select_final_video(outputs: Any, output_node: str | None) -> dict[str, Any]:
    """Explicit output nodes prevent a preview from silently becoming the master."""
    if not isinstance(outputs, dict):
        raise RuntimeError("ComfyUI history 输出结构不正确")
    candidates = [outputs.get(output_node)] if output_node else list(outputs.values())
    for node_out in candidates:
        if not isinstance(node_out, dict):
            continue
        for key in ("videos", "gifs", "images"):
            for item in node_out.get(key, []) or []:
                if isinstance(item, dict) and str(item.get("filename", "")).lower().endswith((".mp4", ".webm", ".mov", ".gif")):
                    return item
    raise RuntimeError("ComfyUI 已完成，但指定最终输出节点没有视频；不会使用预览代替成片")
