"""SQLite 连接、建表与迁移、全局设置。"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from backend.core.paths import DB_PATH, ROOT

DEFAULT_SETTINGS = {
    "comfy_url": "",
    "pika_url": "",
    "fps": "24",
}

# 视频后处理默认值；实际模式由每个 Shot 的 enhance_mode 参数决定。
def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    conn = db()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS projects (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS scenes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            sort_order INTEGER NOT NULL DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS shots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            scene_id INTEGER NOT NULL,
            shot_no INTEGER NOT NULL,
            title TEXT NOT NULL DEFAULT '',
            mode TEXT NOT NULL DEFAULT 'CUT',
            prompt TEXT NOT NULL DEFAULT '',
            dialogue TEXT NOT NULL DEFAULT '',
            seconds REAL NOT NULL DEFAULT 5.0,
            megapixels REAL NOT NULL DEFAULT 0.7,
            aspect_ratio TEXT NOT NULL DEFAULT '9:16',
            steps INTEGER NOT NULL DEFAULT 4,
            seed INTEGER NOT NULL DEFAULT 0,
            video_model TEXT NOT NULL DEFAULT 'b15_49',
            second_pass_enabled INTEGER NOT NULL DEFAULT 0,
            second_pass_resolution TEXT NOT NULL DEFAULT '1k',
            enhance_mode TEXT NOT NULL DEFAULT 'vosr2',
            status TEXT NOT NULL DEFAULT 'PENDING',
            progress REAL NOT NULL DEFAULT 0,
            current_node TEXT NOT NULL DEFAULT '',
            prompt_id TEXT NOT NULL DEFAULT '',
            error TEXT NOT NULL DEFAULT '',
            ref1_path TEXT NOT NULL DEFAULT '',
            ref2_path TEXT NOT NULL DEFAULT '',
            ref3_path TEXT NOT NULL DEFAULT '',
            ref4_path TEXT NOT NULL DEFAULT '',
            first_frame_path TEXT NOT NULL DEFAULT '',
            output_path TEXT NOT NULL DEFAULT '',
            last_frame_path TEXT NOT NULL DEFAULT '',
            face_refined_path TEXT NOT NULL DEFAULT '',
            face_refined_last_frame_path TEXT NOT NULL DEFAULT '',
            source_duration REAL NOT NULL DEFAULT 0,
            timeline_start REAL NOT NULL DEFAULT 0,
            timeline_end REAL NOT NULL DEFAULT 0,
            target_duration REAL NOT NULL DEFAULT 0,
            conform_mode TEXT NOT NULL DEFAULT 'auto',
            lip_sync_enabled INTEGER NOT NULL DEFAULT 0,
            lyric_text TEXT NOT NULL DEFAULT '',
            lip_sync_status TEXT NOT NULL DEFAULT 'IDLE',
            lip_sync_progress REAL NOT NULL DEFAULT 0,
            lip_sync_audio_path TEXT NOT NULL DEFAULT '',
            lip_sync_output_path TEXT NOT NULL DEFAULT '',
            lip_sync_last_frame_path TEXT NOT NULL DEFAULT '',
            lip_sync_error TEXT NOT NULL DEFAULT '',
            selected_version INTEGER NOT NULL DEFAULT 1,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(scene_id) REFERENCES scenes(id) ON DELETE CASCADE,
            UNIQUE(scene_id, shot_no)
        );

        CREATE TABLE IF NOT EXISTS generations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            shot_id INTEGER NOT NULL,
            version INTEGER NOT NULL,
            prompt_id TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'PENDING',
            output_path TEXT NOT NULL DEFAULT '',
            last_frame_path TEXT NOT NULL DEFAULT '',
            error TEXT NOT NULL DEFAULT '',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(shot_id) REFERENCES shots(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS scene_images (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            scene_id INTEGER NOT NULL,
            role TEXT NOT NULL DEFAULT 'ref1',
            prompt TEXT NOT NULL DEFAULT '',
            aspect_ratio TEXT NOT NULL DEFAULT '9:16',
            width INTEGER NOT NULL DEFAULT 768,
            height INTEGER NOT NULL DEFAULT 1360,
            steps INTEGER NOT NULL DEFAULT 35,
            seed INTEGER NOT NULL DEFAULT 42,
            image_model TEXT NOT NULL DEFAULT 'moss_image_5b',
            prompt_id TEXT NOT NULL DEFAULT '',
            output_path TEXT NOT NULL DEFAULT '',
            applied_count INTEGER NOT NULL DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(scene_id) REFERENCES scenes(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS scene_image_configs (
            scene_id INTEGER NOT NULL,
            role TEXT NOT NULL,
            role_name TEXT NOT NULL DEFAULT '',
            prompt TEXT NOT NULL DEFAULT '',
            aspect_ratio TEXT NOT NULL DEFAULT '9:16',
            seed INTEGER NOT NULL DEFAULT 42,
            image_model TEXT NOT NULL DEFAULT 'moss_image_5b',
            enabled INTEGER NOT NULL DEFAULT 1,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY(scene_id, role),
            FOREIGN KEY(scene_id) REFERENCES scenes(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS scene_image_jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            scene_id INTEGER NOT NULL,
            role TEXT NOT NULL,
            prompt TEXT NOT NULL DEFAULT '',
            aspect_ratio TEXT NOT NULL DEFAULT '9:16',
            seed INTEGER NOT NULL DEFAULT 42,
            replace_existing INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'QUEUED',
            progress REAL NOT NULL DEFAULT 0,
            current_node TEXT NOT NULL DEFAULT '',
            prompt_id TEXT NOT NULL DEFAULT '',
            error TEXT NOT NULL DEFAULT '',
            image_id INTEGER,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(scene_id) REFERENCES scenes(id) ON DELETE CASCADE,
            FOREIGN KEY(image_id) REFERENCES scene_images(id) ON DELETE SET NULL
        );

        CREATE TABLE IF NOT EXISTS mv_projects (
            project_id INTEGER PRIMARY KEY,
            title TEXT NOT NULL DEFAULT '',
            genre_prompt TEXT NOT NULL DEFAULT '',
            lyrics TEXT NOT NULL DEFAULT '',
            target_duration INTEGER NOT NULL DEFAULT 60,
            audio_linked INTEGER NOT NULL DEFAULT 0,
            audio_path TEXT NOT NULL DEFAULT '',
            audio_duration REAL NOT NULL DEFAULT 0,
            song_status TEXT NOT NULL DEFAULT 'PENDING',
            song_progress REAL NOT NULL DEFAULT 0,
            song_node TEXT NOT NULL DEFAULT '',
            song_error TEXT NOT NULL DEFAULT '',
            song_prompt_id TEXT NOT NULL DEFAULT '',
            analysis_status TEXT NOT NULL DEFAULT 'IDLE',
            analysis_progress REAL NOT NULL DEFAULT 0,
            analysis_error TEXT NOT NULL DEFAULT '',
            vocals_path TEXT NOT NULL DEFAULT '',
            no_vocals_path TEXT NOT NULL DEFAULT '',
            lyrics_timeline_path TEXT NOT NULL DEFAULT '',
            lyrics_srt_path TEXT NOT NULL DEFAULT '',
            pipeline_status TEXT NOT NULL DEFAULT 'IDLE',
            pipeline_progress REAL NOT NULL DEFAULT 0,
            pipeline_node TEXT NOT NULL DEFAULT '',
            pipeline_error TEXT NOT NULL DEFAULT '',
            master_video_path TEXT NOT NULL DEFAULT '',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
        );
        """
    )
    for name in ("continuation_selection", "queued_input_path", "completed_input_path"):
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(shots)")}
        if name not in cols:
            conn.execute(f"ALTER TABLE shots ADD COLUMN {name} TEXT NOT NULL DEFAULT ''")
    conn.execute("""CREATE TABLE IF NOT EXISTS scene_default_refs (
        scene_id INTEGER NOT NULL, role TEXT NOT NULL, image_path TEXT NOT NULL,
        PRIMARY KEY(scene_id,role), FOREIGN KEY(scene_id) REFERENCES scenes(id) ON DELETE CASCADE
    )""")
    shot_columns = {row["name"] for row in conn.execute("PRAGMA table_info(shots)").fetchall()}
    for name in ("generation_started_at", "generation_finished_at", "generation_elapsed_seconds"):
        if name not in shot_columns:
            conn.execute(f"ALTER TABLE shots ADD COLUMN {name} REAL NOT NULL DEFAULT 0")

    if "face_refined_path" not in shot_columns:
        conn.execute("ALTER TABLE shots ADD COLUMN face_refined_path TEXT NOT NULL DEFAULT ''")
    if "face_refined_last_frame_path" not in shot_columns:
        conn.execute("ALTER TABLE shots ADD COLUMN face_refined_last_frame_path TEXT NOT NULL DEFAULT ''")
    if "enhance_mode" not in shot_columns:
        conn.execute("ALTER TABLE shots ADD COLUMN enhance_mode TEXT NOT NULL DEFAULT 'vosr2'")
    if "video_model" not in shot_columns:
        conn.execute("ALTER TABLE shots ADD COLUMN video_model TEXT NOT NULL DEFAULT 'b15_49'")
    # LTX-2.5 已从项目撤除：历史 ltx25 的 Shot 统一迁到 fused_h3（奇点）分支
    conn.execute("UPDATE shots SET video_model='b15_49', steps=10, megapixels=0.4 WHERE video_model IN ('ltx25', 'fused_h3')")
    for name, declaration in {
        "second_pass_enabled": "INTEGER NOT NULL DEFAULT 0",
        "second_pass_resolution": "TEXT NOT NULL DEFAULT '1k'",
        "realism_lora_enabled": "INTEGER NOT NULL DEFAULT 0",
        "naughty_lora_enabled": "INTEGER NOT NULL DEFAULT 0",
        "source_duration": "REAL NOT NULL DEFAULT 0",
        "timeline_start": "REAL NOT NULL DEFAULT 0",
        "timeline_end": "REAL NOT NULL DEFAULT 0",
        "target_duration": "REAL NOT NULL DEFAULT 0",
        "conform_mode": "TEXT NOT NULL DEFAULT 'auto'",
        "lip_sync_enabled": "INTEGER NOT NULL DEFAULT 0",
        "lyric_text": "TEXT NOT NULL DEFAULT ''",
        "lip_sync_status": "TEXT NOT NULL DEFAULT 'IDLE'",
        "lip_sync_progress": "REAL NOT NULL DEFAULT 0",
        "lip_sync_audio_path": "TEXT NOT NULL DEFAULT ''",
        "lip_sync_output_path": "TEXT NOT NULL DEFAULT ''",
        "lip_sync_last_frame_path": "TEXT NOT NULL DEFAULT ''",
        "lip_sync_error": "TEXT NOT NULL DEFAULT ''",
    }.items():
        if name not in shot_columns:
            conn.execute(f"ALTER TABLE shots ADD COLUMN {name} {declaration}")

    scene_image_config_columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(scene_image_configs)").fetchall()
    }
    if "role_name" not in scene_image_config_columns:
        conn.execute("ALTER TABLE scene_image_configs ADD COLUMN role_name TEXT NOT NULL DEFAULT ''")
    if "ref_scope" not in scene_image_config_columns:
        conn.execute("ALTER TABLE scene_image_configs ADD COLUMN ref_scope TEXT NOT NULL DEFAULT 'scene'")
    if "image_model" not in scene_image_config_columns:
        conn.execute("ALTER TABLE scene_image_configs ADD COLUMN image_model TEXT NOT NULL DEFAULT 'krea2'")
    if "lora_mode" not in scene_image_config_columns:
        conn.execute("ALTER TABLE scene_image_configs ADD COLUMN lora_mode TEXT NOT NULL DEFAULT 'all'")
    if "steps" not in scene_image_config_columns:
        conn.execute("ALTER TABLE scene_image_configs ADD COLUMN steps INTEGER NOT NULL DEFAULT 0")
    if "cfg" not in scene_image_config_columns:
        conn.execute("ALTER TABLE scene_image_configs ADD COLUMN cfg REAL NOT NULL DEFAULT 0")
    scene_image_columns = {row["name"] for row in conn.execute("PRAGMA table_info(scene_images)").fetchall()}
    if "image_model" not in scene_image_columns:
        conn.execute("ALTER TABLE scene_images ADD COLUMN image_model TEXT NOT NULL DEFAULT 'krea2'")
    if "cfg" not in scene_image_columns:
        conn.execute("ALTER TABLE scene_images ADD COLUMN cfg REAL NOT NULL DEFAULT 1.0")
    scene_image_job_columns = {row["name"] for row in conn.execute("PRAGMA table_info(scene_image_jobs)").fetchall()}
    if "image_model" not in scene_image_job_columns:
        conn.execute("ALTER TABLE scene_image_jobs ADD COLUMN image_model TEXT NOT NULL DEFAULT 'krea2'")
    if "lora_mode" not in scene_image_job_columns:
        conn.execute("ALTER TABLE scene_image_jobs ADD COLUMN lora_mode TEXT NOT NULL DEFAULT 'legacy'")
    if "steps" not in scene_image_job_columns:
        conn.execute("ALTER TABLE scene_image_jobs ADD COLUMN steps INTEGER NOT NULL DEFAULT 0")
    if "cfg" not in scene_image_job_columns:
        conn.execute("ALTER TABLE scene_image_jobs ADD COLUMN cfg REAL NOT NULL DEFAULT 0")
    for name in ("generation_started_at", "generation_finished_at", "generation_elapsed_seconds"):
        if name not in scene_image_job_columns:
            conn.execute(f"ALTER TABLE scene_image_jobs ADD COLUMN {name} REAL NOT NULL DEFAULT 0")
    if "elapsed_seconds" not in scene_image_columns:
        conn.execute("ALTER TABLE scene_images ADD COLUMN elapsed_seconds REAL NOT NULL DEFAULT 0")

    mv_project_columns = {row["name"] for row in conn.execute("PRAGMA table_info(mv_projects)").fetchall()}
    for name, declaration in {
        "target_duration": "INTEGER NOT NULL DEFAULT 60",
        "audio_linked": "INTEGER NOT NULL DEFAULT 0",
        "native_audio_enabled": "INTEGER NOT NULL DEFAULT 0",
        "song_status": "TEXT NOT NULL DEFAULT 'PENDING'",
        "song_started_at": "REAL NOT NULL DEFAULT 0",
        "song_finished_at": "REAL NOT NULL DEFAULT 0",
        "song_elapsed_seconds": "REAL NOT NULL DEFAULT 0",
        "song_progress": "REAL NOT NULL DEFAULT 0",
        "song_node": "TEXT NOT NULL DEFAULT ''",
        "song_error": "TEXT NOT NULL DEFAULT ''",
        # ComfyUI 的 prompt_id 只在后端内部使用：用于重新生成时判断旧远端任务是否还存在。
        "song_prompt_id": "TEXT NOT NULL DEFAULT ''",
        "analysis_status": "TEXT NOT NULL DEFAULT 'IDLE'",
        "analysis_progress": "REAL NOT NULL DEFAULT 0",
        "analysis_error": "TEXT NOT NULL DEFAULT ''",
        "vocals_path": "TEXT NOT NULL DEFAULT ''",
        "no_vocals_path": "TEXT NOT NULL DEFAULT ''",
        "lyrics_timeline_path": "TEXT NOT NULL DEFAULT ''",
        "lyrics_srt_path": "TEXT NOT NULL DEFAULT ''",
        "pipeline_status": "TEXT NOT NULL DEFAULT 'IDLE'",
        "pipeline_progress": "REAL NOT NULL DEFAULT 0",
        "pipeline_node": "TEXT NOT NULL DEFAULT ''",
        "pipeline_error": "TEXT NOT NULL DEFAULT ''",
        "audio_source": "TEXT NOT NULL DEFAULT ''",
        "source_voice_job_id": "INTEGER NOT NULL DEFAULT 0",
    }.items():
        if name not in mv_project_columns:
            conn.execute(f"ALTER TABLE mv_projects ADD COLUMN {name} {declaration}")

    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS voice_profiles (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            name           TEXT NOT NULL DEFAULT '',
            ref_audio_path TEXT NOT NULL DEFAULT '',
            remote_name    TEXT NOT NULL DEFAULT '',
            transcript     TEXT NOT NULL DEFAULT '',
            duration       REAL NOT NULL DEFAULT 0,
            created_at     TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at     TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_voice_profiles_path
            ON voice_profiles(ref_audio_path) WHERE ref_audio_path <> '';

        CREATE TABLE IF NOT EXISTS voice_jobs (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id          INTEGER,
            legacy_item_id      TEXT NOT NULL DEFAULT '',
            sort_order          INTEGER NOT NULL DEFAULT 0,
            title               TEXT NOT NULL DEFAULT '',
            script_text         TEXT NOT NULL DEFAULT '',
            model_name          TEXT NOT NULL DEFAULT 'drama3_w6a8experts_bf16.safetensors',
            experts             TEXT NOT NULL DEFAULT 'in RAM',
            expert_cache_gb     REAL NOT NULL DEFAULT 0,
            temperature         REAL NOT NULL DEFAULT 1.0,
            top_p               REAL NOT NULL DEFAULT 0.9,
            top_k               INTEGER NOT NULL DEFAULT 0,
            max_frames          INTEGER NOT NULL DEFAULT 800,
            seed                INTEGER NOT NULL DEFAULT 42,
            speakers_mode       TEXT NOT NULL DEFAULT 'one take per line',
            split_long_text     INTEGER NOT NULL DEFAULT 1,
            shorten_long_pauses INTEGER NOT NULL DEFAULT 1,
            asr_language        TEXT NOT NULL DEFAULT 'auto',
            status              TEXT NOT NULL DEFAULT 'PENDING',
            progress            REAL NOT NULL DEFAULT 0,
            current_node        TEXT NOT NULL DEFAULT '',
            prompt_id           TEXT NOT NULL DEFAULT '',
            error               TEXT NOT NULL DEFAULT '',
            audio_path          TEXT NOT NULL DEFAULT '',
            duration            REAL NOT NULL DEFAULT 0,
            started_at          REAL NOT NULL DEFAULT 0,
            finished_at         REAL NOT NULL DEFAULT 0,
            elapsed_seconds     REAL NOT NULL DEFAULT 0,
            created_at          TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at          TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE SET NULL
        );
        CREATE INDEX IF NOT EXISTS idx_voice_jobs_project ON voice_jobs(project_id, sort_order, id);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_voice_jobs_legacy
            ON voice_jobs(legacy_item_id) WHERE legacy_item_id <> '';

        CREATE TABLE IF NOT EXISTS voice_job_speakers (
            job_id           INTEGER NOT NULL,
            speaker_index    INTEGER NOT NULL,
            name             TEXT NOT NULL DEFAULT '',
            voice_profile_id INTEGER,
            PRIMARY KEY(job_id, speaker_index),
            FOREIGN KEY(job_id) REFERENCES voice_jobs(id) ON DELETE CASCADE,
            FOREIGN KEY(voice_profile_id) REFERENCES voice_profiles(id) ON DELETE SET NULL
        );
        """
    )

    conn.execute(
        """
        UPDATE mv_projects
        SET audio_source='upload'
        WHERE TRIM(audio_path)<>'' AND audio_source=''
        """
    )

    # Legacy duplicate MV Shot system was retired. Existing projects use scenes -> shots only.
    conn.execute("DROP TABLE IF EXISTS mv_shots")
    conn.execute(
        "UPDATE mv_projects SET master_video_path='' "
        "WHERE REPLACE(master_video_path, '\\', '/') LIKE '%/mv/master/%'"
    )
    conn.execute(
        """
        UPDATE mv_projects
        SET song_status='DONE',song_progress=100,song_node='Generation complete'
        WHERE TRIM(audio_path)<>'' AND song_status='PENDING'
        """
    )

    mv_cols = {r["name"] for r in conn.execute("PRAGMA table_info(mv_projects)").fetchall()}
    if "song_model" not in mv_cols:
        conn.execute("ALTER TABLE mv_projects ADD COLUMN song_model TEXT NOT NULL DEFAULT 'yue2'")

    for k, v in DEFAULT_SETTINGS.items():
        conn.execute("INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)", (k, v))
    # Workflow paths moved into config/models.json; drop the retired settings keys.
    conn.execute(
        "DELETE FROM settings WHERE key IN ('cut_workflow_path','continue_workflow_path','cut_bindings_path',"
        "'continue_bindings_path','face_refine_workflow_path','krea_image_workflow_path')"
    )
    conn.commit()
    conn.close()


def get_settings() -> dict[str, str]:
    conn = db()
    rows = conn.execute("SELECT key,value FROM settings").fetchall()
    conn.close()
    data = {r["key"]: r["value"] for r in rows}
    for k, v in DEFAULT_SETTINGS.items():
        data.setdefault(k, v)
    return data


def save_settings(values: dict[str, str]) -> dict[str, str]:
    conn = db()
    for k, v in values.items():
        conn.execute(
            "INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (k, str(v)),
        )
    conn.commit()
    conn.close()
    return get_settings()


def row_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row else None


def resolve_project_file(path_text: str) -> Path:
    p = Path(path_text)
    if not p.is_absolute():
        p = ROOT / p
    return p.resolve()


def read_json_file(path_text: str) -> dict[str, Any]:
    path = resolve_project_file(path_text)
    if not path.exists():
        raise RuntimeError(f"找不到文件: {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not data:
        raise RuntimeError(f"工作流 JSON 为空或格式不对: {path}")
    return data
