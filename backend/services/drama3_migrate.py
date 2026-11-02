"""一次性幂等迁移 data/drama3/state.json 到 SQLite (voice_profiles / voice_jobs / voice_job_speakers / settings)。"""
from __future__ import annotations

import copy
import json
import re
import shutil
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any

from backend.core.paths import DATA_DIR, DB_PATH
from backend.services.drama3 import DEFAULT_PRESET_SCRIPTS, _clean_single_speaker_script

DRAMA3_DIR = DATA_DIR / "drama3"
DRAMA3_STATE_PATH = DRAMA3_DIR / "state.json"


def _default_voice_slot(index: int = 0) -> dict[str, Any]:
    default_name = "Role 0 (lead)" if index == 0 else f"Role {index}"
    return {
        "index": index,
        "name": default_name,
        "local_path": "",
        "remote_name": "",
        "transcript": "",
        "duration": 0.0,
    }


def _normalize_voice_list(voices: Any, fallback_voices: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    base_list = voices if isinstance(voices, list) and voices else (fallback_voices or [_default_voice_slot(0)])
    out: list[dict[str, Any]] = []
    for idx, item in enumerate(base_list[:16]):
        v = dict(item) if isinstance(item, dict) else {}
        default_name = "Role 0 (lead)" if idx == 0 else f"Role {idx}"
        out.append({
            "index": idx,
            "name": str(v.get("name") or default_name).strip() or default_name,
            "local_path": str(v.get("local_path") or "").strip(),
            "remote_name": str(v.get("remote_name") or "").strip(),
            "transcript": str(v.get("transcript") or ""),
            "duration": float(v.get("duration") or 0.0),
        })
    return out if out else [_default_voice_slot(0)]


def _parse_legacy_state_file(state_path: Path) -> dict[str, Any]:
    """读取并规范化旧版 state.json（兼容更早的 history 结构，不回写原文件）。"""
    base_cfg: dict[str, Any] = {
        "title": DEFAULT_PRESET_SCRIPTS["solo_sweet"]["title"],
        "model_name": "drama3_w6a8experts_bf16.safetensors",
        "experts": "in RAM",
        "expert_cache_gb": 0.0,
        "temperature": 1.0,
        "top_p": 0.9,
        "top_k": 0,
        "max_frames": 800,
        "seed": 42,
        "speakers_mode": "one take per line",
        "split_long_text": True,
        "shorten_long_pauses": True,
        "asr_language": "auto",
        "script_text": DEFAULT_PRESET_SCRIPTS["solo_sweet"]["script"],
        "voices": [_default_voice_slot(0)],
    }
    raw_json = json.loads(state_path.read_text(encoding="utf-8"))
    loaded = raw_json if isinstance(raw_json, dict) else {}
    base = {**base_cfg, **loaded}
    base["voices"] = _normalize_voice_list(base.get("voices"))
    base["script_text"] = _clean_single_speaker_script(
        base.get("script_text") or DEFAULT_PRESET_SCRIPTS["solo_sweet"]["script"]
    )

    raw_items: list[dict[str, Any]] = []
    if isinstance(loaded.get("items"), list):
        raw_items = [x for x in loaded["items"] if isinstance(x, dict)]
    elif isinstance(loaded.get("history"), list) and loaded["history"]:
        seen_keys: set[tuple[str, str]] = set()
        deduped: list[dict[str, Any]] = []
        for h in loaded["history"]:
            if not isinstance(h, dict):
                continue
            key = (str(h.get("title") or "").strip(), _clean_single_speaker_script(h.get("script_text") or ""))
            if key in seen_keys:
                continue
            seen_keys.add(key)
            deduped.append({**h, "status": "DONE"})
        raw_items = list(reversed(deduped))
    else:
        raw_items = [
            {
                "id": "drama3_task_001",
                "title": base["title"],
                "script_text": base["script_text"],
                "voices": copy.deepcopy(base["voices"]),
                "status": "PENDING",
            }
        ]

    normalized_items: list[dict[str, Any]] = []
    for idx, raw in enumerate(raw_items):
        item_id = str(raw.get("id") or f"legacy_{idx + 1:03d}_{uuid.uuid4().hex[:6]}").strip()
        audio_path = str(raw.get("audio_path") or "").strip()
        has_audio = bool(audio_path and Path(audio_path).is_file())
        script_text = _clean_single_speaker_script(
            raw.get("script_text") if "script_text" in raw else (base.get("script_text") or "")
        )
        raw_status = str(raw.get("status") or ("DONE" if has_audio else "PENDING")).upper()
        if raw_status in {"RUNNING", "QUEUED", "PREPARING"}:
            status = "PENDING"
        elif has_audio and raw_status == "DONE":
            status = "DONE"
        else:
            status = "DONE" if has_audio else "PENDING"

        if status == "DONE":
            progress = 100.0
            current_node = "Generation complete · 44.1kHz"
        else:
            progress = 0.0
            current_node = "Parameters ready, not generated" if script_text else "Waiting for lines"

        default_title = f"配音任务 #{idx + 1:03d}"
        normalized_items.append({
            "id": item_id,
            "title": str(raw.get("title") or default_title).strip() or default_title,
            "script_text": script_text,
            "model_name": str(raw.get("model_name") or base.get("model_name") or "drama3_w6a8experts_bf16.safetensors"),
            "experts": str(raw.get("experts") or base.get("experts") or "in RAM"),
            "expert_cache_gb": float(
                raw.get("expert_cache_gb") if raw.get("expert_cache_gb") is not None else (base.get("expert_cache_gb") or 0.0)
            ),
            "temperature": float(
                raw.get("temperature") if raw.get("temperature") is not None else (base.get("temperature") if base.get("temperature") is not None else 1.0)
            ),
            "top_p": float(
                raw.get("top_p") if raw.get("top_p") is not None else (base.get("top_p") if base.get("top_p") is not None else 0.9)
            ),
            "top_k": int(raw.get("top_k") if raw.get("top_k") is not None else (base.get("top_k") or 0)),
            "max_frames": int(raw.get("max_frames") if raw.get("max_frames") is not None else (base.get("max_frames") or 800)),
            "seed": int(raw.get("seed") if raw.get("seed") is not None else (base.get("seed") if base.get("seed") is not None else 42)),
            "speakers_mode": str(raw.get("speakers_mode") or base.get("speakers_mode") or "one take per line"),
            "split_long_text": bool(raw.get("split_long_text") if raw.get("split_long_text") is not None else base.get("split_long_text", True)),
            "shorten_long_pauses": bool(raw.get("shorten_long_pauses") if raw.get("shorten_long_pauses") is not None else base.get("shorten_long_pauses", True)),
            "asr_language": str(raw.get("asr_language") or base.get("asr_language") or "auto"),
            "voices": _normalize_voice_list(raw.get("voices"), base.get("voices")),
            "status": status,
            "progress": progress,
            "current_node": current_node,
            "error": "",
            "audio_path": audio_path if has_audio else "",
            "duration": float(raw.get("duration") or 0.0) if has_audio else 0.0,
            "elapsed_seconds": float(raw.get("elapsed_seconds") or 0.0),
            "started_at": float(raw.get("started_at") or 0.0),
            "finished_at": float(raw.get("finished_at") or 0.0),
            "created_at": str(raw.get("created_at") or "").strip(),
        })

    base["items"] = normalized_items
    return base


def migrate_drama3_state(
    state_path: Path | None = None,
    db_path: Path | None = None,
) -> dict[str, Any]:
    """将 state.json 一次性幂等迁入 SQLite。"""
    target_state_path = Path(state_path) if state_path is not None else DRAMA3_STATE_PATH
    target_db_path = Path(db_path) if db_path is not None else DB_PATH
    migrated_path = target_state_path.with_name(target_state_path.name + ".migrated")

    if not target_state_path.is_file() or migrated_path.is_file():
        return {"migrated": False, "reason": "already_migrated_or_missing"}

    # 1. 备份 studio.db 与 state.json
    backups_dir = target_db_path.parent / "backups"
    backups_dir.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%d_%H%M%S")
    db_backup_path = backups_dir / f"studio_before_voice_migration_{ts}.db"
    if target_db_path.is_file():
        shutil.copy2(target_db_path, db_backup_path)
    state_bak_path = target_state_path.with_name(target_state_path.name + ".bak")
    shutil.copy2(target_state_path, state_bak_path)

    # 2. 解析 state.json
    state = _parse_legacy_state_file(target_state_path)
    items = state.get("items") or []

    # 3. 收集所有非空 local_path 的音色（按第一次出现的值去重）
    all_voice_slots: list[dict[str, Any]] = [
        *(state.get("voices") or []),
        *(v for item in items for v in (item.get("voices") or [])),
    ]
    unique_voices: dict[str, dict[str, Any]] = {}
    for v in all_voice_slots:
        local_p = str(v.get("local_path") or "").strip()
        if not local_p or local_p in unique_voices:
            continue
        raw_name = str(v.get("name") or "").strip()
        if not raw_name or re.fullmatch(r"角色\s*\d+.*", raw_name):
            raw_name = Path(local_p).stem
        unique_voices[local_p] = {
            "name": raw_name,
            "ref_audio_path": local_p,
            "remote_name": str(v.get("remote_name") or "").strip(),
            "transcript": str(v.get("transcript") or ""),
            "duration": float(v.get("duration") or 0.0),
        }

    defaults_payload = {
        "model_name": str(state.get("model_name") or "drama3_w6a8experts_bf16.safetensors"),
        "experts": str(state.get("experts") or "in RAM"),
        "expert_cache_gb": float(state.get("expert_cache_gb") or 0.0),
        "temperature": float(state.get("temperature") if state.get("temperature") is not None else 1.0),
        "top_p": float(state.get("top_p") if state.get("top_p") is not None else 0.9),
        "top_k": int(state.get("top_k") or 0),
        "max_frames": int(state.get("max_frames") or 800),
        "seed": int(state.get("seed") if state.get("seed") is not None else 42),
        "speakers_mode": str(state.get("speakers_mode") or "one take per line"),
        "split_long_text": bool(state.get("split_long_text", True)),
        "shorten_long_pauses": bool(state.get("shorten_long_pauses", True)),
        "asr_language": str(state.get("asr_language") or "auto"),
    }

    conn = sqlite3.connect(target_db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        with conn:
            # 4. 写入 voice_profiles
            path_to_profile_id: dict[str, int] = {}
            for local_p, vp in unique_voices.items():
                conn.execute(
                    """
                    INSERT OR IGNORE INTO voice_profiles(name, ref_audio_path, remote_name, transcript, duration)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (vp["name"], vp["ref_audio_path"], vp["remote_name"], vp["transcript"], vp["duration"]),
                )
                row = conn.execute(
                    "SELECT id FROM voice_profiles WHERE ref_audio_path=?",
                    (local_p,),
                ).fetchone()
                if row:
                    path_to_profile_id[local_p] = int(row["id"])

            # 5. 写入 voice_jobs 与 voice_job_speakers
            for sort_idx, item in enumerate(items):
                legacy_id = str(item["id"])
                created_at = item.get("created_at") or time.strftime("%Y-%m-%d %H:%M:%S")
                conn.execute(
                    """
                    INSERT OR IGNORE INTO voice_jobs(
                        project_id, legacy_item_id, sort_order, title, script_text,
                        model_name, experts, expert_cache_gb, temperature, top_p, top_k,
                        max_frames, seed, speakers_mode, split_long_text, shorten_long_pauses,
                        asr_language, status, progress, current_node, prompt_id, error,
                        audio_path, duration, started_at, finished_at, elapsed_seconds, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '', '', ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        None,
                        legacy_id,
                        sort_idx,
                        item["title"],
                        item["script_text"],
                        item["model_name"],
                        item["experts"],
                        float(item["expert_cache_gb"]),
                        float(item["temperature"]),
                        float(item["top_p"]),
                        int(item["top_k"]),
                        int(item["max_frames"]),
                        int(item["seed"]),
                        item["speakers_mode"],
                        1 if item["split_long_text"] else 0,
                        1 if item["shorten_long_pauses"] else 0,
                        item["asr_language"],
                        item["status"],
                        float(item["progress"]),
                        item["current_node"],
                        item["audio_path"],
                        float(item["duration"]),
                        float(item["started_at"]),
                        float(item["finished_at"]),
                        float(item["elapsed_seconds"]),
                        created_at,
                    ),
                )
                job_row = conn.execute(
                    "SELECT id FROM voice_jobs WHERE legacy_item_id=?",
                    (legacy_id,),
                ).fetchone()
                if not job_row:
                    raise RuntimeError(f"迁移配音任务失败: {legacy_id}")
                job_id = int(job_row["id"])

                for spk_idx, v in enumerate(item.get("voices") or []):
                    local_p = str(v.get("local_path") or "").strip()
                    profile_id = path_to_profile_id.get(local_p) if local_p else None
                    default_spk_name = "Role 0 (lead)" if spk_idx == 0 else f"角色 {spk_idx}"
                    spk_name = str(v.get("name") or default_spk_name).strip() or default_spk_name
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO voice_job_speakers(job_id, speaker_index, name, voice_profile_id)
                        VALUES (?, ?, ?, ?)
                        """,
                        (job_id, spk_idx, spk_name, profile_id),
                    )

            # 6. 写入 settings.drama3_defaults
            conn.execute(
                """
                INSERT INTO settings(key, value) VALUES('drama3_defaults', ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value
                """,
                (json.dumps(defaults_payload, ensure_ascii=False),),
            )
    finally:
        conn.close()

    # 7. 事务提交成功后将 state.json 改名为 state.json.migrated
    target_state_path.replace(migrated_path)
    return {
        "migrated": True,
        "jobs_count": len(items),
        "profiles_count": len(unique_voices),
        "backup_db": str(db_backup_path),
        "backup_state": str(state_bak_path),
    }
