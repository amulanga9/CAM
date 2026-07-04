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


def apply(month=None, dry=False):
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
        main = rows[0]
        new_dev = main['organization_name'] or None
        new_pud = main['pudrat_name'] or main['pudrat_direct'] or None
        new_pud_inn = str(main['pudrat_inn'] or '').strip() or None

        is_overdue, days_overdue, days_remaining = compute_deadline_metrics(deadline)
        sets = ['is_overdue=?', 'days_overdue=?', 'days_remaining=?']
        params = [int(is_overdue), days_overdue, days_remaining]
        if deadline:
            sets.append('deadline=?')
            params.append(str(deadline)[:10])
        deadlines += 1

        if new_status == 'delivered' and cur['case_status_clean'] != 'delivered':
            sets += ['case_status_clean=?', 'target=?']
            params += ['delivered', 1]
            promoted += 1

        for field, old_val, new_val, norm in (
            ('developer_name', cur['developer_name'], new_dev, True),
            ('contractor_name', cur['contractor_name'], new_pud, True),
            ('contractor_inn', cur['contractor_inn'], new_pud_inn, False),
        ):
            if not new_val:
                continue
            if not old_val:
                sets.append(f'{field}=?')
                params.append(new_val)
                continue
            oc = normalize_org_name(old_val) if norm else str(old_val).strip()
            nc = normalize_org_name(new_val) if norm else str(new_val).strip()
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

        if not dry:
            params.append(cam_id)
            admin_conn.execute(
                f'UPDATE complexes SET {", ".join(sets)} WHERE cam_id=?', params)

    if not dry:
        admin_conn.commit()
        if unmatched:
            path = out_dir / 'unmatched.csv'
            with open(path, 'w', newline='', encoding='utf-8') as f:
                w = csv.DictWriter(f, fieldnames=list(unmatched[0].keys()))
                w.writeheader()
                w.writerows(unmatched)

    print(f'строк парса: {len(parse_rows)} | совпало карточек: {len(per_card)} '
          f'| несовпавших объектов: {len(unmatched)}')
    print(f'дедлайны/просрочки обновлены: {deadlines}')
    print(f'историй смены застройщика/подрядчика: {history}')
    print(f'переведено в «сдан» (target=1): {promoted}')
    if unmatched and not dry:
        print(f'кандидаты на новые карточки: data/raw/{month}/unmatched.csv')
    return {'matched': len(per_card), 'unmatched': len(unmatched),
            'history': history, 'promoted': promoted}


if __name__ == '__main__':
    month_arg = next((a for a in sys.argv[1:] if re.match(r'\d{4}-\d{2}$', a)), None)
    apply(month_arg, dry='--dry' in sys.argv)
