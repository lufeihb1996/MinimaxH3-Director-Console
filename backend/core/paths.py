"""应用目录常量（全部相对应用根目录 AI_Anime_Studio/）。"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = ROOT / "data"
FRONTEND_DIR = ROOT / "frontend"
WORKFLOW_DIR = ROOT / "workflows"
PROJECTS_DIR = ROOT / "projects"
DB_PATH = DATA_DIR / "studio.db"

for p in (DATA_DIR, FRONTEND_DIR, WORKFLOW_DIR, PROJECTS_DIR):
    p.mkdir(parents=True, exist_ok=True)
