"""
Импорт сырых GASN CSV (из scrapers/parse_shaffof.py) прямо в cam_admin.db,
без мастер-Excel.

Берёт все батч-файлы data/raw/<год-месяц>/shaffof/*.csv (можно ограничить
--month), для каждого объекта генерирует cam_id вида GASN-<object_id>
и добавляет/обновляет запись в complexes. Не трогает объекты, у которых
source_sheet != 'gasn' (т.е. пришедшие из Excel) — апдейтит только свои.
Уважает manual.excluded_ids (помеченные как "не ЖК" больше не возвращаются).

Запуск:
    cd ~/CAM/admin
    python3 import_shaffof.py                 # все batches во всех месяцах
    python3 import_shaffof.py --month 2026-06  # только конкретный месяц
"""
import argparse
import csv
import json
import sqlite3
from datetime import date, datetime
from pathlib import Path

from import_master import CLEAN_STATUS_MAP, attach_manual_db, build_manual_schema, load_excluded_ids

ADMIN_DIR = Path(__file__).parent
CAM_ROOT = ADMIN_DIR.parent
DB_PATH = ADMIN_DIR / 'cam_admin.db'
RAW_DIR = CAM_ROOT / 'data' / 'raw'


def clean_status(raw):
    if not raw:
        return 'unknown'
    raw = str(raw).strip()
    return CLEAN_STATUS_MAP.get(raw, 'unclear')


def to_float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def to_int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def compute_overdue(deadline_str):
    if not deadline_str:
        return False, None
    try:
        deadline = datetime.strptime(deadline_str[:10], '%Y-%m-%d').date()
    except ValueError:
        return False, None
    delta = (date.today() - deadline).days
    return delta > 0, delta if delta > 0 else None


def find_batches(month=None):
    if not RAW_DIR.exists():
        return []
    pattern = f'{month}/shaffof/*.csv' if month else '*/shaffof/*.csv'
    return sorted(RAW_DIR.glob(pattern))


def ensure_complexes_table(conn):
    conn.execute('''
        CREATE TABLE IF NOT EXISTS complexes (
            cam_id            TEXT PRIMARY KEY,
            source_sheet      TEXT,
            project_name      TEXT,
            address           TEXT,
            lat               REAL,
            lng               REAL,
            district_name     TEXT,
            developer_name    TEXT,
            developer_inn     TEXT,
            developer_rating  TEXT,
            contractor_name   TEXT,
            contractor_inn    TEXT,
            contractor_rating TEXT,
            created_at        TEXT,
            deadline          TEXT,
            case_status_raw   TEXT,
            case_status_clean TEXT,
            master_status     TEXT,
            target            INTEGER,
            is_overdue        INTEGER,
            days_overdue      INTEGER,
            dom_class         TEXT,
            price_per_m2_uzs  REAL,
            listing_url       TEXT,
            brand_name        TEXT,
            delivered_year    TEXT,
            needs_review      INTEGER DEFAULT 0,
            raw_json          TEXT
        )
    ''')
    cols = {r[1] for r in conn.execute('PRAGMA table_info(complexes)')}
    for col in ('brand_name', 'delivered_year'):
        if col not in cols:
            conn.execute(f'ALTER TABLE complexes ADD COLUMN {col} TEXT')


def import_batches(conn, batches, excluded):
    cur = conn.cursor()
    total, skipped_excluded = 0, 0
    for path in batches:
        with open(path, encoding='utf-8') as f:
            reader = csv.DictReader(f)
            n = 0
            for row in reader:
                object_id = row.get('object_id')
                if not object_id:
                    continue
                cam_id = f'GASN-{object_id}'
                if cam_id in excluded:
                    skipped_excluded += 1
                    continue

                case_status_raw = row.get('status') or ''
                cs_clean = clean_status(case_status_raw)
                is_overdue, days_overdue = compute_overdue(row.get('deadline'))
                needs_review = 1 if (cs_clean == 'unclear' or (cs_clean == 'in_progress' and is_overdue)) else 0

                cur.execute('''
                    INSERT INTO complexes (
                        cam_id, source_sheet, project_name, address, lat, lng,
                        district_name, developer_name, developer_inn, developer_rating,
                        contractor_name, contractor_inn, contractor_rating,
                        created_at, deadline, case_status_raw, case_status_clean,
                        master_status, target, is_overdue, days_overdue,
                        dom_class, price_per_m2_uzs, needs_review, raw_json
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(cam_id) DO UPDATE SET
                        project_name=excluded.project_name,
                        address=excluded.address,
                        lat=excluded.lat,
                        lng=excluded.lng,
                        developer_name=excluded.developer_name,
                        developer_inn=excluded.developer_inn,
                        contractor_name=excluded.contractor_name,
                        contractor_inn=excluded.contractor_inn,
                        deadline=excluded.deadline,
                        case_status_raw=excluded.case_status_raw,
                        case_status_clean=excluded.case_status_clean,
                        is_overdue=excluded.is_overdue,
                        days_overdue=excluded.days_overdue,
                        raw_json=excluded.raw_json
                    WHERE complexes.source_sheet = 'gasn'
                ''', (
                    cam_id, 'gasn',
                    row.get('name'),
                    row.get('location'),
                    to_float(row.get('lat')),
                    to_float(row.get('long')),
                    None,
                    row.get('organization_name'),
                    None,
                    None,
                    row.get('pudrat_name') or row.get('pudrat_direct'),
                    str(row.get('pudrat_inn') or ''),
                    row.get('pudrat_reyting'),
                    str(row.get('created_at') or ''),
                    str(row.get('deadline') or ''),
                    str(case_status_raw),
                    cs_clean,
                    None,
                    None,
                    int(is_overdue),
                    days_overdue,
                    None,
                    None,
                    needs_review,
                    json.dumps(row, ensure_ascii=False, default=str),
                ))
                n += 1
        print(f'  {path.relative_to(RAW_DIR)}: {n} объектов')
        total += n
    conn.commit()
    print(f'Всего обработано из GASN CSV: {total} (пропущено как "не ЖК": {skipped_excluded})')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--month', default=None, help='только данные за этот год-месяц, напр. 2026-06')
    args = ap.parse_args()

    batches = find_batches(args.month)
    if not batches:
        print(f'Не найдено CSV в {RAW_DIR} (месяц={args.month or "все"})')
        return

    conn = sqlite3.connect(DB_PATH)
    attach_manual_db(conn)
    build_manual_schema(conn)
    ensure_complexes_table(conn)
    excluded = load_excluded_ids(conn)

    import_batches(conn, batches, excluded)

    cur = conn.execute("SELECT COUNT(*) FROM complexes WHERE source_sheet='gasn'")
    print(f'\nВсего объектов из GASN в базе: {cur.fetchone()[0]}')
    conn.close()


if __name__ == '__main__':
    main()
