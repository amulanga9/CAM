"""
Импорт мастер-Excel (cam_master_2026_06_new.xlsx) в SQLite.
Каждая строка любого "объектного" листа -> одна запись в complexes,
полная исходная строка сохраняется в raw_json (на случай если
понадобится поле, которое мы не выносили в отдельную колонку).

Справочники застройщиков/подрядчиков идут в отдельные таблицы.

Запуск:
    cd ~/CAM/admin
    python3 import_master.py
"""
import json
import re
import sqlite3
from pathlib import Path

import openpyxl

XLSX_PATH = Path(__file__).parent / 'data' / 'cam_master_2026_06_new.xlsx'
DB_PATH = Path(__file__).parent / 'cam_admin.db'

OBJECT_SHEETS = [
    'Обучение',
    'Строящиеся_сматченные',
    'Строящиеся_несматченные',
    'Все построенные',
    'Плохие кейсы',
    'Серая зона (1-2 года)',
]
REF_SHEETS = {
    'Все застройщики': 'developers',
    'Все подрядчики': 'contractors',
}

# canonical_field -> возможные имена колонок в разных листах (по порядку приоритета)
FIELD_ALIASES = {
    'cam_id':            ['cam_id', 'CAM ЖК ID'],
    'project_name':      ['project_name', 'dom_name'],
    'address':           ['address'],
    'lat':               ['lat'],
    'lng':               ['lng', 'long'],
    'district_name':     ['district_name'],
    'developer_name':    ['developer_name_norm', 'developer_name_original'],
    'developer_inn':     ['developer_inn'],
    'developer_rating':  ['developer_rating'],
    'contractor_name':   ['contractor_name_norm', 'contractor_name_original'],
    'contractor_inn':    ['contractor_inn'],
    'contractor_rating': ['contractor_rating'],
    'created_at':        ['created_at'],
    'deadline':          ['deadline', 'deadline_max', 'deadline_min'],
    'case_status':       ['case_status', 'shaffof_status'],
    'master_status':     ['master_status'],
    'target':            ['target'],
    'is_overdue':        ['is_overdue'],
    'days_overdue':      ['days_overdue'],
    'dom_class':         ['dom_class'],
    'price_per_m2_uzs':  ['price_per_m2_uzs'],
}

# нормализация "грязного" case_status в чистый бакет.
# всё, что не распознано однозначно -> 'unclear' (=требует ручной проверки)
CLEAN_STATUS_MAP = {
    'Topshirilgan': 'delivered',
    "To'xtatilgan": 'stopped',
    'Jarayonda': 'in_progress',
    'Bekor qilingan': 'cancelled',
    'Muzlatilgan': 'frozen',
    'Просрочен': 'unclear',
}


def clean_status(raw):
    if not raw:
        return 'unknown'
    raw = str(raw).strip()
    if raw in CLEAN_STATUS_MAP:
        return CLEAN_STATUS_MAP[raw]
    return 'unclear'


def get_field(row, header_idx, canonical):
    for alias in FIELD_ALIASES[canonical]:
        i = header_idx.get(alias)
        if i is not None and i < len(row) and row[i] not in (None, ''):
            return row[i]
    return None


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


def build_schema(conn):
    conn.executescript('''
    DROP TABLE IF EXISTS complexes;
    DROP TABLE IF EXISTS developers;
    DROP TABLE IF EXISTS contractors;
    DROP TABLE IF EXISTS reviews;

    CREATE TABLE complexes (
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
        needs_review      INTEGER DEFAULT 0,
        raw_json          TEXT
    );

    CREATE TABLE developers (
        name_norm     TEXT PRIMARY KEY,
        name_original TEXT,
        inn           TEXT,
        rating        TEXT,
        objects_count INTEGER,
        bad_pct       REAL,
        overdue_pct   REAL,
        complexes_count INTEGER
    );

    CREATE TABLE contractors (
        name_norm     TEXT PRIMARY KEY,
        name_original TEXT,
        inn           TEXT,
        rating        TEXT,
        objects_count INTEGER,
        bad_pct       REAL,
        overdue_pct   REAL,
        complexes_count INTEGER
    );

    CREATE TABLE reviews (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        cam_id      TEXT NOT NULL,
        reviewed_at TEXT NOT NULL,
        verdict     TEXT NOT NULL,   -- delivered | building | frozen | cancelled
        comment     TEXT,
        FOREIGN KEY (cam_id) REFERENCES complexes(cam_id)
    );
    ''')


def import_objects(conn, wb):
    cur = conn.cursor()
    seen = set()
    total = 0
    for sheet_name in OBJECT_SHEETS:
        if sheet_name not in wb.sheetnames:
            print(f'  пропущен лист (не найден): {sheet_name}')
            continue
        ws = wb[sheet_name]
        rows = list(ws.iter_rows(values_only=True))
        header = rows[0]
        header_idx = {h: i for i, h in enumerate(header) if h is not None}

        n = 0
        for row in rows[1:]:
            cam_id = get_field(row, header_idx, 'cam_id')
            if not cam_id:
                continue
            cam_id = str(cam_id)
            if cam_id in seen:
                continue  # одна и та же запись может встречаться на нескольких листах
            seen.add(cam_id)

            case_status_raw = get_field(row, header_idx, 'case_status')
            cs_clean = clean_status(case_status_raw)
            is_overdue = bool(get_field(row, header_idx, 'is_overdue'))
            needs_review = 1 if (cs_clean == 'unclear' or (cs_clean == 'in_progress' and is_overdue)) else 0

            raw_dict = {h: row[i] for h, i in header_idx.items()}

            cur.execute('''
                INSERT OR IGNORE INTO complexes (
                    cam_id, source_sheet, project_name, address, lat, lng,
                    district_name, developer_name, developer_inn, developer_rating,
                    contractor_name, contractor_inn, contractor_rating,
                    created_at, deadline, case_status_raw, case_status_clean,
                    master_status, target, is_overdue, days_overdue,
                    dom_class, price_per_m2_uzs, needs_review, raw_json
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ''', (
                cam_id, sheet_name,
                get_field(row, header_idx, 'project_name'),
                get_field(row, header_idx, 'address'),
                to_float(get_field(row, header_idx, 'lat')),
                to_float(get_field(row, header_idx, 'lng')),
                get_field(row, header_idx, 'district_name'),
                get_field(row, header_idx, 'developer_name'),
                str(get_field(row, header_idx, 'developer_inn') or ''),
                get_field(row, header_idx, 'developer_rating'),
                get_field(row, header_idx, 'contractor_name'),
                str(get_field(row, header_idx, 'contractor_inn') or ''),
                get_field(row, header_idx, 'contractor_rating'),
                str(get_field(row, header_idx, 'created_at') or ''),
                str(get_field(row, header_idx, 'deadline') or ''),
                str(case_status_raw or ''),
                cs_clean,
                get_field(row, header_idx, 'master_status'),
                to_int(get_field(row, header_idx, 'target')),
                int(is_overdue),
                to_int(get_field(row, header_idx, 'days_overdue')),
                get_field(row, header_idx, 'dom_class'),
                to_float(get_field(row, header_idx, 'price_per_m2_uzs')),
                needs_review,
                json.dumps(raw_dict, ensure_ascii=False, default=str),
            ))
            n += 1
        print(f'  {sheet_name}: {n} новых записей')
        total += n
    conn.commit()
    print(f'Всего объектов: {total}')


def import_ref_sheet(conn, wb, sheet_name, table):
    if sheet_name not in wb.sheetnames:
        print(f'  пропущен лист (не найден): {sheet_name}')
        return
    ws = wb[sheet_name]
    rows = list(ws.iter_rows(values_only=True))
    cur = conn.cursor()
    n = 0
    for row in rows[1:]:
        name_norm, name_orig, inn, rating, obj_cnt, bad_pct, overdue_pct, cplx_cnt = (list(row) + [None] * 8)[:8]
        if not name_norm:
            continue
        cur.execute(f'''
            INSERT OR REPLACE INTO {table}
            (name_norm, name_original, inn, rating, objects_count, bad_pct, overdue_pct, complexes_count)
            VALUES (?,?,?,?,?,?,?,?)
        ''', (str(name_norm), name_orig, str(inn or ''), rating,
              to_int(obj_cnt), to_float(bad_pct), to_float(overdue_pct), to_int(cplx_cnt)))
        n += 1
    conn.commit()
    print(f'  {sheet_name} -> {table}: {n} записей')


def main():
    print(f'Читаю {XLSX_PATH} ...')
    wb = openpyxl.load_workbook(XLSX_PATH, data_only=True)

    conn = sqlite3.connect(DB_PATH)
    build_schema(conn)

    print('Импорт объектов:')
    import_objects(conn, wb)

    print('Импорт справочников:')
    for sheet, table in REF_SHEETS.items():
        import_ref_sheet(conn, wb, sheet, table)

    cur = conn.execute('SELECT COUNT(*) FROM complexes WHERE needs_review=1')
    print(f'\nТребуют проверки (needs_review=1): {cur.fetchone()[0]}')
    conn.close()
    print(f'\nГотово: {DB_PATH}')


if __name__ == '__main__':
    main()
