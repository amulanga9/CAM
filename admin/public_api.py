"""
Публичный API для динамического сайта (Postgres вместо статических
data/*.json) — на случай хостинга с реальным одновременным трафиком
посетителей, где статический экспорт больше не годится.

Отдаёт ровно тот же формат данных, что и текущие data/complexes.json /
data/complex/<id>.json — во фронтенде (index.html/complex.html) меняется
только URL фетча, не логика парсинга.

    GET /api/complexes        — лёгкий индекс (список карточек)
    GET /api/complex/<cam_id> — полное досье одной карточки

Наполняется через sync_to_postgres.py (обычно из update_weekly.py вместо
или вместе с export_public.py).

Запуск:
    CAM_PG_DSN=postgresql://user:pass@host/db python3 public_api.py
"""
import os

import psycopg2
import psycopg2.extras
from flask import Flask, abort, jsonify

PG_DSN = os.environ.get('CAM_PG_DSN', 'postgresql://cam:cam_dev_pw@localhost/cam_public')

# те же поля, что в текущем export_public.py для лёгкого индекса — фронтенд
# не должен заметить разницы между файлом и API
INDEX_FIELDS = [
    'cam_id', 'project_name', 'address', 'district_name', 'lat', 'lng',
    'case_status_clean', 'developer_name', 'developer_inn', 'dev_dir_rating',
    'deadline', 'is_overdue', 'days_overdue', 'dom_class', 'price_per_m2_uzs',
    'listing_url', 'brand_name', 'needs_review', 'blocks_total',
    'blocks_accepted', 'apartments_count', 'floors_max',
]

app = Flask(__name__)


def get_conn():
    return psycopg2.connect(PG_DSN)


@app.route('/api/complexes')
def complexes_index():
    conn = get_conn()
    with conn.cursor() as cur:
        cur.execute('SELECT data FROM complexes ORDER BY cam_id')
        rows = cur.fetchall()
    conn.close()
    index = [{f: row[0].get(f) for f in INDEX_FIELDS} for row in rows]
    return jsonify(index)


@app.route('/api/complex/<cam_id>')
def complex_detail(cam_id):
    conn = get_conn()
    with conn.cursor() as cur:
        cur.execute('SELECT data FROM complexes WHERE cam_id=%s', (cam_id,))
        row = cur.fetchone()
    conn.close()
    if not row:
        abort(404)
    return jsonify(row[0])


if __name__ == '__main__':
    app.run(port=5050, debug=bool(os.environ.get('CAM_DEBUG')))
