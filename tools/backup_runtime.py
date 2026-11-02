from __future__ import annotations

import argparse
import sqlite3
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "data" / "studio.db"
BACKUP_DIR = ROOT / "backups"


def backup_database(keep: int = 20) -> Path:
    source_db = DB_PATH
    if not source_db.is_file():
        legacy_db = ROOT.parent.parent / "AI_Anime_Studio" / "data" / "studio.db"
        if legacy_db.is_file():
            source_db = legacy_db
        else:
            raise SystemExit(f"数据库不存在: {DB_PATH}")

    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    target = BACKUP_DIR / f"studio_{stamp}.db"

    src = sqlite3.connect(source_db)
    dst = sqlite3.connect(target)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()

    backups = sorted(BACKUP_DIR.glob("studio_*.db"), key=lambda p: p.stat().st_mtime, reverse=True)
    for old in backups[max(1, keep):]:
        old.unlink(missing_ok=True)

    return target


def main() -> None:
    parser = argparse.ArgumentParser(description="Safely back up AI Anime Studio SQLite data.")
    parser.add_argument("--keep", type=int, default=20, help="number of newest DB backups to keep (default: 20)")
    args = parser.parse_args()

    target = backup_database(args.keep)
    print("AI Anime Studio 数据库备份完成")
    print(target)


if __name__ == "__main__":
    main()
