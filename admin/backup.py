"""
Бэкап баз: cam_admin.db + cam_manual.db → admin/backups/YYYY-MM-DD/.

Хранит последние KEEP копий, старые удаляет. Использует sqlite backup API
(безопасно даже при работающей админке), а не копирование файла.

Запуск: python3 backup.py — или автоматически из update_weekly.py.
"""
import os
import shutil
import sqlite3
from datetime import date
from pathlib import Path

BASE = Path(__file__).resolve().parent
BACKUP_DIR = BASE / 'backups'
KEEP = 8  # ~2 месяца еженедельных копий


def backup():
    today = date.today().isoformat()
    dst_dir = BACKUP_DIR / today
    dst_dir.mkdir(parents=True, exist_ok=True)

    for name in ('cam_admin.db', 'cam_manual.db'):
        src_path = BASE / name
        if not src_path.exists():
            continue
        src = sqlite3.connect(src_path)
        dst = sqlite3.connect(dst_dir / name)
        src.backup(dst)
        dst.close()
        src.close()

    # ротация
    dirs = sorted(d for d in BACKUP_DIR.iterdir() if d.is_dir())
    for old in dirs[:-KEEP]:
        shutil.rmtree(old)

    return str(dst_dir)


if __name__ == '__main__':
    path = backup()
    print(f'Бэкап: {path}')
    for f in sorted(Path(path).iterdir()):
        print(f'  {f.name}: {f.stat().st_size // 1024} КБ')
