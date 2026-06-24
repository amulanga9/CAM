"""
Доп. сигналы с domtut.uz: даты/рейтинги отзывов + дата старта листинга
по объявлениям риелторов, для детекции "висяков" Shaffof (объекты,
которые официально "в процессе", но по факту замороже/не продаются).

Обычный requests.get — без headless-браузера, всё приходит в HTML
страницы ЖК (см. domtut_parse_all.py — тот же сайт, те же классы).

ИСТОЧНИКИ:
  - страница ЖК:       https://domtut.uz/nedvizhimost/zhiloj-kompleks-<slug>
  - объявление риелтора: https://domtut.uz/nedvizhimost/<id>-<slug>

ЧТО СЧИТАЕМ НА ОБЪЕКТ:
  - last_review_date           = max(дата отзыва)
  - last_negative_review_date  = max(дата отзыва с rating<=2
                                       или текст содержит "не сдан/ждём/обещал/задерж")
  - domtut_deadline             = "Срок сдачи" со страницы ЖК (YYYY-MM-DD, если распарсилось)
  - listing_start_date          = min("Опубликовано DD.MM.YYYY" по живым объявлениям риелторов)
  - listing_count                = число живых объявлений риелторов
  - is_lower_bound                = True  # старые снятые объявления не видны, это нижняя граница

СИГНАЛ ЗАМОРОЗКИ (фича для ревью, не метка):
  last_negative_review_date > deadline → кандидат на "висяк"/заморозку.
  Позитивный отзыв до дедлайна сигналом не считается.

Запуск:
    cd ~/CAM/scrapers
    python3 parse_domtut_signals.py --urls ../Domtut/domtut_urls.txt --out ../data/domtut_signals.csv
"""
import argparse
import csv
import re
import time
from datetime import date, datetime
from pathlib import Path

import requests
from bs4 import BeautifulSoup

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                  '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Accept-Language': 'ru-RU,ru;q=0.9',
}
TIMEOUT = 15
DELAY = 0.8

MONTHS_RU_GEN = {
    'января': 1, 'февраля': 2, 'марта': 3, 'апреля': 4, 'мая': 5, 'июня': 6,
    'июля': 7, 'августа': 8, 'сентября': 9, 'октября': 10, 'ноября': 11, 'декабря': 12,
}
REVIEW_DATE_RE = re.compile(r'(\d{1,2})\s+([А-Яа-я]+)\s+(\d{4})')
LISTING_DATE_RE = re.compile(r'Опубликовано\s+(\d{2})\.(\d{2})\.(\d{4})')
NEGATIVE_WORDS = ['не сдан', 'ждём', 'ждем', 'обещал', 'задерж', 'заморож', 'обман']

CSV_FIELDS = [
    'slug', 'url', 'domtut_deadline',
    'last_review_date', 'last_negative_review_date', 'reviews_count',
    'listing_start_date', 'listing_count', 'is_lower_bound',
]


def parse_review_date(text):
    m = REVIEW_DATE_RE.search(text)
    if not m:
        return None
    day, month_ru, year = m.groups()
    month = MONTHS_RU_GEN.get(month_ru.lower())
    if not month:
        return None
    try:
        return date(int(year), month, int(day))
    except ValueError:
        return None


def parse_listing_date(text):
    m = LISTING_DATE_RE.search(text)
    if not m:
        return None
    day, month, year = m.groups()
    try:
        return date(int(year), int(month), int(day))
    except ValueError:
        return None


def parse_deadline(soup):
    important = soup.find('section', id='important') or soup.find('div', id='important')
    if not important:
        return None
    for stat in important.select('.stat, .item'):
        label = stat.select_one('.gray, .label, span:first-child')
        value = stat.select_one('b, strong, .val')
        if label and value and ('Срок' in label.text or 'сдач' in label.text.lower()):
            m = re.search(r'(\d{4})-(\d{2})-(\d{2})', value.text)
            if m:
                return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            m2 = re.search(r'(\d{2})\.(\d{2})\.(\d{4})', value.text)
            if m2:
                return date(int(m2.group(3)), int(m2.group(2)), int(m2.group(1)))
    return None


def parse_reviews(soup):
    """Карточки отзывов: автор, рейтинг (число звёзд), текст, дата."""
    reviews = []
    for card in soup.select('.review, .review-item, .otziv, .comment-item'):
        text = card.get_text(' ', strip=True)
        review_date = parse_review_date(text)
        if not review_date:
            continue
        stars = len(card.select('.star.active, .star-filled, .fa-star.active'))
        rating = stars if stars else None
        reviews.append({'date': review_date, 'rating': rating, 'text': text})
    return reviews


def is_negative(review):
    if review['rating'] is not None and review['rating'] <= 2:
        return True
    return any(w in review['text'].lower() for w in NEGATIVE_WORDS)


def collect_listing_links(soup, base_url):
    """Блок 'Предложения от риелторов' — та же секция, что в domtut_parse_all.py."""
    links = set()
    for h5 in soup.find_all('h5', class_='title'):
        if 'риелторов' not in h5.text:
            continue
        wrap = h5.find_parent('div', class_='wr_title')
        section = wrap.find_parent('div', class_='wrap-content') if wrap else None
        if not section:
            continue
        for card in section.select('.property-card.apart'):
            a = card.find('a', href=True)
            if a:
                href = a['href']
                full = href if href.startswith('http') else 'https://domtut.uz' + href
                links.add(full)
        break
    return links


def fetch(url, session):
    for attempt in range(2):
        try:
            r = session.get(url, headers=HEADERS, timeout=TIMEOUT)
            if r.status_code == 200:
                return r.text
            return None
        except requests.RequestException:
            time.sleep(2)
    return None


def parse_listing_page(html):
    soup = BeautifulSoup(html, 'html.parser')
    text = soup.get_text(' ', strip=True)
    published = parse_listing_date(text)
    if not published:
        meta = soup.find('meta', property='article:modified_time')
        if meta and meta.get('content'):
            try:
                published = datetime.fromisoformat(meta['content'][:10]).date()
            except ValueError:
                pass
    return published


def process_complex(url, session):
    html = fetch(url, session)
    if not html:
        return None
    soup = BeautifulSoup(html, 'html.parser')
    slug = url.split('zhiloj-kompleks-')[-1]

    deadline = parse_deadline(soup)
    reviews = parse_reviews(soup)
    last_review = max((r['date'] for r in reviews), default=None)
    negative_dates = [r['date'] for r in reviews if is_negative(r)]
    last_negative = max(negative_dates, default=None)

    listing_links = collect_listing_links(soup, url)
    published_dates = []
    for link in listing_links:
        listing_html = fetch(link, session)
        if listing_html:
            d = parse_listing_page(listing_html)
            if d:
                published_dates.append(d)
        time.sleep(DELAY)

    return {
        'slug': slug,
        'url': url,
        'domtut_deadline': deadline.isoformat() if deadline else '',
        'last_review_date': last_review.isoformat() if last_review else '',
        'last_negative_review_date': last_negative.isoformat() if last_negative else '',
        'reviews_count': len(reviews),
        'listing_start_date': min(published_dates).isoformat() if published_dates else '',
        'listing_count': len(listing_links),
        'is_lower_bound': bool(listing_links),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--urls', required=True, help='файл со списком URL страниц ЖК (как domtut_urls.txt)')
    ap.add_argument('--out', default='domtut_signals.csv')
    args = ap.parse_args()

    urls = [u.strip() for u in Path(args.urls).read_text(encoding='utf-8').splitlines() if u.strip()]
    print(f'Объектов: {len(urls)}')

    session = requests.Session()
    rows = []
    for i, url in enumerate(urls, 1):
        row = process_complex(url, session)
        if row:
            rows.append(row)
            print(f'[{i}/{len(urls)}] {row["slug"][:50]} | дедлайн {row["domtut_deadline"] or "—"} '
                  f'| последний негативный отзыв {row["last_negative_review_date"] or "—"} '
                  f'| листингов {row["listing_count"]}')
        time.sleep(DELAY)

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, 'w', newline='', encoding='utf-8-sig') as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        w.writeheader()
        w.writerows(rows)
    print(f'\nГотово: {len(rows)} объектов -> {out_path}')


if __name__ == '__main__':
    main()
