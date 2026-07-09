"""
Синхронизация curated-данных (cam_admin.db + cam_manual.db) в Postgres —
для динамического публичного сайта (много одновременных посетителей,
в отличие от статического экспорта в data/*.json).

Переиспользует ту же сборку записи, что и export_public.py, чтобы формат
объекта был идентичен текущему data/complex/<id>.json — фронтенду
(index.html/complex.html) достаточно поменять URL с файла на API, логику
парсинга менять не нужно.

Требует переменную окружения CAM_PG_DSN, например:
    postgresql://cam:пароль@localhost/cam_public

Запуск: как и export_public.py — вручную или из update_weekly.py.
    python3 sync_to_postgres.py
"""
import json
import os
import sqlite3

import psycopg2
import psycopg2.extras

from export_public import (ADMIN_DB, MANUAL_DB, build_complex_record,
                           load_delay_flags, load_org_ratings,
                           load_overrides, load_proofs, load_reviews)

PG_DSN = os.environ.get('CAM_PG_DSN', 'postgresql://cam:cam_dev_pw@localhost/cam_public')


def ensure_schema(pg):
    with pg.cursor() as cur:
        cur.execute('''
            CREATE TABLE IF NOT EXISTS complexes (
                cam_id            TEXT PRIMARY KEY,
                project_name      TEXT,
                district_name     TEXT,
                developer_inn     TEXT,
                developer_name    TEXT,
                case_status_clean TEXT,
                is_overdue        BOOLEAN,
                lat               DOUBLE PRECISION,
                lng               DOUBLE PRECISION,
                needs_review      BOOLEAN,
                data              JSONB NOT NULL,
                updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
            );
            CREATE INDEX IF NOT EXISTS idx_complexes_district ON complexes (district_name);
            CREATE INDEX IF NOT EXISTS idx_complexes_dev_inn ON complexes (developer_inn);
            CREATE INDEX IF NOT EXISTS idx_complexes_status ON complexes (case_status_clean);
            CREATE INDEX IF NOT EXISTS idx_complexes_data_gin ON complexes USING GIN (data);
        ''')
    pg.commit()


def sync(admin_db=ADMIN_DB, manual_db=MANUAL_DB, pg_dsn=PG_DSN):
    admin_conn = sqlite3.connect(admin_db)
    manual_conn = sqlite3.connect(manual_db)

    overrides = load_overrides(manual_conn)
    reviews = load_reviews(manual_conn)
    delay_flags = load_delay_flags(manual_conn)
    proofs = load_proofs(manual_conn)
    org_ratings = load_org_ratings(admin_conn)

    permits_by_cam = {}
    try:
        for p in admin_conn.execute(
                'SELECT cam_id, object_id, name, status_clean, deadline, '
                'blocks_total, blocks_accepted, apartment_count, vanished, last_seen_month '
                'FROM permits WHERE cam_id IS NOT NULL'):
            permits_by_cam.setdefault(p[0], []).append({
                'object_id': p[1], 'name': p[2], 'status': p[3], 'deadline': p[4],
                'blocks_total': p[5], 'blocks_accepted': p[6],
                'apartments': p[7], 'vanished': p[8], 'last_seen': p[9]})
    except sqlite3.OperationalError:
        pass

    cols = [r[1] for r in admin_conn.execute('PRAGMA table_info(complexes)')]
    rows = admin_conn.execute(
        "SELECT * FROM complexes WHERE case_status_clean != 'excluded' "
        "OR case_status_clean IS NULL ORDER BY cam_id").fetchall()

    pg = psycopg2.connect(pg_dsn)
    ensure_schema(pg)

    records = []
    for row in rows:
        rec = build_complex_record(row, cols, overrides, reviews, delay_flags, org_ratings)
        rec['proofs'] = proofs.get(rec['cam_id'], [])
        rec['permits'] = permits_by_cam.get(rec['cam_id'], [])
        records.append(rec)

    with pg.cursor() as cur:
        psycopg2.extras.execute_values(cur, '''
            INSERT INTO complexes (cam_id, project_name, district_name, developer_inn,
                developer_name, case_status_clean, is_overdue, lat, lng, needs_review,
                data)
            VALUES %s
            ON CONFLICT (cam_id) DO UPDATE SET
                project_name=EXCLUDED.project_name, district_name=EXCLUDED.district_name,
                developer_inn=EXCLUDED.developer_inn, developer_name=EXCLUDED.developer_name,
                case_status_clean=EXCLUDED.case_status_clean, is_overdue=EXCLUDED.is_overdue,
                lat=EXCLUDED.lat, lng=EXCLUDED.lng, needs_review=EXCLUDED.needs_review,
                data=EXCLUDED.data, updated_at=now()
        ''', [(
            r['cam_id'], r.get('project_name'), r.get('district_name'),
            r.get('developer_inn'), r.get('developer_name'), r.get('case_status_clean'),
            bool(r.get('is_overdue')), r.get('lat'), r.get('lng'),
            bool(r.get('needs_review')), json.dumps(r, ensure_ascii=False),
        ) for r in records])

        # убираем то, чего больше нет в SQLite (слито/исключено)
        alive_ids = tuple(r['cam_id'] for r in records)
        cur.execute('DELETE FROM complexes WHERE cam_id != ALL(%s)', (list(alive_ids),))
        removed = cur.rowcount
    pg.commit()

    admin_conn.close()
    manual_conn.close()
    pg.close()
    return {'total': len(records), 'removed': removed}


if __name__ == '__main__':
    result = sync()
    print(f"Синхронизировано в Postgres: {result['total']} объектов "
          f"(удалено устаревших: {result['removed']})")
