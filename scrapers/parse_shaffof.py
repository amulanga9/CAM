"""
CAM Parser v4 — полный сбор всех полей
- Все поля из API включая blocks.area, blocks.accepted, conclusion.url
- district_soato, task_id, reestr_number, number_protocol
- pudrat/loyiha напрямую + из rating (ИНН, рейтинги)
- 4 потока параллельно
- Только два реальных диапазона: 1-7000 и 52000-60000
- Батч сохраняется каждые 200 найденных объектов

Запуск:
  python3 parse_shaffof.py

Фоновый:
  nohup python3 parse_shaffof.py > parser_v4.log 2>&1 &
  tail -f parser_v4.log
"""

import requests
import csv
import json
from pathlib import Path
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

API_URL   = 'https://api-dshk.shaffofqurilish.uz/api/get-gasn-info'
HEADERS   = {'Content-Type': 'application/json'}
TASHKENT  = 1726
WORKERS   = 4
BATCH_SIZE = 200
MVP_DIR = Path(__file__).resolve().parents[1]

MONTH_NAMES = {
    1: "january",
    2: "february",
    3: "march",
    4: "april",
    5: "may",
    6: "june",
    7: "july",
    8: "august",
    9: "september",
    10: "october",
    11: "november",
    12: "december",
}

RUN_DATE = datetime.now()
RUN_YEAR_MONTH = RUN_DATE.strftime("%Y-%m")
RUN_MONTH = MONTH_NAMES[RUN_DATE.month]

OUTPUT_DIR = MVP_DIR / "data" / "raw" / RUN_YEAR_MONTH / "shaffof"

# Два реальных диапазона где есть данные
ID_RANGES = list(range(1, 7001)) + list(range(52000, 70001))

FIELDS = [
    # Идентификация
    'object_id', 'task_id', 'name', 'sphere_id',
    'region_soato', 'district_soato',
    # Статус
    'status', 'created_at', 'deadline', 'closed_at',
    # Застройщик
    'organization_name',
    # Подрядчик — напрямую и из rating
    'pudrat_direct',        # поле pudrat напрямую
    'pudrat_inn',           # из rating.qurilish.inn
    'pudrat_name',          # из rating.qurilish.name
    'pudrat_reyting',       # из rating.qurilish.reyting_umumiy
    # Проектировщик — напрямую и из rating
    'loyiha_direct',        # поле loyiha напрямую
    'loyiha_inn',           # из rating.loyiha.inn
    'loyiha_name',          # из rating.loyiha.name
    'loyiha_reyting',       # из rating.loyiha.reyting_loyha
    # Объём
    'apartment_count', 'block_count',
    'floor_max',            # макс этажность из blocks
    'area_total',           # суммарная площадь из blocks
    'blocks_accepted',      # сколько корпусов принято
    'blocks_total',         # всего корпусов в blocks
    'difficulty',
    # Документы
    'reestr_number',        # номер экспертизы
    'number_protocol',      # номер архитектурного решения
    'conclusion_url',       # ссылка на PDF экспертизы
    # Гео
    'lat', 'long', 'location',
]


def parse_rating(raw):
    if not raw:
        return {}, {}
    if isinstance(raw, str):
        try: raw = json.loads(raw)
        except: return {}, {}
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
            try: floors.append(int(f))
            except: pass
        a = b.get('area')
        if a:
            try: areas.append(float(a))
            except: pass
        if b.get('accepted'):
            accepted += 1
    floor_max  = max(floors) if floors else ''
    area_total = round(sum(areas), 1) if areas else ''
    return floor_max, area_total, accepted, len(blocks)


def is_residential(data):
    if data.get('sphere_id') == 57:
        return True
    if int(data.get('apartment_count') or 0) > 0:
        return True
    name = (data.get('name') or '').lower()
    return any(m in name for m in ['турар-жой', 'turar-joy', 'квартир', 'яшаш', 'жилой'])


def fetch_and_parse(obj_id):
    try:
        r = requests.post(API_URL, json={'object_id': obj_id},
                          headers=HEADERS, timeout=12)
        if r.status_code != 200:
            return None
        d = r.json().get('data')
        if not d:
            return None
        if d.get('region_soato') != TASHKENT:
            return None
        if not is_residential(d):
            return None

        q, l = parse_rating(d.get('rating'))
        floor_max, area_total, accepted, total_b = parse_blocks(d.get('blocks') or [])
        conc = d.get('conclusion') or {}

        return {
            'object_id':       d.get('id', ''),
            'task_id':         d.get('task_id', ''),
            'name':            (d.get('name') or '').replace('\n', ' '),
            'sphere_id':       d.get('sphere_id', ''),
            'region_soato':    d.get('region_soato', ''),
            'district_soato':  d.get('district_soato', ''),
            'status':          (d.get('status') or {}).get('name', ''),
            'created_at':      (d.get('created_at') or '')[:10],
            'deadline':        d.get('deadline') or '',
            'closed_at':       d.get('closed_at') or '',
            'organization_name': d.get('organization_name') or '',
            'pudrat_direct':   d.get('pudrat') or '',
            'pudrat_inn':      q.get('inn', ''),
            'pudrat_name':     q.get('name', ''),
            'pudrat_reyting':  q.get('reyting_umumiy', ''),
            'loyiha_direct':   d.get('loyiha') or '',
            'loyiha_inn':      l.get('inn', ''),
            'loyiha_name':     l.get('name', ''),
            'loyiha_reyting':  l.get('reyting_loyha', ''),
            'apartment_count': d.get('apartment_count') or 0,
            'block_count':     d.get('block_count') or 0,
            'floor_max':       floor_max,
            'area_total':      area_total,
            'blocks_accepted': accepted,
            'blocks_total':    total_b,
            'difficulty':      d.get('difficulty') or '',
            'reestr_number':   d.get('reestr_number') or '',
            'number_protocol': d.get('number_protocol') or '',
            'conclusion_url':  conc.get('url', ''),
            'lat':             d.get('lat') or '',
            'long':            d.get('long') or '',
            'location':        d.get('location_building') or '',
        }
    except:
        return None


def flush(rows, num):
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    path = OUTPUT_DIR / f"{RUN_MONTH}_{num:02d}.csv"

    with open(path, 'w', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)

    print(f"  БАТЧ {num}: {len(rows)} объектов → {path}")


def run():
    print(f"Shaffof Parser v4 — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"Диапазоны: 1-7000 + 52000-70000 ({len(ID_RANGES)} ID)")
    print(f"Потоков: {WORKERS} | Батч каждые {BATCH_SIZE} найденных\n")

    found, batch_num, batch = 0, 1, []
    done = 0

    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futures = {ex.submit(fetch_and_parse, i): i for i in ID_RANGES}
        for future in as_completed(futures):
            done += 1
            row = future.result()
            if row:
                batch.append(row)
                found += 1
                print(f"++ ID {futures[future]:6d} | {found:4d} | {row['organization_name'][:45]}")
                if len(batch) >= BATCH_SIZE:
                    flush(batch, batch_num)
                    batch_num += 1
                    batch = []
            if done % 1000 == 0:
                print(f"  [{datetime.now().strftime('%H:%M')}] обработано {done}/{len(ID_RANGES)} | найдено {found}")

    if batch:
        flush(batch, batch_num)

    print(f"\nГотово: {found} объектов | {datetime.now().strftime('%Y-%m-%d %H:%M')}")


if __name__ == '__main__':
    run()
