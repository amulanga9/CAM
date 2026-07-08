"""
Парсер Shaffof (api-dshk.shaffofqurilish.uz) — адаптация проверенного
01_parse_shaffof.py (v4) под структуру admin/.

Выход: admin/data/raw/YYYY-MM/shaffof/<month>_NN.csv — тот же формат,
что june_01..04.csv. Дальше: python3 apply_parse.py

Запуск (с вашей машины; из облака портал недоступен):
    python3 parse_shaffof.py            # диапазоны по умолчанию
    python3 parse_shaffof.py 1 80000    # свой диапазон ID
"""
import csv
import json
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

import requests

API_URL = 'https://api-dshk.shaffofqurilish.uz/api/get-gasn-info'
HEADERS = {'Content-Type': 'application/json'}
TASHKENT = 1726
WORKERS = 4
BATCH_SIZE = 200

RUN_DATE = datetime.now()
RUN_YEAR_MONTH = RUN_DATE.strftime('%Y-%m')
MONTH_NAMES = ['january', 'february', 'march', 'april', 'may', 'june', 'july',
               'august', 'september', 'october', 'november', 'december']
RUN_MONTH = MONTH_NAMES[RUN_DATE.month - 1]

OUTPUT_DIR = Path(__file__).resolve().parent / 'data' / 'raw' / RUN_YEAR_MONTH / 'shaffof'

# два реальных диапазона, где есть данные (верх растёт со временем —
# при желании задайте свой: python3 parse_shaffof.py 1 80000)
DEFAULT_RANGES = list(range(1, 7001)) + list(range(52000, 72001))

FIELDS = [
    'object_id', 'task_id', 'name', 'sphere_id', 'region_soato', 'district_soato',
    'status', 'created_at', 'deadline', 'closed_at', 'organization_name',
    'pudrat_direct', 'pudrat_inn', 'pudrat_name', 'pudrat_reyting',
    'loyiha_direct', 'loyiha_inn', 'loyiha_name', 'loyiha_reyting',
    'apartment_count', 'block_count', 'floor_max', 'area_total',
    'blocks_accepted', 'blocks_total', 'difficulty',
    'reestr_number', 'number_protocol', 'conclusion_url',
    'lat', 'long', 'location',
]


def parse_rating(raw):
    if not raw:
        return {}, {}
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return {}, {}
    if not isinstance(raw, list) or not raw:
        return {}, {}
    r0 = raw[0]
    return r0.get('qurilish') or {}, r0.get('loyiha') or {}


def parse_blocks(blocks):
    if not blocks:
        return '', '', 0, 0
    floors, areas, accepted = [], [], 0
    for b in blocks:
        f = b.get('floor')
        if f:
            try:
                floors.append(int(f))
            except (TypeError, ValueError):
                pass
        a = b.get('area')
        if a:
            try:
                areas.append(float(a))
            except (TypeError, ValueError):
                pass
        if b.get('accepted'):
            accepted += 1
    return (max(floors) if floors else '',
            round(sum(areas), 1) if areas else '',
            accepted, len(blocks))


def _max_floor(data):
    floors = []
    for b in (data.get('blocks') or []):
        f = b.get('floor')
        if f:
            try:
                floors.append(int(f))
            except (TypeError, ValueError):
                pass
    return max(floors) if floors else None


def is_residential(data):
    if data.get('sphere_id') == 57:
        return True

    # sphere_id 58/59 в классификации портала — индивидуальный дом/коттедж/
    # таунхаус, не ЖК. Их названия почти всегда содержат "турар-жой"/"жилой"
    # и прошли бы через keyword-фильтр ниже как обычная многоквартирка.
    # Найдено анализом raw-выгрузки (737 строк): все объекты с sphere_id 58/59
    # имели apartment_count=0; исключаем только явно малоэтажные (<=4 этажей
    # или этаж не указан) — 2 объекта из 19 были 16-этажными с 3-4 блоками,
    # что не похоже на частный дом (вероятно, неверно указан sphere_id при
    # подаче заявки) — их не исключаем, они попадут в обычную проверку
    # инспектора "0 квартир на портале" для ручной проверки.
    if data.get('sphere_id') in (58, 59) and int(data.get('apartment_count') or 0) == 0:
        floor = _max_floor(data)
        if floor is None or floor <= 4:
            return False

    if int(data.get('apartment_count') or 0) > 0:
        return True
    name = (data.get('name') or '').lower()
    return any(m in name for m in ['турар-жой', 'turar-joy', 'квартир', 'яшаш', 'жилой'])


def fetch_and_parse(obj_id):
    try:
        r = requests.post(API_URL, json={'object_id': obj_id}, headers=HEADERS, timeout=12)
        if r.status_code != 200:
            return None
        d = r.json().get('data')
        if not d or d.get('region_soato') != TASHKENT or not is_residential(d):
            return None
        q, l = parse_rating(d.get('rating'))
        floor_max, area_total, accepted, total_b = parse_blocks(d.get('blocks') or [])
        conc = d.get('conclusion') or {}
        return {
            'object_id': d.get('id', ''), 'task_id': d.get('task_id', ''),
            'name': (d.get('name') or '').replace('\n', ' '),
            'sphere_id': d.get('sphere_id', ''),
            'region_soato': d.get('region_soato', ''),
            'district_soato': d.get('district_soato', ''),
            'status': (d.get('status') or {}).get('name', ''),
            'created_at': (d.get('created_at') or '')[:10],
            'deadline': d.get('deadline') or '', 'closed_at': d.get('closed_at') or '',
            'organization_name': d.get('organization_name') or '',
            'pudrat_direct': d.get('pudrat') or '',
            'pudrat_inn': q.get('inn', ''), 'pudrat_name': q.get('name', ''),
            'pudrat_reyting': q.get('reyting_umumiy', ''),
            'loyiha_direct': d.get('loyiha') or '',
            'loyiha_inn': l.get('inn', ''), 'loyiha_name': l.get('name', ''),
            'loyiha_reyting': l.get('reyting_loyha', ''),
            'apartment_count': d.get('apartment_count') or 0,
            'block_count': d.get('block_count') or 0,
            'floor_max': floor_max, 'area_total': area_total,
            'blocks_accepted': accepted, 'blocks_total': total_b,
            'difficulty': d.get('difficulty') or '',
            'reestr_number': d.get('reestr_number') or '',
            'number_protocol': d.get('number_protocol') or '',
            'conclusion_url': conc.get('url', ''),
            'lat': d.get('lat') or '', 'long': d.get('long') or '',
            'location': d.get('location_building') or '',
        }
    except Exception:
        return None


def flush(rows, num):
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUTPUT_DIR / f'{RUN_MONTH}_{num:02d}.csv'
    with open(path, 'w', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)
    print(f'  БАТЧ {num}: {len(rows)} объектов → {path}', flush=True)


def run(id_ranges):
    print(f"Shaffof Parser — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f'ID: {len(id_ranges)} шт | Потоков: {WORKERS}\n')
    found, batch_num, batch, done = 0, 1, [], 0
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futures = {ex.submit(fetch_and_parse, i): i for i in id_ranges}
        for future in as_completed(futures):
            done += 1
            row = future.result()
            if row:
                batch.append(row)
                found += 1
                if len(batch) >= BATCH_SIZE:
                    flush(batch, batch_num)
                    batch_num += 1
                    batch = []
            if done % 2000 == 0:
                print(f"  [{datetime.now().strftime('%H:%M')}] {done}/{len(id_ranges)} | найдено {found}", flush=True)
    if batch:
        flush(batch, batch_num)
    print(f'\nГотово: {found} объектов')


if __name__ == '__main__':
    if len(sys.argv) == 3:
        ranges = list(range(int(sys.argv[1]), int(sys.argv[2]) + 1))
    else:
        ranges = DEFAULT_RANGES
    run(ranges)
