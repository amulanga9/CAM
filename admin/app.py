"""
CAM admin v1 — очередь проверки устаревших/неясных статусов ЖК.

Запуск:
    cd ~/CAM/admin
    python3 import_master.py   # один раз / после нового парса
    python3 app.py
    -> http://127.0.0.1:5000
"""
import csv
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, g, redirect, render_template, request, url_for

from import_master import FIELD_ALIASES, build_manual_schema, migrate_complexes, to_float

DB_PATH = Path(__file__).parent / 'cam_admin.db'
MANUAL_DB_PATH = Path(__file__).parent / 'cam_manual.db'
AFFILIATION_GROUPS_PATH = Path(__file__).parent / 'data' / 'affiliation_groups.csv'


def get_affiliation_groups():
    """Group labels built from shared founder names ('учредитель:...' in методы), not bare группа_id numbers."""
    if not AFFILIATION_GROUPS_PATH.exists():
        return []
    founders_by_group = {}
    with open(AFFILIATION_GROUPS_PATH, encoding='utf-8') as f:
        for row in csv.DictReader(f):
            gid = row['группа_id']
            names = founders_by_group.setdefault(gid, set())
            for part in row['методы'].split(';'):
                part = part.strip()
                if part.startswith('учредитель:'):
                    names.add(part[len('учредитель:'):].strip().title())
    labels = []
    for gid, names in founders_by_group.items():
        if names:
            labels.append(f"Группа: {', '.join(sorted(names))}")
        else:
            labels.append(f'Группа {gid}')
    return labels

VERDICTS = {
    'delivered': 'Сдан',
    'building': 'Строится',
    'frozen': 'Заморожен',
    'cancelled': 'Отменён',
    'rebranded': 'Ребрендирован',
}

EDITABLE_FIELDS = [
    'project_name', 'address', 'district_name', 'lat', 'lng',
    'developer_name', 'developer_inn', 'developer_rating',
    'contractor_name', 'contractor_inn', 'contractor_rating',
    'deadline', 'dom_class', 'price_per_m2_uzs', 'listing_url',
    'brand_name', 'delivered_year', 'holding_name',
]

FLOAT_FIELDS = {'lat', 'lng', 'price_per_m2_uzs'}


def original_value(raw_dict, field):
    """Значение поля из исходной выгрузки (до ручных правок), или None для чисто ручных полей."""
    for alias in FIELD_ALIASES.get(field, ()):
        v = raw_dict.get(alias)
        if v not in (None, ''):
            return to_float(v) if field in FLOAT_FIELDS else str(v)
    return None


app = Flask(__name__)


def get_db():
    if 'db' not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute(f"ATTACH DATABASE '{MANUAL_DB_PATH}' AS manual")
        build_manual_schema(g.db)  # на случай если app.py запущен раньше первого import_master.py
        migrate_complexes(g.db)    # подтягивает новые колонки, если БД старее текущей схемы
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
            SUM(CASE WHEN cam_id IN (SELECT cam_id FROM manual.reviews) THEN 1 ELSE 0 END) AS reviewed
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
          AND c.cam_id NOT IN (SELECT cam_id FROM manual.reviews)
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
        'INSERT INTO manual.reviews (cam_id, reviewed_at, verdict, comment) VALUES (?,?,?,?)',
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
        'INSERT OR REPLACE INTO manual.excluded_ids (cam_id, reason, excluded_at) VALUES (?,?,?)',
        (cam_id, reason, datetime.now(timezone.utc).isoformat()),
    )
    # строку НЕ удаляем — застройщик/подрядчик/ИНН должны оставаться
    # доступными для поиска даже у объектов, признанных "не ЖК"/отменённых
    db.execute(
        "UPDATE complexes SET case_status_clean='excluded', needs_review=0 WHERE cam_id=?",
        (cam_id,),
    )
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
    n = db.execute('SELECT COUNT(*) FROM manual.manual_complexes').fetchone()[0]
    cam_id = f'MAN-{n + 1:04d}'
    db.execute('''
        INSERT INTO manual.manual_complexes (cam_id, project_name, address, district_name, lat, lng, note, created_at)
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


@app.route('/complex/<cam_id>/edit', methods=['POST'])
def edit_complex(cam_id):
    db = get_db()

    reset_fields = [f for f in request.form.get('reset_fields', '').split(',') if f in EDITABLE_FIELDS]
    if reset_fields:
        null_cols = ', '.join(f'{f}=NULL' for f in reset_fields)
        db.execute(f'UPDATE manual.overrides SET {null_cols} WHERE cam_id=?', (cam_id,))

        row = db.execute('SELECT raw_json FROM complexes WHERE cam_id=?', (cam_id,)).fetchone()
        raw_dict = json.loads(row['raw_json']) if row and row['raw_json'] else {}
        restored = {f: original_value(raw_dict, f) for f in reset_fields}
        assignment = ', '.join(f'{f}=?' for f in reset_fields)
        db.execute(f'UPDATE complexes SET {assignment} WHERE cam_id=?', [restored[f] for f in reset_fields] + [cam_id])

        db.commit()
        return redirect(url_for('complex_detail', cam_id=cam_id))

    values = {f: request.form.get(f, '').strip() for f in EDITABLE_FIELDS}
    values = {f: (v if v != '' else None) for f, v in values.items()}

    cols = ', '.join(EDITABLE_FIELDS)
    placeholders = ', '.join('?' for _ in EDITABLE_FIELDS)
    update_cols = ', '.join(f'{f}=excluded.{f}' for f in EDITABLE_FIELDS)
    db.execute(f'''
        INSERT INTO manual.overrides (cam_id, {cols}, updated_at)
        VALUES (?, {placeholders}, ?)
        ON CONFLICT(cam_id) DO UPDATE SET {update_cols}, updated_at=excluded.updated_at
    ''', [cam_id] + [values[f] for f in EDITABLE_FIELDS] + [datetime.now(timezone.utc).isoformat()])

    # применяем сразу же, не дожидаясь следующего import_master.py
    sets = [(f, v) for f, v in values.items() if v is not None]
    if sets:
        assignment = ', '.join(f'{f}=?' for f, _ in sets)
        db.execute(f'UPDATE complexes SET {assignment} WHERE cam_id=?', [v for _, v in sets] + [cam_id])

    db.commit()
    return redirect(url_for('complex_detail', cam_id=cam_id))


@app.route('/complex/<cam_id>/brand_status', methods=['POST'])
def set_brand_status(cam_id):
    """Переключает отметку 'бренд не найден' — отдельная категория от статуса сдачи:
    отвечает на вопрос «есть ли у объекта коммерческое имя/листинг», а не «сдан или нет»."""
    db = get_db()
    row = db.execute('SELECT brand_status FROM complexes WHERE cam_id=?', (cam_id,)).fetchone()
    new_status = None if row and row['brand_status'] == 'not_found' else 'not_found'
    db.execute('''
        INSERT INTO manual.overrides (cam_id, brand_status, updated_at) VALUES (?,?,?)
        ON CONFLICT(cam_id) DO UPDATE SET brand_status=excluded.brand_status, updated_at=excluded.updated_at
    ''', (cam_id, new_status, datetime.now(timezone.utc).isoformat()))
    db.execute('UPDATE complexes SET brand_status=? WHERE cam_id=?', (new_status, cam_id))
    db.commit()
    return redirect(url_for('complex_detail', cam_id=cam_id))


@app.route('/complex/<cam_id>/rebrand', methods=['POST'])
def rebrand_complex(cam_id):
    form = request.form
    try:
        db = get_db()
        db.execute('''
            INSERT INTO manual.rebrands (
                cam_id, rebrand_name, rebrand_developer_name, rebrand_developer_inn,
                rebrand_contractor_name, rebrand_contractor_inn,
                old_developer_remained, old_contractor_remained, transition_date, reason,
                matched_cam_id, created_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
        ''', (
            cam_id,
            form.get('rebrand_name', '').strip() or None,
            form.get('rebrand_developer_name', '').strip() or None,
            form.get('rebrand_developer_inn', '').strip() or None,
            form.get('rebrand_contractor_name', '').strip() or None,
            form.get('rebrand_contractor_inn', '').strip() or None,
            form.get('old_developer_remained', '').strip() or None,
            form.get('old_contractor_remained', '').strip() or None,
            form.get('transition_date', '').strip() or None,
            form.get('reason', '').strip() or None,
            form.get('matched_cam_id', '').strip() or None,
            datetime.now(timezone.utc).isoformat(),
        ))
        db.execute(
            'UPDATE complexes SET case_status_clean=?, needs_review=0 WHERE cam_id=?',
            ('rebranded', cam_id),
        )
        db.commit()
    except sqlite3.Error as e:
        return render_complex_detail(cam_id, rebrand_form=form, rebrand_error=str(e))
    return redirect(url_for('complex_detail', cam_id=cam_id))


def render_complex_detail(cam_id, rebrand_form=None, rebrand_error=None):
    db = get_db()
    row = db.execute('SELECT * FROM complexes WHERE cam_id=?', (cam_id,)).fetchone()
    if row is None:
        return 'not found', 404
    raw = json.loads(row['raw_json']) if row['raw_json'] else {}
    history = db.execute(
        'SELECT * FROM manual.reviews WHERE cam_id=? ORDER BY reviewed_at DESC', (cam_id,)
    ).fetchall()
    rebrands = db.execute(
        'SELECT * FROM manual.rebrands WHERE cam_id=? ORDER BY created_at DESC', (cam_id,)
    ).fetchall()
    holdings = [r[0] for r in db.execute(
        "SELECT DISTINCT holding_name FROM complexes WHERE holding_name IS NOT NULL AND holding_name != '' ORDER BY holding_name"
    ).fetchall()]
    holdings = sorted(set(holdings) | set(get_affiliation_groups()))
    return render_template(
        'complex_detail.html', row=row, raw=raw, history=history,
        verdicts=VERDICTS, rebrands=rebrands, holdings=holdings,
        rebrand_form=rebrand_form or {}, rebrand_error=rebrand_error,
    )


@app.route('/complex/<cam_id>')
def complex_detail(cam_id):
    return render_complex_detail(cam_id)


@app.route('/all')
def all_complexes():
    db = get_db()
    q = request.args.get('q', '').strip()
    status = request.args.get('status', '').strip()
    sql = 'SELECT * FROM complexes WHERE 1=1'
    params = []
    if q:
        sql += '''
            AND (project_name LIKE ? OR address LIKE ? OR cam_id LIKE ?
                 OR developer_name LIKE ? OR developer_inn LIKE ?
                 OR contractor_name LIKE ? OR contractor_inn LIKE ?
                 OR holding_name LIKE ?)
        '''
        params += [f'%{q}%'] * 8
    if status:
        sql += ' AND case_status_clean = ?'
        params.append(status)
    sql += ' ORDER BY cam_id LIMIT 500'
    rows = db.execute(sql, params).fetchall()
    return render_template('all_complexes.html', rows=rows, q=q, status=status)


if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0')
