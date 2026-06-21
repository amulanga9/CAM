"""
CAM admin v1 — очередь проверки устаревших/неясных статусов ЖК.

Запуск:
    cd ~/CAM/admin
    python3 import_master.py   # один раз / после нового парса
    python3 app.py
    -> http://127.0.0.1:5000
"""
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, g, redirect, render_template, request, url_for

DB_PATH = Path(__file__).parent / 'cam_admin.db'

VERDICTS = {
    'delivered': 'Сдан',
    'building': 'Строится',
    'frozen': 'Заморожен',
    'cancelled': 'Отменён',
}

app = Flask(__name__)


def get_db():
    if 'db' not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(exc):
    db = g.pop('db', None)
    if db is not None:
        db.close()


@app.route('/')
def dashboard():
    db = get_db()
    stats = db.execute('''
        SELECT
            COUNT(*) AS total,
            SUM(needs_review) AS needs_review,
            SUM(CASE WHEN cam_id IN (SELECT cam_id FROM reviews) THEN 1 ELSE 0 END) AS reviewed
        FROM complexes
    ''').fetchone()
    by_status = db.execute('''
        SELECT case_status_clean, COUNT(*) AS n
        FROM complexes GROUP BY case_status_clean ORDER BY n DESC
    ''').fetchall()
    return render_template('dashboard.html', stats=stats, by_status=by_status)


@app.route('/review')
def review_queue():
    db = get_db()
    rows = db.execute('''
        SELECT c.* FROM complexes c
        WHERE c.needs_review = 1
          AND c.cam_id NOT IN (SELECT cam_id FROM reviews)
        ORDER BY c.days_overdue DESC, c.cam_id
    ''').fetchall()
    return render_template('review_queue.html', rows=rows, verdicts=VERDICTS)


@app.route('/review/<cam_id>', methods=['POST'])
def submit_review(cam_id):
    verdict = request.form['verdict']
    comment = request.form.get('comment', '').strip()
    if verdict not in VERDICTS:
        return 'bad verdict', 400
    db = get_db()
    db.execute(
        'INSERT INTO reviews (cam_id, reviewed_at, verdict, comment) VALUES (?,?,?,?)',
        (cam_id, datetime.now(timezone.utc).isoformat(), verdict, comment),
    )
    # сразу обновляем чистый статус в самой записи, чтобы он попал в выгрузку для обучения
    db.execute(
        'UPDATE complexes SET case_status_clean=?, needs_review=0 WHERE cam_id=?',
        (verdict, cam_id),
    )
    db.commit()
    return redirect(url_for('review_queue'))


@app.route('/exclude/<cam_id>', methods=['POST'])
def exclude_complex(cam_id):
    reason = request.form.get('reason', '').strip()
    db = get_db()
    db.execute(
        'INSERT OR REPLACE INTO excluded_ids (cam_id, reason, excluded_at) VALUES (?,?,?)',
        (cam_id, reason, datetime.now(timezone.utc).isoformat()),
    )
    db.execute('DELETE FROM complexes WHERE cam_id=?', (cam_id,))
    db.commit()
    return redirect(request.form.get('next') or url_for('review_queue'))


@app.route('/new', methods=['GET', 'POST'])
def new_complex():
    if request.method == 'GET':
        return render_template('new_complex.html', error=None)

    address = request.form.get('address', '').strip()
    if not address:
        return render_template('new_complex.html', error='Адрес обязателен для объектов без CAM ID')

    db = get_db()
    n = db.execute('SELECT COUNT(*) FROM manual_complexes').fetchone()[0]
    cam_id = f'MAN-{n + 1:04d}'
    db.execute('''
        INSERT INTO manual_complexes (cam_id, project_name, address, district_name, lat, lng, note, created_at)
        VALUES (?,?,?,?,?,?,?,?)
    ''', (
        cam_id,
        request.form.get('project_name', '').strip() or None,
        address,
        request.form.get('district_name', '').strip() or None,
        request.form.get('lat') or None,
        request.form.get('lng') or None,
        request.form.get('note', '').strip() or None,
        datetime.now(timezone.utc).isoformat(),
    ))
    db.execute('''
        INSERT OR IGNORE INTO complexes (cam_id, source_sheet, project_name, address, district_name, lat, lng, case_status_clean, needs_review, raw_json)
        VALUES (?, 'manual', ?, ?, ?, ?, ?, 'unknown', 0, '{}')
    ''', (
        cam_id,
        request.form.get('project_name', '').strip() or None,
        address,
        request.form.get('district_name', '').strip() or None,
        request.form.get('lat') or None,
        request.form.get('lng') or None,
    ))
    db.commit()
    return redirect(url_for('complex_detail', cam_id=cam_id))


@app.route('/complex/<cam_id>')
def complex_detail(cam_id):
    db = get_db()
    row = db.execute('SELECT * FROM complexes WHERE cam_id=?', (cam_id,)).fetchone()
    if row is None:
        return 'not found', 404
    raw = json.loads(row['raw_json']) if row['raw_json'] else {}
    history = db.execute(
        'SELECT * FROM reviews WHERE cam_id=? ORDER BY reviewed_at DESC', (cam_id,)
    ).fetchall()
    return render_template('complex_detail.html', row=row, raw=raw, history=history, verdicts=VERDICTS)


@app.route('/all')
def all_complexes():
    db = get_db()
    q = request.args.get('q', '').strip()
    status = request.args.get('status', '').strip()
    sql = 'SELECT * FROM complexes WHERE 1=1'
    params = []
    if q:
        sql += ' AND (project_name LIKE ? OR address LIKE ? OR cam_id LIKE ?)'
        params += [f'%{q}%'] * 3
    if status:
        sql += ' AND case_status_clean = ?'
        params.append(status)
    sql += ' ORDER BY cam_id LIMIT 500'
    rows = db.execute(sql, params).fetchall()
    return render_template('all_complexes.html', rows=rows, q=q, status=status)


if __name__ == '__main__':
    app.run(debug=True)
