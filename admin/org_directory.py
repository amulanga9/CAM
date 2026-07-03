"""
Справочник застройщиков и подрядчиков CAM.

Ключ идентификации — ИНН (если есть) или нормализованное название (если нет ИНН).
Один объект = одна строка в org_directory. Все варианты сырого написания — в org_aliases.
Таблицы живут в cam_admin.db.
"""
import re
from datetime import date

LEGAL_FORM_RE = re.compile(
    r"\b(mas['`‘’]?uliyati\s*cheklangan\s*jamiyat(i)?|"
    r"qo['`‘’]?shma\s*korxona(si)?|"
    r"xususiy\s*korxona(si)?|mchj|ooo|ооо|мчж|xk|хк|хорижий корхона|xorijiy korxona|"
    r"yakka tartibdagi tadbirkor|aksiyadorlik\s*jamiyat(i)?|"
    r"чп|оао|пто|ип|сп|sp)\b",
    re.IGNORECASE)


def normalize_org_name(name):
    if not name:
        return ''
    n = name.strip().strip('"«»«»“”‘’`\'').lower()
    n = LEGAL_FORM_RE.sub(' ', n)
    n = re.sub(r'[^\w\s]', ' ', n)
    n = re.sub(r'\s+', ' ', n).strip()
    tokens = n.split(' ')
    # обрезанный остаток юр.формы в старом Excel-мастере выглядит как одна буква "i" в конце
    if len(tokens) > 1 and tokens[-1] == 'i':
        tokens = tokens[:-1]
    return ' '.join(tokens)


def normalize_inn(inn):
    if not inn:
        return None
    digits = re.sub(r'\D', '', str(inn))
    return digits if len(digits) >= 6 else None


def org_key_for(raw_name, raw_inn):
    """Возвращает (org_key, key_type). key_type='inn' или 'norm'."""
    inn = normalize_inn(raw_inn)
    if inn:
        return inn, 'inn'
    norm = normalize_org_name(raw_name)
    if norm:
        return f'NORM:{norm}', 'norm'
    return None, None


def ensure_schema(conn):
    conn.execute('''
        CREATE TABLE IF NOT EXISTS org_directory (
            role           TEXT NOT NULL,
            org_key        TEXT NOT NULL,
            key_type       TEXT NOT NULL,
            name_canonical TEXT,
            rating         TEXT,
            objects_count  INTEGER DEFAULT 0,
            bad_pct        REAL,
            overdue_pct    REAL,
            first_seen     TEXT,
            last_seen      TEXT,
            needs_review   INTEGER DEFAULT 0,
            notes          TEXT,
            PRIMARY KEY (role, org_key)
        )
    ''')
    conn.execute('''
        CREATE TABLE IF NOT EXISTS org_aliases (
            role       TEXT NOT NULL,
            org_key    TEXT NOT NULL,
            raw_name   TEXT NOT NULL,
            source     TEXT,
            first_seen TEXT,
            last_seen  TEXT,
            PRIMARY KEY (role, org_key, raw_name)
        )
    ''')
    conn.commit()


def resolve_org(conn, role, raw_name, raw_inn, snapshot_date=None, source='portal'):
    """Находит/создаёт карточку организации, фиксирует алиас написания.

    Возвращает org_key или None.
    При новой организации ставит needs_review=1.
    При новом алиасе (то же ИНН, новое написание) — просто добавляет в org_aliases,
    НЕ трактует как смену застройщика/подрядчика.
    """
    if not raw_name and not raw_inn:
        return None
    today = snapshot_date or date.today().isoformat()
    org_key, key_type = org_key_for(raw_name, raw_inn)
    if org_key is None:
        return None

    row = conn.execute(
        'SELECT org_key FROM org_directory WHERE role=? AND org_key=?',
        (role, org_key)
    ).fetchone()

    if row is None:
        conn.execute('''
            INSERT INTO org_directory
                (role, org_key, key_type, name_canonical, first_seen, last_seen, needs_review)
            VALUES (?,?,?,?,?,?,1)
        ''', (role, org_key, key_type, raw_name, today, today))
    else:
        conn.execute(
            'UPDATE org_directory SET last_seen=? WHERE role=? AND org_key=?',
            (today, role, org_key)
        )

    if raw_name:
        alias = conn.execute(
            'SELECT 1 FROM org_aliases WHERE role=? AND org_key=? AND raw_name=?',
            (role, org_key, raw_name)
        ).fetchone()
        if alias is None:
            conn.execute('''
                INSERT INTO org_aliases (role, org_key, raw_name, source, first_seen, last_seen)
                VALUES (?,?,?,?,?,?)
            ''', (role, org_key, raw_name, source, today, today))
        else:
            conn.execute(
                'UPDATE org_aliases SET last_seen=? WHERE role=? AND org_key=? AND raw_name=?',
                (today, role, org_key, raw_name)
            )

    return org_key


def seed_from_complexes(conn):
    """Наполняет пустой справочник из complexes (первый запуск на новой машине).

    Идемпотентно: resolve_org не создаёт дублей, повторный вызов безопасен.
    """
    for role, name_col, inn_col in (
        ('developer', 'developer_name', 'developer_inn'),
        ('contractor', 'contractor_name', 'contractor_inn'),
    ):
        for raw_name, raw_inn in conn.execute(
                f'SELECT {name_col}, {inn_col} FROM complexes '
                f'WHERE ({name_col} IS NOT NULL AND {name_col} != "") '
                f'   OR ({inn_col} IS NOT NULL AND {inn_col} != "")'):
            resolve_org(conn, role, raw_name, raw_inn, source='complexes_seed')
    recompute_stats(conn)
    rebuild_objects_count(conn)
    conn.commit()


def ensure_seeded(conn):
    """Если справочник пуст при непустых complexes — заполняет его."""
    n_dir = conn.execute('SELECT COUNT(*) FROM org_directory').fetchone()[0]
    if n_dir == 0:
        n_cx = conn.execute('SELECT COUNT(*) FROM complexes').fetchone()[0]
        if n_cx:
            seed_from_complexes(conn)


def merge_org(conn, role, src_key, dst_key):
    """Объединяет src_key в dst_key: переносит алиасы, обновляет статистику, удаляет src."""
    conn.execute('''
        INSERT OR IGNORE INTO org_aliases (role, org_key, raw_name, source, first_seen, last_seen)
        SELECT role, ?, raw_name, source, first_seen, last_seen
        FROM org_aliases WHERE role=? AND org_key=?
    ''', (dst_key, role, src_key))
    conn.execute('DELETE FROM org_aliases WHERE role=? AND org_key=?', (role, src_key))
    conn.execute('DELETE FROM org_directory WHERE role=? AND org_key=?', (role, src_key))
    conn.commit()


def recompute_stats(conn):
    """Пересчитывает objects_count, overdue_pct, bad_pct из текущих complexes.
    Рейтинг (rating) — только ручной, не трогаем.
    """
    from collections import defaultdict
    for role, name_col, inn_col in (
        ('developer', 'developer_name', 'developer_inn'),
        ('contractor', 'contractor_name', 'contractor_inn'),
    ):
        counts = defaultdict(lambda: [0, 0, 0])  # [total, overdue, bad]
        for name, inn, is_overdue, status in conn.execute(
            f'SELECT {name_col}, {inn_col}, is_overdue, case_status_clean FROM complexes'
        ):
            key, _ = org_key_for(name, inn)
            if not key:
                continue
            counts[key][0] += 1
            if is_overdue:
                counts[key][1] += 1
            if status in ('stopped', 'cancelled'):
                counts[key][2] += 1
        conn.execute('UPDATE org_directory SET objects_count=0 WHERE role=?', (role,))
        for key, (total, overdue, bad) in counts.items():
            if total == 0:
                continue
            conn.execute('''
                UPDATE org_directory
                SET objects_count=?, overdue_pct=?, bad_pct=?
                WHERE role=? AND org_key=?
            ''', (total, round(overdue / total * 100, 1), round(bad / total * 100, 1), role, key))
    conn.commit()


def rebuild_objects_count(conn):
    """Пересчитывает objects_count для всех карточек справочника по текущим complexes."""
    from collections import Counter
    for role, name_col, inn_col in (
        ('developer', 'developer_name', 'developer_inn'),
        ('contractor', 'contractor_name', 'contractor_inn'),
    ):
        counts = Counter()
        for row in conn.execute(f'SELECT {name_col}, {inn_col} FROM complexes'):
            key, _ = org_key_for(row[0], row[1])
            if key:
                counts[key] += 1
        conn.execute('UPDATE org_directory SET objects_count=0 WHERE role=?', (role,))
        for key, cnt in counts.items():
            conn.execute(
                'UPDATE org_directory SET objects_count=? WHERE role=? AND org_key=?',
                (cnt, role, key)
            )
    conn.commit()
