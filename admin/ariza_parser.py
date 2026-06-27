"""
Парсер "ariza"/"ko'chirma" PDF (выписка о регистрации объекта для начала СМР,
портал shaffofqurilish.uz / my.gov.uz). Документы встречаются на двух
алфавитах узбекского (кириллица/латиница) и с опечатками в самих официальных
формах ("яратилган" / "яратилинган"), поэтому каждое поле описано списком
вариантов, а не одной регуляркой. Проверено на 4 реальных образцах (юрлицо и
физлицо-застройщик, оба алфавита, КЎЧИРМА и RUXSATNOMA) — все поля совпали.

Экспертные заключения (xulosa) — другой шаблон с табличной вёрсткой, сюда не
включены: наивное извлечение текста путает порядок "метка/значение", для
надёжного парсинга нужна работа с таблицами PDF, а не просто текст.
"""
import re

import pypdf

FIELD_VARIANTS = {
    'created_at': [
        r"[ҲH]ужжат\s+яратил(?:ган|инган)\s+сана:\s*([0-9-]{10})",
        r"Hujjat\s+yaratilgan\s+sana:\s*([0-9-]{10})",
    ],
    'application_number': [
        r"Ариза\s+рақами:\s*(\d+)",
        r"Ariza\s+raqami:\s*(\d+)",
    ],
    'tax_id_legal': [  # СТИР — юрлицо
        r"СТИР:\s*(\d+)",
        r"STIR:\s*(\d+)",
    ],
    'tax_id_individual': [  # ЖШШИР/JShShIR — физлицо (личный номер)
        r"ЖШШИР:\s*(\d+)",
        r"JShShIR:\s*(\d+)",
    ],
    'doc_title': [
        r"((?:Қурилиш-монтаж|Qurilish-montaj)[\s\S]{0,150}?(?:КЎЧИРМА|KO['‘’]?CHIRMA|RUXSATNOMA))",
    ],
}

NAME_VARIANTS = [
    r"[ҲH]ужжат\s+берилган:\s*([\s\S]+?)\s*(?=СТИР:|ЖШШИР:)",
    r"Hujjat\s+berilgan:\s*([\s\S]+?)\s*(?=STIR:|JShShIR:)",
]

DOC_HASH_RE = r"№\s*([0-9a-fA-F-]{20,})"


def extract_text(file_obj):
    reader = pypdf.PdfReader(file_obj)
    return '\n'.join(page.extract_text() or '' for page in reader.pages)


def _first_match(text, patterns):
    for pat in patterns:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            return re.sub(r'\s+', ' ', m.group(1)).replace('"', '').replace('“', '').replace('”', '').strip()
    return None


def parse(text):
    out = {'doc_hash': _first_match(text, [DOC_HASH_RE])}
    for field, patterns in FIELD_VARIANTS.items():
        out[field] = _first_match(text, patterns)
    out['applicant_name'] = _first_match(text, NAME_VARIANTS)
    out['tax_id'] = out['tax_id_legal'] or out['tax_id_individual']
    out['is_individual'] = bool(out['tax_id_individual']) and not out['tax_id_legal']
    return out


def parse_file(file_obj):
    return parse(extract_text(file_obj))
