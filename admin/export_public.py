"""
Export public JSON data from cam_admin.db + cam_manual.db.

Generates:
  ../data/complexes.json   — full index (all complexes, lightweight fields)
  ../data/complex/<id>.json — per-complex dossier (all detail)

Run manually or after each re-parse:
  python3 export_public.py
"""
import json
import os
import sqlite3

ADMIN_DB  = os.path.join(os.path.dirname(__file__), 'cam_admin.db')
MANUAL_DB = os.path.join(os.path.dirname(__file__), 'cam_manual.db')
DATA_DIR  = os.path.join(os.path.dirname(__file__), '..', 'data')
COMPLEX_DIR = os.path.join(DATA_DIR, 'complex')


def _scalar(v):
    if isinstance(v, float) and v != v:
        return None
    return v


def load_overrides(manual_conn):
    rows = manual_conn.execute('SELECT * FROM overrides').fetchall()
    cols = [r[1] for r in manual_conn.execute('PRAGMA table_info(overrides)')]
    return {r[0]: dict(zip(cols, r)) for r in rows}


def load_reviews(manual_conn):
    rows = manual_conn.execute(
        'SELECT cam_id, verdict, comment, reviewed_at FROM reviews ORDER BY id'
    ).fetchall()
    result = {}
    for cam_id, verdict, comment, reviewed_at in rows:
        result.setdefault(cam_id, []).append({
            'verdict': verdict, 'comment': comment, 'reviewed_at': reviewed_at
        })
    return result


def load_delay_flags(manual_conn):
    try:
        rows = manual_conn.execute('SELECT cam_id, flag, note FROM delay_flags').fetchall()
        result = {}
        for cam_id, flag, note in rows:
            result.setdefault(cam_id, []).append({'flag': flag, 'note': note})
        return result
    except Exception:
        return {}


def load_org_ratings(admin_conn):
    """Returns (role, org_key) -> rating from org_directory."""
    rows = admin_conn.execute(
        'SELECT role, org_key, rating, name_canonical FROM org_directory'
    ).fetchall()
    return {(r[0], r[1]): {'rating': r[2], 'name_canonical': r[3]} for r in rows}


def load_org_keys(admin_conn):
    """Returns (role, raw_inn) -> org_key mapping for INN-keyed orgs."""
    rows = admin_conn.execute(
        "SELECT role, org_key FROM org_directory WHERE key_type='inn'"
    ).fetchall()
    return {(r[0], r[1]): r[1] for r in rows}


def build_complex_record(row, cols, overrides, reviews, delay_flags, org_ratings):
    d = dict(zip(cols, [_scalar(v) for v in row]))
    cam_id = d['cam_id']

    # apply overrides
    ov = overrides.get(cam_id, {})
    for field in ('project_name', 'address', 'district_name', 'lat', 'lng',
                  'developer_name', 'developer_inn', 'contractor_name', 'contractor_inn',
                  'deadline', 'dom_class', 'price_per_m2_uzs', 'listing_url',
                  'brand_name', 'delivered_year', 'holding_name', 'brand_status'):
        if ov.get(field) not in (None, ''):
            d[field] = ov[field]

    # org directory ratings
    dev_inn = str(d.get('developer_inn') or '').strip()
    cont_inn = str(d.get('contractor_inn') or '').strip()
    if dev_inn:
        org = org_ratings.get(('developer', dev_inn))
        if org and org['rating']:
            d['dev_dir_rating'] = org['rating']
            d['dev_dir_name'] = org['name_canonical']
    if cont_inn:
        org = org_ratings.get(('contractor', cont_inn))
        if org and org['rating']:
            d['cont_dir_rating'] = org['rating']
            d['cont_dir_name'] = org['name_canonical']

    # reviews
    d['reviews'] = reviews.get(cam_id, [])

    # delay flags
    d['delay_flags'] = delay_flags.get(cam_id, [])

    # clean up raw_json (large, internal)
    d.pop('raw_json', None)

    return d


def export(admin_db=ADMIN_DB, manual_db=MANUAL_DB, data_dir=DATA_DIR):
    os.makedirs(os.path.join(data_dir, 'complex'), exist_ok=True)

    admin_conn  = sqlite3.connect(admin_db)
    manual_conn = sqlite3.connect(manual_db)

    overrides   = load_overrides(manual_conn)
    reviews     = load_reviews(manual_conn)
    delay_flags = load_delay_flags(manual_conn)
    org_ratings = load_org_ratings(admin_conn)

    cols = [r[1] for r in admin_conn.execute('PRAGMA table_info(complexes)')]
    rows = admin_conn.execute('SELECT * FROM complexes ORDER BY cam_id').fetchall()

    index = []
    for row in rows:
        rec = build_complex_record(row, cols, overrides, reviews, delay_flags, org_ratings)

        # write per-complex file
        path = os.path.join(data_dir, 'complex', f"{rec['cam_id']}.json")
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(rec, f, ensure_ascii=False, separators=(',', ':'))

        # lightweight index entry
        index.append({
            'cam_id':        rec['cam_id'],
            'project_name':  rec.get('project_name'),
            'address':       rec.get('address'),
            'district_name': rec.get('district_name'),
            'lat':           rec.get('lat'),
            'lng':           rec.get('lng'),
            'case_status_clean': rec.get('case_status_clean'),
            'developer_name': rec.get('developer_name'),
            'developer_inn':  rec.get('developer_inn'),
            'dev_dir_rating': rec.get('dev_dir_rating'),
            'deadline':      rec.get('deadline'),
            'is_overdue':    rec.get('is_overdue'),
            'days_overdue':  rec.get('days_overdue'),
            'dom_class':     rec.get('dom_class'),
            'price_per_m2_uzs': rec.get('price_per_m2_uzs'),
            'listing_url':   rec.get('listing_url'),
            'brand_name':    rec.get('brand_name'),
            'needs_review':  rec.get('needs_review'),
        })

    with open(os.path.join(data_dir, 'complexes.json'), 'w', encoding='utf-8') as f:
        json.dump(index, f, ensure_ascii=False, separators=(',', ':'))

    admin_conn.close()
    manual_conn.close()
    return {'total': len(rows), 'data_dir': data_dir}


if __name__ == '__main__':
    result = export()
    print(f"Exported {result['total']} complexes → {result['data_dir']}")
