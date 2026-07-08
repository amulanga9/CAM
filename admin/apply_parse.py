"""
Применяет свежий парс Shaffof (data/raw/YYYY-MM/shaffof/*.csv) к базе.

Матчинг: object_id парса → shaffof_ids в raw_json карточки.
Для каждой совпавшей карточки (логика проверена в песочнице,
30 реальных изменений вместо 460 ложных за счёт нормализации юрформ):

  1. смена застройщика/подрядчика (нормализованное сравнение) →
     запись в manual.change_history + обновление поля;
  2. пересчёт is_overdue / days_overdue / days_remaining по свежему дедлайну;
  3. новый статус Topshirilgan → case_status_clean='delivered', target=1;
  4. обновление blocks_total/blocks_accepted в raw_json.

Несовпавшие object_id складываются в data/raw/YYYY-MM/unmatched.csv —
это кандидаты на новые карточки (создаются отдельно, вручную).

Запуск:
    python3 apply_parse.py                # текущий месяц
    python3 apply_parse.py 2026-06        # конкретный месяц
    python3 apply_parse.py 2026-06 --dry  # показать без записи
"""
import csv
import json
import os
import re
import sqlite3
import sys
from datetime import date, datetime, timezone
from pathlib import Path

BASE = Path(__file__).resolve().parent

LEGAL_FORM_RE = re.compile(
    r"\b(mas['`‘’]?uliyati\s*cheklangan\s*jamiyat(i)?|qo['`‘’]?shma\s*korxona(si)?|"
    r"xususiy\s*korxona(si)?|mchj|ooo|ооо|мчж|xk|хк|хорижий корхона|xorijiy korxona|"
    r"yakka tartibdagi tadbirkor|aksiyadorlik\s*jamiyat(i)?|"
    r"чп|оао|пто|ип|сп|sp\b)\b",
    re.IGNORECASE)

STATUS_MAP = {
    'Topshirilgan': 'delivered',
    "To'xtatilgan": 'stopped',
    'Jarayonda': 'in_progress',
    'Bekor qilingan': 'cancelled',
    'Muzlatilgan': 'frozen',
}


def normalize_org_name(name):
    if not name:
        return ''
    n = name.strip().strip('"«»‘’“”`\'').lower()
    n = LEGAL_FORM_RE.sub(' ', n)
    n = re.sub(r'[^\w\s]', ' ', n)
    n = re.sub(r'\s+', ' ', n).strip()
    tokens = n.split(' ')
    if len(tokens) > 1 and tokens[-1] == 'i':
        tokens = tokens[:-1]
    return ' '.join(tokens)


def compute_deadline_metrics(deadline_str):
    if not deadline_str:
        return False, None, None
    try:
        deadline = datetime.strptime(str(deadline_str)[:10], '%Y-%m-%d').date()
    except ValueError:
        return False, None, None
    delta = (date.today() - deadline).days
    if delta > 0:
        return True, delta, None
    return False, None, -delta


def load_raw(month):
    raw_dir = BASE / 'data' / 'raw' / month / 'shaffof'
    files = sorted(raw_dir.glob('*.csv'))
    if not files:
        raise FileNotFoundError(f'нет CSV в {raw_dir} — сначала python3 parse_shaffof.py')
    rows = {}
    for f in files:
        with open(f, encoding='utf-8-sig') as fh:
            for r in csv.DictReader(fh):
                rows[str(r['object_id'])] = r  # дедуп: последний выигрывает
    return rows, raw_dir.parent


def load_overrides(conn):
    """cam_id -> set(полей, исправленных вручную). Эти поля парс НЕ трогает."""
    protected = {}
    cols = [r[1] for r in conn.execute('PRAGMA manual.table_info(overrides)')
            if r[1] not in ('cam_id', 'updated_at')]
    for row in conn.execute(f"SELECT cam_id, {', '.join(cols)} FROM manual.overrides"):
        fields = {c for c, v in zip(cols, row[1:]) if v not in (None, '')}
        if fields:
            protected[row[0]] = fields
    return protected


def shaffof_id_index(conn):
    """object_id (str) -> cam_id по raw_json.shaffof_ids."""
    idx = {}
    for cam_id, rj in conn.execute('SELECT cam_id, raw_json FROM complexes'):
        raw = json.loads(rj) if rj else {}
        ids = raw.get('shaffof_ids') or []
        if isinstance(ids, str):
            ids = re.findall(r'\d+', ids)
        for sid in ids:
            idx[str(sid)] = cam_id
    return idx


def ensure_permits_schema(conn):
    """Сущность «разрешение/очередь»: каждый портальный object_id живёт и
    отслеживается отдельно ВНУТРИ карточки ЖК (статус, дедлайн, корпуса)."""
    conn.execute('''
        CREATE TABLE IF NOT EXISTS permits (
            object_id        TEXT PRIMARY KEY,   -- id на портале
            cam_id           TEXT,               -- к какому ЖК привязано
            name             TEXT,               -- напр. "Manhattan, Блок 7"
            status           TEXT,
            status_clean     TEXT,
            created_at       TEXT,
            deadline         TEXT,
            closed_at        TEXT,
            blocks_total     INTEGER,
            blocks_accepted  INTEGER,
            apartment_count  INTEGER,
            floor_max        TEXT,
            first_seen_month TEXT,
            last_seen_month  TEXT,
            vanished         INTEGER DEFAULT 0   -- 1 = пропал из свежего парса
        )''')


def upsert_permits(conn, parse_rows, idx, month):
    """Обновляет permits из свежего парса; отмечает исчезнувшие."""
    ensure_permits_schema(conn)
    for oid, r in parse_rows.items():
        cam_id = idx.get(oid)
        conn.execute('''
            INSERT INTO permits (object_id, cam_id, name, status, status_clean,
                created_at, deadline, closed_at, blocks_total, blocks_accepted,
                apartment_count, floor_max, first_seen_month, last_seen_month, vanished)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,0)
            ON CONFLICT(object_id) DO UPDATE SET
                cam_id=COALESCE(excluded.cam_id, cam_id),
                name=excluded.name, status=excluded.status,
                status_clean=excluded.status_clean,
                deadline=excluded.deadline, closed_at=excluded.closed_at,
                blocks_total=excluded.blocks_total,
                blocks_accepted=excluded.blocks_accepted,
                apartment_count=excluded.apartment_count,
                floor_max=excluded.floor_max,
                last_seen_month=excluded.last_seen_month, vanished=0
        ''', (oid, cam_id, (r['name'] or '')[:200], r['status'] or None,
              STATUS_MAP.get((r['status'] or '').strip(), 'unclear'),
              (r['created_at'] or '')[:10] or None,
              (r['deadline'] or '')[:10] or None,
              (r['closed_at'] or '')[:10] or None,
              int(float(r['blocks_total'] or 0)) or None,
              int(float(r['blocks_accepted'] or 0)),
              int(float(r['apartment_count'] or 0)) or None,
              r['floor_max'] or None, month, month))
    # исчезнувшие: были в permits, в этом парсе нет
    conn.execute(
        'UPDATE permits SET vanished=1 WHERE last_seen_month < ? '
        'AND object_id NOT IN (SELECT object_id FROM permits WHERE last_seen_month = ?)',
        (month, month))


def apply(month=None, dry=False, create_new=True):
    month = month or date.today().strftime('%Y-%m')
    admin_conn = sqlite3.connect(BASE / 'cam_admin.db')
    admin_conn.row_factory = sqlite3.Row
    admin_conn.execute(f"ATTACH '{BASE / 'cam_manual.db'}' AS manual")
    admin_conn.execute('''
        CREATE TABLE IF NOT EXISTS manual.change_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            cam_id TEXT NOT NULL, field TEXT NOT NULL,
            old_value TEXT, new_value TEXT,
            changed_at TEXT, snapshot_date TEXT, source TEXT)''')
    cols = {r[1] for r in admin_conn.execute('PRAGMA table_info(complexes)')}
    if 'days_remaining' not in cols:
        admin_conn.execute('ALTER TABLE complexes ADD COLUMN days_remaining INTEGER')

    parse_rows, out_dir = load_raw(month)
    idx = shaffof_id_index(admin_conn)
    protected = load_overrides(admin_conn)
    now = datetime.now(timezone.utc).isoformat()

    # группируем строки парса по карточкам
    per_card, unmatched = {}, []
    for oid, r in parse_rows.items():
        cam_id = idx.get(oid)
        if cam_id:
            per_card.setdefault(cam_id, []).append(r)
        else:
            unmatched.append(r)

    history = deadlines = promoted = 0
    for cam_id, rows in per_card.items():
        cur = admin_conn.execute(
            'SELECT * FROM complexes WHERE cam_id=?', (cam_id,)).fetchone()
        if cur is None:
            continue

        # агрегация по нескольким разрешениям одной карточки
        statuses = {STATUS_MAP.get((r['status'] or '').strip(), 'unclear') for r in rows}
        new_status = 'delivered' if statuses == {'delivered'} else (
            sorted(statuses - {'delivered'})[0] if statuses - {'delivered'} else 'delivered')
        deadline = max((r['deadline'] or '' for r in rows)) or None
        bt = sum(int(float(r['blocks_total'] or 0)) for r in rows)
        ba = sum(int(float(r['blocks_accepted'] or 0)) for r in rows)
        # детерминированный выбор главного разрешения (минимальный object_id),
        # иначе у карточек с несколькими разрешениями застройщик «прыгает»
        rows = sorted(rows, key=lambda r: int(r['object_id']))
        main = rows[0]
        new_dev = main['organization_name'] or None
        new_pud = main['pudrat_name'] or main['pudrat_direct'] or None
        new_pud_inn = str(main['pudrat_inn'] or '').strip() or None

        prot = protected.get(cam_id, set())

        # дедлайн: если исправлен вручную — метрики считаем от ручного
        if 'deadline' in prot:
            deadline = cur['deadline']
        sets, params = [], []
        # для уже сданных (в т.ч. подтверждённых вручную через /review) не
        # пересчитываем is_overdue/days_overdue от "сегодня" — иначе просрочка
        # растёт бесконечно каждую неделю даже после реальной сдачи объекта.
        # Метрики остаются такими, какими были на момент подтверждения сдачи.
        if cur['case_status_clean'] != 'delivered':
            is_overdue, days_overdue, days_remaining = compute_deadline_metrics(deadline)
            sets += ['is_overdue=?', 'days_overdue=?', 'days_remaining=?']
            params += [int(is_overdue), days_overdue, days_remaining]
        if deadline and 'deadline' not in prot:
            new_dl = str(deadline)[:10]
            old_dl = (cur['deadline'] or '')[:10]
            if old_dl and new_dl != old_dl and not dry:
                admin_conn.execute(
                    'INSERT INTO manual.change_history '
                    '(cam_id, field, old_value, new_value, changed_at, snapshot_date, source) '
                    'VALUES (?,?,?,?,?,?,?)',
                    (cam_id, 'deadline', old_dl, new_dl, now, month, 'weekly_parse'))
            sets.append('deadline=?')
            params.append(new_dl)
        deadlines += 1

        if new_status == 'delivered' and cur['case_status_clean'] != 'delivered':
            sets += ['case_status_clean=?', 'target=?']
            params += ['delivered', 1]
            promoted += 1
            if not dry:
                admin_conn.execute(
                    'INSERT INTO manual.change_history '
                    '(cam_id, field, old_value, new_value, changed_at, snapshot_date, source) '
                    'VALUES (?,?,?,?,?,?,?)',
                    (cam_id, 'case_status_clean', cur['case_status_clean'], 'delivered',
                     now, month, 'weekly_parse'))

        for field, old_val, new_val, norm in (
            ('developer_name', cur['developer_name'], new_dev, True),
            ('contractor_name', cur['contractor_name'], new_pud, True),
            ('contractor_inn', cur['contractor_inn'], new_pud_inn, False),
        ):
            if not new_val:
                continue
            oc = normalize_org_name(old_val) if norm else str(old_val or '').strip()
            nc = normalize_org_name(new_val) if norm else str(new_val).strip()

            # поле исправлено вручную: НЕ перезаписываем; если портал не согласен —
            # одна запись в историю (source=parse_conflict_manual), без дублей
            if field in prot:
                if old_val and oc and nc and oc != nc and not dry:
                    dup = admin_conn.execute(
                        'SELECT 1 FROM manual.change_history '
                        'WHERE cam_id=? AND field=? AND new_value=? AND source=?',
                        (cam_id, field, new_val, 'parse_conflict_manual')).fetchone()
                    if not dup:
                        admin_conn.execute(
                            'INSERT INTO manual.change_history '
                            '(cam_id, field, old_value, new_value, changed_at, snapshot_date, source) '
                            'VALUES (?,?,?,?,?,?,?)',
                            (cam_id, field, old_val, new_val, now, month, 'parse_conflict_manual'))
                continue

            if not old_val:
                sets.append(f'{field}=?')
                params.append(new_val)
                continue
            if oc and nc and oc != nc:
                if not dry:
                    admin_conn.execute(
                        'INSERT INTO manual.change_history '
                        '(cam_id, field, old_value, new_value, changed_at, snapshot_date, source) '
                        'VALUES (?,?,?,?,?,?,?)',
                        (cam_id, field, old_val, new_val, now, month, 'weekly_parse'))
                history += 1
                sets.append(f'{field}=?')
                params.append(new_val)

        # blocks в raw_json
        raw = json.loads(cur['raw_json']) if cur['raw_json'] else {}
        if bt:
            raw['blocks_total'] = bt
            raw['blocks_accepted_for_card'] = ba
            raw['last_parse_month'] = month
            sets.append('raw_json=?')
            params.append(json.dumps(raw, ensure_ascii=False))

        if not dry and sets:
            params.append(cam_id)
            admin_conn.execute(
                f'UPDATE complexes SET {", ".join(sets)} WHERE cam_id=?', params)

    # ── новые объекты: прицепить к существующей карточке или выдать CAM ID ──
    attached = created = 0
    if create_new and unmatched:
        import math

        existing = [dict(r) for r in admin_conn.execute(
            'SELECT cam_id, developer_inn, lat, lng, raw_json FROM complexes '
            'WHERE lat IS NOT NULL')]

        def dist_m(la1, lo1, la2, lo2):
            dlat = (la2 - la1) * 111000
            dlng = (lo2 - lo1) * 111000 * math.cos(math.radians(la1))
            return math.sqrt(dlat ** 2 + dlng ** 2)

        still_unmatched = []
        for r in unmatched:
            oid = str(r['object_id'])
            try:
                la, lo = float(r['lat']), float(r['long'])
            except (TypeError, ValueError):
                la = lo = None
            dev_inn = str(r.get('pudrat_inn') or '').strip()  # ИНН застройщика в парсе нет,
            org_name = r.get('organization_name') or ''

            # 1) попытка прицепить к существующей карточке: <150 м и совпадает
            #    нормализованный застройщик (organization_name)
            host = None
            if la and lo:
                on = normalize_org_name(org_name)
                for e in existing:
                    if not e['lat'] or not e['lng']:
                        continue
                    if dist_m(la, lo, e['lat'], e['lng']) < 150:
                        raw_e = json.loads(e['raw_json']) if e['raw_json'] else {}
                        en = normalize_org_name(raw_e.get('developer_name_norm') or '')
                        if on and en and (on == en or on in en or en in on):
                            host = e['cam_id']
                            break
            if host:
                if not dry:
                    row_h = admin_conn.execute(
                        'SELECT raw_json FROM complexes WHERE cam_id=?', (host,)).fetchone()
                    raw_h = json.loads(row_h['raw_json']) if row_h['raw_json'] else {}
                    ids = raw_h.get('shaffof_ids') or []
                    if isinstance(ids, str):
                        ids = re.findall(r'\d+', ids)
                    if oid not in [str(i) for i in ids]:
                        raw_h['shaffof_ids'] = sorted({str(i) for i in ids} | {oid})
                        admin_conn.execute(
                            'UPDATE complexes SET raw_json=? WHERE cam_id=?',
                            (json.dumps(raw_h, ensure_ascii=False), host))
                attached += 1
                idx[oid] = host   # permits привяжутся этим же прогоном
                continue

            # 2) новая карточка: CAM ID = GASN-<object_id> (стабильный, от портала)
            new_cam_id = f'GASN-{oid}'
            if admin_conn.execute('SELECT 1 FROM complexes WHERE cam_id=?',
                                  (new_cam_id,)).fetchone():
                continue
            status_clean = STATUS_MAP.get((r['status'] or '').strip(), 'unclear')
            is_ov, d_ov, d_rem = compute_deadline_metrics(r['deadline'])
            raw_new = dict(r)
            raw_new['shaffof_ids'] = [oid]
            raw_new['first_parse_month'] = month
            if not dry:
                admin_conn.execute(
                    '''INSERT INTO complexes
                       (cam_id, source_sheet, project_name, lat, lng,
                        developer_name, contractor_name, contractor_inn,
                        created_at, deadline, case_status_raw, case_status_clean,
                        target, is_overdue, days_overdue, days_remaining,
                        needs_review, raw_json)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                    (new_cam_id, 'GASN_new', (r['name'] or '')[:300],
                     la, lo, org_name or None,
                     r.get('pudrat_name') or r.get('pudrat_direct') or None,
                     dev_inn or None,
                     (r['created_at'] or '')[:10] or None,
                     (r['deadline'] or '')[:10] or None,
                     r['status'] or None, status_clean,
                     0, int(is_ov), d_ov, d_rem, 1,
                     json.dumps(raw_new, ensure_ascii=False)))
            created += 1
            idx[oid] = new_cam_id   # permits привяжутся этим же прогоном
            still_unmatched.append(r)
        unmatched = [] if not dry else unmatched

    # ── исчезновения: id был привязан к карточке, но в новом парсе его нет ──
    #    (кейс Манхэттена: блоки 5 и 6 удалены с портала молча, 404 в API)
    vanished = 0
    if not dry:
        admin_conn.execute('''
            CREATE TABLE IF NOT EXISTS manual.data_issues (
                fingerprint TEXT PRIMARY KEY,
                check_id    TEXT NOT NULL,
                severity    TEXT NOT NULL,
                entity_type TEXT NOT NULL,
                entity_id   TEXT NOT NULL,
                message     TEXT,
                status      TEXT DEFAULT 'new',
                created_at  TEXT,
                resolved_at TEXT
            )''')
        import hashlib
        parsed_ids = set(parse_rows.keys())
        missing = {sid: cam_id for sid, cam_id in idx.items() if sid not in parsed_ids}
        # предохранитель: если «исчезло» слишком много — вероятно, парс
        # неполный (оборвался/узкий диапазон), а не массовое удаление
        if len(missing) > max(20, len(idx) * 0.10):
            print(f'ВНИМАНИЕ: {len(missing)} из {len(idx)} привязанных id нет в парсе — '
                  f'похоже на неполный парс, исчезновения НЕ регистрирую')
            missing = {}
        for sid, cam_id in missing.items():
            fp = hashlib.md5(f'vanished_from_portal|{cam_id}|{sid}'.encode()).hexdigest()[:16]
            if admin_conn.execute('SELECT 1 FROM manual.data_issues WHERE fingerprint=?',
                                  (fp,)).fetchone():
                continue
            admin_conn.execute(
                'INSERT INTO manual.data_issues '
                '(fingerprint, check_id, severity, entity_type, entity_id, message, status, created_at) '
                "VALUES (?,?,?,?,?,?,'new',date('now'))",
                (fp, 'vanished_from_portal', 'high', 'complex', cam_id,
                 f'{cam_id}: портальный id={sid} был в прошлых парсах, '
                 f'в парсе {month} отсутствует — объект удалён с портала?'))
            vanished += 1
        if vanished:
            print(f'ИСЧЕЗЛО с портала (id были, теперь нет): {vanished} — в Инспектор')

    # ── permits: каждое разрешение отслеживается отдельно внутри карточки ──
    if not dry:
        upsert_permits(admin_conn, parse_rows, idx, month)

    # ── синхронизация справочника: новые застройщики/подрядчики из парса
    #    получают карточки (needs_review=1), у старых обновляется last_seen ──
    if not dry:
        from org_directory import seed_from_complexes
        before_orgs = admin_conn.execute('SELECT COUNT(*) FROM org_directory').fetchone()[0]
        seed_from_complexes(admin_conn)
        new_orgs = admin_conn.execute('SELECT COUNT(*) FROM org_directory').fetchone()[0] - before_orgs
        if new_orgs:
            print(f'новых организаций в справочнике: {new_orgs}')

    if not dry:
        admin_conn.execute('''
            CREATE TABLE IF NOT EXISTS manual.parse_runs (
                month      TEXT PRIMARY KEY,
                run_at     TEXT,
                matched    INTEGER, history  INTEGER, promoted INTEGER,
                attached   INTEGER, created  INTEGER
            )''')
        admin_conn.execute(
            'INSERT INTO manual.parse_runs (month, run_at, matched, history, promoted, attached, created) '
            'VALUES (?,?,?,?,?,?,?) '
            'ON CONFLICT(month) DO UPDATE SET run_at=excluded.run_at, '
            'matched=excluded.matched, '
            'history=history+excluded.history, promoted=promoted+excluded.promoted, '
            'attached=attached+excluded.attached, created=created+excluded.created',
            (month, now, len(per_card), history, promoted, attached, created))
        admin_conn.commit()
        if unmatched:
            path = out_dir / 'unmatched.csv'
            with open(path, 'w', newline='', encoding='utf-8') as f:
                w = csv.DictWriter(f, fieldnames=list(unmatched[0].keys()))
                w.writeheader()
                w.writerows(unmatched)

    print(f'строк парса: {len(parse_rows)} | совпало карточек: {len(per_card)}')
    print(f'дедлайны/просрочки обновлены: {deadlines}')
    print(f'историй смены застройщика/подрядчика: {history}')
    print(f'переведено в «сдан» (target=1): {promoted}')
    if create_new:
        print(f'прицеплено к существующим карточкам: {attached}')
        print(f'создано новых карточек (GASN-*, на проверку): {created}')
    return {'matched': len(per_card), 'unmatched': len(unmatched),
            'history': history, 'promoted': promoted,
            'attached': attached, 'created': created}


if __name__ == '__main__':
    month_arg = next((a for a in sys.argv[1:] if re.match(r'\d{4}-\d{2}$', a)), None)
    apply(month_arg, dry='--dry' in sys.argv)
