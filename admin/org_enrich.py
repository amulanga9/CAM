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


if __name__ == '__main__':
    import sys
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
