"""
Автообновление данных организации по ИНН из официальных источников
(вместо ручного parsing'а страниц).

Кандидаты-источники (по ИНН, JSON):
  1. stat.uz / registr.stat.uz — ЕГРПО Госкомстата: название, ОКЭД, дата
     регистрации, статус (действует/ликвидирована), адрес
  2. orginfo.uz — агрегатор (внутренний JSON их фронтенда)
  3. data.egov.uz — открытые датасеты (реестр юрлиц), скорее для массовой сверки

ВАЖНО: из облачной среды эти хосты заблокированы, эндпоинты не проверены
вживую. Запустите диагностику локально:

    python3 org_enrich.py 305171188

Она пробует все кандидаты и печатает, кто ответил и чем. Если все мимо —
откройте карточку компании на orginfo.uz с DevTools (F12 → Network → XHR),
найдите JSON-запрос и пришлите его URL+ответ: добавлю точную интеграцию,
как сделали с reyting.mc.uz.
"""
import json
import urllib.request

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)',
    'Accept': 'application/json, text/plain, */*',
}

# (source, url_template, note)
CANDIDATES = [
    ('stat.uz',    'https://registr.stat.uz/api/v1/organizations?tin={inn}',
     'ЕГРПО, поиск по СТИР'),
    ('stat.uz',    'https://registr.stat.uz/api/v1/organizations/by-tin/{inn}',
     'ЕГРПО, карточка по СТИР'),
    ('stat.uz',    'https://api.stat.uz/api/v1/registr/organizations?tin={inn}',
     'ЕГРПО, альтернативный хост'),
    ('orginfo.uz', 'https://orginfo.uz/api/v1/organizations/{inn}',
     'карточка организации'),
    ('orginfo.uz', 'https://orginfo.uz/api/search?q={inn}',
     'поиск'),
    ('soliq.uz',   'https://new.soliq.uz/api/np1/tin-info?tin={inn}',
     'налоговая, проверка налогоплательщика'),
]


def try_url(url, timeout=12):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        ctype = resp.headers.get('Content-Type', '')
        body = resp.read().decode('utf-8', errors='replace')
    return resp.status, ctype, body


def probe(inn):
    """Пробует все кандидаты, возвращает список результатов для диагностики."""
    results = []
    for source, tpl, note in CANDIDATES:
        url = tpl.format(inn=inn)
        try:
            status, ctype, body = try_url(url)
            is_json = 'json' in ctype or body.lstrip()[:1] in ('{', '[')
            preview = body[:400]
            results.append({'source': source, 'url': url, 'note': note,
                            'status': status, 'json': is_json, 'preview': preview})
        except Exception as e:
            results.append({'source': source, 'url': url, 'note': note,
                            'status': None, 'json': False, 'preview': f'ОШИБКА: {e}'})
    return results


import html as html_lib
import re

ORGINFO_SEARCH = 'https://orginfo.uz/ru/search/all/?q={inn}'


def fetch_orginfo(inn, timeout=15):
    """orginfo.uz не имеет JSON-API (страница рендерится на сервере),
    поэтому скачиваем HTML и вытаскиваем поля. Автоматически, без ручной работы.

    Возвращает dict с полями или {'error': ...}.
    """
    try:
        _, _, search_html = try_url(ORGINFO_SEARCH.format(inn=inn), timeout=timeout)
    except Exception as e:
        return {'error': f'поиск: {e}', 'inn': inn}

    # ссылка на карточку организации из результатов поиска
    m = re.search(r'href="(/ru/organization/[^"]+)"', search_html)
    if not m:
        return {'error': 'организация не найдена в поиске', 'inn': inn}
    card_url = 'https://orginfo.uz' + html_lib.unescape(m.group(1))

    try:
        _, _, page = try_url(card_url, timeout=timeout)
    except Exception as e:
        return {'error': f'карточка: {e}', 'inn': inn, 'url': card_url}

    text = re.sub(r'<[^>]+>', '\n', page)
    text = html_lib.unescape(text)
    text = re.sub(r'\n\s*\n+', '\n', text)

    def after(label):
        m2 = re.search(re.escape(label) + r'\s*\n\s*([^\n]+)', text, re.IGNORECASE)
        return m2.group(1).strip() if m2 else None

    reg_date = after('Дата регистрации') or after("Ro'yxatdan o'tgan sana")
    # 13.12.2017 -> 2017-12-13
    if reg_date:
        m3 = re.match(r'(\d{2})\.(\d{2})\.(\d{4})', reg_date)
        if m3:
            reg_date = f'{m3.group(3)}-{m3.group(2)}-{m3.group(1)}'

    def code_only(v):
        # "152 -" / "41201 -" -> "152" / "41201"
        if not v:
            return v
        m4 = re.match(r'(\d+)', v)
        return m4.group(1) if m4 else v

    result = {
        'inn': inn,
        'url': card_url,
        'name_official': after('Официальное название организации') or after('Официальное название'),
        'name_short': after('Краткое название организации') or after('Краткое название'),
        'status': after('Статус') or after('Holati'),
        'reg_date': reg_date,
        'reg_authority': after('Регистрирующий орган'),
        'opf': code_only(after('ОПФ')),
        'oked': code_only(after('ОКЭД') or after('OKED')),
        'address': after('Адрес') or after('Manzil'),
        'director': after('Руководитель') or after('Rahbar'),
        'charter_capital': after('Уставный фонд') or after('Ustav fondi'),
    }
    mt = re.search(r'<title>([^<]+)</title>', page)
    if mt:
        result['name_title'] = html_lib.unescape(mt.group(1)).split('|')[0].strip()
    return result


def _bulk_orginfo():
    """python3 org_enrich.py bulk [limit]

    Обходит все ИНН-карточки справочника, сохраняет статус (активна/
    ликвидирована), дату регистрации и официальные названия (в алиасы).
    ~1 сек на организацию, весь справочник ≈ 11 минут. Прерванный запуск
    можно повторить — уже проверенные сегодня пропускаются.
    """
    import os
    import sqlite3
    import sys
    import time
    from datetime import date as _date

    limit = int(sys.argv[2]) if len(sys.argv) > 2 else None
    db_path = os.path.join(os.path.dirname(__file__), 'cam_admin.db')
    conn = sqlite3.connect(db_path)
    today = _date.today().isoformat()

    rows = conn.execute(
        "SELECT role, org_key FROM org_directory WHERE key_type='inn' "
        "AND (org_checked IS NULL OR org_checked < ?) "
        "ORDER BY objects_count DESC", (today,)).fetchall()
    if limit:
        rows = rows[:limit]

    ok = errors = liq = 0
    for i, (role, inn) in enumerate(rows, 1):
        result = fetch_orginfo(inn)
        if result.get('error'):
            errors += 1
            print(f'[{i}/{len(rows)}] {inn}: ошибка — {result["error"]}', flush=True)
        else:
            sets, params = ['org_checked=?'], [today]
            if result.get('status'):
                sets.append('org_status=?')
                params.append(result['status'])
                if 'ктив' not in result['status'] and 'ctive' not in result['status']:
                    liq += 1
            if result.get('reg_date'):
                sets.append('reg_date=?')
                params.append(result['reg_date'])
            params.append(inn)
            conn.execute(f'UPDATE org_directory SET {", ".join(sets)} WHERE org_key=?', params)
            conn.commit()
            ok += 1
            print(f'[{i}/{len(rows)}] {inn}: {result.get("status")} · {result.get("reg_date")}', flush=True)
        if i < len(rows):
            time.sleep(1.0)
    print(f'\nитого: проверено {ok}, НЕ активных: {liq}, ошибок {errors}')


if __name__ == '__main__':
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == 'bulk':
        _bulk_orginfo()
        sys.exit(0)
    if len(sys.argv) > 2 and sys.argv[1] == 'orginfo':
        print(json.dumps(fetch_orginfo(sys.argv[2]), ensure_ascii=False, indent=2))
        sys.exit(0)
    inn = sys.argv[1] if len(sys.argv) > 1 else '305171188'
    print(f'Диагностика источников по ИНН {inn}\n' + '=' * 60)
    for r in probe(inn):
        mark = '✓' if (r['status'] == 200 and r['json']) else '✗'
        print(f"\n{mark} [{r['source']}] {r['note']}")
        print(f"  {r['url']}")
        print(f"  статус: {r['status']}, json: {r['json']}")
        print(f"  ответ: {r['preview'][:300]}")
    print('\n' + '=' * 60)
    print('Если есть ✓ — пришлите вывод целиком, встрою автообновление.')
    print('Если все ✗ — откройте orginfo.uz с DevTools (F12 → Network),')
    print('найдите JSON-запрос при открытии карточки компании и пришлите URL.')
