"""
Фильтр «точно не ЖК» — помечает нежилые объекты как excluded (обратимо).

Сигнал точный, а не по одному слову: помечаем только когда объект явно
НЕжилого назначения И у него 0 квартир. Смешанные ЖК (жильё + магазины/
паркинг на первом этаже) НЕ трогаются — они называют себя «турар-жой» и
часто просто не имеют заполненного apartment_count на портале.

Что считается мусором:
  - «нотурар-жой» / «noturar-joy» в названии — портал прямо говорит НЕжилое
    (реконструкция склада/офиса/ресторана/автомойки/общежития и т.п.);
  - переделка существующего жилого В торговлю/сервис (перестаёт быть жильём);
  и при этом apartments_count == 0.

Механизм: как кнопка «Не ЖК» в админке — запись в manual.excluded_ids +
case_status_clean='excluded'. Обратимо: удалить строку из excluded_ids и
перезапустить импорт/парс.

    python3 clean_non_residential.py            # только показать (dry-run)
    python3 clean_non_residential.py --apply     # пометить excluded
"""
import json
import os
import sqlite3
import sys
from datetime import date

BASE = os.path.dirname(os.path.abspath(__file__))

# явно НЕжилое назначение (портальная формулировка «мавжуд нотурар-жой…»)
NON_RESIDENTIAL = ('нотурар-жой', 'нотурар жой', 'noturar-joy', 'noturar joy')
# переделка жилого В торговлю/сервис — объект перестаёт быть жильём на продажу
TO_COMMERCIAL = ('savdo va maishiyga qayta', 'савдо ва маишийга қайта',
                 'savdo va maishiy xizmatga qayta')


def _apartments(raw_json):
    try:
        raw = json.loads(raw_json) if raw_json else {}
        return float(raw.get('apartments_count') or 0)
    except (TypeError, ValueError):
        return 0.0


def non_residential_reason(project_name, raw_json):
    """Возвращает причину-строку, если объект точно не ЖК, иначе None."""
    if _apartments(raw_json) > 0:
        return None  # есть квартиры — это жильё (пусть и со встроенной коммерцией)
    name = (project_name or '').lower()
    if any(kw in name for kw in NON_RESIDENTIAL):
        return 'нежилое здание (нотурар-жой) без квартир'
    if any(kw in name for kw in TO_COMMERCIAL):
        return 'переделка жилого в торговлю/сервис'
    return None


def find(conn):
    """Список (cam_id, reason, name) кандидатов на исключение."""
    out = []
    for r in conn.execute(
            'SELECT cam_id, project_name, raw_json, case_status_clean '
            'FROM complexes'):
        if r[3] == 'excluded':
            continue
        reason = non_residential_reason(r[1], r[2])
        if reason:
            out.append((r[0], reason, r[1] or '(без названия)'))
    return out


def apply(conn, candidates):
    today = date.today().isoformat()
    for cam_id, reason, _ in candidates:
        conn.execute(
            'INSERT OR REPLACE INTO manual.excluded_ids (cam_id, reason, excluded_at) '
            'VALUES (?,?,?)', (cam_id, f'auto: {reason}', today))
        conn.execute(
            "UPDATE complexes SET case_status_clean='excluded', needs_review=0 "
            'WHERE cam_id=?', (cam_id,))
    conn.commit()


def main():
    do_apply = '--apply' in sys.argv
    conn = sqlite3.connect(os.path.join(BASE, 'cam_admin.db'))
    conn.execute(f"ATTACH '{os.path.join(BASE, 'cam_manual.db')}' AS manual")

    candidates = find(conn)
    print(f'Найдено нежилых объектов (точный сигнал, 0 квартир): {len(candidates)}\n')
    for cam_id, reason, name in candidates:
        print(f'  {cam_id:12} {reason}')
        print(f'       {name[:90]}')

    if not candidates:
        print('\nБаза чистая — исключать нечего.')
    elif do_apply:
        apply(conn, candidates)
        print(f'\nПомечено excluded: {len(candidates)}. '
              f'Обратимо: удалить из manual.excluded_ids и перезапустить парс.')
    else:
        print(f'\nЭто dry-run. Для применения: '
              f'python3 clean_non_residential.py --apply')
    conn.close()


if __name__ == '__main__':
    main()
