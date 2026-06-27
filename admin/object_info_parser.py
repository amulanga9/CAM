"""
Парсер страницы https://api-nazorat.mc.uz/object-info/{object_id} (портал
shaffofqurilish/nazorat) — там же лежит дата выдачи разрешения на стройку
(permit_date), которой нет в ariza-PDF, плюс номера экспертизы/АПЗ/кучирмы
и собственный дедлайн портала (portal_deadline).

Страница сгенерирована на сервере (не XHR), поэтому достаточно обычного
GET и разбора HTML регулярками — без браузера/JS.
"""
import html as html_lib
import re

import requests

OBJECT_INFO_URL = 'https://api-nazorat.mc.uz/object-info/{object_id}'
OBJECT_PDF_URL = 'https://api-nazorat.mc.uz/api/get-object-pdf/{object_id}'

LABELS = {
    'object_name': 'Obyekt nomi:',
    'permit_date': 'Qurilishga ruxsat berilgan sana:',
    'customer': 'Buyurtmachi:',
    'designer': 'Loyihachi:',
    'contractor': 'Pudrat tashkiloti:',
    'address': 'Manzili:',
}


def parse(text):
    out = {}
    for field, label in LABELS.items():
        pat = re.escape(label) + r'</span>\s*<(?:p|div)[^>]*>([\s\S]+?)</(?:p|div)>'
        m = re.search(pat, text)
        if m:
            raw = re.sub(r'<[^>]+>', ' ', m.group(1))
            raw = html_lib.unescape(raw)
            out[field] = re.sub(r'\s+', ' ', raw).strip()
        else:
            out[field] = None

    m = re.search(r"Ekspertiza xulosasining reestr raqami[\s\S]*?<td[^>]*>(\d+)</td>", text)
    out['expertise_number'] = m.group(1) if m else None

    m = re.search(r"Arxitektura-shaxarsozlik kengashi[\s\S]*?<td[^>]*>(\d+)</td>", text)
    out['apz_number'] = m.group(1) if m else None

    m = re.search(r"ko'?chirma</td>\s*<td[^>]*>(\d+)</td>", text, re.IGNORECASE)
    out['kochirma_number'] = m.group(1) if m else None

    m = re.search(r'maps\.google\.com/maps\?q=([\-0-9.]+),([\-0-9.]+)', text)
    out['lat'], out['lng'] = (m.group(1), m.group(2)) if m else (None, None)

    m = re.search(r'const\s+endDate\s*=\s*new Date\("([0-9T:\-]+)"\)', text)
    out['portal_deadline'] = m.group(1).split('T')[0] if m else None

    m = re.search(r'const\s+taskId\s*=\s*"(\d+)"', text)
    out['task_id'] = m.group(1) if m else None

    return out


def fetch(object_id, timeout=10):
    resp = requests.get(OBJECT_INFO_URL.format(object_id=object_id), timeout=timeout)
    resp.raise_for_status()
    return parse(resp.text)


def fetch_pdf_bytes(object_id, timeout=15):
    """Возвращает байты PDF-выписки (ariza/ko'chirma), скачанной с портала
    по object_id (без ручной загрузки пользователем), либо None, если
    портал не нашёл документ для этого объекта."""
    resp = requests.get(OBJECT_PDF_URL.format(object_id=object_id), timeout=timeout)
    resp.raise_for_status()
    data = resp.json()
    b64 = (data or {}).get('result', {}).get('data', {}).get('file')
    if not b64:
        return None
    import base64
    return base64.b64decode(b64)


def object_id_from_cam_id(cam_id):
    if not cam_id or not cam_id.startswith('GASN-'):
        return None
    return cam_id.split('-', 1)[1]
