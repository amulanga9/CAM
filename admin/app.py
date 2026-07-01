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

import ariza_parser
import object_info_parser
from import_master import FIELD_ALIASES, build_manual_schema, migrate_complexes, to_float
from org_directory import ensure_schema as ensure_dir_schema, merge_org, normalize_inn, org_key_for, rebuild_objects_count, resolve_org

DB_PATH = Path(__file__).parent / 'cam_admin.db'
MANUAL_DB_PATH = Path(__file__).parent / 'cam_manual.db'
AFFILIATION_GROUPS_PATH = Path(__file__).parent / 'data' / 'affiliation_groups.csv'


def get_affiliation_groups(db):
    """Group labels built from commercial/brand names of complexes tied to the
    group's ИНН-ы (brand_name, falling back to developer_name) — founder full
    names aren't recognizable to a reviewer, commercial names are."""
    if not AFFILIATION_GROUPS_PATH.exists():
        return []
    inns_by_group = {}
    with open(AFFILIATION_GROUPS_PATH, encoding='utf-8') as f:
        for row in csv.DictReader(f):
            inns_by_group.setdefault(row['группа_id'], set()).add(row['ИНН'])

    labels = []
    for gid, inns in inns_by_group.items():
        placeholders = ', '.join('?' for _ in inns)
        rows = db.execute(f'''
            SELECT DISTINCT brand_name, developer_name FROM complexes
            WHERE developer_inn IN ({placeholders}) OR contractor_inn IN ({placeholders})
        ''', list(inns) * 2).fetchall()
        names = set()
        for r in rows:
            names.add(r['brand_name'] or r['developer_name'])
        names.discard(None)
        names.discard('')
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

DELAY_TYPES = {
    'unconfirmed': 'Перенос сроков (без подтверждения)',
    'confirmed': 'Долгострой / перенос с документами',
}

PROOF_TYPES = {
    'gasn_closed': 'get-gasn-info: closed_at / смена статуса',
    'suspended_registry': 'реестр приостановленных (shr-tashkent.uz)',
    'court': 'судебное дело по объекту',
    'news_named': 'новость, прямо называющая этот ЖК/застройщика/адрес',
}

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
    ensure_dir_schema(g.db)
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
        SELECT c.*, d.delay_type AS delay_type FROM complexes c
        LEFT JOIN manual.delay_flags d ON d.cam_id = c.cam_id
        WHERE c.needs_review = 1
          AND c.cam_id NOT IN (SELECT cam_id FROM manual.reviews)
        ORDER BY c.days_overdue DESC, c.cam_id
    ''').fetchall()
    return render_template('review_queue.html', rows=rows, verdicts=VERDICTS, delay_types=DELAY_TYPES)


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


@app.route('/complex/<cam_id>/delay', methods=['POST'])
def set_delay_flag(cam_id):
    form = request.form
    delay_type = form.get('delay_type')
    if delay_type not in DELAY_TYPES:
        return 'bad delay_type', 400

    proof_url = form.get('proof_url', '').strip() or None
    proof_type = form.get('proof_type', '').strip() or None
    if proof_type not in PROOF_TYPES:
        proof_type = None
    # без источника про сам объект "подтверждённый долгострой" — это догадка,
    # а не документ, поэтому понижаем до неподтверждённого вместо ошибки
    if delay_type == 'confirmed' and not (proof_url and proof_type):
        delay_type = 'unconfirmed'

    db = get_db()
    db.execute('''
        INSERT INTO manual.delay_flags (
            cam_id, delay_type, original_deadline, current_deadline,
            proof_url, proof_type, comment, updated_at,
            permit_date, smr_registration_date, application_number
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(cam_id) DO UPDATE SET
            delay_type=excluded.delay_type, original_deadline=excluded.original_deadline,
            current_deadline=excluded.current_deadline, proof_url=excluded.proof_url,
            proof_type=excluded.proof_type, comment=excluded.comment, updated_at=excluded.updated_at,
            permit_date=excluded.permit_date, smr_registration_date=excluded.smr_registration_date,
            application_number=excluded.application_number
    ''', (
        cam_id, delay_type,
        form.get('original_deadline', '').strip() or None,
        form.get('current_deadline', '').strip() or None,
        proof_url, proof_type,
        form.get('comment', '').strip() or None,
        datetime.now(timezone.utc).isoformat(),
        form.get('permit_date', '').strip() or None,
        form.get('smr_registration_date', '').strip() or None,
        form.get('application_number', '').strip() or None,
    ))
    db.commit()
    return redirect(url_for('complex_detail', cam_id=cam_id))


@app.route('/complex/<cam_id>/delay/parse-ariza', methods=['POST'])
def parse_ariza_pdf(cam_id):
    """Загрузка PDF выписки (ariza/ko'chirma) — поля вытаскиваются автоматически
    и сохраняются как черновик в delay_flags (delay_type не трогаем, если
    запись уже есть — пользователь сам решает unconfirmed/confirmed отдельно)."""
    f = request.files.get('ariza_pdf')
    if not f or not f.filename:
        return 'нет файла', 400
    try:
        parsed = ariza_parser.parse_file(f.stream)
    except Exception as e:
        return render_complex_detail(cam_id, delay_parse_error=str(e))

    db = get_db()
    existing = db.execute(
        'SELECT delay_type FROM manual.delay_flags WHERE cam_id=?', (cam_id,)
    ).fetchone()
    delay_type = existing['delay_type'] if existing else 'unconfirmed'
    db.execute('''
        INSERT INTO manual.delay_flags (
            cam_id, delay_type, application_number, smr_registration_date,
            doc_hash, applicant_name, tax_id, updated_at
        ) VALUES (?,?,?,?,?,?,?,?)
        ON CONFLICT(cam_id) DO UPDATE SET
            application_number=excluded.application_number,
            smr_registration_date=excluded.smr_registration_date,
            doc_hash=excluded.doc_hash, applicant_name=excluded.applicant_name,
            tax_id=excluded.tax_id, updated_at=excluded.updated_at
    ''', (
        cam_id, delay_type,
        parsed.get('application_number'), parsed.get('created_at'),
        parsed.get('doc_hash'), parsed.get('applicant_name'), parsed.get('tax_id'),
        datetime.now(timezone.utc).isoformat(),
    ))
    db.commit()
    return redirect(url_for('complex_detail', cam_id=cam_id))


@app.route('/complex/<cam_id>/delay/fetch-portal', methods=['POST'])
def fetch_portal_info(cam_id):
    """Автоматически подтягивает object-info с nazorat.mc.uz (permit_date,
    portal_deadline, номера экспертизы/АПЗ) и, если получится, саму PDF
    выписку (smr_registration_date/application_number/doc_hash/applicant_name/
    tax_id) — без ручного скачивания/загрузки файлов пользователем. Работает
    только для GASN-объектов (cam_id вида GASN-<object_id>), у других нет
    object_id для портала."""
    object_id = object_info_parser.object_id_from_cam_id(cam_id)
    if not object_id:
        return render_complex_detail(cam_id, delay_parse_error='нет object_id портала для этого объекта (не GASN)')

    try:
        info = object_info_parser.fetch(object_id)
    except Exception as e:
        return render_complex_detail(cam_id, delay_parse_error=f'не удалось получить object-info: {e}')

    ariza_fields = {}
    try:
        pdf_bytes = object_info_parser.fetch_pdf_bytes(object_id)
        if pdf_bytes:
            import io
            ariza_fields = ariza_parser.parse_file(io.BytesIO(pdf_bytes))
    except Exception:
        pass  # выписки может не быть на портале — это не ошибка получения object-info

    db = get_db()
    existing = db.execute(
        'SELECT delay_type FROM manual.delay_flags WHERE cam_id=?', (cam_id,)
    ).fetchone()
    delay_type = existing['delay_type'] if existing else 'unconfirmed'
    db.execute('''
        INSERT INTO manual.delay_flags (
            cam_id, delay_type, permit_date, portal_deadline,
            expertise_number, apz_number, application_number,
            smr_registration_date, doc_hash, applicant_name, tax_id,
            updated_at, portal_fetched_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(cam_id) DO UPDATE SET
            permit_date=excluded.permit_date, portal_deadline=excluded.portal_deadline,
            expertise_number=excluded.expertise_number, apz_number=excluded.apz_number,
            application_number=COALESCE(excluded.application_number, manual.delay_flags.application_number),
            smr_registration_date=COALESCE(excluded.smr_registration_date, manual.delay_flags.smr_registration_date),
            doc_hash=COALESCE(excluded.doc_hash, manual.delay_flags.doc_hash),
            applicant_name=COALESCE(excluded.applicant_name, manual.delay_flags.applicant_name),
            tax_id=COALESCE(excluded.tax_id, manual.delay_flags.tax_id),
            updated_at=excluded.updated_at, portal_fetched_at=excluded.portal_fetched_at
    ''', (
        cam_id, delay_type, info.get('permit_date'), info.get('portal_deadline'),
        info.get('expertise_number'), info.get('apz_number'),
        ariza_fields.get('application_number'), ariza_fields.get('created_at'),
        ariza_fields.get('doc_hash'), ariza_fields.get('applicant_name'), ariza_fields.get('tax_id'),
        datetime.now(timezone.utc).isoformat(), datetime.now(timezone.utc).isoformat(),
    ))
    db.commit()
    return redirect(url_for('complex_detail', cam_id=cam_id))


def dedupe_phases(phases):
    """Несколько листов могут содержать буквально одну и ту же запись
    (тот же статус/дедлайн/факт. сдача) — это не разные корпуса, а просто
    повтор строки в исходнике. Схлопываем такие повторы в одну фазу со
    списком источников, иначе в карточке появляются фиктивные "Корпус N"."""
    merged = {}
    order = []
    for p in phases:
        sig = (p['corpus_label'], p['case_status_clean'], p['deadline'], p['delivery_date_fact'])
        if sig not in merged:
            merged[sig] = dict(p)
            merged[sig]['sources'] = [p['source_sheet']]
            order.append(sig)
        else:
            merged[sig]['sources'].append(p['source_sheet'])
    return [merged[sig] for sig in order]


def render_complex_detail(cam_id, rebrand_form=None, rebrand_error=None, delay_parse_error=None):
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
    phases = dedupe_phases(db.execute(
        'SELECT * FROM phases WHERE cam_id=? ORDER BY id', (cam_id,)
    ).fetchall())
    delay_flag = db.execute(
        'SELECT * FROM manual.delay_flags WHERE cam_id=?', (cam_id,)
    ).fetchone()
    holdings = [r[0] for r in db.execute(
        "SELECT DISTINCT holding_name FROM complexes WHERE holding_name IS NOT NULL AND holding_name != '' ORDER BY holding_name"
    ).fetchall()]
    holdings = sorted(set(holdings) | set(get_affiliation_groups(db)))
    return render_template(
        'complex_detail.html', row=row, raw=raw, history=history,
        verdicts=VERDICTS, rebrands=rebrands, phases=phases, holdings=holdings,
        delay_flag=delay_flag, delay_types=DELAY_TYPES, proof_types=PROOF_TYPES,
        rebrand_form=rebrand_form or {}, rebrand_error=rebrand_error,
        delay_parse_error=delay_parse_error,
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


# ─── Справочник организаций ───────────────────────────────────────────────────

@app.route('/directory')
def directory():
    db = get_db()
    role = request.args.get('role', 'developer')
    q = request.args.get('q', '').strip()
    review_only = request.args.get('review', '') == '1'
    sql = '''
        SELECT d.role, d.org_key, d.key_type, d.name_canonical,
               d.rating, d.objects_count, d.bad_pct, d.overdue_pct,
               d.first_seen, d.last_seen, d.needs_review,
               COUNT(a.raw_name) AS alias_count
        FROM org_directory d
        LEFT JOIN org_aliases a ON a.role=d.role AND a.org_key=d.org_key
        WHERE d.role=?
    '''
    params = [role]
    if q:
        sql += ' AND (d.name_canonical LIKE ? OR d.org_key LIKE ?)'
        params += [f'%{q}%', f'%{q}%']
    if review_only:
        sql += ' AND d.needs_review=1'
    sql += ' GROUP BY d.role, d.org_key ORDER BY d.objects_count DESC, d.name_canonical'
    rows = db.execute(sql, params).fetchall()
    counts = {
        r[0]: r[1]
        for r in db.execute(
            'SELECT role, COUNT(*) FROM org_directory GROUP BY role'
        )
    }
    review_counts = {
        r[0]: r[1]
        for r in db.execute(
            'SELECT role, COUNT(*) FROM org_directory WHERE needs_review=1 GROUP BY role'
        )
    }
    return render_template('directory.html', rows=rows, role=role, q=q,
                           review_only=review_only, counts=counts,
                           review_counts=review_counts)


@app.route('/directory/<role>/<path:org_key>')
def directory_detail(role, org_key):
    db = get_db()
    org = db.execute(
        'SELECT * FROM org_directory WHERE role=? AND org_key=?', (role, org_key)
    ).fetchone()
    if not org:
        return 'Организация не найдена', 404
    aliases = db.execute(
        'SELECT raw_name, source, first_seen, last_seen FROM org_aliases '
        'WHERE role=? AND org_key=? ORDER BY last_seen DESC',
        (role, org_key)
    ).fetchall()
    name_col = 'developer_name' if role == 'developer' else 'contractor_name'
    inn_col = 'developer_inn' if role == 'developer' else 'contractor_inn'
    if org['key_type'] == 'inn':
        objects = db.execute(
            f'SELECT cam_id, project_name, {name_col}, case_status_clean FROM complexes '
            f'WHERE {inn_col}=? ORDER BY cam_id',
            (org_key,)
        ).fetchall()
    else:
        norm_key = org_key[len('NORM:'):]
        objects = db.execute(
            f'SELECT cam_id, project_name, {name_col}, case_status_clean FROM complexes '
            f'WHERE {inn_col} IS NULL OR {inn_col}="" ORDER BY cam_id'
        ).fetchall()
        from org_directory import normalize_org_name
        objects = [o for o in objects if normalize_org_name(o[name_col]) == norm_key]
    all_orgs = db.execute(
        'SELECT org_key, name_canonical FROM org_directory WHERE role=? AND org_key!=? ORDER BY name_canonical',
        (role, org_key)
    ).fetchall()
    return render_template('directory_detail.html', org=org, aliases=aliases,
                           objects=objects, role=role, all_orgs=all_orgs)


@app.route('/directory/<role>/<path:org_key>/edit', methods=['POST'])
def directory_edit(role, org_key):
    db = get_db()
    name_canonical = request.form.get('name_canonical', '').strip()
    rating = request.form.get('rating', '').strip() or None
    if name_canonical:
        db.execute(
            'UPDATE org_directory SET name_canonical=?, rating=? WHERE role=? AND org_key=?',
            (name_canonical, rating, role, org_key)
        )
        db.commit()
    return redirect(url_for('directory_detail', role=role, org_key=org_key))


@app.route('/directory/<role>/<path:org_key>/confirm', methods=['POST'])
def directory_confirm(role, org_key):
    db = get_db()
    db.execute(
        'UPDATE org_directory SET needs_review=0 WHERE role=? AND org_key=?',
        (role, org_key)
    )
    db.commit()
    return redirect(request.referrer or url_for('directory', role=role))


@app.route('/directory/<role>/<path:org_key>/merge', methods=['POST'])
def directory_merge(role, org_key):
    """Слить текущую карточку (src) в выбранную dst — алиасы переходят, src удаляется."""
    db = get_db()
    dst_key = request.form.get('dst_key', '').strip()
    if not dst_key or dst_key == org_key:
        return redirect(url_for('directory_detail', role=role, org_key=org_key))
    dst = db.execute(
        'SELECT 1 FROM org_directory WHERE role=? AND org_key=?', (role, dst_key)
    ).fetchone()
    if not dst:
        return 'Целевая карточка не найдена', 404
    merge_org(db, role, org_key, dst_key)
    rebuild_objects_count(db)
    return redirect(url_for('directory_detail', role=role, org_key=dst_key))


@app.route('/directory/rebuild', methods=['POST'])
def directory_rebuild():
    """Перестроить objects_count по текущим complexes."""
    db = get_db()
    rebuild_objects_count(db)
    return redirect(url_for('directory'))


if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0')
