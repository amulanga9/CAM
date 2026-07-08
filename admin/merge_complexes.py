"""
Объединение карточек-дублей одного ЖК.

Портал (и старый мастер) дробят один ЖК на несколько объектов — по одному
на разрешение/очередь. Здесь: поиск кандидатов и слияние.

Правила корпусов:
  - обе карточки портальные (OBJ/EXT) — корпуса НЕ пересекаются, складываем
  - участвует GRP (мастер) — заявленное число могло уже включать соседей,
    берём максимум (и карточка остаётся на ручную сверку)

Слияние (src -> dst):
  - пустые поля dst заполняются из src
  - phases, reviews, complex_proofs, метки — перепривязка на dst
  - overrides/delay_flags src берутся, только если у dst их нет
  - blocks_total/apartments_count в raw_json — сумма или максимум
  - связь пишется в manual.cam_id_links (old -> new)
  - src удаляется из complexes
Проверено в песочнице: 9 автослияний, 609 -> 600 карточек, сирот нет.
"""
import json
import math
import re

FILL_FIELDS = [
    'project_name', 'address', 'district_name', 'lat', 'lng',
    'developer_name', 'developer_inn', 'developer_rating',
    'contractor_name', 'contractor_inn', 'contractor_rating',
    'created_at', 'deadline', 'dom_class', 'price_per_m2_uzs',
    'listing_url', 'brand_name', 'brand_status', 'delivered_year', 'holding_name',
]


def _num(x):
    try:
        return int(float(x))
    except (TypeError, ValueError):
        return None


def _dist_m(a, b):
    dlat = (a['lat'] - b['lat']) * 111000
    dlng = (a['lng'] - b['lng']) * 111000 * math.cos(math.radians(a['lat']))
    return math.sqrt(dlat ** 2 + dlng ** 2)


def _norm_name(s):
    if not s:
        return ''
    s = s.lower().replace('жк', '').replace('«', '').replace('»', '').replace('"', '')
    return re.sub(r'[^a-zа-я0-9]', '', s)


def find_duplicate_clusters(conn, radius_m=300):
    """Кластеры карточек: <radius_m и (один ИНН застройщика ИЛИ похожее имя).

    Возвращает список кластеров; кластер = list of dict(cam_id, project_name,
    developer_name, developer_inn, blocks_total, auto) — auto=True, если кластер
    можно слить автоматически (ровно 2 карточки, один ИНН, без GRP).
    """
    rows = []
    for r in conn.execute(
            'SELECT cam_id, project_name, developer_name, developer_inn, lat, lng, '
            'case_status_clean, deadline, raw_json FROM complexes WHERE lat IS NOT NULL'):
        raw = json.loads(r['raw_json']) if r['raw_json'] else {}
        rows.append({
            'cam_id': r['cam_id'], 'project_name': r['project_name'],
            'developer_name': r['developer_name'], 'developer_inn': r['developer_inn'],
            'lat': r['lat'], 'lng': r['lng'],
            'case_status_clean': r['case_status_clean'], 'deadline': r['deadline'],
            'blocks_total': _num(raw.get('blocks_total')),
        })

    parent = {r['cam_id']: r['cam_id'] for r in rows}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for i in range(len(rows)):
        for j in range(i + 1, len(rows)):
            a, b = rows[i], rows[j]
            if _dist_m(a, b) >= radius_m:
                continue
            same_inn = a['developer_inn'] and a['developer_inn'] == b['developer_inn']
            na, nb = _norm_name(a['project_name']), _norm_name(b['project_name'])
            same_name = na and nb and (na == nb or na in nb or nb in na)
            if same_inn or same_name:
                parent[find(a['cam_id'])] = find(b['cam_id'])

    groups = {}
    by_id = {r['cam_id']: r for r in rows}
    for r in rows:
        groups.setdefault(find(r['cam_id']), []).append(r['cam_id'])

    clusters = []
    for ids in groups.values():
        if len(ids) < 2:
            continue
        members = [by_id[i] for i in sorted(ids)]
        inns = {m['developer_inn'] for m in members if m['developer_inn']}
        has_grp = any(m['cam_id'].startswith('GRP') for m in members)
        auto = len(members) == 2 and len(inns) == 1 and not has_grp
        clusters.append({'members': members, 'auto': auto,
                         'same_inn': len(inns) <= 1, 'has_grp': has_grp})
    clusters.sort(key=lambda c: (not c['auto'], -len(c['members'])))
    return clusters


def _filled_score(row):
    return sum(1 for f in FILL_FIELDS if row[f] not in (None, ''))


def _as_list(v):
    if isinstance(v, list):
        return v
    if isinstance(v, str) and v.strip():
        return re.findall(r'\d+', v)
    return []


def merge_pair(conn, cam_a, cam_b, dst=None, reason='merge_duplicate'):
    """Объединяет две карточки. dst можно задать явно; иначе — более заполненная.

    Возвращает (dst, src). Требует ATTACH manual.
    """
    a = conn.execute('SELECT * FROM complexes WHERE cam_id=?', (cam_a,)).fetchone()
    b = conn.execute('SELECT * FROM complexes WHERE cam_id=?', (cam_b,)).fetchone()
    if a is None or b is None:
        raise ValueError(f'карточка не найдена: {cam_a if a is None else cam_b}')
    if dst is None:
        dst_row, src_row = (a, b) if _filled_score(a) >= _filled_score(b) else (b, a)
    else:
        dst_row, src_row = (a, b) if a['cam_id'] == dst else (b, a)
    dst, src = dst_row['cam_id'], src_row['cam_id']

    sets, params = [], []
    for f in FILL_FIELDS:
        if dst_row[f] in (None, '') and src_row[f] not in (None, ''):
            sets.append(f'{f}=?')
            params.append(src_row[f])

    raw_dst = json.loads(dst_row['raw_json']) if dst_row['raw_json'] else {}
    raw_src = json.loads(src_row['raw_json']) if src_row['raw_json'] else {}
    # GRP-карточка могла уже включать корпуса соседа — тогда max, иначе сумма
    sum_blocks = not (dst.startswith('GRP') or src.startswith('GRP'))
    for field in ('blocks_total', 'blocks_accepted_for_card', 'apartments_count'):
        va, vb = _num(raw_dst.get(field)), _num(raw_src.get(field))
        if va is None and vb is None:
            continue
        raw_dst[field] = ((va or 0) + (vb or 0)) if sum_blocks else max(va or 0, vb or 0)
    ids = list({str(i) for i in _as_list(raw_dst.get('shaffof_ids')) + _as_list(raw_src.get('shaffof_ids'))})
    if ids:
        raw_dst['shaffof_ids'] = sorted(ids)
    raw_dst['merged_from'] = sorted(set(raw_dst.get('merged_from', []) + [src]))
    sets.append('raw_json=?')
    params.append(json.dumps(raw_dst, ensure_ascii=False))

    params.append(dst)
    conn.execute(f'UPDATE complexes SET {", ".join(sets)} WHERE cam_id=?', params)

    conn.execute('UPDATE phases SET cam_id=? WHERE cam_id=?', (dst, src))
    try:
        conn.execute('UPDATE permits SET cam_id=? WHERE cam_id=?', (dst, src))
    except Exception:
        pass  # старая БД без таблицы permits
    conn.execute('INSERT OR IGNORE INTO complex_tags SELECT ?, tag FROM complex_tags WHERE cam_id=?', (dst, src))
    conn.execute('DELETE FROM complex_tags WHERE cam_id=?', (src,))

    conn.execute('UPDATE manual.reviews SET cam_id=? WHERE cam_id=?', (dst, src))
    conn.execute('UPDATE manual.complex_proofs SET cam_id=? WHERE cam_id=?', (dst, src))
    for tbl in ('overrides', 'delay_flags'):
        if conn.execute(f'SELECT 1 FROM manual.{tbl} WHERE cam_id=?', (dst,)).fetchone():
            conn.execute(f'DELETE FROM manual.{tbl} WHERE cam_id=?', (src,))
        else:
            conn.execute(f'UPDATE manual.{tbl} SET cam_id=? WHERE cam_id=?', (dst, src))
    conn.execute('UPDATE manual.rebrands SET cam_id=? WHERE cam_id=?', (dst, src))

    conn.execute(
        "INSERT INTO manual.cam_id_links (old_cam_id, new_cam_id, reason, created_at) "
        "VALUES (?,?,?,date('now'))", (src, dst, reason))
    conn.execute('DELETE FROM complexes WHERE cam_id=?', (src,))
    conn.commit()
    return dst, src


def auto_merge_all(conn):
    """Сливает все автоматические пары. Возвращает список (src, dst)."""
    done = []
    for cl in find_duplicate_clusters(conn):
        if not cl['auto']:
            continue
        a, b = cl['members']
        dst, src = merge_pair(conn, a['cam_id'], b['cam_id'], reason='auto_merge_same_inn')
        done.append((src, dst))
    return done
