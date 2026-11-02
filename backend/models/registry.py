"""统一模型目录：生图（image）/ 视频（video）/ 歌曲（audio）。

所有模型都登记在 config/models.json，每个模型通过 adapter 字段找到
backend/models/adapters/ 下对应的适配器。排队、GPU 调度、上传、监控、下载
都是公共流程；适配器只负责“这个模型怎么改工作流”。

加一个新模型：
1. 把 ComfyUI API 工作流放进 workflows/{image,video,audio}/；
2. 在 config/models.json 对应 kind 下加一项；
3. 新模型的工作流改法与已有适配器不同，就在 adapters/ 加一个文件并用 @register 登记。
"""
from __future__ import annotations

import importlib
import json
import pkgutil
from copy import deepcopy
from pathlib import Path
from typing import Any

CATALOG_PATH = Path(__file__).resolve().parents[2] / "config" / "models.json"
KINDS = ("image", "video", "audio")
KIND_LABELS = {"image": "Image generation", "video": "视频", "audio": "歌曲"}

_ADAPTERS: dict[tuple[str, str], Any] = {}
_adapters_loaded = False


def register(kind: str, name: str):
    """Class decorator: register an adapter instance under (kind, adapter name)."""
    if kind not in KINDS:
        raise ValueError(f"未知模型类型：{kind}")

    def decorate(cls):
        _ADAPTERS[(kind, name)] = cls()
        return cls

    return decorate


def _load_catalog() -> dict[str, dict[str, Any]]:
    data = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    catalog: dict[str, dict[str, Any]] = {}
    for kind in KINDS:
        section = data[kind]
        models = {m["id"]: dict(m, kind=kind) for m in section["models"]}
        if len(models) != len(section["models"]):
            raise ValueError(f"{KIND_LABELS[kind]}模型目录包含重复 ID")
        if section["default"] not in models:
            raise ValueError(f"{KIND_LABELS[kind]}模型目录缺少默认模型 {section['default']}")
        for m in models.values():
            if not m.get("adapter"):
                raise ValueError(f"{m['id']} 缺少 adapter 字段")
        catalog[kind] = {"default": section["default"], "models": models}
    return catalog


_CATALOG = _load_catalog()


def _ensure_adapters() -> None:
    global _adapters_loaded
    if _adapters_loaded:
        return
    from backend.models import adapters as package
    for info in pkgutil.iter_modules(package.__path__):
        importlib.import_module(f"{package.__name__}.{info.name}")
    _adapters_loaded = True


def default_model(kind: str) -> str:
    return _CATALOG[kind]["default"]


def get_model(kind: str, model_id: str | None = None) -> dict[str, Any]:
    key = str(model_id or default_model(kind)).strip().lower()
    try:
        return deepcopy(_CATALOG[kind]["models"][key])
    except KeyError:
        raise ValueError(f"不支持的{KIND_LABELS[kind]}模型：{key}") from None


def list_models(kind: str) -> list[dict[str, Any]]:
    return deepcopy(list(_CATALOG[kind]["models"].values()))


def catalog() -> dict[str, Any]:
    return {kind: {"default": default_model(kind), "models": list_models(kind)} for kind in KINDS}


def get_adapter(kind: str, model_id: str | None = None):
    _ensure_adapters()
    model = get_model(kind, model_id)
    try:
        return _ADAPTERS[(kind, model["adapter"])]
    except KeyError:
        raise ValueError(f"{model['label']} 的适配器 {model['adapter']} 不存在") from None
