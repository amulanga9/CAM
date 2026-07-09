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
    # ── специфика Шаффофа ──
    'coords_outside_tashkent': 'Координаты вне Ташкента (или lat/lng перепутаны)',
    'zero_blocks_active': 'Активный ЖК с 0 корпусов на портале (портал не заполнил)',
    'zero_apartments': 'Жилой объект с 0 квартир на портале',
    'no_deadline_active': 'Активный объект без дедлайна',
    'deadline_min_max_differ': 'deadline_min ≠ deadline_max (несколько разрешений с разными сроками)',
    'self_contractor': 'Застройщик = подрядчик (сам себе строит)',
    'delivered_no_closed_at': 'Статус «сдан», но нет closed_at (сдача не подтверждена документом)',
    'vanished_from_portal': 'Объект ИСЧЕЗ с портала (был в прошлом парсе, нет в новом)',
    'design_stage_name': 'Название похоже на заявку на проектирование/реконструкцию, а не на стройку',
    'ambiguous_no_apt_profile': '0 квартир + малоэтажно/1 блок + нет жилого самоназвания — не ЖК или пробел в данных?',
    'blocks_undercounted': 'Квартир на блок нереалистично много — вероятно blocks_total занижен на портале',
}

# слова в названии, указывающие на заявку по проектированию/реконструкции —
# такие объекты часто содержат и жилые ключевые слова ("турар-жой"/"жилой"),
# поэтому проходят через is_residential() в parse_shaffof.py, но по сути не
# новое строительство. Не исключаем автоматически (реконструкция бывает и
# настоящей), только флагаем на ручную проверку.
DESIGN_STAGE_KEYWORDS = (
    'лойиҳалаштириш', 'loyihalashtirish',  # проектирование
    'қайта қуриш', 'qayta qurish',          # реконструкция (перестройка)
    'реконструкц',                          # реконструкция (рус.)
    'ихтисослаштириб', 'ixtisoslashtirib',  # перепрофилирование
    'генплан', 'генеральн',                 # корректура генплана — тоже проектная стадия
)

# если объект называет себя жильём — числовой профиль "частный дом" не
# применяем, что бы ни говорили цифры (ЖК «Malika»/«Savr Avenue» проверкой
# показали: apartments_count/blocks_total у настоящих ЖК тоже бывают битыми)
RESIDENTIAL_SELF_NAME = (
    'жк', 'жилой комплекс', 'жилой дом', 'турар-жой', 'turar-joy', 'uy-joy',
    'residence', 'жилищн',
)

# Ташкент и окрестности (запас на область)
TASHKENT_BBOX = (40.9, 68.6, 41.6, 69.9)  # lat_min, lng_min, lat_max, lng_max


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

        # название похоже на заявку по проектированию/реконструкции
        name_lower = (r['project_name'] or '').lower()
        is_design_stage = any(kw in name_lower for kw in DESIGN_STAGE_KEYWORDS)
        if is_design_stage:
            findings.append(('design_stage_name', 'medium', 'complex', cid,
                             f"{cid}: название содержит признаки заявки на "
                             f"проектирование/реконструкцию, а не стройки — "
                             f"«{(r['project_name'] or '')[:120]}»", ''))

        # кросс-проверка по числовым полям (не только по имени): похоже на
        # частный дом (0 квартир + малоэтажно + <=1 блок), но название не
        # называет себя жильём и не является проектной заявкой — тогда либо
        # это правда не ЖК, либо просто пробел в данных портала. Числовой
        # профиль сам по себе ненадёжен (проверкой найдены реальные ЖК
        # «Malika»/«Savr Avenue» с битым blocks_total) — только на ручную
        # проверку, не auto-exclude.
        try:
            raw = json.loads(r['raw_json']) if r['raw_json'] else {}
        except (TypeError, ValueError):
            raw = {}
        apt = _num(raw.get('apartments_count')) or 0
        floors = _num(raw.get('floors_max'))
        blocks_total = _num(raw.get('blocks_total'))
        is_residential_name = any(kw in name_lower for kw in RESIDENTIAL_SELF_NAME)
        if (apt == 0 and (floors is None or floors <= 4) and (blocks_total is None or blocks_total <= 1)
                and not is_residential_name and not is_design_stage):
            findings.append(('ambiguous_no_apt_profile', 'low', 'complex', cid,
                             f"{cid}: 0 квартир, малоэтажно/1 блок, нет жилого "
                             f"самоназвания — «{(r['project_name'] or '(без названия)')[:100]}»", ''))

        # квартир на блок нереалистично много — вероятно blocks_total занижен
        if apt > 0 and blocks_total and blocks_total > 0:
            apt_per_block = apt / blocks_total
            if apt_per_block > 400:
                findings.append(('blocks_undercounted', 'low', 'complex', cid,
                                 f"{cid}: {int(apt)} квартир на {int(blocks_total)} "
                                 f"блок(ов) — {apt_per_block:.0f} кв./блок, вероятно "
                                 f"blocks_total занижен на портале", ''))

        if not is_design_stage:
            la, lo = _num(r['lat']), _num(r['lng'])
            lat_min, lng_min, lat_max, lng_max = TASHKENT_BBOX
            if la and lo and not (lat_min <= la <= lat_max and lng_min <= lo <= lng_max):
                swapped = (lat_min <= lo <= lat_max and lng_min <= la <= lng_max)
                findings.append(('coords_outside_tashkent', 'medium', 'complex', cid,
                                 f'{cid}: координаты ({la:.4f}, {lo:.4f}) вне Ташкента'
                                 + (' — похоже, lat/lng перепутаны' if swapped else ''),
                                 f'{la:.3f},{lo:.3f}'))

        # активный без дедлайна
        if active and not r['deadline']:
            findings.append(('no_deadline_active', 'medium', 'complex', cid,
                             f'{cid}: активный объект без дедлайна', ''))

        # застройщик = подрядчик
        if (r['developer_inn'] and r['contractor_inn']
                and str(r['developer_inn']).strip() == str(r['contractor_inn']).strip()):
            findings.append(('self_contractor', 'low', 'complex', cid,
                             f"{r['project_name'] or cid}: застройщик и подрядчик — одно юрлицо "
                             f"({r['developer_name']})", ''))

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

        # ── специфика Шаффофа ────────────────────────────────────────────
        # портал не заполнил корпуса у активного ЖК
        if active and bt is not None and bt == 0:
            findings.append(('zero_blocks_active', 'low', 'complex', cid,
                             f'{cid}: на портале 0 корпусов у активного объекта', ''))

        # жилой объект с 0 квартир
        apts = _num(raw.get('apartments_count'))
        if active and apts is not None and apts == 0:
            findings.append(('zero_apartments', 'low', 'complex', cid,
                             f'{cid}: на портале 0 квартир', ''))

        # несколько разрешений с разными сроками (deadline_min != deadline_max)
        dmin, dmax = raw.get('deadline_min'), raw.get('deadline_max')
        if dmin and dmax and dmin != dmax:
            findings.append(('deadline_min_max_differ', 'medium', 'complex', cid,
                             f'{cid}: сроки разрешений расходятся: {dmin} … {dmax} '
                             f'(взят {r["deadline"]})', f'{dmin}/{dmax}'))

        # «сдан», но нет подтверждающего closed_at
        if r['case_status_clean'] == 'delivered':
            closed = raw.get('closed_at') or raw.get('delivery_date_fact')
            if not closed:
                findings.append(('delivered_no_closed_at', 'medium', 'complex', cid,
                                 f'{cid}: статус «сдан», но нет даты сдачи (closed_at) — '
                                 f'сдача не подтверждена документом', ''))

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
