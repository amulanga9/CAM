"""
Метки (теги) объектов — принадлежность к исходным листам мастера.

Объект может состоять в нескольких листах сразу (Обучение + Плохие кейсы
и т.п.), поэтому это отдельная таблица многие-ко-многим, а не колонка
complexes.source_sheet (там хранится только один лист, и для 302 объектов
из нескольких листов расчёты по нему некорректны).

Метки пересобираются из phases + complexes целиком: rebuild_tags(conn).
"""

SHEET_TO_TAG = {
    'Обучение': 'training',
    'Все построенные': 'delivered_list',
    'Плохие кейсы': 'bad_cases',
    'Серая зона (1-2 года)': 'grey_zone',
    'Строящиеся_сматченные': 'building_matched',
    'Строящиеся_несматченные': 'building_unmatched',
}

TAG_LABELS = {
    'training': 'Обучение',
    'delivered_list': 'Все построенные',
    'bad_cases': 'Плохие кейсы',
    'grey_zone': 'Серая зона',
    'building_matched': 'Строящиеся (сматч.)',
    'building_unmatched': 'Строящиеся (несматч.)',
}

# цвет бейджа в UI (класс .badge из base.html)
TAG_BADGE_CLASS = {
    'training': 'in_progress',
    'delivered_list': 'delivered',
    'bad_cases': 'stopped',
    'grey_zone': 'unclear',
    'building_matched': 'in_progress',
    'building_unmatched': 'rebranded',
}


def ensure_tags_schema(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS complex_tags (
        cam_id TEXT NOT NULL,
        tag    TEXT NOT NULL,
        PRIMARY KEY (cam_id, tag)
    )''')


def rebuild_tags(conn):
    """Полная пересборка меток из phases + complexes.source_sheet."""
    ensure_tags_schema(conn)
    conn.execute('DELETE FROM complex_tags')
    for cam_id, sheet in conn.execute(
            'SELECT DISTINCT cam_id, source_sheet FROM phases WHERE source_sheet IS NOT NULL'):
        tag = SHEET_TO_TAG.get(sheet)
        if tag:
            conn.execute('INSERT OR IGNORE INTO complex_tags VALUES (?,?)', (cam_id, tag))
    for cam_id, sheet in conn.execute(
            'SELECT cam_id, source_sheet FROM complexes WHERE source_sheet IS NOT NULL'):
        tag = SHEET_TO_TAG.get(sheet)
        if tag:
            conn.execute('INSERT OR IGNORE INTO complex_tags VALUES (?,?)', (cam_id, tag))
    conn.commit()


def ensure_tags(conn):
    """Создаёт таблицу; если пустая при непустых complexes — собирает один раз."""
    ensure_tags_schema(conn)
    n_tags = conn.execute('SELECT COUNT(*) FROM complex_tags').fetchone()[0]
    if n_tags == 0:
        n_cx = conn.execute('SELECT COUNT(*) FROM complexes').fetchone()[0]
        if n_cx:
            rebuild_tags(conn)


def tags_for(conn, cam_id):
    return [t for (t,) in conn.execute(
        'SELECT tag FROM complex_tags WHERE cam_id=? ORDER BY tag', (cam_id,))]


def tag_counts(conn):
    return dict(conn.execute(
        'SELECT tag, COUNT(*) FROM complex_tags GROUP BY tag').fetchall())
