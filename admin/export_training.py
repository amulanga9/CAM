"""
Выгружает текущее состояние БД (с учётом ручных проверок) обратно в CSV
для следующего цикла обучения модели.

Запуск:
    cd ~/CAM/admin
    python3 export_training.py
Пишет: data/cam_training_export.csv
"""
import csv
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent / 'cam_admin.db'
OUT_CSV = Path(__file__).parent / 'data' / 'cam_training_export.csv'

FIELDS = [
    'cam_id', 'source_sheet', 'project_name', 'address', 'lat', 'lng',
    'district_name', 'developer_name', 'developer_inn', 'developer_rating',
    'contractor_name', 'contractor_inn', 'contractor_rating',
    'created_at', 'deadline', 'case_status_raw', 'case_status_clean',
    'master_status', 'target', 'is_overdue', 'days_overdue',
    'dom_class', 'price_per_m2_uzs', 'listing_url', 'brand_name',
    'delivered_year', 'needs_review',
]


def main():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(f'SELECT {",".join(FIELDS)} FROM complexes ORDER BY cam_id').fetchall()

    with open(OUT_CSV, 'w', newline='', encoding='utf-8-sig') as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        for r in rows:
            w.writerow(dict(r))

    reviewed = conn.execute('SELECT COUNT(DISTINCT cam_id) FROM reviews').fetchone()[0]
    print(f'Экспортировано {len(rows)} строк -> {OUT_CSV}')
    print(f'Из них вручную проверено: {reviewed}')


if __name__ == '__main__':
    main()
