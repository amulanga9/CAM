"""
Инспектор данных — автоматические проверки, выводящие подозрительное на
ручную проверку.

Каждая проверка возвращает список "находок". Находки складываются в
manual.data_issues с отпечатком (fingerprint): повторный прогон не создаёт
дублей и не воскрешает то, что вы уже разобрали (confirmed/dismissed).

Прогонять: кнопкой в админке или `python3 inspector.py`.
"""
import hashlib
import json
from datetime import date

SEVERITIES = ('high', 'medium', 'low')

CHECK_LABELS = {
    'liquidated_developer': 'Ликвидированный застройщик у активного ЖК',
    'company_younger_than_object': 'Компания зарегистрирована ПОЗЖЕ старта стройки',
    'deadline_before_start': 'Дедлайн раньше начала строительства',
    'construction_too_long': 'Стройка длиннее 8 лет по плану',
    'overdue_2y_in_progress': 'Просрочка 2+ года, статус всё ещё «строится»',
    'blocks_mismatch': 'Число корпусов расходится с Shaffof',
    'no_coords': 'Нет координат',
    'bad_inn': 'Подозрительный ИНН (не 9 цифр)',
    'no_developer': 'Нет застройщика у активного объекта',
    'young_company_big_project': 'Компании меньше 2 лет, а объект крупный (5+ корпусов)',
    'price_outlier': 'Цена м² сильно выбивается из рынка',
}


def _fp(check, entity_id, extra=''):
    return hashlib.md5(f'{check}|{entity_id}|{extra}'.encode()).hexdigest()[:16]


def ensure_schema(conn):
    conn.execute('''
        CREATE TABLE IF NOT EXISTS manual.data_issues (
            fingerprint TEXT PRIMARY KEY,
            check_id    TEXT NOT NULL,
            severity    TEXT NOT NULL,
            entity_type TEXT NOT NULL,   -- complex | org
            entity_id   TEXT NOT NULL,
            message     TEXT,
            status      TEXT DEFAULT 'new',  -- new | confirmed | dismissed
            created_at  TEXT,
            resolved_at TEXT
        )
    ''')
    conn.commit()


def _num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def run_checks(conn):
    """Прогоняет все проверки, возвращает {'new': n, 'total': n}."""
    ensure_schema(conn)
    today = date.today().isoformat()
    findings = []  # (check, severity, entity_type, entity_id, message, extra_fp)

    # ── объекты ──────────────────────────────────────────────────────────
    for r in conn.execute('''SELECT c.*,
            (SELECT org_status FROM org_directory d
              WHERE d.role='developer' AND d.org_key=c.developer_inn) AS dev_status,
            (SELECT reg_date FROM org_directory d
              WHERE d.role='developer' AND d.org_key=c.developer_inn) AS dev_reg_date
            FROM complexes c'''):
        cid = r['cam_id']
        active = r['case_status_clean'] in ('in_progress', 'unclear', 'frozen', 'stopped')

        # ликвидированный застройщик у незавершённого объекта
        if active and r['dev_status'] and 'ктив' not in r['dev_status'] and 'ctive' not in r['dev_status']:
            findings.append(('liquidated_developer', 'high', 'complex', cid,
                             f"{r['project_name'] or cid}: застройщик {r['developer_name']} — статус «{r['dev_status']}»",
                             r['dev_status']))

        # компания зарегистрирована позже старта стройки
        if r['dev_reg_date'] and r['created_at'] and r['dev_reg_date'] > r['created_at']:
            findings.append(('company_younger_than_object', 'high', 'complex', cid,
                             f"{r['project_name'] or cid}: стройка с {r['created_at']}, "
                             f"а застройщик зарегистрирован {r['dev_reg_date']}",
                             r['dev_reg_date']))

        # дедлайн раньше начала
        if r['deadline'] and r['created_at'] and r['deadline'] < r['created_at']:
            findings.append(('deadline_before_start', 'medium', 'complex', cid,
                             f"{cid}: дедлайн {r['deadline']} раньше старта {r['created_at']}", ''))

        # стройка длиннее 8 лет по плану
        if r['deadline'] and r['created_at']:
            try:
                years = (date.fromisoformat(r['deadline'][:10]) -
                         date.fromisoformat(r['created_at'][:10])).days / 365.25
                if years > 8:
                    findings.append(('construction_too_long', 'low', 'complex', cid,
                                     f"{cid}: плановый срок стройки {years:.1f} лет", ''))
            except ValueError:
                pass

        # просрочка 2+ года и всё ещё строится
        if r['case_status_clean'] == 'in_progress' and _num(r['days_overdue']) and _num(r['days_overdue']) > 730:
            findings.append(('overdue_2y_in_progress', 'high', 'complex', cid,
                             f"{r['project_name'] or cid}: просрочка {int(_num(r['days_overdue']))} дн., статус «строится»", ''))

        # нет координат
        if r['lat'] is None or r['lng'] is None:
            findings.append(('no_coords', 'low', 'complex', cid, f'{cid}: нет координат', ''))

        # нет застройщика у активного
        if active and not (r['developer_name'] or r['developer_inn']):
            findings.append(('no_developer', 'medium', 'complex', cid,
                             f'{cid}: активный объект без застройщика', ''))

        # корпуса vs shaffof
        raw = json.loads(r['raw_json']) if r['raw_json'] else {}
        bt, bs = _num(raw.get('blocks_total')), _num(raw.get('blocks_total_shaffof'))
        if bt is not None and bs is not None and bt != bs:
            findings.append(('blocks_mismatch', 'medium', 'complex', cid,
                             f'{cid}: корпусов у нас {int(bt)}, в Shaffof {int(bs)}',
                             f'{bt}/{bs}'))

        # молодая компания + крупный объект
        if r['dev_reg_date'] and bt and bt >= 5 and r['created_at']:
            try:
                age_at_start = (date.fromisoformat(r['created_at'][:10]) -
                                date.fromisoformat(r['dev_reg_date'][:10])).days / 365.25
                if 0 <= age_at_start < 2:
                    findings.append(('young_company_big_project', 'medium', 'complex', cid,
                                     f"{r['project_name'] or cid}: {int(bt)} корпусов, а компании на старте "
                                     f"было {age_at_start:.1f} г.", ''))
            except ValueError:
                pass

    # ── ИНН ──────────────────────────────────────────────────────────────
    for role, inn_col in (('developer', 'developer_inn'), ('contractor', 'contractor_inn')):
        for cid, inn in conn.execute(
                f'SELECT cam_id, {inn_col} FROM complexes '
                f'WHERE {inn_col} IS NOT NULL AND {inn_col} != ""'):
            digits = ''.join(ch for ch in str(inn) if ch.isdigit())
            if len(digits) != 9:
                findings.append(('bad_inn', 'medium', 'complex', cid,
                                 f'{cid}: {role} ИНН «{inn}» — не 9 цифр', str(inn)))

    # ── цены ─────────────────────────────────────────────────────────────
    prices = [(_num(r[1]), r[0]) for r in conn.execute(
        'SELECT cam_id, price_per_m2_uzs FROM complexes WHERE price_per_m2_uzs IS NOT NULL')]
    prices = [(p, c) for p, c in prices if p]
    if len(prices) >= 20:
        vals = sorted(p for p, _ in prices)
        med = vals[len(vals) // 2]
        for p, cid in prices:
            if p > med * 5 or p < med / 5:
                findings.append(('price_outlier', 'low', 'complex', cid,
                                 f'{cid}: цена {p:,.0f} сум/м² при медиане {med:,.0f}', f'{p:.0f}'))

    # ── запись с дедупликацией ───────────────────────────────────────────
    new = 0
    for check, sev, etype, eid, msg, extra in findings:
        fp = _fp(check, eid, extra)
        cur = conn.execute('SELECT 1 FROM manual.data_issues WHERE fingerprint=?', (fp,)).fetchone()
        if cur is None:
            conn.execute(
                'INSERT INTO manual.data_issues '
                '(fingerprint, check_id, severity, entity_type, entity_id, message, status, created_at) '
                "VALUES (?,?,?,?,?,?,'new',?)",
                (fp, check, sev, etype, eid, msg, today))
            new += 1
    conn.commit()
    return {'new': new, 'total': len(findings)}


if __name__ == '__main__':
    import os
    import sqlite3
    base = os.path.dirname(__file__)
    conn = sqlite3.connect(os.path.join(base, 'cam_admin.db'))
    conn.row_factory = sqlite3.Row
    conn.execute(f"ATTACH '{os.path.join(base, 'cam_manual.db')}' AS manual")
    result = run_checks(conn)
    print(f"новых находок: {result['new']} (всего сработало: {result['total']})")
    for row in conn.execute(
            "SELECT check_id, severity, COUNT(*) FROM manual.data_issues "
            "WHERE status='new' GROUP BY check_id ORDER BY 3 DESC"):
        print(f"  [{row[1]:6}] {CHECK_LABELS.get(row[0], row[0])}: {row[2]}")
