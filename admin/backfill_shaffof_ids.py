"""
Бэкфилл raw_json.shaffof_ids для карточек старого мастер-импорта (OBJ*/EXT*),
у которых оно никогда не заполнялось — из-за этого apply_parse.py на
следующем парсе не может узнать объект по object_id и рискует создать
дубль (новый GASN-<id>) вместо обновления существующей карточки.

Найдено: у OBJ*/EXT* номер в cam_id == raw_json['ID'] == реальный
Shaffof object_id (проверено на всей базе, 100% совпадение для этих
префиксов) — восстанавливаем полностью и точно.

GRP* (мастер-карточки, объединяющие несколько разрешений/корпусов) устроены
иначе: их 'ID' — это ID только ОДНОГО (обычно первого исторического)
разрешения, не всех корпусов ЖК. Всё равно засеваем его как shaffof_ids —
это лучше, чем пусто (карточка хотя бы не потеряется на след. парсе), но
это ЧАСТИЧНОЕ восстановление: остальные корпуса такого ЖК либо появятся
сами через эвристику "прикрепить к существующей карточке" на следующих
парсах, либо их нужно дозаполнить вручную (см. панель "Разрешения/очереди
на портале" у карточки).

    python3 backfill_shaffof_ids.py            # dry-run
    python3 backfill_shaffof_ids.py --apply    # применить
"""
import json
import os
import re
import sqlite3
import sys

BASE = os.path.dirname(os.path.abspath(__file__))


def find(conn):
    """Возвращает (fixable, partial, unclear)."""
    fixable, partial, unclear = [], [], []
    for cam_id, rj in conn.execute('SELECT cam_id, raw_json FROM complexes'):
        raw = json.loads(rj) if rj else {}
        if raw.get('shaffof_ids'):
            continue
        rid = raw.get('ID') or raw.get('id')
        m = re.match(r'(OBJ|EXT)0*(\d+)$', cam_id)
        if m and rid and int(m.group(2)) == int(rid):
            fixable.append((cam_id, str(int(rid))))
        elif cam_id.startswith('GRP') and rid:
            partial.append((cam_id, str(int(rid))))
        else:
            unclear.append(cam_id)
    return fixable, partial, unclear


def apply(conn, entries):
    for cam_id, object_id in entries:
        row = conn.execute('SELECT raw_json FROM complexes WHERE cam_id=?', (cam_id,)).fetchone()
        raw = json.loads(row[0]) if row[0] else {}
        raw['shaffof_ids'] = [object_id]
        conn.execute('UPDATE complexes SET raw_json=? WHERE cam_id=?',
                     (json.dumps(raw, ensure_ascii=False), cam_id))
    conn.commit()


def main():
    do_apply = '--apply' in sys.argv
    conn = sqlite3.connect(os.path.join(BASE, 'cam_admin.db'))

    fixable, partial, unclear = find(conn)
    print(f'Полностью восстановимо (OBJ*/EXT*, номер в cam_id == raw_json[ID]): {len(fixable)}')
    print(f'Частично (GRP*, только первое разрешение из возможных нескольких): {len(partial)}')
    print('  ' + ', '.join(c for c, _ in partial))
    print(f'Неясно (нет даже ID в raw_json): {len(unclear)}')
    if unclear:
        print('  ' + ', '.join(unclear[:30]) + (' ...' if len(unclear) > 30 else ''))

    if do_apply:
        apply(conn, fixable + partial)
        print(f'\nПрименено: {len(fixable)} полных + {len(partial)} частичных = '
              f'{len(fixable) + len(partial)} карточек получили shaffof_ids.')
    else:
        print('\nDry-run. Для применения: python3 backfill_shaffof_ids.py --apply')
    conn.close()


if __name__ == '__main__':
    main()
