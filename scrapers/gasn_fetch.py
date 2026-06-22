"""
Прямая выгрузка объектов из API ShaffofQurilish (GASN), без мастер-Excel.

Тот же эндпоинт, который дёргает сам сайт dshk.shaffofqurilish.uz:
    POST https://api-dshk.shaffofqurilish.uz/api/get-gasn-info
    body: {"object_id": <int>}

object_id — внутренний числовой id GASN-объекта (не CAM ID), судя по
наблюдениям растёт примерно последовательно, так что можно перебирать
диапазон. Несуществующие/недоступные id обычно отвечают ошибкой или
пустым data — такие пропускаем.

Запуск:
    python3 gasn_fetch.py --start 1 --end 5000 --out data/gasn_raw.jsonl
"""
import argparse
import json
import time
from pathlib import Path

import requests

API_URL = 'https://api-dshk.shaffofqurilish.uz/api/get-gasn-info'
HEADERS = {
    'Content-Type': 'application/json',
    'Origin': 'https://dshk.shaffofqurilish.uz',
    'Referer': 'https://dshk.shaffofqurilish.uz/',
    'User-Agent': 'Mozilla/5.0 (compatible; CAM-research-bot/1.0)',
}


def fetch_one(session, object_id, timeout=15):
    resp = session.post(API_URL, json={'object_id': object_id}, headers=HEADERS, timeout=timeout)
    if resp.status_code != 200:
        return None
    try:
        payload = resp.json()
    except ValueError:
        return None
    return payload.get('data')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--start', type=int, required=True)
    ap.add_argument('--end', type=int, required=True)
    ap.add_argument('--out', default='data/gasn_raw.jsonl')
    ap.add_argument('--delay', type=float, default=0.3, help='пауза между запросами, сек')
    args = ap.parse_args()

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    session = requests.Session()
    found, empty = 0, 0

    with open(out_path, 'a', encoding='utf-8') as f:
        for object_id in range(args.start, args.end + 1):
            data = fetch_one(session, object_id)
            if data:
                f.write(json.dumps(data, ensure_ascii=False) + '\n')
                found += 1
            else:
                empty += 1
            if object_id % 100 == 0:
                print(f'  ...{object_id}: найдено {found}, пусто {empty}')
            time.sleep(args.delay)

    print(f'Готово: {found} объектов -> {out_path} (пропущено: {empty})')


if __name__ == '__main__':
    main()
