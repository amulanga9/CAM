"""
Получает рейтинг организации с reyting.mc.uz по ИНН.

API: GET https://reyting.mc.uz/get-modal?inn=<ИНН>
Ответ JSON:
  {
    "success": true,
    "data": {
      "umum":     {"hasData": true,  "name": "...", "reyting": "B"},
      "yol":      {"hasData": false, "message": "..."},
      "mel":      {"hasData": false, "message": "..."},
      "loyiha":   {"hasData": false, "message": "..."},
      "developer":{"hasData": false, "message": "..."}
    }
  }

Категории:
  umum      — Umumqurilish-ijtimoiy (общестрой)
  yol       — Avtomobil yo'llari, ko'priklar (дороги/мосты)
  mel       — Melioratsiya va irrigatsiya
  loyiha    — Loyiha tashkilotlari (проектировщики)
  developer — Застройщик
"""
import time
import json

import requests

BASE_URL = 'https://reyting.mc.uz/'
REYTING_URL = 'https://reyting.mc.uz/get-modal?inn={inn}'

HEADERS = {
    'User-Agent': ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                   'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36'),
    'Accept': 'application/json, text/javascript, */*; q=0.01',
    'X-Requested-With': 'XMLHttpRequest',
    'Referer': 'https://reyting.mc.uz/',
}

_session = None


def _get_session():
    """Сессия с куками: сначала «прогрев» главной страницей — без её
    session-куки портал отвечает HTML вместо JSON."""
    global _session
    if _session is None:
        s = requests.Session()
        s.headers.update(HEADERS)
        try:
            s.get(BASE_URL, timeout=15,
                  headers={'Accept': 'text/html,application/xhtml+xml'})
        except requests.RequestException:
            pass
        _session = s
    return _session

# категории в порядке приоритета для contractors и developers
CONTRACTOR_CATS = ('umum', 'yol', 'mel', 'loyiha')
DEVELOPER_CATS  = ('developer', 'umum')


def fetch_reyting(inn, timeout=10):
    """Запрашивает рейтинг по ИНН. Возвращает dict или None при ошибке.

    Возвращаемый dict:
        {
          'inn': '303094443',
          'name': '"SHAFOAT QURILISH" MCHJ',
          'categories': {
              'umum': 'B',
              'yol': None,
              ...
          },
          'rating': 'B',          # лучший найденный рейтинг
          'rating_category': 'umum',
          'raw': { ... }          # полный ответ
        }
    """
    if not inn:
        return None
    url = REYTING_URL.format(inn=str(inn).strip())
    try:
        resp = _get_session().get(url, timeout=timeout)
        body = resp.text
    except Exception as e:
        return {'error': str(e), 'inn': inn}

    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        # показываем начало ответа — сразу видно, HTML это, капча или редирект
        preview = ' '.join(body[:400].split())
        return {'error': f'не-JSON ответ: «{preview}»', 'inn': inn}

    if not data.get('success'):
        return {'error': 'success=false', 'inn': inn, 'raw': data}

    cats = data.get('data', {})
    categories = {}
    name = None
    score = None
    for cat_key, cat_data in cats.items():
        if not isinstance(cat_data, dict):
            continue
        if cat_data.get('hasData'):
            categories[cat_key] = cat_data.get('reyting')
            if not name:
                name = cat_data.get('name')
            # числовой балл (34.54 и т.п.) — поле может называться по-разному
            for k in ('ball', 'score', 'bal'):
                if score is None and cat_data.get(k) is not None:
                    try:
                        score = float(cat_data[k])
                    except (TypeError, ValueError):
                        pass
        else:
            categories[cat_key] = None

    # лучший рейтинг по приоритету (A > B > C > D)
    # реальная шкала портала: AAA лучший … DDD худший (видно по живым ответам)
    grade_order = {'AAA': 0, 'AA': 1, 'A': 2, 'BBB': 3, 'BB': 4, 'B': 5,
                   'CCC': 6, 'CC': 7, 'C': 8, 'DDD': 11, 'DD': 10, 'D': 9}
    best_rating, best_cat = None, None
    for cat_key in list(cats.keys()):
        g = categories.get(cat_key)
        if g and (best_rating is None or grade_order.get(g, 9) < grade_order.get(best_rating, 9)):
            best_rating = g
            best_cat = cat_key

    return {
        'inn': inn,
        'name': name,
        'categories': categories,
        'rating': best_rating,
        'rating_category': best_cat,
        'score': score,
        'raw': data,
    }


def fetch_and_save(conn, role, org_key, inn, snapshot_date=None):
    """Получает рейтинг и сохраняет в org_directory. Возвращает результат fetch."""
    result = fetch_reyting(inn)
    if not result or 'error' in result:
        return result

    rating = result.get('rating')
    name_from_portal = result.get('name')

    if rating:
        # портальный грейд (AAA..DDD) и балл — в отдельные поля;
        # ручной rating (ваша оценка A-D) НЕ трогается
        conn.execute(
            "UPDATE org_directory SET portal_rating=?, portal_score=?, "
            "portal_rating_checked=date('now') WHERE role=? AND org_key=?",
            (rating, result.get('score'), role, org_key)
        )
    if name_from_portal:
        # обновляем каноническое название только если оно ещё не правили вручную
        existing = conn.execute(
            'SELECT name_canonical FROM org_directory WHERE role=? AND org_key=?',
            (role, org_key)
        ).fetchone()
        if existing and not existing[0]:
            conn.execute(
                'UPDATE org_directory SET name_canonical=? WHERE role=? AND org_key=?',
                (name_from_portal, role, org_key)
            )
    conn.commit()
    return result


def bulk_fetch(conn, role=None, delay=0.5, limit=None):
    """Массово обновляет рейтинги для всех карточек с ИНН.

    delay — пауза между запросами (секунды), чтобы не нагружать сайт.
    """
    where = "key_type='inn'"
    params = []
    if role:
        where += ' AND role=?'
        params.append(role)
    rows = conn.execute(
        f'SELECT role, org_key FROM org_directory WHERE {where} ORDER BY objects_count DESC',
        params
    ).fetchall()
    if limit:
        rows = rows[:limit]

    ok, errors, not_found = 0, 0, 0
    for i, (r, key) in enumerate(rows):
        result = fetch_and_save(conn, r, key, key)
        if not result:
            errors += 1
        elif 'error' in result:
            errors += 1
        elif result.get('rating') is None:
            not_found += 1
        else:
            ok += 1
        if delay and i < len(rows) - 1:
            time.sleep(delay)

    return {'ok': ok, 'errors': errors, 'not_found': not_found, 'total': len(rows)}


def _probe_cli(inn):
    """python3 reyting_parser.py probe <ИНН> — ищет живой JSON-эндпоинт.

    Сайт переехал на SPA: старый /get-modal отдаёт оболочку React.
    Перебираем кандидатов; ✓ = пришёл JSON.
    """
    candidates = [
        'https://reyting.mc.uz/api/get-modal?inn={inn}',
        'https://api.reyting.mc.uz/get-modal?inn={inn}',
        'https://reyting.mc.uz/api/check?stir={inn}',
        'https://reyting.mc.uz/api/organization?stir={inn}',
        'https://reyting.mc.uz/api/organizations/{inn}',
        'https://reyting.mc.uz/backend/get-modal?inn={inn}',
        'https://api.mc.uz/reyting/get-modal?inn={inn}',
    ]
    s = _get_session()
    for tpl in candidates:
        url = tpl.format(inn=inn)
        try:
            r = s.get(url, timeout=12)
            body = r.text.lstrip()
            is_json = body[:1] in ('{', '[')
            mark = '✓' if (r.status_code == 200 and is_json) else '✗'
            print(f'{mark} [{r.status_code}] {url}')
            if is_json:
                print('   ', ' '.join(body[:300].split()))
        except Exception as e:
            print(f'✗ [ERR] {url} — {e}')
    print('\nЕсли всё ✗: DevTools (F12) → Network → Fetch/XHR → ввести ИНН,')
    print('нажать Tekshirish → пришлите URL запроса, который вернул данные.')


def _bulk_cli():
    """python3 reyting_parser.py bulk [developer|contractor] [limit]

    Массово подтягивает рейтинги для ВСЕХ карточек с ИНН (в отличие от
    кнопки топ-50 в админке). ~0.5с на запрос, весь справочник ≈ 6 минут.
    """
    import os
    import sqlite3
    import sys
    role = sys.argv[2] if len(sys.argv) > 2 else None
    limit = int(sys.argv[3]) if len(sys.argv) > 3 else None
    db_path = os.path.join(os.path.dirname(__file__), 'cam_admin.db')
    conn = sqlite3.connect(db_path)
    from org_directory import ensure_schema
    ensure_schema(conn)  # добавит portal_rating/portal_score, если БД старее кода

    where = "key_type='inn'"
    params = []
    if role:
        where += ' AND role=?'
        params.append(role)
    rows = conn.execute(
        f'SELECT role, org_key FROM org_directory WHERE {where} ORDER BY objects_count DESC',
        params).fetchall()
    if limit:
        rows = rows[:limit]

    ok = errors = not_found = 0
    for i, (r, inn) in enumerate(rows, 1):
        result = fetch_and_save(conn, r, inn, inn)
        if not result or 'error' in (result or {}):
            errors += 1
            mark = 'ошибка: ' + str((result or {}).get('error', 'нет ответа'))[:60]
        elif result.get('rating') is None:
            not_found += 1
            mark = 'нет на портале'
        else:
            ok += 1
            mark = f"рейтинг {result['rating']} ({result.get('rating_category')})"
        print(f'[{i}/{len(rows)}] {r} {inn}: {mark}', flush=True)
        if i < len(rows):
            time.sleep(0.5)
    print(f'\nитого: обновлено {ok}, нет на портале {not_found}, ошибок {errors}')


if __name__ == '__main__':
    import sys
    if len(sys.argv) > 2 and sys.argv[1] == 'probe':
        _probe_cli(sys.argv[2])
        sys.exit(0)
    if len(sys.argv) > 1 and sys.argv[1] == 'bulk':
        _bulk_cli()
    else:
        inn = sys.argv[1] if len(sys.argv) > 1 else '303094443'
        result = fetch_reyting(inn)
        print(json.dumps(result, ensure_ascii=False, indent=2))
