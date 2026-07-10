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
import time

# несколько вариантов поиска: язык мог смениться/редиректить, а раскладка
# результатов отличаться — пробуем по очереди, пока не найдём карточку
ORGINFO_SEARCH_URLS = (
    'https://orginfo.uz/ru/search/all/?q={inn}',
    'https://orginfo.uz/ru/search/organizations/?q={inn}',
    'https://orginfo.uz/search/all/?q={inn}',
    'https://orginfo.uz/uz/search/all/?q={inn}',
)
# ссылка на карточку: язык в URL бывает /ru/ | /uz/ | /en/ или вовсе без него
ORG_LINK_RE = re.compile(r'href="((?:/(?:ru|uz|en))?/organization/[^"]+)"')

_orginfo_session = None


def _get_orginfo_session():
    """requests.Session с прогревом главной страницей — тот же приём, что
    вылечил reyting.mc.uz: без сессионных кук часть порталов отдаёт
    заглушку/403 вместо контента."""
    global _orginfo_session
    if _orginfo_session is None:
        import requests
        s = requests.Session()
        s.headers.update({
            'User-Agent': ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                           'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36'),
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Accept-Language': 'ru-RU,ru;q=0.9,uz;q=0.8,en;q=0.7',
        })
        try:
            s.get('https://orginfo.uz/', timeout=15)
        except Exception:
            pass
        _orginfo_session = s
    return _orginfo_session


def _get_html(url, timeout=15, attempts=2):
    """GET с ретраем; возвращает (status_code, text). Бросает последнюю ошибку."""
    last_err = None
    for i in range(attempts):
        try:
            resp = _get_orginfo_session().get(url, timeout=timeout)
            return resp.status_code, resp.text
        except Exception as e:
            last_err = e
            if i + 1 < attempts:
                time.sleep(1.5)
    raise last_err


def fetch_orginfo(inn, timeout=15):
    """orginfo.uz не имеет JSON-API (страница рендерится на сервере),
    поэтому скачиваем HTML и вытаскиваем поля. Автоматически, без ручной работы.

    Возвращает dict с полями или {'error': ...}.
    """
    inn = str(inn).strip()
    card_url = None
    search_errors = []
    for tpl in ORGINFO_SEARCH_URLS:
        url = tpl.format(inn=inn)
        try:
            status, search_html = _get_html(url, timeout=timeout)
        except Exception as e:
            search_errors.append(f'{url} -> {e}')
            continue
        if status != 200:
            search_errors.append(f'{url} -> HTTP {status}')
            continue
        m = ORG_LINK_RE.search(search_html)
        if m:
            card_url = 'https://orginfo.uz' + html_lib.unescape(m.group(1))
            break
        low = search_html.lower()
        if 'captcha' in low or 'cloudflare' in low:
            search_errors.append(f'{url} -> похоже на антибот/captcha')
        else:
            search_errors.append(f'{url} -> 200, но ссылки на карточку нет')
    if not card_url:
        return {'error': 'организация не найдена в поиске: ' + ' | '.join(search_errors),
                'inn': inn}

    try:
        status, page = _get_html(card_url, timeout=timeout)
        if status != 200:
            return {'error': f'карточка: HTTP {status}', 'inn': inn, 'url': card_url}
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
    """python3 org_enrich.py bulk [limit] [workers]

    Обходит все ИНН-карточки справочника параллельно (по умолчанию 6
    потоков): сохраняет статус (активна/ликвидирована) и дату регистрации.
    Весь справочник ≈ 2-3 минуты. Прерванный запуск можно повторить —
    уже проверенные сегодня пропускаются.
    """
    import os
    import sqlite3
    import sys
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from datetime import date as _date

    limit = int(sys.argv[2]) if len(sys.argv) > 2 else None
    workers = int(sys.argv[3]) if len(sys.argv) > 3 else 6
    db_path = os.path.join(os.path.dirname(__file__), 'cam_admin.db')
    conn = sqlite3.connect(db_path)
    from org_directory import ensure_schema
    ensure_schema(conn)  # добавит org_status/org_checked, если БД старее кода
    today = _date.today().isoformat()

    rows = conn.execute(
        "SELECT role, org_key FROM org_directory WHERE key_type='inn' "
        "AND (org_checked IS NULL OR org_checked < ?) "
        "ORDER BY objects_count DESC", (today,)).fetchall()
    if limit:
        rows = rows[:limit]
    inns = sorted({inn for _, inn in rows})

    # fail-fast: если сайт лежит/блокирует — первые же запросы падают все
    # подряд; нет смысла молоть весь справочник с ошибками
    probe_inn = inns[0] if inns else None
    if probe_inn:
        probe_result = fetch_orginfo(probe_inn)
        if probe_result.get('error'):
            print(f'Пробный запрос ({probe_inn}) не прошёл:\n  {probe_result["error"]}')
            print('\nСайт недоступен или блокирует запросы — bulk остановлен.')
            print('Проверьте вручную: python3 org_enrich.py orginfo ' + probe_inn)
            print('Если в браузере карточка открывается, а скрипт падает — '
                  'пришлите вывод, поправлю селекторы.')
            return

    ok = errors = liq = 0
    done = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(fetch_orginfo, inn): inn for inn in inns}
        for fut in as_completed(futures):
            inn = futures[fut]
            done += 1
            try:
                result = fut.result()
            except Exception as e:
                result = {'error': str(e)}
            if result.get('error'):
                errors += 1
                print(f'[{done}/{len(inns)}] {inn}: ошибка — {result["error"]}', flush=True)
                continue
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
            print(f'[{done}/{len(inns)}] {inn}: {result.get("status")} · {result.get("reg_date")}', flush=True)
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
