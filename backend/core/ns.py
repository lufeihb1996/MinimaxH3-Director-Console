"""Merged helper namespace for code that takes an ``ns`` dict (continuation, native audio)."""
from __future__ import annotations

import importlib
from typing import Any

MODULES = (
    "backend.core.paths",
    "backend.core.db",
    "backend.core.runtime",
    "backend.core.comfy",
    "backend.core.gpu",
    "backend.core.tasks",
    "backend.core.media",
    "backend.models.video",
    "backend.services.native_audio",
    "backend.services.continuation",
    "backend.services.repo",
    "backend.services.merge",
    "backend.services.images",
    "backend.services.songs",
    "backend.services.video",
)


def namespace() -> dict[str, Any]:
    ns: dict[str, Any] = {}
    for name in MODULES:
        ns.update({k: v for k, v in vars(importlib.import_module(name)).items() if not k.startswith("__")})
    return ns
