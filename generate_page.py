#!/usr/bin/env python3
"""Парсинг rutracker f=22, обогащение IMDB, генерация index-kino.html."""

import gzip
import json
import os
import re
import sys
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from html import escape, unescape

import requests
from bs4 import BeautifulSoup

from config import LISTING_CACHE_MAX_AGE_DAYS, WORKER_COUNT
from project_io import atomic_write_json, atomic_write_text
from world_sources import (
    deduplicate_world_topics,
    filter_world_top,
    is_verified_world_youtube_trailer,
    is_world_collection,
    is_world_source,
    is_world_topic,
    merge_world_duplicates,
    merge_world_by_id,
    parse_world_page,
    search_world_youtube_trailer,
    world_page_hash,
)

COLLECTIONS = {
    'nashe_kino':          {'name': 'Наше кино',                       'url': 'https://rutracker.net/forum/viewforum.php?f=22',     'age_cleanup': True,  'skip_topics': 2},
    'kino_sng':            {'name': 'Фильмы ближнего зарубежья',        'url': 'https://rutracker.net/forum/viewforum.php?f=2540', 'age_cleanup': False, 'skip_topics': 0},
    'novinki_2026':        {'name': 'Новинки 2026',                    'url': 'https://rutracker.net/forum/viewforum.php?f=252',   'age_cleanup': True,  'skip_topics': 0},
    'kino_sng_hd':         {'name': 'Фильмы Ближнего Зарубежья (HD Video)', 'url': 'https://rutracker.net/forum/viewforum.php?f=1247', 'age_cleanup': False, 'skip_topics': 0},
    'piratebay_top':       {'name': 'World *',                         'url': 'https://1.piratebays.to/top/207', 'age_cleanup': True, 'source': 'piratebay', 'max_topics': 60},
    'tpbparty_top':        {'name': 'World **',                        'url': 'https://tpb.party/top/207',       'age_cleanup': True, 'source': 'tpbparty',  'max_topics': 60},
}
FORUM_URL = COLLECTIONS['nashe_kino']['url']
TOPIC_URL_T = "https://rutracker.net/forum/viewtopic.php?t={}"
MAX_TOPICS = 30
PAGE_SIZE = 50
MAX_RETRY = 10

DATA_DIR = os.environ.get("LOCAL_KINO_DATA_DIR", "data")
RATINGS_URL = "https://datasets.imdbws.com/title.ratings.tsv.gz"
BASICS_URL = "https://datasets.imdbws.com/title.basics.tsv.gz"
RATINGS_CACHE = os.path.join(DATA_DIR, "imdb_ratings_cache.json")
BASICS_CACHE = os.path.join(DATA_DIR, "imdb_basics_cache.json")
IMDB_DATA_DIR = os.path.join(DATA_DIR, "imdb")
RATINGS_DATASET = os.path.join(IMDB_DATA_DIR, "title.ratings.tsv.gz")
BASICS_DATASET = os.path.join(IMDB_DATA_DIR, "title.basics.tsv.gz")

FORBIDDEN_GENRES = {'ужасы', 'horror', 'секс', 'sex', 'эротика', 'erotica', 'порно', 'porn'}


def is_forbidden_topic(topic):
    for g in topic.get('genre', '').split(','):
        if g.strip().lower() in FORBIDDEN_GENRES:
            return True
    title = (topic.get('movie_title', '') + ' ' + topic.get('orig_title', '')).lower()
    for kw in FORBIDDEN_GENRES:
        if kw in title:
            return True
    return False


def sanitize_topic(topic):
    topic['poster_url'] = 'data/posters/placeholder.png'
    topic['kp_rating'] = '0'
    topic['kp_votes'] = '0'
    topic['imdb_rating'] = '0'
    topic['imdb_votes'] = '0'
    topic['kp_id'] = '0'
    topic['imdb_id'] = '0'
    topic['youtube_url'] = '0'
    topic['magnet'] = '0'
    topic['_sanitized'] = True
    add_hidden_topic(topic['topic_id'])
    return topic
SEARCH_CACHE = os.path.join(DATA_DIR, "imdb_search_cache.json")
KP_SEARCH_CACHE = os.path.join(DATA_DIR, "kp_search_cache.json")
YOUTUBE_CACHE = os.path.join(DATA_DIR, "youtube_cache.json")
KP_TRAILER_CACHE = os.path.join(DATA_DIR, "kinopoisk_trailer_cache.json")
IMDB_TRAILER_CACHE = os.path.join(DATA_DIR, "imdb_trailer_cache.json")
OUTPUT_FILE = os.path.join(DATA_DIR, "index-kino.html")
TORRENTS_CACHE = os.path.join(DATA_DIR, "torrents_data.json")
HIDDEN_TOPICS_FILE = os.path.join(DATA_DIR, "hidden_topics.json")
POSTERS_DIR = os.path.join(DATA_DIR, "posters")
POSTERS_URL = "data/posters"
POSTER_PLACEHOLDER_URL = f"{POSTERS_URL}/placeholder.png"
POSTER_RETRY_DAYS = 7
TOPIC_CACHE_DIR = os.path.join(DATA_DIR, "topic_cache")
WORLD_HASH_CACHE_DIR = os.path.join(DATA_DIR, "world_hash")
WORLD_LEGACY_SOURCE_CACHE = {
    'piratebay': {
        'page_cache': os.path.join(DATA_DIR, 'piratebay_page.html'),
        'torrents_cache': os.path.join(DATA_DIR, 'torrents_data.json'),
        'hash_cache': os.path.join(DATA_DIR, 'piratebay_hash.txt'),
    },
    'tpbparty': {
        'page_cache': os.path.join(DATA_DIR, 'tpbparty_page.html'),
        'torrents_cache': os.path.join(DATA_DIR, 'torrents_data_tpbparty.json'),
        'hash_cache': os.path.join(DATA_DIR, 'tpbparty_hash.txt'),
    },
}

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36",
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
}
SESSION = requests.Session()
SESSION.headers.update(HEADERS)


def now_text():
    return datetime.now().strftime('%Y-%m-%d %H:%M')


def today_text():
    return datetime.now().date().isoformat()


def listing_cache_is_valid(path):
    if LISTING_CACHE_MAX_AGE_DAYS < 0:
        return False
    if not os.path.exists(path):
        return False
    age_seconds = time.time() - os.path.getmtime(path)
    return age_seconds <= LISTING_CACHE_MAX_AGE_DAYS * 86400


def world_hash_cache_path(collection):
    return os.path.join(WORLD_HASH_CACHE_DIR, f"{collection}.txt")


def load_world_page_hash(collection):
    path = world_hash_cache_path(collection)
    try:
        with open(path, encoding="utf-8") as f:
            return f.read().strip()
    except FileNotFoundError:
        return ""


def save_world_page_hash(collection, html):
    os.makedirs(WORLD_HASH_CACHE_DIR, exist_ok=True)
    path = world_hash_cache_path(collection)
    with open(path, "w", encoding="utf-8") as f:
        f.write(world_page_hash(html))


def world_legacy_cache_cfg(source):
    return WORLD_LEGACY_SOURCE_CACHE.get(str(source or '').strip().lower(), {})


def world_legacy_page_cache_path(source):
    return world_legacy_cache_cfg(source).get('page_cache', '')


def world_legacy_hash_cache_path(source):
    return world_legacy_cache_cfg(source).get('hash_cache', '')


def world_legacy_torrents_cache_path(source):
    return world_legacy_cache_cfg(source).get('torrents_cache', '')


def save_world_legacy_page_cache(source, raw, html):
    page_path = world_legacy_page_cache_path(source)
    hash_path = world_legacy_hash_cache_path(source)
    if page_path:
        os.makedirs(os.path.dirname(page_path), exist_ok=True)
        with open(page_path, 'wb') as f:
            f.write(raw)
    if hash_path:
        os.makedirs(os.path.dirname(hash_path), exist_ok=True)
        with open(hash_path, 'w', encoding='utf-8') as f:
            f.write(world_page_hash(html))


def load_world_listing_cache_bytes(collection, source):
    candidates = [os.path.join(TOPIC_CACHE_DIR, f'{collection}_p0.html')]
    legacy_page = world_legacy_page_cache_path(source)
    if legacy_page and legacy_page not in candidates:
        candidates.append(legacy_page)
    for path in candidates:
        if not path or not listing_cache_is_valid(path) or not os.path.exists(path):
            continue
        with open(path, 'rb') as f:
            raw = f.read()
        return path, raw
    return '', b''


def normalize_legacy_world_topic(topic, collection, source):
    topic = dict(topic or {})
    title = topic.get('title') or topic.get('name') or ''
    movie_title = topic.get('movie_title') or ''
    movie_year = str(topic.get('movie_year') or '')
    magnet = topic.get('magnet') or ''
    source = str(source or topic.get('source') or '').strip().lower()
    prefix = 'pb' if source == 'piratebay' else 'tpb'
    topic_id = topic.get('topic_id') or world_topic_id(prefix, magnet, info_hash_from_magnet)
    if not topic_id:
        return None
    size_str = topic.get('size_str') or topic.get('size') or ''
    size_bytes = topic.get('size_bytes')
    if size_bytes in (None, ''):
        size_bytes, size_str = parse_size(size_str)
    topic_url = topic.get('topic_url') or topic.get('detail_url') or ''
    if topic_url and not topic_url.startswith('http'):
        base = 'https://1.piratebays.to' if source == 'piratebay' else 'https://tpb.party'
        topic_url = urllib.parse.urljoin(base, topic_url)
    normalized = {
        'topic_id': topic_id,
        'title': title or movie_title,
        'movie_title': movie_title or clean_title(title)[0],
        'orig_title': topic.get('orig_title', '') or '',
        'movie_year': movie_year or clean_title(title)[1],
        'genre': topic.get('genre') or '',
        'quality': topic.get('quality') or '',
        'collection': collection,
        'source': source,
        'source_category': topic.get('source_category') or topic.get('category') or '',
        'author': topic.get('author') or topic.get('uploader') or '',
        'size_str': size_str,
        'size_bytes': size_bytes or 0,
        'seeders': int(topic.get('seeders') or 0),
        'leechers': int(topic.get('leechers') or 0),
        'date_str': topic.get('date_str') or parse_world_date(topic.get('uploaded') or ''),
        'added_at': topic.get('added_at') or now_text(),
        'topic_url': topic_url,
        'listing_order': topic.get('listing_order', 999999),
        'magnet': magnet,
        'imdb_id': topic.get('imdb_id'),
        'imdb_rating': topic.get('imdb_rating'),
        'imdb_votes': topic.get('imdb_votes'),
        'kp_id': topic.get('kp_id'),
        'kp_rating': topic.get('kp_rating'),
        'kp_votes': topic.get('kp_votes'),
        'poster_url': normalize_poster_url(topic.get('poster_url', '')),
        'cast': topic.get('cast', '') or '',
        'youtube_url': topic.get('youtube_url'),
        'format': topic.get('format') or detect_format_from_text(title),
    }
    return ensure_topic_defaults(normalized)


def load_legacy_world_torrents(collection, source):
    cache_path = world_legacy_torrents_cache_path(source)
    if not cache_path or not os.path.exists(cache_path):
        return []
    data = load_json(cache_path) or []
    if not isinstance(data, list):
        return []
    topics = []
    for item in data:
        normalized = normalize_legacy_world_topic(item, collection, source)
        if normalized:
            topics.append(normalized)
    topics.sort(key=lambda t: t.get('listing_order', 999999))
    return deduplicate_world_topics(topics)


def bootstrap_missing_world_collections(topics):
    topics = list(topics or [])
    have = {str(t.get('collection') or '') for t in topics}
    bootstrapped = []
    for collection, coll_info in COLLECTIONS.items():
        source = coll_info.get('source', 'rutracker')
        if not is_world_source(source) or collection in have:
            continue
        legacy_topics = load_legacy_world_torrents(collection, source)
        if legacy_topics:
            bootstrapped.extend(legacy_topics)
    if bootstrapped:
        topics.extend(bootstrapped)
        topics = clean_catalog_topics(topics)
    return topics, bootstrapped


def date_to_timestamp(value):
    if not value:
        return 0
    value = str(value).strip()
    for fmt in ('%Y-%m-%d %H:%M', '%Y-%m-%d'):
        try:
            return int(datetime.strptime(value, fmt).timestamp())
        except ValueError:
            pass
    return 0


GENRE_TRANSLATION = {
    'Action': 'боевик', 'Adventure': 'приключения', 'Animation': 'мультфильм',
    'Biography': 'биография', 'Comedy': 'комедия', 'Crime': 'криминал',
    'Documentary': 'документальный', 'Drama': 'драма', 'Family': 'семейный',
    'Fantasy': 'фэнтези', 'Film-Noir': 'нуар', 'History': 'история',
    'Horror': 'ужасы', 'Music': 'музыка', 'Musical': 'мюзикл',
    'Mystery': 'детектив', 'Romance': 'мелодрама', 'Sci-Fi': 'фантастика',
    'Short': 'короткометражка', 'Sport': 'спорт', 'Thriller': 'триллер',
    'War': 'военный', 'Western': 'вестник',
    'военный фильм': 'военный', 'военная драма': 'военный, драма',
    'кинофантазия': 'фантастика',
}
GENRE_STOP = {'Betacam SP', 'DVDRemux', 'DVD', 'DVB', 'SATRip', 'TVRip', 'HDRip',
              'WEB-DL', 'WEBRip', 'BDRip', 'AVI', 'MKV', 'MP4'}
COUNTRY_STOP = {'Россия', 'Украина', 'США', 'Великобритания', 'Франция', 'Германия',
                'Ирландия', 'Испания', 'Канада', 'ОАЭ', 'Казахстан', 'Кыргызстан',
                'Беларусь', 'Эстония', 'Грузия', 'Латвия', 'Литва', 'Армения',
                'Азербайджан', 'Узбекистан', 'Таджикистан', 'Молдова', 'Польша',
                'Италия', 'Швеция', 'Норвегия', 'Дания', 'Нидерланды', 'Бельгия',
                'Австралия', 'Новая Зеландия', 'Китай', 'Япония', 'Корея',
                'Индия', 'Турция', 'Мексика', 'Бразилия', 'Аргентина'}
HIDDEN_GENRES = {'ужасы', 'эротика', 'порно', 'для взрослых', 'взрослый',
                 'horror', 'erotica', 'porn', 'adult'}


def is_listing_category_genre(genre):
    genre = (genre or '').strip().lower()
    return genre.startswith('video >') or genre in {'video', 'hd - movies', 'movies'}


def fetch(url, timeout=30):
    r = SESSION.get(url, timeout=timeout)
    r.raise_for_status()
    r.encoding = 'windows-1251'
    return r.text


def get_topic_html(topic_id, topic_url, timeout=10):
    os.makedirs(TOPIC_CACHE_DIR, exist_ok=True)
    cache_path = os.path.join(TOPIC_CACHE_DIR, f'{topic_id}.html')
    if os.path.exists(cache_path):
        with open(cache_path, 'rb') as f:
            raw = f.read()
    else:
        last_e = None
        for attempt in range(1, MAX_RETRY + 1):
            try:
                r = SESSION.get(topic_url, timeout=timeout)
                r.raise_for_status()
                raw = r.content
                with open(cache_path, 'wb') as f:
                    f.write(raw)
                break
            except Exception as e:
                last_e = e
                if attempt < MAX_RETRY:
                    print(f"  [retry {attempt}/{MAX_RETRY}] {e}")
                    time.sleep(2)
                    continue
                return None

    # determine encoding: prefer charset from meta, fallback utf8→cp1251
    enc = 'cp1251'
    m = re.search(rb'charset[\s"\'=]+([\w-]+)', raw[:4096], re.I)
    if m:
        candidate = m.group(1).decode('ascii', errors='replace')
        if candidate.lower() in ('utf-8', 'utf8'):
            enc = 'utf-8'
    for e in (enc, 'utf-8' if enc == 'cp1251' else 'cp1251'):
        try:
            return raw.decode(e)
        except (UnicodeDecodeError, UnicodeError):
            continue
    return raw.decode('cp1251', errors='replace')


def fetch_page(url):
    try:
        return fetch(url)
    except Exception as e:
        print(f"ошибка: {e}", end='')
        return None


def load_json(path):
    try:
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def save_json(path, data):
    atomic_write_json(path, data)


def parse_size(text):
    text = text.strip().replace('\xa0', ' ').replace(',', '.')
    m = re.match(r'([\d.]+)\s*(TB|GB|MB|KB|TIB|GIB|MIB|KIB)', text, re.I)
    if not m:
        return 0, text
    val = float(m.group(1))
    unit = m.group(2).upper()
    multipliers = {
        'KB': 1024, 'KIB': 1024,
        'MB': 1024**2, 'MIB': 1024**2,
        'GB': 1024**3, 'GIB': 1024**3,
        'TB': 1024**4, 'TIB': 1024**4,
    }
    return int(val * multipliers.get(unit, 1)), text


def parse_rutracker_title(raw):
    t = raw.strip()
    suffix_parts = re.split(r'\s+(?<!\w)(Original|Rus|Sub|Line|MEGA|PRoFX|HDTV)(?!\w)\s*', t, maxsplit=1, flags=re.I)
    t = suffix_parts[0].strip()

    year = ''
    genre = ''
    quality = ''
    quality_keywords = re.compile(
        r'^(WEB|BluRay|HDTV|DVDRip|HDRip|WEB-DL|WEBRip|BDRip|DVD|SATRip|TVRip|CamRip|'
        r'TS|TC|Screener|WEB-DLRip|BDRip-AVC|DVDRip-AVC|HDTVRip|'
        r'Betacam\s*SP|DVDRemux|DVDRip-AVC|BDRemux|WEBRip-AVC|'
        r'AVI|MKV|MP4|MPEG|TS|M2TS|BluRay\s*Remux|WEB\s*Rip|'
        r'BDRip-AVC|BDRemux)', re.I)
    bracket_m = re.search(r'\[([^\]]*\d{4}[^\]]*)\]', t)
    if bracket_m:
        meta_text = bracket_m.group(1)
        parts = [p.strip() for p in meta_text.split(',')]
        genre_parts = []
        quality_parts = []
        for p in parts:
            p = p.strip()
            if re.match(r'^(19\d{2}|20\d{2})$', p):
                year = p
            elif quality_keywords.match(p):
                quality_parts.append(p)
            elif p.lower() in {s.lower() for s in GENRE_STOP | COUNTRY_STOP}:
                continue
            else:
                # Check if this part contains a year like 2022 (2021)
                ym = re.search(r'\b(19\d{2}|20\d{2})\b', p)
                if ym and not year:
                    year = ym.group(1)
                elif not re.match(r'^\(?\d{4}\)?$', p):
                    genre_parts.append(p)
        genre = ', '.join(genre_parts)
        quality = ' / '.join(quality_parts)
        title_part = t[:bracket_m.start()].strip()
        title_part = re.sub(r'\s*\[[^\]]*\]', '', title_part).strip()
    else:
        # Truncated title without closing bracket — try to extract anyway
        title_part = t
        bracket_open = t.find('[')
        if bracket_open >= 0:
            title_part = t[:bracket_open].strip()
            content = t[bracket_open+1:].rstrip('.] ')
            parts = [x.strip() for x in content.split(',')]
            genre_parts = []
            quality_parts = []
            for p in parts:
                p = p.strip().rstrip('.')
                if re.match(r'^(19\d{2}|20\d{2})$', p):
                    year = p
                elif quality_keywords.match(p):
                    quality_parts.append(p)
                elif p.lower() not in {s.lower() for s in GENRE_STOP | COUNTRY_STOP}:
                    if not re.match(r'^\.{2,}$', p) and p:
                        ym = re.search(r'\b(19\d{2}|20\d{2})\b', p)
                        if ym and not year:
                            year = ym.group(1)
                        elif not re.match(r'^\(?\d{4}\)?$', p):
                            genre_parts.append(p)
            if genre_parts:
                genre = ', '.join(genre_parts)
            if quality_parts:
                quality = ' / '.join(quality_parts)
            if not year:
                ym = re.search(r'\b(19\d{2}|20\d{2})\b', t)
                if ym:
                    year = ym.group(1)

    title_part = re.sub(r'\([^)]*\)', '', title_part).strip()

    russian_title = title_part
    english_title = ''
    if '/' in title_part:
        parts = [p.strip() for p in title_part.split('/')]
        russian_title = parts[0]
        for p in parts[1:]:
            if re.search(r'[a-zA-Z]', p) and len(p) > 2:
                english_title = p
                break

    title_part = re.sub(r'\s+', ' ', title_part).strip()
    movie_title = (russian_title or title_part).strip()
    orig_title = english_title
    return movie_title, orig_title, year, genre, quality


def clean_title(raw):
    t = raw.strip()
    t = re.sub(r'^tt\d+\s*', '', t)
    t = re.sub(r'\s*\[.*?\]', '', t)
    t = re.sub(r'[._]', ' ', t)
    year_m = re.search(r'\((\d{4})\)', t) or re.search(r'\b(19\d{2}|20\d{2})\b', t)
    year = year_m.group(1) if year_m else ''
    t = re.sub(r'\s*\(\d{4}\)\s*', ' ', t)
    t = re.sub(r'\(.*?\)', ' ', t)
    t = re.sub(r'\[.*?\]', ' ', t)
    t = re.sub(r'(?i)\b(1080p|720p|2160p|480p|WEBRip|WEB-DL|WEB|BluRay|BRRip|HDRip|DVDRip|DCPRip|'
               r'x264|x265|h264|h265|HEVC|AVC|AAC\d*|AC3\d*|DDP\d*|DTS|MP4|MKV|AVI|'
               r'10bit|8bit|5\s*[. ]\s*1|2\s*[. ]\s*0|6CH|'
               r'REPACK|PROPER|READNFO|iNTERNAL|EXTENDED|UNRATED|DC|FINAL|COMPLETE|'
               r'YTS|RARBG|RMTeam|NeoNoir|SupaCvnt|FLUX|BTM|'
               r'WEBRip|WEB\s*[.-]\s*DL|WEB\s*Line|AMZN|DSNP|NF|MA|PMNTP|PLAY|Early\s*Release|'
               r'CAM|TELESYNC|HDTS|TS|TC|SCREENER'
               r')\b', ' ', t, flags=re.I)
    t = re.sub(r'\s+', ' ', t).strip()
    t = re.sub(r'\s*-\s*\w+$', '', t)
    t = re.sub(r'(?i)\b(LEAK|PLAY|DUAL|LINKS|SCREENER|TS|CAM|HDRip)\b', '', t)
    t = re.sub(r'\s+', ' ', t).strip()
    return t, year


def parse_piratebay_date(raw):
    raw = (raw or '').strip()
    now = datetime.now(timezone.utc)
    m = re.match(r'^(\d{4})-(\d{2})-(\d{2})\s+(\d{1,2}):(\d{2})', raw)
    if m:
        try:
            return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)),
                            int(m.group(4)), int(m.group(5)), tzinfo=timezone.utc).strftime('%Y-%m-%d %H:%M')
        except ValueError:
            return now_text()
    m = re.match(r'^(\d{2})-(\d{2})\s+(\d{1,2}):(\d{2})', raw)
    if m:
        try:
            return datetime(now.year, int(m.group(1)), int(m.group(2)),
                            int(m.group(3)), int(m.group(4)), tzinfo=timezone.utc).strftime('%Y-%m-%d %H:%M')
        except ValueError:
            return now_text()
    m = re.match(r'^(?:Today|Сегодня)\s+(\d{1,2}):(\d{2})', raw)
    if m:
        return now.replace(hour=int(m.group(1)), minute=int(m.group(2)), second=0, microsecond=0).strftime('%Y-%m-%d %H:%M')
    m = re.match(r'^(?:Y[- ]?day|Yesterday|Вчера)\s+(\d{1,2}):(\d{2})', raw)
    if m:
        return (now - timedelta(days=1)).replace(hour=int(m.group(1)), minute=int(m.group(2)), second=0, microsecond=0).strftime('%Y-%m-%d %H:%M')
    for unit, delta_arg in (('min|mins|minute|minutes', 'minutes'), ('hour|hours', 'hours'), ('day|days', 'days'), ('week|weeks', 'weeks')):
        m = re.match(rf'(\d+)\s+({unit})', raw, re.I)
        if m:
            return (now - timedelta(**{delta_arg: int(m.group(1))})).strftime('%Y-%m-%d %H:%M')
    return now_text()


def info_hash_from_magnet(magnet):
    m = re.search(r'btih:([A-Fa-f0-9]{40})', magnet or '')
    return m.group(1).lower() if m else ''


def detect_format_from_text(text):
    text = (text or '').lower()
    if 'mkv' in text or 'matroska' in text:
        return 'MKV'
    if 'mp4' in text or 'mpeg-4' in text:
        return 'MP4'
    if 'avi' in text or 'xvid' in text or 'divx' in text:
        return 'AVI'
    if 'webm' in text:
        return 'WEBM'
    if re.search(r'\bmpeg\b', text):
        return 'MPEG'
    return ''


def parse_int_text(value):
    digits = re.sub(r'[^\d]', '', str(value or ''))
    return int(digits) if digits else 0


def clean_title_deep(raw):
    t = raw.strip()
    t = re.sub(r'^[a-fA-F0-9]{32,40}\s+', '', t)
    t = re.sub(r'^tt\d+\s*', '', t)
    t = re.sub(r'\[.*?\]', '', t)
    t = re.sub(r'\(.*?\)', '', t)
    t = re.sub(r'(?i)\.(mp4|mkv|avi|m4v|webm|ts|m2ts)', ' ', t)
    t = re.sub(r'[._]', ' ', t)
    t = re.sub(r'\s*-\s*\S+$', '', t)
    t = re.sub(r'(?i)\b(1080p|720p|2160p|480p|WEBRip|WEB-DL|WEB|BluRay|BRRip|HDRip|DVDRip|'
               r'DCPRip|HDTS|HDRip|CAM|TS|TC|TELESYNC|'
               r'x264|x265|h264|h265|HEVC|AVC|AAC\d*|AC3\d*|DDP\d*|DTS|MP4|MKV|AVI|'
               r'10bit|8bit|5[.\s]+1|2[.\s]+0|6CH|7CH|'
               r'REPACK|PROPER|READNFO|iNTERNAL|EXTENDED|UNRATED|DC|FINAL|COMPLETE|'
               r'YTS|RARBG|RMTeam|NeoNoir|SupaCvnt|FLUX|BTM|'
               r'WEBRip|WEB[.\s-]*DL|WEB\s*Line|AMZN|DSNP|NF|MA|PMNTP|PLAY|'
               r'Early\s*Release|VOSTFR|MULTi|DUAL|Line\s*Audio|'
               r'LEAK|PLAY|LINKS|SCREENER|HDRip|'
               r'BONE|SCOPE|UNiON|FS|BYNDR|Rapta|SyncUP|Asiimov|GalaxyRG|'
               r'HDTS|TELESYNC|VOSTFR|MULTi|DUAL|'
               r'10bits|YTS|YIFY|RARBG|RMTeam|NeoNoir)'
               r'\s*', ' ', t, flags=re.I)
    t = re.sub(r'[⭐★☆\-—─╌●•·]', ' ', t)
    year_m = re.search(r'\b(19\d{2}|20\d{2})\b', t)
    year = year_m.group(1) if year_m else ''
    if year:
        idx = t.find(year)
        before = t[:idx].strip()
        words = before.split()
        before = ' '.join(words)
        t = f'{before} {year}'
    else:
        words = [w for w in t.split() if len(w) > 1]
        t = ' '.join(words[:4])
    t = re.sub(r'\s+', ' ', t).strip()
    return t, year


def search_imdb(title, year):
    first_char = title[0].lower()
    url = f"https://v2.sg.media-imdb.com/suggestion/{first_char}/{urllib.parse.quote(title)}.json"
    try:
        r = SESSION.get(url, timeout=8)
        data = r.json()
        items = [it for it in data.get('d', []) if it.get('id', '').startswith('tt')]
        if not items:
            return None
        if year:
            items = [it for it in items if str(it.get('y')) == str(year)]
        if not items:
            return None

        def poster_url(it):
            img_data = it.get('i')
            if img_data:
                if isinstance(img_data, dict):
                    return img_data.get('imageUrl', '')
                if isinstance(img_data, list) and len(img_data) > 0:
                    return img_data[0].get('imageUrl', '')
            return ''

        items.sort(key=lambda it: (1 if poster_url(it) else 0), reverse=True)
        best = items[0]
        return {'id': best['id'], 'poster': poster_url(best), 'cast': best.get('s', '')}
    except Exception:
        return None


def search_imdb_deep(raw_name):
    title, year = clean_title_deep(raw_name)
    queries = [title]
    if year:
        queries.append(title.replace(f' {year}', '').strip())
    words = title.split()
    if len(words) > 4:
        queries.append(' '.join(words[:3]))
        if year:
            queries.append(f'{" ".join(words[:3])} {year}')
    if len(words) > 5:
        queries.append(' '.join(words[:2]))

    for q in queries:
        if not q:
            continue
        first_char = q[0].lower()
        url = f"https://v2.sg.media-imdb.com/suggestion/{first_char}/{urllib.parse.quote(q)}.json"
        try:
            r = SESSION.get(url, timeout=8)
            data = r.json()
            items = [it for it in data.get('d', []) if it.get('id', '').startswith('tt')]
            if not items:
                continue
            if year:
                items = [it for it in items if str(it.get('y')) == str(year)]
            if not items:
                continue

            def poster_url(it):
                img_data = it.get('i')
                if img_data:
                    if isinstance(img_data, dict):
                        return img_data.get('imageUrl', '')
                    if isinstance(img_data, list) and len(img_data) > 0:
                        return img_data[0].get('imageUrl', '')
                return ''

            items.sort(key=lambda it: (1 if poster_url(it) else 0), reverse=True)
            return _parse_imdb_result(items[0])
        except Exception:
            continue
    return None


def _parse_imdb_result(item):
    img_data = item.get('i')
    img = ''
    if img_data:
        if isinstance(img_data, dict):
            img = img_data.get('imageUrl', '')
        elif isinstance(img_data, list) and len(img_data) > 0:
            img = img_data[0].get('imageUrl', '')
    return {'id': item['id'], 'poster': img, 'cast': item.get('s', '')}


def search_imdb_ids(topics):
    total = len(topics)
    imdb_cache = load_json(SEARCH_CACHE) or {}
    cache_lock = threading.Lock()
    processed = 0
    processed_lock = threading.Lock()

    def _search_one(t):
        nonlocal processed
        if t.get('imdb_id'):
            return
        title = t.get('orig_title') or t['movie_title']
        year = t['movie_year']
        raw_name = t['title']
        if not title:
            return
        with processed_lock:
            processed += 1
            idx = processed

        cache_key = f"{title}|{year}".lower()
        with cache_lock:
            result = imdb_cache.get(cache_key) if cache_key in imdb_cache else None
        if result is not None:
            pass
        else:
            print(f"  [{idx}/{total}] {title}...", end=' ', flush=True)
            result = search_imdb(title, year)
            if result is None:
                deep_cache_key = f"deep:{raw_name.lower().strip()}"
                with cache_lock:
                    cached_deep = imdb_cache.get(deep_cache_key) if deep_cache_key in imdb_cache else None
                if cached_deep is not None:
                    result = cached_deep
                else:
                    result = search_imdb_deep(raw_name)
                    with cache_lock:
                        if result is not None:
                            imdb_cache[deep_cache_key] = result
                        save_json(SEARCH_CACHE, imdb_cache)
            with cache_lock:
                if result is not None:
                    imdb_cache[cache_key] = result
                    save_json(SEARCH_CACHE, imdb_cache)
        if result:
            if isinstance(result, str):
                imdb_id = result
                poster, cast = '', ''
            else:
                imdb_id = result.get('id')
                poster = result.get('poster', '')
                cast = result.get('cast', '')
            t['imdb_id'] = imdb_id
            if not has_real_poster(t) and poster:
                local_url = download_poster(imdb_id, poster)
                if local_url:
                    t['poster_url'] = local_url
            t['cast'] = cast
            print(f"ID {imdb_id}", end='')
        else:
            print("не найдено", end='')
        print()

    with ThreadPoolExecutor(max_workers=min(WORKER_COUNT, total or 1)) as executor:
        executor.map(_search_one, topics)
    return topics


def fetch_imdb_rating(imdb_id):
    if not imdb_id:
        return {}
    url = f"https://www.imdb.com/title/{imdb_id}/"
    try:
        r = SESSION.get(url, timeout=10, headers={
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Accept-Language': 'en-US,en;q=0.9',
        })
        if r.status_code != 200:
            return {}
        html = r.text
        result = {}
        m = re.search(r'"ratingValue"\s*:\s*"([\d.]+)"', html)
        if m:
            result['rating'] = m.group(1)
        m = re.search(r'"ratingCount"\s*:\s*(\d+)', html)
        if m:
            result['votes'] = m.group(1)
        genres = re.findall(r'"genre"\s*:\s*"([^"]+)"', html)
        if genres:
            result['genres'] = ','.join(genres)
        m = re.search(r'<meta\s+property="og:image"\s+content="([^"]+)"', html)
        if m:
            result['poster'] = m.group(1)
        return result
    except Exception:
        return {}


_kp_session_init = False


_KP_TECH_WORDS = {
    '1080p','720p','2160p','4k','hd','web','webrip','web-dl','bluray','bdrip',
    'hdr','dts','ac3','aac','ddp','ddp5','ddp7','x264','x265','hevc','h264',
    'h265','10bit','8bit','bone','repack','proper','internal','readnfo',
    'vf1','vf2','vf3','vostfr','multi','subs','sub','eng','rus',
}


def _clean_kp_search_title(title):
    title = re.sub(r'\b\d{4}\b', '', title)
    pattern = r'\b(?:' + '|'.join(_KP_TECH_WORDS) + r')\b'
    title = re.sub(pattern, '', title, flags=re.IGNORECASE)
    title = re.sub(r'[\s,;:.!?\-]+', ' ', title).strip()
    return title


def _kp_verify_result(title, year, rating_html):
    """Verify that the KP film page matches the search query."""
    og_m = re.search(r'<meta[^>]+property="og:title"[^>]+content="([^"]+)"', rating_html)
    if not og_m:
        return True
    og_title = og_m.group(1)
    if year:
        y_m = re.search(r'\((\d{4})\)', og_title)
        if y_m and y_m.group(1) != year:
            return False
    title_lower = title.lower().strip()
    if title_lower and title_lower not in og_title.lower():
        words = [w for w in re.split(r'[\s,;:.!?-]+', title_lower) if len(w) > 2]
        if words and not any(w in og_title.lower() for w in words):
            return False
    return True


def search_kinopoisk(title, year):
    global _kp_session_init
    if not title:
        return None
    if not _kp_session_init:
        try:
            SESSION.get('https://www.kinopoisk.ru/', timeout=10)
        except Exception:
            pass
        _kp_session_init = True

    clean_title = _clean_kp_search_title(title)
    query = f"{clean_title} {year}" if year else clean_title
    search_url = f"https://www.kinopoisk.ru/index.php?kp_query={urllib.parse.quote(query)}"
    try:
        r = SESSION.get(search_url, timeout=10)
        if len(r.text) < 5000:
            SESSION.get('https://www.kinopoisk.ru/', timeout=10)
            r = SESSION.get(search_url, timeout=10)
        soup = BeautifulSoup(r.text, 'html.parser')
        result_link = soup.select_one('a[href*="/film/"]')
        if not result_link:
            return None
        href = result_link['href']
        m = re.search(r'/film/(\d+)/', href)
        if not m:
            return None
        kp_id = m.group(1)

        page_url = f"https://www.kinopoisk.ru/film/{kp_id}/"
        r2 = SESSION.get(page_url, timeout=10)
        rating_html = r2.text

        if not _kp_verify_result(clean_title, year, rating_html):
            return None

        rating = ''
        votes = ''
        rm = re.search(r'ratingValue["\']?\s*:\s*["\']?([\d.]+)', rating_html)
        if rm:
            rating = rm.group(1)
        vm = re.search(r'ratingCount["\']?\s*:\s*["\']?(\d[\d\s]*)', rating_html)
        if vm:
            votes = re.sub(r'[\s,]', '', vm.group(1))
        if not rating:
            rm = re.search(r'<span[^>]*class="[^"]*rating[^"]*"[^>]*>([\d.]+)</span>', rating_html)
            if rm:
                rating = rm.group(1)
        return {'kp_id': kp_id, 'kp_rating': rating, 'kp_votes': votes}
    except Exception:
        return None


def _extract_year_from_title(topic):
    raw = topic.get('title', '') or topic.get('movie_title', '')
    if not raw:
        return ''
    m = re.search(r'\b(19\d{2}|20\d{2})\b', raw)
    return m.group(1) if m else ''


def search_kinopoisk_ids(topics):
    total = len(topics)
    kp_cache = load_json(KP_SEARCH_CACHE) or {}
    cache_lock = threading.Lock()
    counter = 0
    counter_lock = threading.Lock()

    def _search_one(t):
        nonlocal counter
        title, year = t.get('movie_title'), t.get('movie_year')
        if not title:
            return
        if not year:
            year = _extract_year_from_title(t)
        cache_key = f"{title}|{year}".lower()
        with counter_lock:
            counter += 1
            idx = counter
        with cache_lock:
            result = kp_cache.get(cache_key) if cache_key in kp_cache else None
        if result is None:
            print(f"  [{idx}/{total}] {title}...", end=' ', flush=True)
            result = search_kinopoisk(title, year)
            with cache_lock:
                kp_cache[cache_key] = result
                if result:
                    save_json(KP_SEARCH_CACHE, kp_cache)
        if result:
            t['kp_id'] = result['kp_id']
            t['kp_rating'] = result['kp_rating']
            t['kp_votes'] = result['kp_votes']
            print(f"КП {result['kp_rating'] or '—'}", end='')
        else:
            print("не найдено", end='')
        print()

    with ThreadPoolExecutor(max_workers=min(WORKER_COUNT, total or 1)) as executor:
        executor.map(_search_one, topics)
    return topics


TRAILER_POSITIVE = ('trailer', 'трейлер', 'official', 'официальный')
TRAILER_NEGATIVE = (
    'review', 'reaction', 'explained', 'ending', 'soundtrack', 'clip', 'scene',
    'обзор', 'реакция', 'разбор', 'саундтрек', 'песня', 'клип', 'сцена',
    'прохождение', 'gameplay', 'game trailer',
    'сери', 'серия', 'сезон', 'выпуск', 'эпизод', 'episode',
)


def _yt_json_text(value):
    if not value:
        return ''
    try:
        return unescape(json.loads(f'"{value}"'))
    except Exception:
        return unescape(value)


def _title_tokens(value):
    value = (value or '').lower()
    return {
        token for token in re.findall(r'[a-zа-яё0-9]+', value, flags=re.I)
        if len(token) >= 3
    }


def _youtube_candidates(html):
    candidates = []
    seen = set()
    for match in re.finditer(r'"videoId":"([a-zA-Z0-9_-]{11})"', html):
        video_id = match.group(1)
        if video_id in seen:
            continue
        seen.add(video_id)
        chunk = html[match.start():match.start() + 3000]
        title = ''
        title_match = re.search(r'"title":\{"runs":\[\{"text":"(.*?)"', chunk)
        if not title_match:
            title_match = re.search(r'"title":\{"simpleText":"(.*?)"', chunk)
        if title_match:
            title = _yt_json_text(title_match.group(1))
        channel = ''
        channel_match = re.search(r'"ownerText":\{"runs":\[\{"text":"(.*?)"', chunk)
        if channel_match:
            channel = _yt_json_text(channel_match.group(1))
        length = ''
        length_match = re.search(r'"lengthText":\{"[^}]*"simpleText":"(.*?)"', chunk)
        if length_match:
            length = _yt_json_text(length_match.group(1))
        if title:
            candidates.append({
                'video_id': video_id,
                'title': title,
                'channel': channel,
                'length': length,
            })
        if len(candidates) >= 10:
            break
    return candidates


def _duration_seconds(value):
    parts = [int(p) for p in re.findall(r'\d+', value or '')]
    if not parts:
        return 0
    if len(parts) == 1:
        return parts[0]
    total = 0
    for part in parts:
        total = total * 60 + part
    return total


def _transliterate(text):
    """Convert Cyrillic characters to Latin approximants."""
    table = {
        'а': 'a', 'б': 'b', 'в': 'v', 'г': 'g', 'д': 'd',
        'е': 'e', 'ё': 'yo', 'ж': 'zh', 'з': 'z', 'и': 'i',
        'й': 'y', 'к': 'k', 'л': 'l', 'м': 'm', 'н': 'n',
        'о': 'o', 'п': 'p', 'р': 'r', 'с': 's', 'т': 't',
        'у': 'u', 'ф': 'f', 'х': 'kh', 'ц': 'ts', 'ч': 'ch',
        'ш': 'sh', 'щ': 'shch', 'ъ': '', 'ы': 'y', 'ь': '',
        'э': 'e', 'ю': 'yu', 'я': 'ya',
    }
    result = []
    for ch in (text or '').lower():
        result.append(table.get(ch, ch))
    return ''.join(result)


def _score_youtube_candidate(candidate, title, year):
    cand_title = (candidate.get('title') or '').lower()
    cand_channel = (candidate.get('channel') or '').lower()
    wanted = (title or '').lower().strip()
    wanted_tokens = _title_tokens(wanted)
    cand_tokens = _title_tokens(cand_title)
    if not wanted_tokens or all(token.isdigit() for token in wanted_tokens):
        return 0

    # transliteration — match Cyrillic titles against Latin YouTube titles
    wanted_translit = _transliterate(wanted)
    wanted_translit_tokens = _title_tokens(wanted_translit)

    old_overlap = len(wanted_tokens & cand_tokens) / max(len(wanted_tokens), 1)
    # boost overlap via transliteration: check if any translit token prefixes a cand token
    translit_boost = 0
    for tt in wanted_translit_tokens:
        if len(tt) >= 3:
            for ct in cand_tokens:
                if ct.startswith(tt) or tt.startswith(ct):
                    translit_boost = max(translit_boost, 1)
                    break
        if translit_boost:
            break
    old_overlap_boosted = max(old_overlap, translit_boost * 0.5)
    unified_tokens = wanted_tokens | wanted_translit_tokens
    jaccard = len(unified_tokens & cand_tokens) / max(len(unified_tokens | cand_tokens), 1)
    blended_overlap = 0.7 * old_overlap_boosted + 0.3 * jaccard
    score = 0
    if wanted and (wanted in cand_title or (wanted_translit and wanted_translit in cand_title)):
        token_count = len(wanted_tokens)
        factor = min(token_count, 4) / 4 if token_count > 0 else 1.0
        score += int(45 * max(factor, 0.6))
    score += int(blended_overlap * 40)
    if any(word in cand_title for word in ('trailer', 'трейлер')):
        score += 25
    if any(word in cand_title for word in ('official', 'официальный')):
        score += 10
    title_years = set(re.findall(r'\b(19\d{2}|20\d{2})\b', cand_title))
    if year and str(year) in title_years:
        score += 15
    if year and title_years and str(year) not in title_years:
        score -= 70
    if year and not title_years:
        score -= 30
    if any(name in cand_channel for name in (
        'кинопоиск', 'kinopoisk', 'movieclips', 'amazon mgm', 'warner bros',
        'universal pictures', 'paramount pictures', 'sony pictures',
        'netflix', 'disney', '20th century studios',
    )):
        score += 18
    if any(name in cand_channel for name in ('rapid trailer', 'kinocheck')):
        score -= 5
    if any(name in cand_channel for name in ('wink', 'more.tv', 'ivi', 'start', 'ctc', 'ctc love')):
        score -= 50
    if any(word in cand_title for word in TRAILER_NEGATIVE):
        score -= 60
    duration = _duration_seconds(candidate.get('length') or '')
    if duration and duration > 8 * 60:
        score -= 15
    if old_overlap < 0.6 and wanted not in cand_title and (not wanted_translit or wanted_translit not in cand_title):
        score -= 40
    if not any(word in cand_title for word in TRAILER_POSITIVE[:2]):
        score -= 30
    title_proportion = min(len(wanted) / max(len(cand_title), 1), 1.0) if cand_title else 0
    if title_proportion > 0.4:
        score += int(title_proportion * 50)
    return score


def search_youtube_trailer_verified(title, year):
    queries = [
        f'{title} {year} official trailer',
        f'{title} {year} трейлер',
        f'"{title}" {year} трейлер фильм',
        f'{title} {year} trailer',
        f'{title} official trailer',
        f'{title} трейлер',
    ]
    best = None
    for query in queries:
        url = f"https://www.youtube.com/results?search_query={urllib.parse.quote(query)}"
        try:
            r = SESSION.get(url, timeout=10)
        except Exception:
            continue
        for candidate in _youtube_candidates(r.text):
            score = _score_youtube_candidate(candidate, title, year)
            if not best or score > best['score']:
                best = {**candidate, 'score': score}
        time.sleep(0.1)
    if best and best['score'] >= 80:
        return f"https://www.youtube.com/watch?v={best['video_id']}"
    return best


def search_youtube_trailer_legacy(title, year, best=None):
    if best and isinstance(best, dict) and best.get('score', 0) >= 65:
        return f"https://www.youtube.com/watch?v={best['video_id']}"
    query = f"{title} {year} official trailer"
    url = f"https://www.youtube.com/results?search_query={urllib.parse.quote(query)}"
    try:
        r = SESSION.get(url, timeout=10)
        for candidate in _youtube_candidates(r.text):
            score = _score_youtube_candidate(candidate, title, year)
            if not best or (isinstance(best, dict) and score > best['score']):
                best = {**candidate, 'score': score}
    except Exception:
        pass
    if isinstance(best, dict) and best.get('score', 0) >= 65:
        return f"https://www.youtube.com/watch?v={best['video_id']}"
    return None


def search_youtube_trailer(title, year):
    best = search_youtube_trailer_verified(title, year)
    if isinstance(best, str):
        return best
    return search_youtube_trailer_legacy(title, year, best)


def search_youtube_by_kp_id(kp_id, title, year):
    kp_id = str(kp_id or '').strip()
    if not kp_id or kp_id == '0':
        return None
    kp_cache = load_json(KP_TRAILER_CACHE) or {}
    kp_key = kp_id
    cached = kp_cache.get(kp_key)
    if cached and _validate_youtube_url(cached, title, year):
        return cached
    queries = [
        f'кинопоиск {kp_id} трейлер',
        f'kp {kp_id} trailer',
        f'"{kp_id}" трейлер',
    ]
    best = None
    for query in queries:
        url = f"https://www.youtube.com/results?search_query={urllib.parse.quote(query)}"
        try:
            r = SESSION.get(url, timeout=10)
        except Exception:
            continue
        for candidate in _youtube_candidates(r.text):
            score = _score_youtube_candidate(candidate, title, year) + 25
            if not best or score > best['score']:
                best = {**candidate, 'score': score}
        time.sleep(0.1)
    if isinstance(best, dict) and best.get('score', 0) >= 60:
        url = f"https://www.youtube.com/watch?v={best['video_id']}"
        if _validate_youtube_url(url, title, year):
            kp_cache[kp_key] = url
            save_json(KP_TRAILER_CACHE, kp_cache)
            return url
    return None


def search_kinopoisk_trailer(kp_id):
    kp_id = str(kp_id or '').strip()
    if not kp_id or kp_id == '0':
        return None

    urls = [
        f"https://www.kinopoisk.ru/film/{kp_id}/video/type/0/",
        f"https://www.kinopoisk.ru/film/{kp_id}/video/",
    ]
    trailer_words = ('трейлер', 'тизер', 'trailer', 'teaser')
    try:
        for url in urls:
            r = SESSION.get(url, timeout=12, headers={**HEADERS, 'Referer': 'https://www.kinopoisk.ru/'})
            html = r.text or ''
            if 'passport.yandex' in r.url or 'sso.kinopoisk' in html:
                continue

            soup = BeautifulSoup(html, 'html.parser')
            candidates = []
            for link in soup.select('a[href*="/video/"]'):
                href = link.get('href') or ''
                match = re.search(rf'/film/{re.escape(kp_id)}/video/(\d+)/', href)
                if not match:
                    continue
                text = link.get_text(' ', strip=True).lower()
                full_url = urllib.parse.urljoin('https://www.kinopoisk.ru/', href)
                score = 10
                if any(word in text for word in trailer_words):
                    score += 50
                if any(word in text for word in ('фрагмент', 'интервью', 'съемк', 'реклама', 'тв-ролик')):
                    score -= 40
                candidates.append((score, full_url))

            if candidates:
                candidates.sort(reverse=True)
                best_score, best_url = candidates[0]
                if best_score >= 50:
                    return best_url

    except Exception:
        pass
    return None


def search_imdb_trailer(imdb_id):
    imdb_id = str(imdb_id or '').strip()
    if not imdb_id or imdb_id == '0' or not imdb_id.startswith('tt'):
        return None
    url = f"https://www.imdb.com/title/{imdb_id}/"
    try:
        r = SESSION.get(url, timeout=12, headers={
            **HEADERS,
            'Accept-Language': 'en-US,en;q=0.9',
        })
        html = r.text or ''
        match = re.search(
            r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>',
            html, re.DOTALL | re.IGNORECASE
        )
        if match:
            data = json.loads(match.group(1))
            trailer = data.get('trailer') or {}
            if isinstance(trailer, dict):
                embed_url = trailer.get('embedUrl') or ''
                if 'youtube.com/embed/' in embed_url:
                    vid_match = re.search(r'/embed/([a-zA-Z0-9_-]{11})', embed_url)
                    if vid_match:
                        return f"https://www.youtube.com/watch?v={vid_match.group(1)}"
    except Exception:
        pass
    return None


def search_youtube_full_movie(title, year):
    """Поиск полного фильма на YouTube как fallback для трейлера."""
    queries = [
        f'{title} {year} полный фильм',
        f'{title} {year} фильм',
        f'{title} {year} full movie',
        f'{title} {year} movie',
    ]
    best = None
    for query in queries:
        url = f"https://www.youtube.com/results?search_query={urllib.parse.quote(query)}"
        try:
            r = SESSION.get(url, timeout=10)
        except Exception:
            continue
        for candidate in _youtube_candidates(r.text):
            score = _score_youtube_candidate(candidate, title, year)
            if not best or score > best['score']:
                best = {**candidate, 'score': score}
        time.sleep(0.1)
    if isinstance(best, dict) and best.get('score', 0) >= 25:
        return f"https://www.youtube.com/watch?v={best['video_id']}"
    return None


def _validate_youtube_url(url, title, year):
    """Verify youtube URL by fetching oEmbed title and scoring.
    Returns url if score >= 50, None otherwise."""
    if not url:
        return None
    try:
        oembed_url = f'https://www.youtube.com/oembed?url={urllib.parse.quote(url)}&format=json'
        resp = SESSION.get(oembed_url, timeout=5)
        if resp.status_code == 200:
            info = resp.json()
            cand = {
                'title': info.get('title', ''),
                'channel': info.get('author_name', ''),
                'video_id': url.split('v=')[-1].split('&')[0],
                'length': '2:00',
            }
            score = _score_youtube_candidate(cand, title, year)
            if score >= 50:
                return url
    except Exception:
        pass
    return None


def resolve_trailer_url(title, year, kp_id=None, imdb_id=None):
    if kp_id and str(kp_id) != '0':
        kp_cache = load_json(KP_TRAILER_CACHE) or {}
        kp_key = str(kp_id)
        kp_url = kp_cache.get(kp_key)
        if not kp_url:
            kp_url = search_kinopoisk_trailer(kp_id)
            if kp_url:
                kp_cache[kp_key] = kp_url
                save_json(KP_TRAILER_CACHE, kp_cache)
        if kp_url:
            return kp_url

    if imdb_id and str(imdb_id) != '0':
        imdb_cache = load_json(IMDB_TRAILER_CACHE) or {}
        imdb_key = str(imdb_id)
        imdb_url = imdb_cache.get(imdb_key)
        if not imdb_url:
            imdb_url = search_imdb_trailer(imdb_id)
            if imdb_url:
                imdb_cache[imdb_key] = imdb_url
                save_json(IMDB_TRAILER_CACHE, imdb_cache)
        if imdb_url:
            return imdb_url

    if kp_id and str(kp_id) != '0':
        kp_yt_url = search_youtube_by_kp_id(kp_id, title, year)
        if kp_yt_url:
            return kp_yt_url

    result = search_youtube_trailer(title, year)
    if result and _validate_youtube_url(result, title, year):
        return result
    result = search_youtube_full_movie(title, year)
    if result and _validate_youtube_url(result, title, year):
        return result
    return None


def resolve_topic_trailer_url(topic):
    title = topic.get('orig_title') or topic.get('movie_title') or ''
    year = topic.get('movie_year')
    if not title:
        return None
    if is_world_topic(topic):
        cache_key = f"{title}|{year}".lower()
        yt_cache = load_json(YOUTUBE_CACHE) or {}
        cached_url = yt_cache.get(cache_key)
        if cached_url and is_verified_world_youtube_trailer(SESSION, cached_url, title, year):
            return cached_url
        yt_url = search_world_youtube_trailer(SESSION, title, year)
        if yt_url and is_verified_world_youtube_trailer(SESSION, yt_url, title, year):
            yt_cache[cache_key] = yt_url
            save_json(YOUTUBE_CACHE, yt_cache)
            return yt_url
        return None
    return resolve_trailer_url(title, year, topic.get('kp_id'), topic.get('imdb_id'))


def download_poster(imdb_id, url):
    if not imdb_id or not url:
        return ''
    os.makedirs(POSTERS_DIR, exist_ok=True)
    filename = f"{imdb_id}.jpg"
    local_path = os.path.join(POSTERS_DIR, filename)
    poster_path = f"{POSTERS_URL}/{filename}"
    if os.path.exists(local_path):
        return poster_path
    try:
        r = SESSION.get(url, timeout=15)
        if r.status_code == 200:
            with open(local_path, 'wb') as f:
                f.write(r.content)
            return poster_path
    except Exception:
        pass
    return ''


def download_kinopoisk_poster(kp_id):
    if not kp_id:
        return ''
    os.makedirs(POSTERS_DIR, exist_ok=True)
    filename = f"kp_{kp_id}.jpg"
    local_path = os.path.join(POSTERS_DIR, filename)
    poster_path = f"{POSTERS_URL}/{filename}"
    if os.path.exists(local_path):
        return poster_path
    urls = [
        f"https://st.kp.yandex.net/images/film_big/{kp_id}.jpg",
        f"https://st.kp.yandex.net/images/film_iphone/iphone360_{kp_id}.jpg",
        f"https://st.kp.yandex.net/images/film/{kp_id}.jpg",
    ]
    headers = dict(HEADERS)
    headers["Referer"] = "https://www.kinopoisk.ru/"
    for url in urls:
        try:
            r = SESSION.get(url, timeout=15, headers=headers)
            content_type = r.headers.get('content-type', '').lower()
            if r.status_code == 200 and 'image' in content_type and r.content.startswith(b'\xff\xd8'):
                with open(local_path, 'wb') as f:
                    f.write(r.content)
                return poster_path
        except Exception:
            continue
    return ''


def clean_and_translate_genre(genre_text):
    if not genre_text:
        return ''
    parts = [p.strip() for p in genre_text.split(',')]
    stop_lower = {s.lower() for s in GENRE_STOP}
    trans_lower = {k.lower(): v for k, v in GENRE_TRANSLATION.items()}
    clean = []
    for p in parts:
        p_stripped = p.strip()
        if not p_stripped:
            continue
        pl = p_stripped.lower()
        if any(sw in pl for sw in stop_lower):
            continue
        translated = trans_lower.get(pl, p_stripped)
        clean.append(translated)
    return ', '.join(clean)


def _dataset_is_fresh(path, max_age_days=7):
    if not path or not os.path.exists(path):
        return False
    mtime = os.path.getmtime(path)
    age_days = (time.time() - mtime) / 86400
    return age_days < max_age_days


def download_imdb_dataset(url, path, label):
    print(f"Скачиваю IMDB {label} dataset...")
    r = SESSION.get(url, stream=True, timeout=120)
    r.raise_for_status()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp_path = f"{path}.download"
    with open(tmp_path, 'wb') as f:
        f.write(r.content)
    return tmp_path


def promote_imdb_dataset(tmp_path, path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    os.replace(tmp_path, path)


def remove_file_quietly(path):
    try:
        os.remove(path)
    except FileNotFoundError:
        pass


def _verify_imdb_ids_exist(ids, label):
    """Проверить, существуют ли IMDB ID через лёгкий GET-запрос.
    Возвращает множество ID, которые существуют (HTTP 200)."""
    valid = set()
    not_found = set()
    for imdb_id in sorted(ids):
        try:
            r = SESSION.get(f'https://www.imdb.com/title/{imdb_id}/',
                            timeout=8, allow_redirects=True)
            if r.status_code == 200 and 'Page not found' not in r.text[:500]:
                valid.add(imdb_id)
            else:
                not_found.add(imdb_id)
        except Exception:
            valid.add(imdb_id)
    if not_found:
        print(f"   IMDB IDs не найдены ({len(not_found)}): {', '.join(sorted(not_found)[:5])}{'...' if len(not_found) > 5 else ''}")
    return valid


def scan_ratings_dataset(path, needed_ids):
    found = {}
    if not os.path.exists(path):
        return found
    with gzip.open(path, mode='rt', encoding='utf-8') as f:
        f.readline()
        for line in f:
            parts = line.rstrip('\n').split('\t')
            if len(parts) >= 3:
                tid = parts[0]
                if tid in needed_ids:
                    found[tid] = {'rating': parts[1], 'votes': parts[2]}
                    if len(found) == len(needed_ids):
                        break
    return found


def scan_basics_dataset(path, needed_ids):
    found = {}
    if not os.path.exists(path):
        return found
    with gzip.open(path, mode='rt', encoding='utf-8') as f:
        f.readline()
        for line in f:
            parts = line.rstrip('\n').split('\t')
            if len(parts) >= 9:
                tid = parts[0]
                if tid in needed_ids:
                    genres = parts[8] if parts[8] != r'\N' else ''
                    found[tid] = {'type': parts[1], 'genres': genres}
                    if len(found) == len(needed_ids):
                        break
    return found


def load_ratings(needed_ids):
    cached = load_json(RATINGS_CACHE) or {}
    if cached and needed_ids.issubset(cached.keys()):
        return {k: v for k, v in cached.items() if k in needed_ids and v is not None}
    missing_ids = set(needed_ids) - set(cached.keys())
    try:
        ratings = scan_ratings_dataset(RATINGS_DATASET, missing_ids)
        still_missing = {tid for tid in missing_ids if tid not in ratings}
        if still_missing:
            valid_ids = _verify_imdb_ids_exist(still_missing, "ratings")
            if valid_ids:
                if _dataset_is_fresh(RATINGS_DATASET):
                    print(f"   Dataset свежий (<7 дней), скачка пропущена")
                else:
                    tmp_path = download_imdb_dataset(RATINGS_URL, RATINGS_DATASET, "ratings")
                    fresh_ratings = scan_ratings_dataset(tmp_path, valid_ids)
                    if fresh_ratings:
                        promote_imdb_dataset(tmp_path, RATINGS_DATASET)
                        ratings = {**ratings, **fresh_ratings}
                    else:
                        remove_file_quietly(tmp_path)
                        print("   Свежий ratings dataset не содержит нужные ID; оставляю текущий файл")
            else:
                print("   Все недостающие ID не найдены в IMDB, скачка dataset пропущена")
        for tid in missing_ids:
            if tid not in ratings:
                ratings[tid] = None
        ratings = {**cached, **ratings}
        save_json(RATINGS_CACHE, ratings)
        return {k: v for k, v in ratings.items() if k in needed_ids and v is not None}
    except Exception as e:
        print(f"   Не удалось скачать ratings: {e}")
        if cached:
            return {k: v for k, v in cached.items() if k in needed_ids and v is not None}
        return {}


def load_basics(needed_ids):
    cached = load_json(BASICS_CACHE) or {}
    if cached and needed_ids.issubset(cached.keys()):
        return {k: v for k, v in cached.items() if k in needed_ids and v is not None}
    missing_ids = set(needed_ids) - set(cached.keys())
    try:
        basics = scan_basics_dataset(BASICS_DATASET, missing_ids)
        still_missing = {tid for tid in missing_ids if tid not in basics}
        if still_missing:
            valid_ids = _verify_imdb_ids_exist(still_missing, "basics")
            if valid_ids:
                if _dataset_is_fresh(BASICS_DATASET):
                    print(f"   Dataset свежий (<7 дней), скачка пропущена")
                else:
                    tmp_path = download_imdb_dataset(BASICS_URL, BASICS_DATASET, "basics")
                    fresh_basics = scan_basics_dataset(tmp_path, valid_ids)
                    if fresh_basics:
                        promote_imdb_dataset(tmp_path, BASICS_DATASET)
                        basics = {**basics, **fresh_basics}
                    else:
                        remove_file_quietly(tmp_path)
                        print("   Свежий basics dataset не содержит нужные ID; оставляю текущий файл")
            else:
                print("   Все недостающие ID не найдены в IMDB, скачка dataset пропущена")
        for tid in missing_ids:
            if tid not in basics:
                basics[tid] = None
        basics = {**cached, **basics}
        save_json(BASICS_CACHE, basics)
        return {k: v for k, v in basics.items() if k in needed_ids and v is not None}
    except Exception as e:
        print(f"   Не удалось скачать basics: {e}")
        if cached:
            return {k: v for k, v in cached.items() if k in needed_ids and v is not None}
        return {}


def parse_forum_page(html, collection='nashe_kino', skip_topics=0):
    soup = BeautifulSoup(html, 'html.parser')
    topics = []
    rows = soup.select('tr.hl-tr')
    filtered = 0
    for row in rows:
        topic_id = row.get('data-topic_id')
        if not topic_id:
            continue
        size_el = row.select_one('a.dl-stub')
        if not size_el:
            continue
        if filtered < skip_topics:
            filtered += 1
            continue
        filtered += 1
        title_el = row.select_one('a.torTopic.tt-text')
        if not title_el:
            continue
        title = title_el.get_text(strip=True)
        author_el = row.select_one('.topicAuthor')
        author = author_el.get_text(strip=True) if author_el else ''
        size_str = size_el.get_text(strip=True)

        seed_el = row.select_one('.seedmed b')
        leech_el = row.select_one('.leechmed b')
        seeders = int(seed_el.get_text(strip=True)) if seed_el else 0
        leechers = int(leech_el.get_text(strip=True)) if leech_el else 0

        date_el = row.select_one('.vf-col-last-post p')
        date_str = date_el.get_text(strip=True) if date_el else ''

        movie_title, orig_title, movie_year, genre, quality = parse_rutracker_title(title)
        size_bytes, _ = parse_size(size_str)

        topics.append({
            'topic_id': topic_id,
            'title': title,
            'movie_title': movie_title,
            'orig_title': orig_title,
            'movie_year': movie_year,
            'genre': genre,
            'quality': quality,
            'collection': collection,
            'author': author,
            'size_str': size_str,
            'size_bytes': size_bytes,
            'seeders': seeders,
            'leechers': leechers,
            'date_str': date_str,
            'added_at': now_text(),
            'topic_url': TOPIC_URL_T.format(topic_id),
            'listing_order': len(topics),
            'magnet': '',
            'imdb_id': None,
            'imdb_rating': None,
            'imdb_votes': None,
            'kp_id': None,
            'kp_rating': None,
            'kp_votes': None,
            'poster_url': '',
            'cast': '',
            'youtube_url': None,
        })
    return topics


def parse_piratebay_page(html, collection='piratebay_top'):
    soup = BeautifulSoup(html, 'html.parser')
    topics = []
    rows = soup.select('#searchResult tbody tr')
    for row in rows:
        title_el = row.select_one('a.detLink')
        if not title_el:
            continue
        title = title_el.get_text(strip=True)
        magnet_el = row.select_one('a[href^="magnet:"]')
        magnet = magnet_el.get('href', '') if magnet_el else ''
        info_hash = info_hash_from_magnet(magnet)
        if not info_hash:
            continue
        tds = row.find_all('td')
        category_el = row.select_one('.vertTh a')
        category = category_el.get_text(strip=True) if category_el else ''
        uploaded = tds[2].get_text(strip=True) if len(tds) > 2 else ''
        size_str = tds[4].get_text(strip=True) if len(tds) > 4 else ''
        seeders = tds[5].get_text(strip=True) if len(tds) > 5 else '0'
        leechers = tds[6].get_text(strip=True) if len(tds) > 6 else '0'
        author_el = tds[7].select_one('a') if len(tds) > 7 else None
        author = author_el.get_text(strip=True) if author_el else ''
        href = title_el.get('href') or ''
        topic_url = urllib.parse.urljoin('https://1.piratebays.to', href)
        movie_title, movie_year = clean_title(title)
        size_bytes, size_clean = parse_size(size_str)
        date_str = parse_piratebay_date(uploaded)

        topics.append({
            'topic_id': f'pb_{info_hash}',
            'title': title,
            'movie_title': movie_title,
            'orig_title': '',
            'movie_year': movie_year,
            'genre': '',
            'source_category': category,
            'quality': '',
            'collection': collection,
            'source': 'piratebay',
            'author': author,
            'size_str': size_clean,
            'size_bytes': size_bytes,
            'seeders': parse_int_text(seeders),
            'leechers': parse_int_text(leechers),
            'date_str': date_str,
            'added_at': now_text(),
            'topic_url': topic_url,
            'listing_order': len(topics),
            'magnet': magnet,
            'imdb_id': None,
            'imdb_rating': None,
            'imdb_votes': None,
            'kp_id': None,
            'kp_rating': None,
            'kp_votes': None,
            'poster_url': '',
            'cast': '',
            'youtube_url': None,
            'format': detect_format_from_text(title),
        })
    return topics


def parse_piratebay_detail(html):
    soup = BeautifulSoup(html, 'html.parser')
    imdb = ''
    for link in soup.select('a[href*="imdb.com/title/tt"]'):
        href = link.get('href', '')
        m = re.search(r'/title/(tt\d+)', href)
        if m:
            imdb = m.group(1)
            break
    return imdb


_PB_CONTAINER_MAP = {
    'matroska': 'MKV',
    'mpeg-4': 'MP4',
    'mpeg4': 'MP4',
    'avi': 'AVI',
    'webm': 'WEBM',
    'mpeg': 'MPEG',
}


def parse_piratebay_format(html):
    soup = BeautifulSoup(html, 'html.parser')
    nfo_pre = soup.select_one('.nfo pre')
    if nfo_pre:
        for line in nfo_pre.get_text('\n').splitlines():
            line = line.strip()
            m = re.match(r'Container[\.\s]+:\s*(.+)', line, re.IGNORECASE)
            if m:
                container = m.group(1).strip()
                for key, fmt in _PB_CONTAINER_MAP.items():
                    if container.lower() == key or key in container.lower():
                        return fmt
    if not nfo_pre:
        title_div = soup.select_one('#title')
        if title_div:
            text = title_div.get_text()
            for key, fmt in _PB_CONTAINER_MAP.items():
                if key in text.lower():
                    return fmt
    return ''


def fetch_piratebay_format(topic_id, topic_url, timeout=10):
    html = get_topic_html(topic_id, topic_url, timeout=timeout)
    if html:
        fmt = parse_piratebay_format(html)
        if fmt:
            return fmt
    m = re.search(r'/torrent/(\d+)', topic_url)
    if m:
        torrent_id = m.group(1)
        try:
            r = SESSION.get(f'https://1.piratebays.to/ajax_details_filelist.php?id={torrent_id}', timeout=timeout)
            if r.status_code == 200:
                for line in r.text.splitlines():
                    for ext, fmt in [('.mkv', 'MKV'), ('.mp4', 'MP4'), ('.avi', 'AVI'), ('.webm', 'WEBM'), ('.mpeg', 'MPEG')]:
                        if ext in line.lower():
                            return fmt
        except Exception:
            pass
    return ''


def fetch_piratebay_imdb_ids(topics):
    total = len(topics)
    ok = 0
    def fetch_one(t):
        try:
            html = get_topic_html(t['topic_id'], t['topic_url'], timeout=10)
            if html is None:
                return False
            imdb = parse_piratebay_detail(html)
            if imdb:
                t['imdb_id'] = imdb
                return True
        except Exception:
            pass
        return False

    with ThreadPoolExecutor(max_workers=min(WORKER_COUNT, max(total, 1))) as executor:
        futures = {executor.submit(fetch_one, t): t for t in topics}
        for future in as_completed(futures):
            t = futures[future]
            try:
                if future.result():
                    ok += 1
            except Exception:
                pass
    print(f"  Получено IMDB: {ok}/{total}")


def fetch_world_imdb_ids(topics):
    return fetch_piratebay_imdb_ids(topics)


def parse_topic_for_magnet(html):
    soup = BeautifulSoup(html, 'html.parser')
    magnet_el = soup.select_one('a.magnet-link')
    magnet = magnet_el['href'] if magnet_el else ''
    imdb = ''
    for link in soup.select('a.postLink'):
        href = link.get('href', '')
        if 'imdb.com/title' in href:
            m = re.search(r'/title/(tt\d+)', href)
            if m:
                imdb = m.group(1)
    poster = ''
    poster_el = soup.select_one('var.postImgAligned.img-right')
    if poster_el:
        poster = poster_el.get('title', '')
    if not poster:
        poster_el = soup.select_one('div.post_body var.postImgAligned')
        if poster_el:
            poster = poster_el.get('title', '')
    fmt = ''
    known = {'avi', 'mkv', 'matroska', 'mp4', 'mpeg-4', 'mpeg', 'webm'}
    known_map = {'AVI': 'AVI', 'Matroska': 'MKV', 'MKV': 'MKV', 'MP4': 'MP4', 'MPEG-4': 'MP4', 'WEBM': 'WEBM'}
    text = re.sub(r'<[^>]+>', '', html)
    fmt_token = r'(AVI|MKV|Matroska|MP4|MPEG-4|MPEG|WEBM)'
    m = re.search(r'Формат(?:\s+видео)?\s*[:：]\s*' + fmt_token, text, re.I)
    if not m:
        m = re.search(r'Format(?:\s+video)?\s*[:：]\s*' + fmt_token, text, re.I)
    if m:
        candidate = m.group(1).strip().rstrip(':').rstrip(',')
        if candidate.lower() in known:
            fmt = known_map.get(candidate, candidate)
    if not fmt:
        # fallback: split at known separators (br, newline) and check before
        part = re.sub(r'<br\s*/?>', '\n', html, flags=re.I)
        part = re.sub(r'<[^>]+>', '', part)
        for line in part.split('\n'):
            ck = re.search(r'(?:Формат|Format)(?:\s+(?:видео|video))?\s*[:：]\s*' + fmt_token, line, re.I)
            if ck:
                cand = ck.group(1).rstrip(':').rstrip(',')
                if cand.lower() in known:
                    fmt = known_map.get(cand, cand)
                    break
    return {'magnet': magnet, 'imdb': imdb, 'poster': poster, 'format': fmt}


def download_rutracker_poster(topic_id, url):
    if not topic_id or not url:
        return ''
    os.makedirs(POSTERS_DIR, exist_ok=True)
    filename = f"{topic_id}.jpg"
    local_path = os.path.join(POSTERS_DIR, filename)
    poster_path = f"{POSTERS_URL}/{filename}"
    if os.path.exists(local_path):
        return poster_path
    try:
        r = SESSION.get(url, timeout=15)
        if r.status_code == 200:
            with open(local_path, 'wb') as f:
                f.write(r.content)
            return poster_path
    except Exception:
        pass
    return ''


def local_poster_path(poster_url):
    poster_url = (poster_url or '').replace('\\', '/')
    if poster_url.startswith('data/posters/'):
        filename = poster_url.rsplit('/', 1)[-1]
    elif poster_url.startswith('posters/'):
        filename = poster_url.rsplit('/', 1)[-1]
    else:
        return None
    return os.path.join(POSTERS_DIR, filename)


def normalize_poster_url(poster_url):
    poster_url = (poster_url or '').replace('\\', '/')
    if poster_url.startswith('posters/'):
        return f"data/{poster_url}"
    return poster_url


def is_external_poster_url(poster_url):
    poster_url = (poster_url or '').lower()
    return poster_url.startswith('http://') or poster_url.startswith('https://')


def should_try_external_poster_fallback(topic):
    return (
        is_external_poster_url(topic.get('poster_url'))
        and not has_real_poster(topic)
        and topic.get('_poster_fallback_failed_at') != today_text()
    )


def has_real_poster(topic):
    poster_url = topic.get('poster_url', '') or ''
    if not poster_url:
        return False
    local_path = local_poster_path(poster_url)
    if not (local_path and os.path.exists(local_path)):
        return False
    imdb_id = topic.get('imdb_id', '')
    if imdb_id and re.search(r'/tt\d+', poster_url):
        m = re.search(r'tt(\d+)', poster_url)
        if m and m.group(0) != imdb_id:
            return False
    return True


def resolve_existing_local_poster(topic):
    """Set poster_url from already-downloaded poster by imdb_id or kp_id.
    Returns True if a local poster file was found and assigned."""
    imdb_id = topic.get('imdb_id')
    if imdb_id:
        filename = f"{imdb_id}.jpg"
        local_path = os.path.join(POSTERS_DIR, filename)
        if os.path.exists(local_path):
            topic['poster_url'] = f"{POSTERS_URL}/{filename}"
            clear_poster_failed(topic)
            return True
    kp_id = topic.get('kp_id')
    if kp_id:
        filename = f"kp_{kp_id}.jpg"
        local_path = os.path.join(POSTERS_DIR, filename)
        if os.path.exists(local_path):
            topic['poster_url'] = f"{POSTERS_URL}/{filename}"
            clear_poster_failed(topic)
            return True
    return False


def display_poster_url(topic):
    poster_url = topic.get('poster_url', '') or ''
    local_path = local_poster_path(poster_url)
    if local_path and os.path.exists(local_path):
        return normalize_poster_url(poster_url)
    return POSTER_PLACEHOLDER_URL


def should_retry_poster(topic):
    failed_at = topic.get('_poster_failed_at')
    if not failed_at:
        return True
    try:
        failed_date = datetime.strptime(failed_at, '%Y-%m-%d').date()
    except Exception:
        return True
    return datetime.now().date() - failed_date >= timedelta(days=POSTER_RETRY_DAYS)


def mark_poster_failed(topic):
    topic['_poster_failed_at'] = today_text()
    if is_external_poster_url(topic.get('poster_url')):
        topic['_poster_fallback_failed_at'] = today_text()


def fix_bad_topics(topics):
    """Перепарсить все записи, где movie_title содержит мусор из обрезанного заголовка."""
    count = 0
    need_re_enrich = []
    for t in topics:
        if is_world_source(t.get('source')):
            continue
        mt = t.get('movie_title', '')
        raw = t.get('title', '')
        if not raw:
            continue
        needs_fix = False
        if '[' in mt or mt.rstrip().endswith(','):
            needs_fix = True
        elif not t.get('movie_year') and re.search(r'\b(19\d{2}|20\d{2})\b', raw):
            needs_fix = True
        if not needs_fix:
            # Re-parse and compare — if stored title differs, it was truncated before
            parsed = parse_rutracker_title(raw)
            if parsed[0] and parsed[0] != mt and '[' not in parsed[0]:
                needs_fix = True
        if needs_fix:
            parsed = parse_rutracker_title(raw)
            if parsed[0] and '[' not in parsed[0]:
                old = mt
                old_year = t.get('movie_year') or ''
                t['movie_title'] = parsed[0]
                t['orig_title'] = parsed[1] or ''
                if parsed[2]:
                    t['movie_year'] = parsed[2]
                if parsed[3]:
                    t['genre'] = parsed[3]
                if parsed[4]:
                    t['quality'] = parsed[4]
                if old != t['movie_title'] or (parsed[2] and old_year != parsed[2]):
                    count += 1
                    print(f"  Исправлен: {old[:50]} -> {t['movie_title']} ({t.get('movie_year','')})")
                    t['kp_id'] = None
                    t['kp_rating'] = None
                    t['kp_votes'] = None
                    t['imdb_id'] = None
                    t['imdb_rating'] = None
                    t['imdb_votes'] = None
                    t['poster_url'] = ''
                    need_re_enrich.append((old, old_year, t))
    if not count:
        print("  Битых записей не найдено")
    if need_re_enrich:
        print(f"\n  Повторный поиск Кинопоиска для {len(need_re_enrich)} исправленных...")
        kp_cache = load_json(KP_SEARCH_CACHE) or {}
        for old_title, old_year, t in need_re_enrich:
            title = t['movie_title']
            year = t.get('movie_year', '')
            if not year:
                year = _extract_year_from_title(t)
            cache_key = f"{title}|{year}".lower()
            old_cache_key = f"{old_title}|{old_year}".lower()
            if old_cache_key in kp_cache:
                del kp_cache[old_cache_key]
            result = search_kinopoisk(title, year)
            if result:
                t['kp_id'] = result['kp_id']
                t['kp_rating'] = result['kp_rating']
                t['kp_votes'] = result['kp_votes']
                kp_cache[cache_key] = result
                save_json(KP_SEARCH_CACHE, kp_cache)
                print(f"    {title} ({year}): КП {result['kp_id']} рейтинг {result['kp_rating'] or '—'}")
                kp_local = download_kinopoisk_poster(result['kp_id'])
                if kp_local:
                    t['poster_url'] = kp_local
                    print(f"    Постер: {kp_local}")
            else:
                print(f"    {title} ({year}): не найдено")
            time.sleep(0.3)
    return topics


def repair_world_titles(topics):
    """Restore clean movie_title for world topics corrupted by fix_bad_topics."""
    tech = re.compile(
        r'(1080p|720p|4K|2160p|WEBRip|WEB-DL|BluRay|HDRip|DVDRip|DCPRip|'
        r'x264|x265|h264|h265|HEVC|AAC|AC3|DDP|DTS|MP4|MKV|'
        r'10bit|8bit|BONE|VOSTFR|TELESYNC|CAM|HDTS|SCREENER|'
        r'YIFY|YTS|RARBG|RMTeam|NeoNoir|SupaCvnt|FLUX|BrRip)',
        re.I,
    )
    count = 0
    for t in topics:
        if not is_world_topic(t):
            continue
        mt = t.get('movie_title', '') or ''
        raw = t.get('title', '') or ''
        if not raw or not tech.search(mt):
            continue
        new_title, year = clean_title(raw)
        if year:
            idx = new_title.find(year)
            if idx >= 0:
                before = new_title[:idx].strip()
                words = before.split()
                new_title = ' '.join(words)
            new_title = new_title.replace(f' {year}', '').strip()
            new_title = re.sub(rf'\s*{year}\s*', ' ', new_title).strip()
            new_title = re.sub(r'\s+', ' ', new_title).strip()
        if not new_title or new_title == mt:
            continue
        old = mt[:50]
        t['movie_title'] = new_title
        if year:
            t['movie_year'] = year
        t['imdb_id'] = None
        t['imdb_rating'] = None
        t['imdb_votes'] = None
        t['kp_id'] = None
        t['kp_rating'] = None
        t['kp_votes'] = None
        t['poster_url'] = ''
        count += 1
        print(f"  title: {old} -> {new_title} ({t['movie_year']})")
    if count:
        print(f"  World-тем восстановлено: {count}")
    return topics


def recheck_trailers(topics):
    """
    Generator that scores all youtube_urls, stores yt_score, replaces weak ones.
    Yields status lines for streaming. Processes from weakest to strongest.
    After iteration completes, caller must save topics and regenerate HTML.
    """
    import requests as _req
    scored_topics = []
    total = scored = 0

    yield 'Фаза 1: оценка всех трейлеров...'
    for t in topics:
        yt = t.get('youtube_url')
        if not yt or yt == '0':
            continue
        title = t.get('movie_title', '')
        year = t.get('movie_year', '')
        if is_world_topic(t):
            score = 100 if is_verified_world_youtube_trailer(SESSION, yt, title, year) else 0
            t['yt_score'] = score
            scored_topics.append((score, yt, t))
            total += 1
            if total % 50 == 0:
                yield f'  оценено: {total}'
            continue
        try:
            r = _req.get(f'https://www.youtube.com/oembed?url={yt}&format=json', timeout=5)
            if r.status_code == 200:
                info = r.json()
                cand = {
                    'title': info.get('title', ''),
                    'channel': info.get('author_name', ''),
                    'video_id': yt.split('v=')[-1].split('&')[0],
                    'length': '2:00',
                }
                score = _score_youtube_candidate(cand, title, year)
            else:
                score = 0
        except Exception:
            score = 0
        t['yt_score'] = score
        scored_topics.append((score, yt, t))
        total += 1
        if total % 50 == 0:
            yield f'  оценено: {total}'

    yield f'Оценено: {total} трейлеров'

    scored_topics.sort(key=lambda x: x[0])
    weak = [st for st in scored_topics if st[0] < 50]
    strong = [st for st in scored_topics if st[0] >= 50]
    yield f'Слабых (<50): {len(weak)}, сильных (>=50): {len(strong)}'

    if weak:
        yield ''
        yield 'Фаза 2: замена слабых...'

    # build peer lookup: kp_id/imdb_id -> good url from strong topics
    peer_url = {}
    for s, u, t in strong:
        kp = t.get('kp_id')
        im = t.get('imdb_id')
        if u and u != '0':
            if kp:
                peer_url.setdefault(('kp', str(kp)), u)
            if im:
                peer_url.setdefault(('im', str(im)), u)

    replaced_weak = 0
    kept_weak = 0
    for score, yt, t in weak:
        title = t.get('movie_title', '')
        year = t.get('movie_year', '')
        tid = t['topic_id']
        new_yt = None
        kp = t.get('kp_id')
        im = t.get('imdb_id')
        if kp and ('kp', str(kp)) in peer_url:
            new_yt = peer_url[('kp', str(kp))]
            yield f'  [{tid}] {title} ({year}) score {score} -> reused from peer (kp={kp})'
        elif im and ('im', str(im)) in peer_url:
            new_yt = peer_url[('im', str(im))]
            yield f'  [{tid}] {title} ({year}) score {score} -> reused from peer (imdb={im})'
        if not new_yt:
            try:
                new_yt = resolve_topic_trailer_url(t)
            except Exception:
                new_yt = None
        if new_yt:
            if is_world_topic(t):
                if is_verified_world_youtube_trailer(SESSION, new_yt, title, year):
                    t['youtube_url'] = new_yt
                    t['yt_score'] = 100
                    replaced_weak += 1
                    yield f'  [{tid}] {title} ({year}) score {score} -> 100: {new_yt}'
                    continue
                kept_weak += 1
                yield f'  [{tid}] {title} ({year}) score {score} — оставлено'
                continue
            try:
                r = _req.get(f'https://www.youtube.com/oembed?url={new_yt}&format=json', timeout=5)
                if r.status_code == 200:
                    info = r.json()
                    cand = {
                        'title': info.get('title', ''),
                        'channel': info.get('author_name', ''),
                        'video_id': new_yt.split('v=')[-1].split('&')[0],
                        'length': '2:00',
                    }
                    new_score = _score_youtube_candidate(cand, title, year)
                    if new_score >= 50:
                        t['youtube_url'] = new_yt
                        t['yt_score'] = new_score
                        replaced_weak += 1
                        yield f'  [{tid}] {title} ({year}) score {score} -> {new_score}: {new_yt}'
                        continue
            except Exception:
                pass
        kept_weak += 1
        yield f'  [{tid}] {title} ({year}) score {score} — оставлено'

    yield ''
    yield f'Итого: заменено {replaced_weak}, слабых осталось {kept_weak}, всего оценено {total}'


def clear_poster_failed(topic):
    topic.pop('_poster_failed_at', None)
    topic.pop('_poster_fallback_failed_at', None)


def localize_existing_poster(topic):
    poster_url = topic.get('poster_url', '') or ''
    if not is_external_poster_url(poster_url):
        return False
    local_url = download_rutracker_poster(topic.get('topic_id'), poster_url)
    if not local_url:
        return False
    topic['poster_url'] = local_url
    clear_poster_failed(topic)
    return True


def set_local_poster_from_url(topic, url):
    local_url = download_rutracker_poster(topic.get('topic_id'), url)
    if not local_url:
        return False
    topic['poster_url'] = local_url
    clear_poster_failed(topic)
    return True


def fetch_magnets(topics):
    total = len(topics)
    ok_count = 0
    fail_count = 0

    def fetch_one(t):
        if t.get('magnet') and has_real_poster(t):
            return 'ok', 'уже есть'
        if t.get('_magnet_failed'):
            return 'failed', 'ранее не удалось'
        try:
            html = get_topic_html(t['topic_id'], t['topic_url'], timeout=10)
            if html is None:
                raise RuntimeError('fetch failed')
            data = parse_topic_for_magnet(html)
            t['magnet'] = data['magnet']
            parts = []
            if data['magnet']:
                status = 'ok'
                parts.append('OK')
                if data.get('poster') and not has_real_poster(t):
                    if set_local_poster_from_url(t, data['poster']):
                        parts.append('постер ✓')
            else:
                status = 'failed'
                parts.append('нет магнета')
            if data.get('imdb') and not t.get('imdb_id'):
                t['imdb_id'] = data['imdb']
            if data.get('format') and not t.get('format'):
                t['format'] = data['format']
            return status, ', '.join(parts)
        except Exception as e:
            t['_magnet_failed'] = True
            return 'failed', f"ошибка: {e}"

    with ThreadPoolExecutor(max_workers=min(WORKER_COUNT, max(total, 1))) as executor:
        future_map = {executor.submit(fetch_one, t): (i, t) for i, t in enumerate(topics, 1)}
        for future in as_completed(future_map):
            i, t = future_map[future]
            try:
                status, message = future.result()
            except Exception as e:
                t['_magnet_failed'] = True
                status, message = 'failed', f"ошибка: {e}"
            if status == 'ok':
                ok_count += 1
            else:
                fail_count += 1
            print(f"  [{i}/{total}] Загрузка магнета t={t['topic_id']}... {message}", flush=True)
    return {'total': total, 'ok': ok_count, 'failed': fail_count}


def prune_unplayable_topics(topics):
    playable = [t for t in topics if t.get('magnet')]
    removed = len(topics) - len(playable)
    if removed:
        print(f"  Удалено тем без magnet: {removed}")
    return playable


def ensure_topic_defaults(topic):
    defaults = {
        'title': '',
        'movie_title': '',
        'orig_title': '',
        'movie_year': '',
        'genre': '',
        'format': '',
        'poster_url': '',
        'kp_id': '',
        'kp_rating': '',
        'kp_votes': '',
        'imdb_id': '',
        'imdb_rating': '',
        'imdb_votes': '',
        'youtube_url': '',
        'cast': '',
        'source': '',
        'collection': '',
        'size_str': '',
        'size_bytes': 0,
        'seeders': 0,
        'leechers': 0,
        'listing_order': 999999,
    }
    for key, value in defaults.items():
        topic.setdefault(key, value)
    return topic


def clean_catalog_topics(topics):
    return [ensure_topic_defaults(t) for t in prune_unplayable_topics(topics)]


def enrich(topics, ratings, basics):
    total = len(topics)
    yt_cache = load_json(YOUTUBE_CACHE) or {}
    for i, t in enumerate(topics, 1):
        eng_title = t.get('orig_title') or t['movie_title']
        year = t['movie_year']
        if not eng_title:
            continue
        cache_key = f"{eng_title}|{year}".lower()
        imdb_id = t.get('imdb_id')
        print(f"  [{i}/{total}] {t['movie_title']}...", end=' ', flush=True)

        if not has_real_poster(t):
            resolve_existing_local_poster(t)

        if imdb_id:
            bdata = basics.get(imdb_id)
            genre = bdata.get('genres', '') if isinstance(bdata, dict) else ''
            if not genre:
                rating_data = fetch_imdb_rating(imdb_id)
                if rating_data.get('genres'):
                    genre = rating_data['genres']
                if not has_real_poster(t) and rating_data.get('poster'):
                    local_url = download_poster(imdb_id, rating_data['poster'])
                    if local_url:
                        t['poster_url'] = local_url
                if rating_data.get('rating'):
                    t['imdb_rating'] = rating_data['rating']
                    t['imdb_votes'] = rating_data.get('votes', '')
                    print(f"IMDB {rating_data['rating']} (scraped)", end='')
            t['genre'] = clean_and_translate_genre(genre)
            rdata = ratings.get(imdb_id)
            if isinstance(rdata, dict):
                t['imdb_rating'] = rdata['rating']
                t['imdb_votes'] = rdata['votes']
                print(f"IMDB {rdata['rating']}", end='')
            elif not t.get('imdb_rating'):
                print(f"ID {imdb_id} — нет рейтинга", end='')
        if not has_real_poster(t) and t.get('kp_id'):
            kp_local = download_kinopoisk_poster(t['kp_id'])
            if kp_local:
                t['poster_url'] = kp_local
                print(f", KP постер ✓", end='')
        if t.get('youtube_url'):
            pass
        elif yt_cache.get(cache_key):
            cached_url = yt_cache[cache_key]
            cached_ok = False
            if is_world_topic(t):
                cached_ok = bool(is_verified_world_youtube_trailer(SESSION, cached_url, eng_title, year))
            else:
                cached_ok = True
            if cached_ok:
                t['youtube_url'] = cached_url
            else:
                yt_url = resolve_topic_trailer_url(t)
                t['youtube_url'] = yt_url
                yt_cache[cache_key] = yt_url
                save_json(YOUTUBE_CACHE, yt_cache)
                if yt_url:
                    print(f", трейлер ✓", end='')
                time.sleep(0.1)
        else:
            yt_url = resolve_topic_trailer_url(t)
            t['youtube_url'] = yt_url
            yt_cache[cache_key] = yt_url
            save_json(YOUTUBE_CACHE, yt_cache)
            if yt_url:
                print(f", трейлер ✓", end='')
            time.sleep(0.1)
        print()
    return topics


def sync_listing_order_for_collection(collection: str, cache_only: bool = False) -> dict[str, int]:
    """Return {topic_id: listing_order} for page 1. If cache_only, skip network."""
    coll_info = COLLECTIONS.get(collection)
    if not coll_info:
        return {}
    base_url = coll_info['url']
    source = coll_info.get('source', 'rutracker')
    if is_world_source(source):
        cache_path = os.path.join(TOPIC_CACHE_DIR, f'{collection}_p0.html')
        if cache_only:
            cache_used, raw = load_world_listing_cache_bytes(collection, source)
            if raw:
                html = raw.decode('utf-8', errors='replace')
                topics = parse_world_page(html, collection, source, clean_title, parse_size, now_text, info_hash_from_magnet, detect_format_from_text)
                topics = deduplicate_world_topics(topics)
                return {t['topic_id']: t.get('listing_order', i) for i, t in enumerate(topics)}
            return {}
        for attempt in range(1, MAX_RETRY + 1):
            try:
                r = SESSION.get(base_url, timeout=30)
                r.raise_for_status()
                raw = r.content
                html = raw.decode(r.encoding or 'utf-8', errors='replace')
                topics = parse_world_page(html, collection, source, clean_title, parse_size, now_text, info_hash_from_magnet, detect_format_from_text)
                topics = deduplicate_world_topics(topics)
                if topics:
                    os.makedirs(TOPIC_CACHE_DIR, exist_ok=True)
                    with open(cache_path, 'wb') as f:
                        f.write(raw)
                    save_world_page_hash(collection, html)
                    save_world_legacy_page_cache(source, raw, html)
                return {t['topic_id']: t.get('listing_order', i) for i, t in enumerate(topics)}
            except Exception:
                if attempt < MAX_RETRY:
                    time.sleep(2)
                    continue
                cache_used, raw = load_world_listing_cache_bytes(collection, source)
                if raw:
                    html = raw.decode('utf-8', errors='replace')
                    topics = parse_world_page(html, collection, source, clean_title, parse_size, now_text, info_hash_from_magnet, detect_format_from_text)
                    topics = deduplicate_world_topics(topics)
                    return {t['topic_id']: t.get('listing_order', i) for i, t in enumerate(topics)}
        return {}
    m_fid = re.search(r'f=(\d+)', base_url)
    forum_id = m_fid.group(1) if m_fid else collection
    cache_path = os.path.join(TOPIC_CACHE_DIR, f'f{forum_id}_p0.html')
    if cache_only:
        if listing_cache_is_valid(cache_path):
            with open(cache_path, 'rb') as f:
                raw = f.read()
            html = raw.decode('cp1251', errors='replace')
            skip = COLLECTIONS.get(collection, {}).get('skip_topics', 0)
            topics = parse_forum_page(html, collection=collection, skip_topics=skip)
            return {t['topic_id']: t.get('listing_order', i) for i, t in enumerate(topics)}
        return {}
    for attempt in range(1, MAX_RETRY + 1):
        try:
            r = SESSION.get(base_url, timeout=30)
            r.raise_for_status()
            raw = r.content
            html = raw.decode('cp1251', errors='replace')
            skip = COLLECTIONS.get(collection, {}).get('skip_topics', 0)
            topics = parse_forum_page(html, collection=collection, skip_topics=skip)
            if topics:
                os.makedirs(TOPIC_CACHE_DIR, exist_ok=True)
                with open(cache_path, 'wb') as f:
                    f.write(raw)
            return {t['topic_id']: t.get('listing_order', i) for i, t in enumerate(topics)}
        except Exception:
            if attempt < MAX_RETRY:
                time.sleep(2)
                continue
            if listing_cache_is_valid(cache_path):
                with open(cache_path, 'rb') as f:
                    raw = f.read()
                html = raw.decode('cp1251', errors='replace')
                skip = COLLECTIONS.get(collection, {}).get('skip_topics', 0)
                topics = parse_forum_page(html, collection=collection, skip_topics=skip)
                return {t['topic_id']: t.get('listing_order', i) for i, t in enumerate(topics)}
    return {}


def generate_html(topics, hidden_ids: set[str] | None = None):
    if hidden_ids is None:
        hidden_ids = load_hidden_topic_ids()
    rows = []
    tiles = []

    coll_opts = ''.join(f'<option value="{k}">{v["name"]}</option>' for k, v in COLLECTIONS.items())
    sort_opts = '''<option value="lo">Серверная</option><option value="dh">Сначала новые</option><option value="dl">Сначала старые</option><option value="na">Название А-Я</option><option value="nz">Название Я-А</option><option value="rh">Рейтинг (выс.)</option><option value="rl">Рейтинг (низ.)</option>'''
    filter_bar = '''<div class="gf"><span class="gl">Коллекция:</span><select class="gs" onchange="af()" id="cs"><option value="">Все</option>''' + coll_opts + '''</select>
<span class="gl">Жанр:</span><select class="gs" onchange="af()" id="gs"><option value="">Все</option></select>
<span class="gl">Дата:</span><select class="gs" onchange="af()" id="ds"><option value="0">Все</option><option value="7">Неделя</option><option value="14">2 недели</option><option value="30">Месяц</option><option value="60">2 месяца</option><option value="180">Полгода</option></select>
<span class="gl">Сорт:</span><select class="gs" onchange="af()" id="ss">''' + sort_opts + '''</select></div>'''

    for t in topics:
        magnet = t.get('magnet')
        if not magnet or magnet == '0':
            continue
        if str(t.get('topic_id', '')) in hidden_ids:
            continue
        prefer_imdb = is_world_topic(t)
        if prefer_imdb:
            rating = t.get('imdb_rating') or t.get('kp_rating') or '—'
            rating_label = 'IMDB' if t.get('imdb_rating') else 'КП' if t.get('kp_rating') else ''
        else:
            rating = t.get('kp_rating') or t.get('imdb_rating') or '—'
            rating_label = 'КП' if t.get('kp_rating') else 'IMDB' if t.get('imdb_rating') else ''
        rating_cls = ''
        if rating and rating != '—':
            r = float(rating)
            rating_cls = 'rh' if r >= 8 else 'rm' if r >= 7 else 'rl'
        if prefer_imdb and t.get('imdb_id'):
            rating_url = f"https://www.imdb.com/title/{t['imdb_id']}/"
        elif t.get('kp_id'):
            rating_url = f"https://www.kinopoisk.ru/film/{t['kp_id']}/"
        elif t.get('imdb_id'):
            rating_url = f"https://www.imdb.com/title/{t['imdb_id']}/"
        else:
            rating_url = '#'
        votes = t.get('kp_votes') or t.get('imdb_votes') or ''
        votes_str = f" ({votes})" if votes else ''
        votes_html = f'<span class="rv">{escape(votes_str)}</span>' if votes_str else ''
        votes_title = f' title="{votes} голосов"' if votes else ''
        trailer_q = urllib.parse.quote(f"{t['movie_title']} {t['movie_year']} official trailer")
        trailer_url = t.get('youtube_url') or f"https://www.youtube.com/results?search_query={trailer_q}"
        trailer_is_fallback = '/results?' in trailer_url or not t.get('youtube_url')
        trailer_cls = 'bt bt-warn' if trailer_is_fallback else 'bt'
        clean_t = escape(t['movie_title'].lower().strip())

        real_poster_missing = not has_real_poster(t)
        poster = display_poster_url(t)
        cast_str = t.get('cast', '') or ''
        raw_genre = t.get('genre', '') or ''
        hidden_source = f"{raw_genre} {t.get('title', '')}".lower()
        if any(h in hidden_source for h in HIDDEN_GENRES):
            continue
        genre = clean_and_translate_genre(raw_genre)
        cont = t.get('format') or ''
        cont_text = f"{cont} {t.get('title', '')}".lower()
        if 'mkv' in cont_text or 'matroska' in cont_text:
            container = 'mkv'
        elif 'mp4' in cont_text:
            container = 'mp4'
        elif 'avi' in cont_text or 'xvid' in cont_text or 'divx' in cont_text:
            container = 'avi'
        else:
            container = ''
        fmt_html = f'<span class="tile-format">Формат: {escape(cont)}</span>' if cont else ''
        poster_html = f'<div class="pc" data-yt="{escape(trailer_url)}" onclick="pt(this)"><img loading="lazy" decoding="async" data-src="{escape(poster)}" class="ps" alt=""><span class="pb">▶</span></div>'
        cast_html = f'<p class="ca">{escape(cast_str)}</p>' if cast_str else ''
        genre_html = f'<p class="gn">{escape(genre)}</p>' if genre else ''
        fmt_row = f'<p class="ff">Формат: {escape(cont)}</p>' if cont else ''
        esize = escape(t['size_str'])
        magnet = t.get('magnet', '')
        collection = escape(t.get('collection', 'nashe_kino'))
        movie_year = escape(str(t.get('movie_year') or ''))
        seeders = int(t.get('seeders') or 0)
        topic_title = escape(t.get('title', ''))
        date_ts = date_to_timestamp(t.get('date_str') or t.get('added_at'))
        missing = real_poster_missing or not t.get('kp_rating') or not t.get('youtube_url')
        enrich_btn = f'<button class="eb" data-tid="{escape(t["topic_id"])}" onclick="enrich(this)">◈</button>' if missing else ''

        watch_attrs = f'data-magnet="{escape(magnet)}" data-title="{clean_t}" data-year="{movie_year}" data-container="{escape(container)}" data-size-bytes="{int(t.get("size_bytes") or 0)}" data-seeders="{seeders}" data-topic-title="{topic_title}" data-collection="{collection}"'

        rated_attr = '1' if t.get('kp_rating') or t.get('imdb_rating') else '0'

        listing_order = t.get('listing_order', 999)
        rows.append(f'''<tr data-date="{date_ts}" data-order="{listing_order}" data-tid="{escape(t['topic_id'])}" data-title="{clean_t}" data-year="{movie_year}" data-container="{escape(container)}" data-seeders="{seeders}" data-genre="{escape(genre.lower())}" data-collection="{collection}" data-rated="{rated_attr}">
<td><a href="{escape(t['topic_url'])}" class="tn" target="_blank">{escape(t['title'])}</a>
<div class="ml">
<span class="tg" onclick="td(this)">+</span>
<span class="un">{escape(t['author'])}</span>
<a href="{trailer_url}" onclick="window.open(this.href,'tr','width=960,height=540,menubar=no,toolbar=no,location=no');return false" class="{trailer_cls}">▶ Трейлер</a>
<a href="{rating_url}" target="_blank"{votes_title}><span class="rb {rating_cls}">{rating_label} {escape(str(rating))}{votes_html}</span></a>
{enrich_btn}
<span class="rmv" onclick="hm(this)">✕</span>
</div>
<div class="dtc" style="display:none"><div class="dc">{poster_html}<div class="dx">{genre_html}{fmt_row}<div class="rbi"><span class="rb {rating_cls}">{rating_label} {escape(str(rating))}{votes_html}</span></div>{cast_html}</div></div></div>
</td>
<td>{esize}</td>
<td>{escape(t['date_str'])}</td>
<td data-s="{rating or '0'}"><a href="{escape(magnet)}" class="bm" title="Скачать kino">🧲</a><button class="wb" {watch_attrs} onclick="watch(this)">▶ Смотреть</button></td>
</tr>''')

        poster_card = f'<div class="pc" data-yt="{escape(trailer_url)}" onclick="pt(this)"><img loading="lazy" decoding="async" data-src="{escape(poster)}" class="tps" alt=""><span class="pb">▶</span></div>'
        cast_short = escape(cast_str)[:120] + '…' if len(cast_str) > 120 else escape(cast_str)

        tiles.append(f'''<div class="tile-card" data-date="{date_ts}" data-order="{listing_order}" data-tid="{escape(t['topic_id'])}" data-title="{clean_t}" data-year="{movie_year}" data-container="{escape(container)}" data-seeders="{seeders}" data-genre="{escape(genre.lower())}" data-size="{esize}" data-rating="{rating or '0'}" data-collection="{collection}" data-rated="{rated_attr}">
{poster_card}
<div class="tile-body">
<a href="{escape(t['topic_url'])}" class="tile-title" target="_blank">{escape(t['title'])}</a>
<div class="tile-info">
<span class="rb {rating_cls}">{rating_label} {escape(str(rating))}</span>
{'' if not genre else f'<span class="tile-genre">{escape(genre)}</span>'}
{fmt_html}
<span class="tile-size">{esize}</span>
</div>
{'' if not cast_short else f'<div class="tile-cast">{cast_short}</div>'}
<div class="tile-actions">
{enrich_btn}
<a href="{trailer_url}" onclick="window.open(this.href,'tr','width=960,height=540,menubar=no,toolbar=no,location=no');return false" class="{trailer_cls}">▶ Трейлер</a>
<button class="wb" {watch_attrs} onclick="watch(this)">▶ Смотреть</button>
<a href="{escape(magnet)}" class="bm" title="Скачать kino">🧲</a>
<a href="{rating_url}" target="_blank" class="tile-imdb">{rating_label}</a>
<span class="rmv" onclick="htm(this)">✕</span>
</div>
</div>
</div>''')

    with_r = sum(1 for t in topics if t.get('kp_rating') or t.get('imdb_rating'))

    html = f'''<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>LocaL-Kino</title>
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;background:#fff;color:#1a1a1a;padding:20px}}
.c{{max-width:1200px;margin:0 auto}}
h1{{font-size:28px;margin-bottom:4px;color:#1a1a1a}}
.sub{{color:#666;margin-bottom:20px;font-size:13px}}
.sub a{{color:#1a73e8}}
.sub .tv{{display:inline-block;padding:2px 8px;font-size:11px;font-weight:600;color:#fff;background:#1a73e8;border-radius:4px;cursor:pointer;user-select:none;margin-left:8px;border:none;vertical-align:middle}}
.sub .tv:hover{{background:#1557b0}}
table{{width:100%;border-collapse:collapse}}
th{{position:sticky;top:0;background:#f5f5f5;padding:12px 16px;text-align:left;font-size:12px;text-transform:uppercase;letter-spacing:.5px;color:#666;border-bottom:2px solid #e0e0e0;cursor:pointer;user-select:none}}
th:hover{{background:#e8e8e8}}
th .ar{{font-size:10px;margin-left:4px}}
tr{{border-bottom:1px solid #eee;transition:background .15s}}
tr:hover{{background:#f9f9f9}}
td{{padding:12px 16px;font-size:14px;vertical-align:middle}}
.tg{{display:inline-block;width:20px;height:20px;line-height:18px;text-align:center;font-size:14px;font-weight:700;color:#666;border:1px solid #ccc;border-radius:3px;cursor:pointer;vertical-align:middle;margin-right:8px;user-select:none;background:#f0f0f0;flex-shrink:0}}
.tg:hover{{background:#ddd;border-color:#999}}
.dtc{{border-top:1px solid #e0e0e0;margin-top:8px;padding-top:8px}}
.dc{{display:flex;gap:16px;align-items:flex-start}}
.ps{{max-width:360px;border-radius:4px;flex-shrink:0;display:block}}
.pc{{position:relative;display:inline-block;flex-shrink:0;cursor:pointer;line-height:0}}
.pb{{position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);font-size:64px;color:rgba(255,255,255,0.85);text-shadow:0 0 20px rgba(0,0,0,0.6);transition:color .2s,transform .2s;pointer-events:none}}
.pc:hover .pb{{color:#fff;transform:translate(-50%,-50%) scale(1.1)}}
.dx{{flex:1}}
.dx .rbi{{margin-bottom:8px}}
.gn{{font-size:36px;color:#888;margin-bottom:6px}}
.ff{{font-size:14px;color:#e67e22;font-weight:600;margin-bottom:6px}}
.ca{{font-size:39px;color:#555;line-height:1.5;margin:0}}
.tn{{color:#1a73e8;text-decoration:none;font-weight:600;display:block;margin-bottom:2px;line-height:1.4;font-size:28px}}
.tn:hover{{text-decoration:underline}}
.ml{{display:flex;gap:6px;align-items:center;flex-wrap:wrap;margin:4px 0 8px}}
.bt{{display:inline-block;padding:2px 8px;font-size:11px;font-weight:600;color:#fff;background:#da3633;border-radius:4px;text-decoration:none;white-space:nowrap}}
.bt:hover{{background:#b62324}}
.bt-warn{{background:#e67e22}}
.bt-warn:hover{{background:#d35400}}
.rb{{display:inline-block;padding:2px 8px;font-size:11px;font-weight:700;border-radius:4px;white-space:nowrap;text-decoration:none}}
.rbi .rb{{font-size:33px}}
.rh{{background:#f5c518;color:#000}}
.rm{{background:#b0881a;color:#fff}}
.rl{{background:#e0e0e0;color:#666}}
.rv{{color:#b8b8b8;font-weight:400}}
.un{{font-size:12px;color:#888}}
.rmv{{display:inline-block;width:18px;height:18px;line-height:16px;text-align:center;font-size:11px;font-weight:700;color:#fff;background:#da3633;border-radius:3px;cursor:pointer;user-select:none;flex-shrink:0;margin-left:6px}}
.rmv:hover{{background:#b62324}}
.bm{{font-size:16px;text-decoration:none;color:#333}}
.wb{{display:inline-block;padding:3px 10px;font-size:11px;font-weight:700;color:#fff;background:#e94560;border:none;border-radius:4px;cursor:pointer;white-space:nowrap;vertical-align:middle}}
.wb:hover{{background:#d63850}}
button.wb[data-container="avi"]{{background:#2563eb}}
button.wb[data-container="avi"]:hover{{background:#1d4ed8}}
.eb{{display:inline-block;padding:2px 6px;font-size:12px;font-weight:700;color:#fff;background:#7c4dff;border:none;border-radius:4px;cursor:pointer;white-space:nowrap;vertical-align:middle;line-height:1.4}}
.eb:hover{{background:#651fff}}
.eb:disabled{{opacity:.4;cursor:wait}}
.st{{margin:16px 0;padding:12px 16px;background:#f5f5f5;border-radius:6px;font-size:13px;color:#666;border:1px solid #e0e0e0}}
.tile-grid{{display:none;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:16px}}
.tile-card{{border:1px solid #e0e0e0;border-radius:8px;overflow:hidden;transition:box-shadow .2s}}
.tile-card:hover{{box-shadow:0 2px 12px rgba(0,0,0,.1)}}
.tile-card>.pc{{display:block;width:100%}}
.tps{{width:100%;display:block;border-radius:0}}
.tile-body{{padding:8px 10px}}
.tile-title{{font-size:20px;font-weight:600;color:#1a73e8;text-decoration:none;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;line-height:1.3;margin-bottom:4px}}
.tile-title:hover{{text-decoration:underline}}
.tile-info{{display:flex;gap:6px;align-items:center;flex-wrap:wrap;margin-bottom:4px}}
.tile-genre{{font-size:18px;color:#888}}
.tile-format{{font-size:18px;color:#e67e22;font-weight:600}}
.tile-size{{font-size:18px;color:#999}}
.tile-cast{{font-size:18px;color:#555;line-height:1.4;margin-bottom:4px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}
.tile-actions{{display:flex;gap:6px;align-items:center;flex-wrap:wrap;margin-top:4px}}
.tile-imdb{{font-size:18px;color:#555;text-decoration:none}}
.tile-imdb:hover{{text-decoration:underline}}
body.tile table{{display:none}}
body.tile .tile-grid{{display:grid}}
body.tile .st{{display:none}}
@media(max-width:768px){{body{{padding:10px}}h1{{font-size:22px}}.sub{{font-size:12px}}.tn{{font-size:20px}}.ca{{font-size:18px}}.gn{{font-size:18px}}.ps{{max-width:260px}}td{{padding:8px 10px;font-size:13px}}tr{{padding:6px 0}}.tile-title{{font-size:17px}}.tile-genre,.tile-size,.tile-format,.tile-cast,.tile-imdb{{font-size:14px}}.tile-grid{{grid-template-columns:1fr}}table,thead,tbody,tr,td,th{{display:block}}thead{{display:none}}}}
body.mobile{{padding:10px}}body.mobile h1{{font-size:22px}}body.mobile .sub{{font-size:12px}}body.mobile .tn{{font-size:20px}}body.mobile .ca{{font-size:18px}}body.mobile .gn{{font-size:18px}}body.mobile .ps{{max-width:260px}}body.mobile td{{padding:8px 10px;font-size:13px}}body.mobile tr{{padding:6px 0}}body.mobile .tile-title{{font-size:17px}}body.mobile .tile-genre,body.mobile .tile-size,body.mobile .tile-cast,body.mobile .tile-imdb{{font-size:14px}}body.mobile .tile-grid{{grid-template-columns:1fr}}body.mobile table,body.mobile thead,body.mobile tbody,body.mobile tr,body.mobile td,body.mobile th{{display:block}}body.mobile thead{{display:none}}body.tile.mobile table{{display:none}}
.gf{{display:flex;align-items:center;gap:6px;padding:6px 0;margin-bottom:8px;font-size:18px}}
.gl{{font-weight:600;color:#555;flex-shrink:0}}
.gs{{font-size:18px;padding:2px 6px;border:1px solid #ccc;border-radius:4px;max-width:200px}}
.hidden{{display:none!important}}
.po{{position:fixed;inset:0;background:rgba(0,0,0,.86);z-index:1000;display:flex;align-items:center;justify-content:center;padding:18px}}
.pw{{width:min(1100px,100%);position:relative}}
.pv{{width:100%;max-height:82vh;background:#000;border-radius:4px;display:block;position:relative;z-index:1}}
.px{{position:absolute;top:-42px;right:0;width:34px;height:34px;border:0;border-radius:4px;background:#e94560;color:#fff;font-size:20px;font-weight:700;cursor:pointer;z-index:20;pointer-events:auto}}
.pu{{position:fixed;top:114px;right:284px;height:42px;border:0;border-radius:5px;background:#1a73e8;color:#fff;font-size:16px;font-weight:700;cursor:pointer;padding:0 16px;z-index:2147483647;pointer-events:auto}}
.pu:active{{background:#0b57d0;transform:translateY(1px)}}
.pa{{position:fixed;top:114px;right:412px;height:42px;border:0;border-radius:5px;background:#2da44e;color:#fff;font-size:16px;font-weight:700;cursor:pointer;padding:0 16px;z-index:2147483647;pointer-events:auto}}
.pa:active{{background:#1f883d;transform:translateY(1px)}}
.pp{{position:fixed;top:114px;right:584px;height:42px;border:0;border-radius:5px;background:#e94560;color:#fff;font-size:16px;font-weight:800;cursor:pointer;padding:0 16px;z-index:2147483647;pointer-events:auto}}
.pp:active{{background:#d63850;transform:translateY(1px)}}
.pm{{margin-top:10px;color:#ddd;font-size:15px;text-align:center}}
.pe{{margin-top:6px;color:#ff8a8a;font-size:14px;text-align:center}}
</style>
</head>
<body>
<div class="c">
<div class="sub">
<span id="stats-line">• Всего: <strong id="stat-total">{len(topics)}</strong> kino
• С рейтингом: <strong id="stat-rated">{with_r}</strong></span>
<a href="/sync_order" class="tv" title="Синхронизировать порядок" style="font-size:14px;margin-left:8px;text-decoration:none;cursor:pointer;font-family:monospace">[sync]</a>
<a href="/recheck_trailers" class="tv" title="Проверить трейлеры" style="font-size:14px;margin-left:8px;text-decoration:none;cursor:pointer">🎬</a>
<span class="tv" onclick="tv()" id="tvb">Вид: плитка</span>
<span class="tv" onclick="md()" id="mdb">📱</span>
</div>
{filter_bar}

<table id="tbl">
<thead><tr>
<th onclick="st(0,'f')">Название <span class="ar"></span></th>
<th onclick="st(1,'f')">Размер <span class="ar"></span></th>
<th onclick="st(2,'n')">Дата <span class="ar"></span></th>
<th onclick="st(3,'n')">Рейтинг <span class="ar"></span></th>
</tr></thead>
<tbody>
{chr(10).join(rows)}
</tbody>
</table>

<div class="tile-grid" id="tile-grid">
{chr(10).join(tiles)}
</div>

<p class="st">✕ — скрыть фильм и все одноимённые. Кликните на заголовки для сортировки. Очистить скрытые: очистите localStorage.</p>
</div>
<div id="player-overlay" class="po hidden">
<div class="pw">
<button id="aac-button" type="button" class="pa" title="Совместимый звук AAC">AAC-звук</button>
<button id="sound-button" type="button" class="pu" onpointerdown="unmutePlayer(event)" onmousedown="unmutePlayer(event)" onclick="unmutePlayer(event)" title="Включить звук">Звук</button>
<button type="button" class="px" onclick="closePlayer()" title="Закрыть">×</button>
<video id="inline-player" class="pv" controls playsinline></video>
<button id="play-button" type="button" class="pp" title="Запустить воспроизведение">▶ Пуск</button>
<div id="player-status" class="pm"></div>
<div id="player-error" class="pe"></div>
</div>
</div>
<script>
var sd={{i:-1,d:1}};
function sortTiles(){{var tg=document.getElementById('tile-grid'),cards=Array.from(tg.children),a=sd.d;if(sd.i===0){{cards.sort(function(x,y){{return a*((x.getAttribute('data-title')||'').localeCompare(y.getAttribute('data-title')||''))}})}}else if(sd.i===1){{cards.sort(function(x,y){{return a*((x.getAttribute('data-size')||'').localeCompare(y.getAttribute('data-size')||''))}})}}else if(sd.i===3){{cards.sort(function(x,y){{return a*(parseFloat(x.getAttribute('data-rating')||'0')-parseFloat(y.getAttribute('data-rating')||'0'))}})}}else if(sd.i===4){{cards.sort(function(x,y){{return a*(parseFloat(x.getAttribute('data-order')||'999')-parseFloat(y.getAttribute('data-order')||'999'))}})}}else{{cards.sort(function(x,y){{return a*(parseFloat(x.getAttribute('data-date')||'0')-parseFloat(y.getAttribute('data-date')||'0'))}})}}cards.forEach(function(c){{tg.appendChild(c)}})}}
function st(c,t){{var tb=document.querySelector('#tbl tbody'),r=Array.from(tb.children),a=sd.i===c?sd.d*-1:1;
r.sort(function(x,y){{var va=x.children[c].getAttribute('data-s')||(t==='n'?x.getAttribute('data-date')||'0':x.children[c].textContent.trim()),vb=y.children[c].getAttribute('data-s')||(t==='n'?y.getAttribute('data-date')||'0':y.children[c].textContent.trim());if(t==='n'){{return a*(parseFloat(va)-parseFloat(vb))}}return a*va.localeCompare(vb)}});
r.forEach(function(r){{tb.appendChild(r)}});sd.i=c;sd.d=a;
document.querySelectorAll('th .ar').forEach(function(e){{e.textContent=''}});document.querySelectorAll('th')[c].querySelector('.ar').textContent=a>0?'▲':'▼';sortTiles()}}
function td(el){{var r=el.closest('td').querySelector('.dtc');if(!r)return;var on=r.style.display!=='none';if(on){{r.style.display='none';el.textContent='+';return}};r.querySelectorAll('img[data-src]').forEach(function(img){{img.src=img.getAttribute('data-src');img.removeAttribute('data-src')}});r.style.display='';el.textContent='−'}}
function pt(el){{var u=el.getAttribute('data-yt');if(!u)return;window.open(u,'tr','width=960,height=540,menubar=no,toolbar=no,location=no')}}
function hm(el){{var tr=el.closest('tr'),tid=tr.getAttribute('data-tid');if(!tid)return;fetch('/hide/'+tid,{{method:'POST'}}).then(function(){{location.reload()}}).catch(function(){{location.reload()}})}}
function htm(el){{var card=el.closest('.tile-card'),tid=card.getAttribute('data-tid');if(!tid)return;fetch('/hide/'+tid,{{method:'POST'}}).then(function(){{location.reload()}}).catch(function(){{location.reload()}})}}
function hideSaved(sel){{var h=JSON.parse(localStorage.getItem('ph')||'[]');[].forEach.call(document.querySelectorAll(sel),function(r){{if(h.indexOf(r.getAttribute('data-title'))!==-1)r.style.display='none'}})}}
function fh(){{hideSaved('#tbl tbody tr')}}
function fht(){{hideSaved('.tile-card')}}
var sx=/(?:\\bhorror\\b|\\b(?:sex|porn|xxx|erotic|adult|nsfw|onlyfans)\\b)/i;
function updateStats(){{var rows=Array.from(document.querySelectorAll('#tbl tbody tr')).filter(function(r){{return r.style.display!=='none'}}),total=document.getElementById('stat-total'),rated=document.getElementById('stat-rated');if(total)total.textContent=rows.length;if(rated)rated.textContent=rows.filter(function(r){{return r.getAttribute('data-rated')==='1'}}).length}}
(function(){{var dv=localStorage.getItem('dv'),sv=localStorage.getItem('sv');if(dv)document.getElementById('ds').value=dv;if(sv)document.getElementById('ss').value=sv;af();sortTiles();
var isTile=localStorage.getItem('tv')==='tile';if(isTile){{document.body.classList.add('tile');document.getElementById('tvb').textContent='Вид: список';sortTiles();document.querySelectorAll('#tile-grid img[data-src]').forEach(function(img){{img.src=img.getAttribute('data-src');img.removeAttribute('data-src')}})}}
var isMob=localStorage.getItem('mb')==='1';if(isMob){{document.body.classList.add('mobile');document.getElementById('mdb').textContent='🖥'}}}})()
function tv(){{var b=document.body;b.classList.toggle('tile');var isTile=b.classList.contains('tile');localStorage.setItem('tv',isTile?'tile':'list');document.getElementById('tvb').textContent=isTile?'Вид: список':'Вид: плитка';if(isTile){{sortTiles();document.querySelectorAll('#tile-grid img[data-src]').forEach(function(img){{img.src=img.getAttribute('data-src');img.removeAttribute('data-src')}})}}}}
function md(){{var b=document.body;b.classList.toggle('mobile');var isMob=b.classList.contains('mobile');localStorage.setItem('mb',isMob?'1':'0');document.getElementById('mdb').textContent=isMob?'🖥':'📱'}}
function bgf(){{var gs={{}};[].forEach.call(document.querySelectorAll('#tbl tbody tr,.tile-card'),function(r){{if(r.style.display==='none')return;var rg=(r.getAttribute('data-genre')||'').toLowerCase();rg.split(',').forEach(function(g){{g=g.trim();if(g)gs[g]=1}})}});var sel=document.getElementById('gs'),v=sel.value;sel.innerHTML='<option value=\\"\\">Все</option>';Object.keys(gs).sort().forEach(function(g){{var s=g.charAt(0).toUpperCase()+g.slice(1);sel.innerHTML+='<option value=\\"'+g+'\\">'+s+'</option>'}});sel.value=v}}
var playerPoll=null;
var currentHash='';
var currentSession='';
var currentWatchStartedAt=0;
function fmtBytes(b){{if(!b)return'0 B';var u=['B','KB','MB','GB','TB'],i=0,v=b;while(v>=1024&&i<u.length-1){{v/=1024;i++}}return v.toFixed(1)+' '+u[i]}}
function hashFromMagnet(m){{var x=(m||'').match(/btih:([A-Fa-f0-9]{{40}})/i);return x?x[1].toLowerCase():''}}
function isStreamContainer(c){{c=(c||'').toLowerCase();return c==='mkv'||c==='mp4'}}
function findStreamReplacement(el){{var src={{magnet:el.getAttribute('data-magnet')||'',title:el.getAttribute('data-title')||'',year:el.getAttribute('data-year')||'',container:(el.getAttribute('data-container')||'').toLowerCase(),collection:el.getAttribute('data-collection')||'',hash:hashFromMagnet(el.getAttribute('data-magnet')||'')}};if(src.container!=='avi'||!src.title)return null;var best=null,bestScore=-1;[].forEach.call(document.querySelectorAll('button.wb[data-magnet]'),function(b){{var m=b.getAttribute('data-magnet')||'',h=hashFromMagnet(m),c=(b.getAttribute('data-container')||'').toLowerCase(),title=b.getAttribute('data-title')||'',year=b.getAttribute('data-year')||'';if(!m||!h||h===src.hash||!isStreamContainer(c)||title!==src.title)return;if(src.year&&year!==src.year)return;var score=parseInt(b.getAttribute('data-seeders')||'0',10)||0;if(c==='mp4')score+=5;if(b.getAttribute('data-collection')===src.collection)score+=3;if(score>bestScore){{bestScore=score;best=b}}}});if(!best)return null;return {{magnet:best.getAttribute('data-magnet')||'',container:(best.getAttribute('data-container')||'').toUpperCase(),title:best.getAttribute('data-topic-title')||best.getAttribute('data-title')||'',seeders:parseInt(best.getAttribute('data-seeders')||'0',10)||0}}}}
async function findExternalStreamReplacement(el){{var payload={{title:el.getAttribute('data-title')||'',raw_title:el.getAttribute('data-topic-title')||'',year:el.getAttribute('data-year')||'',container:(el.getAttribute('data-container')||'').toLowerCase(),size_bytes:parseInt(el.getAttribute('data-size-bytes')||'0',10)||0,topic_id:(el.closest('[data-tid]')||{{}}).getAttribute?el.closest('[data-tid]').getAttribute('data-tid'):''}};try{{var ctl=window.AbortController?new AbortController():null,timer=ctl?setTimeout(function(){{ctl.abort()}},22000):null;var r=await fetch('/stream_replacement',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify(payload),signal:ctl?ctl.signal:undefined}});if(timer)clearTimeout(timer);if(!r.ok)return null;var d=await r.json().catch(function(){{return {{}}}});return d.found&&d.replacement&&d.replacement.magnet?d.replacement:null}}catch(_e){{return null}}}}
function newSession(h){{return h+'-'+Date.now()+'-'+Math.random().toString(36).slice(2)}}
function streamUrl(kind,h){{var sid=encodeURIComponent(currentSession||'');return '/'+kind+'/'+h+(sid?'?sid='+sid:'')}}
function stopCurrentSession(){{if(!currentSession&&!currentHash)return;var sid=currentSession,h=currentHash;currentSession='';currentHash='';var payload=JSON.stringify({{sid:sid,hash:h}});try{{if(navigator.sendBeacon){{var blob=new Blob([payload],{{type:'application/json'}});navigator.sendBeacon('/stop_session',blob);return}}}}catch(_e){{}}fetch('/stop_session',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:payload,keepalive:true}}).catch(function(){{}})}}
function unmutePlayer(ev){{if(ev){{ev.preventDefault();ev.stopPropagation()}}var p=document.getElementById('inline-player'),s=document.getElementById('player-status'),b=document.getElementById('sound-button');p.muted=false;p.defaultMuted=false;p.removeAttribute('muted');if(b)b.textContent='Звук включён';s.textContent='Звук включён';p.play().catch(function(){{s.textContent='Нажмите ▶ в плеере для запуска со звуком'}})}}
function playPlayer(ev){{if(ev){{ev.preventDefault();ev.stopPropagation()}}var p=document.getElementById('inline-player'),s=document.getElementById('player-status');p.muted=false;p.defaultMuted=false;p.removeAttribute('muted');p.play().then(function(){{s.textContent='Воспроизведение запущено'}}).catch(function(err){{s.textContent='Не удалось запустить: '+(err&&err.name?err.name:'ошибка')}})}}
function useAacAudio(ev){{if(ev){{ev.preventDefault();ev.stopPropagation()}}if(!currentHash)return;var p=document.getElementById('inline-player'),s=document.getElementById('player-status'),b=document.getElementById('aac-button');if(p.dataset.mode==='aac')return;p.dataset.mode='aac';p.pause();p.muted=false;p.defaultMuted=false;p.removeAttribute('muted');p.src=streamUrl('transcode',currentHash);if(b)b.textContent='AAC включён';s.textContent='Запускаю совместимый AAC-звук...';p.play().catch(function(){{s.textContent='Нажмите ▶ в плеере для запуска AAC-звука'}})}}
function closePlayer(){{if(playerPoll){{clearInterval(playerPoll);playerPoll=null}}var p=document.getElementById('inline-player');p.pause();p.removeAttribute('src');p.load();stopCurrentSession();document.getElementById('player-overlay').classList.add('hidden')}}
function startStream(p, h) {{p.dataset.mode='stream';p.muted=true;p.src=streamUrl('stream',h);p.play().then(function(){{}}).catch(function(){{document.getElementById('player-status').textContent='Нажмите ▶ в плеере для запуска'}})}}
function startTranscode(p, h) {{var s=document.getElementById('player-status');s.textContent='Перекодирование AVI в MP4...';p.dataset.mode='aac';p.muted=true;p.src=streamUrl('transcode',h);p.play().then(function(){{s.textContent='Воспроизведение запущено'}}).catch(function(){{s.textContent='Нажмите ▶ в плеере для запуска'}})}}
function stalledText(d){{var elapsed=currentWatchStartedAt?Math.floor((Date.now()-currentWatchStartedAt)/1000):0;if(elapsed>=120&&!(d.downloaded||0))return'Торрент не грузится: за 2 минуты нет входящей загрузки. peers '+(d.num_peers||0);return ''}}
function pollPlayer(h){{var s=document.getElementById('player-status'),p=document.getElementById('inline-player');if(playerPoll)clearInterval(playerPoll);playerPoll=setInterval(async function(){{try{{var r=await fetch('/status/'+h);if(!r.ok){{s.textContent='Ожидание добавления...';return}}var d=await r.json();var stalled=stalledText(d);if(stalled){{s.textContent=stalled;return}}if(d.state==='pending'){{s.textContent='Получаю метаданные... '+fmtBytes(d.download_rate)+'/с · peers '+(d.num_peers||0);return}}if(d.state==='checking_files'){{s.textContent='Проверяю уже скачанные данные... '+fmtBytes(d.total)+' · peers '+(d.num_peers||0);return}}var pct=Math.round((d.progress||0)*1000)/10;s.textContent=(d.ready?'Видео готово, запускаю...':'Буферизация...')+' '+pct+'% · '+fmtBytes(d.downloaded)+' / '+fmtBytes(d.total)+' · '+fmtBytes(d.download_rate)+'/с · peers '+(d.num_peers||0);if(d.ready){{clearInterval(playerPoll);playerPoll=null;if(d.format==='avi'){{startTranscode(p,h)}}else{{startStream(p,h)}}}}}}catch(_e){{s.textContent='Нет связи с сервером'}}}},1500)}}
async function startWatchMagnet(m,statusText,asyncOnly){{var h=hashFromMagnet(m),s=document.getElementById('player-status'),e=document.getElementById('player-error');if(!m)return '';if(!h){{window.open(m);return ''}}currentHash=h;currentSession=newSession(h);currentWatchStartedAt=Date.now();s.textContent=statusText||'Запускаю поток...';pollPlayer(h);try{{var payload={{magnet:m}};if(asyncOnly)payload.async_only=true;var r=await fetch('/watch_sync',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify(payload)}});var d=await r.json().catch(function(){{return {{}}}});if(!r.ok||!d.info_hash){{e.textContent=d.error||'Не удалось добавить kino';return ''}}if(d.info_hash.toLowerCase()!==h){{h=d.info_hash;currentHash=h;currentSession=newSession(h);pollPlayer(h)}}s.textContent=d.async_mode?'Получаю метаданные...':'Буферизация...';return h}}catch(_err){{e.textContent='Ошибка соединения с сервером';return ''}}}}
async function watch(el){{var m=el.getAttribute('data-magnet'),container=(el.getAttribute('data-container')||'').toLowerCase(),replacement=findStreamReplacement(el);stopCurrentSession();var o=document.getElementById('player-overlay'),p=document.getElementById('inline-player'),s=document.getElementById('player-status'),e=document.getElementById('player-error'),b=document.getElementById('sound-button'),ab=document.getElementById('aac-button');o.classList.remove('hidden');p.dataset.mode='stream';if(b)b.textContent='Звук';if(ab)ab.textContent='AAC-звук';e.textContent='';if(replacement){{await startWatchMagnet(replacement.magnet,'Найден быстрый способ онлайн-просмотра, запускаю...',false);return}}if(container==='avi'){{var originalHash=hashFromMagnet(m);var originalSid='';var started=await startWatchMagnet(m,'Запускаю подготовку файла, параллельно ищу быстрый способ онлайн-просмотра...',true);originalSid=currentSession;try{{var _sr=await fetch('/status/'+started);if(_sr.ok){{var _sd=await _sr.json();if(_sd.progress>=1.0){{s.textContent='Файл уже загружен, запускаю поток...';if(playerPoll){{clearInterval(playerPoll);playerPoll=null}}startTranscode(p,started);return}}}}}}catch(_e){{}}findExternalStreamReplacement(el).then(async function(rep){{if(!rep){{if(currentHash===originalHash||currentHash===started)s.textContent='Быстрый онлайн-вариант не найден, продолжаю подготовку файла...';return}}if(currentHash!==originalHash&&currentHash!==started)return;var oldHash=currentHash,oldSid=currentSession||originalSid;s.textContent='Найден быстрый способ онлайн-просмотра, переключаю...';try{{await fetch('/stop_session',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{sid:oldSid,hash:oldHash}})}})}}catch(_e){{}}currentHash='';currentSession='';await startWatchMagnet(rep.magnet,'Найден быстрый способ онлайн-просмотра, запускаю...',false)}});return}}await startWatchMagnet(m,'Запускаю поток...',false)}}
function ac(){{af()}}
function af(){{var d=document.getElementById('ds').value,g=document.getElementById('gs').value,c=document.getElementById('cs').value,s=document.getElementById('ss').value,h=JSON.parse(localStorage.getItem('ph')||'[]');localStorage.setItem('dv',d);localStorage.setItem('sv',s);var n=Date.now()/1000,cut=d>0?n-d*86400:0;
[].forEach.call(document.querySelectorAll('#tbl tbody tr,.tile-card'),function(r){{var show=true,dt=parseFloat(r.getAttribute('data-date')||'0'),rg=(r.getAttribute('data-genre')||'').toLowerCase(),t=r.getAttribute('data-title')||'';if(c&&r.getAttribute('data-collection')!==c)show=false;if(show&&cut&&dt<cut)show=false;if(show&&g&&rg.indexOf(g)===-1)show=false;if(show&&(sx.test(rg)||sx.test(t)))show=false;if(show&&h.indexOf(t)!==-1)show=false;r.style.display=show?'':'none'}});
bgf();updateStats();if(s==='lo'){{sd.i=4;sd.d=1;sortTiles()}}else if(s==='na'){{sd.i=0;sd.d=1;sortTiles()}}else if(s==='nz'){{sd.i=0;sd.d=-1;sortTiles()}}else if(s==='rh'){{sd.i=3;sd.d=-1;sortTiles()}}else if(s==='rl'){{sd.i=3;sd.d=1;sortTiles()}}else if(s==='dh'||s==='s'){{sd.i=2;sd.d=-1;sortTiles()}}else if(s==='dl'){{sd.i=2;sd.d=1;sortTiles()}}}}
['pointerdown','mousedown','click'].forEach(function(n){{document.addEventListener(n,function(e){{if(e.target&&e.target.id==='sound-button')unmutePlayer(e)}},true)}});
document.addEventListener('click',function(e){{if(e.target&&e.target.id==='aac-button')useAacAudio(e)}},true);
['pointerdown','mousedown','click'].forEach(function(n){{document.addEventListener(n,function(e){{if(e.target&&e.target.id==='play-button')playPlayer(e)}},true)}});
document.getElementById('inline-player').addEventListener('pointerdown',function(){{this.muted=false;this.defaultMuted=false;this.removeAttribute('muted')}});
document.getElementById('inline-player').addEventListener('playing',function(){{document.getElementById('player-status').textContent='Воспроизведение запущено'}});
window.addEventListener('beforeunload',stopCurrentSession);
async function enrich(el){{var tid=el.getAttribute('data-tid');if(!tid)return;el.disabled=true;el.textContent='◈';fetch('/enrich/'+tid,{{method:'POST'}}).catch(function(){{}});var iv=setInterval(async function(){{try{{var r=await fetch('/enrich/status/'+tid);if(!r.ok)return;var d=await r.json();if(d.status==='done'){{clearInterval(iv);el.textContent='✓';el.disabled=false;setTimeout(function(){{location.reload()}},1500)}}else if(d.status==='in_progress'){{el.textContent='◈'}}else if(d.status==='queued'){{el.textContent='⌛'}}else if(d.status&&d.status.startsWith('error')){{clearInterval(iv);el.textContent='✗';el.disabled=false;console.error('Enrich error:',d.status)}}}}catch(_e){{}}}},2000)}}
</script>
</body>
</html>'''
    return html


def enrich_topic(topic, force_poster_retry=False, include_trailer=True):
    """Enrich a single topic dict with missing data (poster, magnet, ratings, trailers)."""
    if topic.get('_sanitized') or topic.get('imdb_id') == '0':
        return topic
    title = topic.get('orig_title') or topic['movie_title']
    russian_title = topic['movie_title']
    year = topic['movie_year']
    raw_name = topic['title']
    if not title:
        return topic
    kp_cache = load_json(KP_SEARCH_CACHE) or {}
    retry_poster = (
        force_poster_retry
        or should_retry_poster(topic)
        or should_try_external_poster_fallback(topic)
    )

    if not has_real_poster(topic) and retry_poster:
        localize_existing_poster(topic)

    if not has_real_poster(topic):
        resolve_existing_local_poster(topic)

    if not topic.get('magnet') or topic.get('_magnet_failed'):
        try:
            html = get_topic_html(topic['topic_id'], topic['topic_url'], timeout=10)
            if html:
                data = parse_topic_for_magnet(html)
                if data.get('magnet'):
                    topic['magnet'] = data['magnet']
                    topic.pop('_magnet_failed', None)
                    if data.get('poster') and not has_real_poster(topic) and retry_poster:
                        set_local_poster_from_url(topic, data['poster'])
                    if data.get('imdb') and not topic.get('imdb_id'):
                        topic['imdb_id'] = data['imdb']
                    if data.get('format') and not topic.get('format'):
                        topic['format'] = data['format']
        except Exception:
            topic['_magnet_failed'] = True

    if (not has_real_poster(topic) and retry_poster) or not topic.get('format'):
        try:
            html = get_topic_html(topic['topic_id'], topic['topic_url'], timeout=10)
            if html:
                data = parse_topic_for_magnet(html)
                if data.get('poster') and not has_real_poster(topic) and retry_poster:
                    set_local_poster_from_url(topic, data['poster'])
                if data.get('format') and not topic.get('format'):
                    topic['format'] = data['format']
        except Exception:
            pass

    if is_world_topic(topic):
        try:
            html = get_topic_html(topic['topic_id'], topic['topic_url'], timeout=10)
            if html:
                imdb = parse_piratebay_detail(html)
                if imdb:
                    old_imdb = topic.get('imdb_id')
                    topic['imdb_id'] = imdb
                    if imdb != old_imdb:
                        topic['poster_url'] = ''
                        topic.pop('_poster_failed', None)
                if topic.get('source') == 'piratebay' and not topic.get('format'):
                    fmt = fetch_piratebay_format(topic['topic_id'], topic['topic_url'], timeout=10)
                    if fmt:
                        topic['format'] = fmt
        except Exception:
            pass

    if not topic.get('imdb_id'):
        result = search_imdb(title, year)
        if result is None:
            result = search_imdb_deep(raw_name)
        if result:
            if isinstance(result, str):
                topic['imdb_id'] = result
            else:
                topic['imdb_id'] = result.get('id')
                if not has_real_poster(topic) and retry_poster and result.get('poster'):
                    local_url = download_poster(topic['imdb_id'], result['poster'])
                    if local_url:
                        topic['poster_url'] = local_url
                        clear_poster_failed(topic)
                topic['cast'] = result.get('cast', '')

    imdb_id = topic.get('imdb_id')
    if imdb_id:
        needs_genre = not topic.get('genre') or is_listing_category_genre(topic.get('genre'))
        if needs_genre:
            bdata = load_basics({imdb_id}).get(imdb_id)
            genre = bdata.get('genres', '') if isinstance(bdata, dict) else ''
            if genre:
                topic['genre'] = clean_and_translate_genre(genre)
        if not topic.get('imdb_rating'):
            rdata = load_ratings({imdb_id}).get(imdb_id)
            if isinstance(rdata, dict):
                topic['imdb_rating'] = rdata.get('rating')
                topic['imdb_votes'] = rdata.get('votes', '')
        if needs_genre or not topic.get('imdb_rating'):
            rating_data = fetch_imdb_rating(imdb_id)
            if needs_genre and rating_data.get('genres'):
                topic['genre'] = clean_and_translate_genre(rating_data['genres'])
            if not topic.get('imdb_rating') and rating_data.get('rating'):
                topic['imdb_rating'] = rating_data['rating']
                topic['imdb_votes'] = rating_data.get('votes', '')
            if not has_real_poster(topic) and retry_poster and rating_data.get('poster'):
                local_url = download_poster(imdb_id, rating_data['poster'])
                if local_url:
                    topic['poster_url'] = local_url
                    clear_poster_failed(topic)

    cache_key = f"{title}|{year}".lower()
    kp_key = f"{russian_title}|{year}".lower()
    needs_kp = not topic.get('kp_rating')
    is_world = is_world_topic(topic)
    if is_world and topic.get('kp_id'):
        needs_kp = True
    if needs_kp and russian_title:
        if kp_key in kp_cache and kp_cache[kp_key] is not None:
            result = kp_cache[kp_key]
        else:
            result = search_kinopoisk(russian_title, year)
            kp_cache[kp_key] = result
            save_json(KP_SEARCH_CACHE, kp_cache)
        if result:
            old_kp_id = topic.get('kp_id')
            topic['kp_id'] = result['kp_id']
            topic['kp_rating'] = result['kp_rating']
            topic['kp_votes'] = result['kp_votes']
            if old_kp_id != result['kp_id']:
                topic.pop('_poster_failed_at', None)
                if topic.get('poster_url', '').startswith('data/posters/kp_'):
                    topic['poster_url'] = ''
        elif is_world:
            topic.pop('kp_id', None)
            topic.pop('kp_rating', None)
            topic.pop('kp_votes', None)
            if topic.get('poster_url', '').startswith('data/posters/kp_'):
                topic['poster_url'] = ''
        if is_world:
            topic['_kp_validated'] = True
            if not topic.get('kp_id'):
                topic.pop('_poster_failed_at', None)
                topic['_kp_retried'] = True

    if not has_real_poster(topic) and retry_poster and topic.get('kp_id'):
        local_url = download_kinopoisk_poster(topic['kp_id'])
        if local_url:
            topic['poster_url'] = local_url
            clear_poster_failed(topic)

    if not has_real_poster(topic) and retry_poster:
        mark_poster_failed(topic)

    if include_trailer and not topic.get('youtube_url'):
        yt_cache = load_json(YOUTUBE_CACHE) or {}
        cached_url = yt_cache.get(cache_key)
        if cached_url and (is_verified_world_youtube_trailer(SESSION, cached_url, title, year) if is_world_topic(topic) else _validate_youtube_url(cached_url, title, year)):
            topic['youtube_url'] = cached_url
        else:
            if cached_url:
                yt_cache[cache_key] = None
                save_json(YOUTUBE_CACHE, yt_cache)
            yt_url = resolve_topic_trailer_url(topic)
            topic['youtube_url'] = yt_url
            yt_cache[cache_key] = yt_url
            save_json(YOUTUBE_CACHE, yt_cache)

    return topic


def load_hidden_topic_ids() -> set[str]:
    try:
        data = json.loads(open(HIDDEN_TOPICS_FILE, encoding='utf-8').read())
        return set(str(t) for t in data if t)
    except (FileNotFoundError, json.JSONDecodeError):
        return set()


def save_hidden_topic_ids(ids: set[str]):
    atomic_write_json(HIDDEN_TOPICS_FILE, sorted(ids))


def add_hidden_topic(topic_id: str):
    ids = load_hidden_topic_ids()
    ids.add(str(topic_id))
    save_hidden_topic_ids(ids)


def remove_hidden_topic(topic_id: str):
    ids = load_hidden_topic_ids()
    ids.discard(str(topic_id))
    save_hidden_topic_ids(ids)


def load_collection_listing(collection, coll_info, topics_limit):
    all_topics = []
    listing_errors = 0
    page1_ok = False
    page1_used_cache = False
    skip_ids: set[str] = set()
    base_url = coll_info['url']
    source = coll_info.get('source', 'rutracker')

    if is_world_source(source):
        listing_cache_path = os.path.join(TOPIC_CACHE_DIR, f'{collection}_p0.html')
        page_topics = []
        world_label = coll_info.get('name', collection)
        print(f"  Страница 1 ({world_label})...", end=' ', flush=True)
        last_error = None
        for attempt in range(1, MAX_RETRY + 1):
            try:
                r = SESSION.get(base_url, timeout=30)
                r.raise_for_status()
                raw = r.content
                html = raw.decode(r.encoding or 'utf-8', errors='replace')
                current_hash = world_page_hash(html)
                cached_hash = load_world_page_hash(collection)
                if cached_hash and current_hash == cached_hash and listing_cache_is_valid(listing_cache_path):
                    with open(listing_cache_path, 'rb') as f:
                        raw = f.read()
                    html = raw.decode('utf-8', errors='replace')
                page_topics = parse_world_page(html, collection, source, clean_title, parse_size, now_text, info_hash_from_magnet, detect_format_from_text)
                page_topics = deduplicate_world_topics(page_topics)
                if page_topics:
                    page1_ok = True
                    os.makedirs(TOPIC_CACHE_DIR, exist_ok=True)
                    with open(listing_cache_path, 'wb') as f:
                        f.write(raw)
                    save_world_page_hash(collection, html)
                    save_world_legacy_page_cache(source, raw, html)
                break
            except Exception as e:
                last_error = e
                if attempt < MAX_RETRY:
                    print(f"\n  попытка {attempt}/{MAX_RETRY}: {e}; жду 2с", flush=True)
                    time.sleep(2)
        else:
            listing_errors += 1
            cache_used, raw = load_world_listing_cache_bytes(collection, source)
            if raw:
                html = raw.decode('utf-8', errors='replace')
                page_topics = parse_world_page(html, collection, source, clean_title, parse_size, now_text, info_hash_from_magnet, detect_format_from_text)
                page_topics = deduplicate_world_topics(page_topics)
                if page_topics:
                    page1_ok = True
                    page1_used_cache = True
                print(f"ошибка: {last_error}; используем кеш", end=' ', flush=True)
            else:
                page_topics = load_legacy_world_torrents(collection, source)
                if page_topics:
                    page1_ok = True
                    page1_used_cache = True
                    print(f"ошибка: {last_error}; используем legacy cache", end=' ', flush=True)
                else:
                    print(f"ошибка: {last_error}; свежего кеша нет")
        all_topics.extend(page_topics)
        if is_world_source(source):
            print(f"{len(page_topics)} тем (загружены все)")
        elif topics_limit and len(all_topics) >= topics_limit:
            all_topics = all_topics[:topics_limit]
            print(f"{len(page_topics)} тем; берём {topics_limit} (MAX_TOPICS)")
        else:
            print(f"{len(page_topics)} тем")
        return all_topics, listing_errors, page1_ok, page1_used_cache, skip_ids

    m_fid = re.search(r'f=(\d+)', base_url)
    forum_id = m_fid.group(1) if m_fid else collection
    for page in range(1):
        start = page * PAGE_SIZE
        url = base_url if start == 0 else f"{base_url}&start={start}"
        listing_cache_path = os.path.join(TOPIC_CACHE_DIR, f'f{forum_id}_p{page}.html')
        page_topics = []
        print(f"  Страница {page + 1} (start={start})...", end=' ', flush=True)
        last_error = None
        for attempt in range(1, MAX_RETRY + 1):
            try:
                r = SESSION.get(url, timeout=30)
                r.raise_for_status()
                raw = r.content
                html = raw.decode('cp1251', errors='replace')
                skip_top = COLLECTIONS.get(collection, {}).get('skip_topics', 0)
                page_topics = parse_forum_page(html, collection=collection, skip_topics=skip_top)
                if page == 0 and skip_top:
                    all_page = parse_forum_page(html, collection=collection, skip_topics=0)
                    skip_ids.update(t['topic_id'] for t in all_page[:skip_top])
                if page_topics:
                    if page == 0:
                        page1_ok = True
                    os.makedirs(TOPIC_CACHE_DIR, exist_ok=True)
                    with open(listing_cache_path, 'wb') as f:
                        f.write(raw)
                break
            except Exception as e:
                last_error = e
                if attempt < MAX_RETRY:
                    print(f"\n  попытка {attempt}/{MAX_RETRY}: {e}; жду 2с", flush=True)
                    time.sleep(2)
        else:
            listing_errors += 1
            if listing_cache_is_valid(listing_cache_path):
                with open(listing_cache_path, 'rb') as f:
                    raw = f.read()
                html = raw.decode('cp1251', errors='replace')
                skip_top = COLLECTIONS.get(collection, {}).get('skip_topics', 0)
                page_topics = parse_forum_page(html, collection=collection, skip_topics=skip_top)
                if page == 0 and skip_top:
                    all_page = parse_forum_page(html, collection=collection, skip_topics=0)
                    skip_ids.update(t['topic_id'] for t in all_page[:skip_top])
                if page == 0 and page_topics:
                    page1_ok = True
                    page1_used_cache = True
                print(f"ошибка: {last_error}; используем кеш", end=' ', flush=True)
            else:
                print(f"ошибка: {last_error}; свежего кеша нет")
                continue
        print(f"{len(page_topics)} тем")
        all_topics.extend(page_topics)
        if topics_limit and len(all_topics) >= topics_limit:
            all_topics = all_topics[:topics_limit]
            print(f"  Берём {topics_limit} тем (MAX_TOPICS)")
            break
        time.sleep(0.5)
    return all_topics, listing_errors, page1_ok, page1_used_cache, skip_ids


def main():
    refresh = '--refresh' in sys.argv
    quick = '--quick' in sys.argv
    fast = '--fast' in sys.argv
    topics_limit = MAX_TOPICS
    collection_arg = None
    refresh_failed = False
    collection_summaries = []

    limit = 0
    for arg in sys.argv[1:]:
        if arg.startswith('--collection='):
            collection_arg = arg.split('=')[1]
        elif arg.startswith('--limit='):
            limit = int(arg.split('=')[1])
        elif arg == '--limit':
            idx = sys.argv.index(arg)
            if idx + 1 < len(sys.argv) and not sys.argv[idx + 1].startswith('--'):
                limit = int(sys.argv[idx + 1])

    if collection_arg and collection_arg not in COLLECTIONS:
        print(f"Неизвестная коллекция '{collection_arg}'. Доступны: {', '.join(COLLECTIONS.keys())}")
        sys.exit(1)

    if refresh:
        topics_limit = MAX_TOPICS

    if quick:
        topics_limit = 0
    if refresh:
        topics_limit = MAX_TOPICS

    # Determine which collections to process
    if not refresh and os.path.exists(TORRENTS_CACHE):
        collections_to_process = []
    elif collection_arg:
        collections_to_process = [collection_arg]
    else:
        collections_to_process = list(COLLECTIONS.keys())

    if not collections_to_process:
        print("Загружаю кеш...")
        topics = load_json(TORRENTS_CACHE) or []
        for i, t in enumerate(topics):
            if t.get('listing_order') is None:
                t['listing_order'] = i
        topics, bootstrapped_world = bootstrap_missing_world_collections(topics)
        if bootstrapped_world:
            print(f"  Подхвачены world-кеши: {len(bootstrapped_world)}")
            save_json(TORRENTS_CACHE, topics)
        cleaned_topics = clean_catalog_topics(topics)
        if len(cleaned_topics) != len(topics):
            topics = cleaned_topics
            save_json(TORRENTS_CACHE, topics)
        changed = False
        for t in topics:
            if not t.get('_sanitized') and is_forbidden_topic(t):
                sanitize_topic(t)
                changed = True
                print(f"  {t.get('movie_title','')}: скрыто (кеш)")
        if changed:
            save_json(TORRENTS_CACHE, topics)

        hidden_ids = load_hidden_topic_ids()
        hidden_changed = False
        for t in topics:
            tid = str(t.get('topic_id', ''))
            if t.get('_sanitized') and tid not in hidden_ids:
                hidden_ids.add(tid)
                hidden_changed = True
        if hidden_changed:
            save_hidden_topic_ids(hidden_ids)
            print(f"  Скрытые обновлены: {len(hidden_ids)}")

    else:
        topics = load_json(TORRENTS_CACHE) or []
        for i, t in enumerate(topics):
            if t.get('listing_order') is None:
                t['listing_order'] = i
        original_topics_snapshot = json.loads(json.dumps(topics, ensure_ascii=False))
        all_new_topics: list[dict] = []

        # === PHASE 1: Parallel listing parse for all collections ===
        def _parse_listing(collection, topics_limit):
            coll_info = COLLECTIONS[collection]
            return load_collection_listing(collection, coll_info, coll_info.get('max_topics', topics_limit))

        parse_results: dict[str, tuple] = {}
        if len(collections_to_process) > 1:
            print(f"\n--- Параллельный парсинг {len(collections_to_process)} коллекций ({WORKER_COUNT} потоков) ---")
            with ThreadPoolExecutor(max_workers=min(WORKER_COUNT, len(collections_to_process))) as executor:
                futures = {executor.submit(_parse_listing, c, topics_limit): c for c in collections_to_process}
                for future in as_completed(futures):
                    c = futures[future]
                    try:
                        parse_results[c] = future.result()
                    except Exception as e:
                        print(f"  ОШИБКА {c}: {e}")
                        parse_results[c] = ([], 999, False, False, set())
        else:
            for c in collections_to_process:
                parse_results[c] = _parse_listing(c, topics_limit)

        # === PHASE 2: Sequential merge + fetch for each collection ===
        for col_idx, collection in enumerate(collections_to_process):
            coll_info = COLLECTIONS[collection]
            print(f"\n{'='*60}")
            print(f"Коллекция ({col_idx+1}/{len(collections_to_process)}): {coll_info['name']}")
            print(f"{'='*60}")

            print(f"1. Данные получены (параллельный парсинг)")
            critical_collection_error = False
            all_topics, listing_errors, page1_ok, page1_used_cache, _skip_ids = parse_results[collection]

            print(f"\nВсего найдено тем: {len(all_topics)}")
            if refresh and (not page1_ok or not all_topics):
                print("  КРИТИЧНО: первая страница коллекции не получена; refresh коллекции невалиден")
                critical_collection_error = True

            if limit and len(all_topics) > limit:
                all_topics = all_topics[:limit]
                print(f"  (лимит --limit={limit} применён к свежему набору)")

            if not all_topics:
                print("  Новых тем нет для этой коллекции")
                collection_summaries.append({
                    'collection': collection,
                    'status': 'failed' if critical_collection_error else 'ok',
                    'listing_errors': listing_errors,
                    'fresh': 0,
                    'new': 0,
                    'magnet_ok': 0,
                    'magnet_failed': 0,
                    'page1_cache': page1_used_cache,
                })
                if critical_collection_error:
                    refresh_failed = True
                continue

            print("2. Слияние с кешем...")
            cache_by_id = {t['topic_id']: t for t in topics if t.get('topic_id')}

            other = [t for t in topics if t.get('collection', 'nashe_kino') != collection]
            existing_current = [
                t for t in topics
                if t.get('collection', 'nashe_kino') == collection and t['topic_id'] not in _skip_ids
            ]
            existing_ids = {t['topic_id'] for t in existing_current}

            # Update listing_order for topics still on the current page
            fresh_by_id = {t['topic_id']: t for t in all_topics}
            fresh_order = {tid: t.get('listing_order', 0) for tid, t in fresh_by_id.items()}
            listing_update_fields = (
                'title', 'movie_title', 'orig_title', 'movie_year', 'genre', 'quality',
                'author', 'size_str', 'size_bytes', 'seeders', 'leechers', 'date_str',
                'topic_url', 'magnet', 'format', 'source',
            )
            for t in existing_current:
                fresh_topic = fresh_by_id.get(t['topic_id'])
                if fresh_topic:
                    t['listing_order'] = fresh_topic.get('listing_order', t.get('listing_order', 999))
                    for field in listing_update_fields:
                        if field in fresh_topic and fresh_topic.get(field) not in (None, ''):
                            t[field] = fresh_topic[field]

            # Add only genuinely new topics (not already in cache)
            new_current: list[dict] = []
            for t in all_topics:
                if t['topic_id'] not in existing_ids:
                    new_current.append(t)

            merged = other + existing_current + new_current
            print(f"  В кеше: {len(cache_by_id)}, свежих (всего): {len(all_topics)}, "
                  f"новых: {len(new_current)}, "
                  f"из других коллекций: {len(other)}, всего: {len(merged)}")

            source = coll_info.get('source', 'rutracker')
            if is_world_source(source):
                need_fetch = []
            else:
                need_fetch = [t for t in new_current if not t.get('_magnet_failed') and (not t.get('magnet') or not has_real_poster(t))]
            if need_fetch:
                print(f"\n3. Загрузка магнетов и постеров для {len(need_fetch)} новых тем...")
                magnet_stats = fetch_magnets(need_fetch)
                for t in need_fetch:
                    if is_forbidden_topic(t):
                        sanitize_topic(t)
                        print(f"  {t.get('movie_title','')}: запрещённая тема, скрыто")
            else:
                magnet_stats = {'total': 0, 'ok': 0, 'failed': 0}

            topics = clean_catalog_topics(merged)
            if is_world_source(source):
                legacy_world_topics = [t for t in topics if t.get('collection') == collection]
                if legacy_world_topics:
                    save_json(world_legacy_torrents_cache_path(source), legacy_world_topics)
            topic_ids = {t.get('topic_id') for t in topics}
            all_new_topics.extend(t for t in new_current if t.get('topic_id') in topic_ids)
            save_json(TORRENTS_CACHE, topics)
            if critical_collection_error:
                refresh_failed = True
            collection_summaries.append({
                'collection': collection,
                'status': 'failed' if critical_collection_error else 'ok',
                'listing_errors': listing_errors,
                'fresh': len(all_topics),
                'new': len(new_current),
                'magnet_ok': magnet_stats['ok'],
                'magnet_failed': magnet_stats['failed'],
                'page1_cache': page1_used_cache,
            })

        # Enrich all new topics across all collections in one batch
        if not all_new_topics:
            print("\nНовых тем нет, обогащение пропущено")
            skip_kp_posters = True
        elif fast:
            print(f"\nБыстрый refresh: обогащение {len(all_new_topics)} новых тем пропущено")
            skip_kp_posters = True
        else:
            skip_kp_posters = False

            # Fetch IMDB IDs from PirateBay detail pages before title-based search
            world_new = [t for t in all_new_topics if is_world_topic(t) and not t.get('imdb_id')]
            if world_new:
                print(f"\nЗагрузка IMDB со страниц world-источников для {len(world_new)} тем...")
                fetch_world_imdb_ids(world_new)

            print(f"\n{'='*60}")
            print("Обогащение новых тем за все коллекции")
            print(f"{'='*60}")

            print("\n4. Поиск IMDB ID...")
            search_imdb_ids(all_new_topics)

            print("\n5. Поиск Кинопоиск рейтинга...")
            search_kinopoisk_ids(all_new_topics)

            needed_ids = set()
            for t in all_new_topics:
                if t.get('imdb_id'):
                    needed_ids.add(t['imdb_id'])

            if needed_ids:
                print(f"\n6. Загрузка IMDB ratings для {len(needed_ids)} фильмов...")
                ratings = load_ratings(needed_ids)
                print(f"   Получено рейтингов: {sum(1 for k in needed_ids if k in ratings)}/{len(needed_ids)}")

                print(f"\n7. Загрузка IMDB basics (жанры) для {len(needed_ids)} фильмов...")
                basics = load_basics(needed_ids)
                print(f"   Получено жанров: {sum(1 for k in needed_ids if k in basics)}/{len(needed_ids)}")
            else:
                ratings = {}
                basics = {}

            print(f"\n8. Обогащение данных...")
            enrich(all_new_topics, ratings, basics)

            print(f"\n9. Проверка запрещённых тем (новые)...")
            for t in all_new_topics:
                if t.get('_sanitized'):
                    continue
                if is_forbidden_topic(t):
                    sanitize_topic(t)
                    print(f"  {t.get('movie_title','')}: запрещённая тема, скрыто")

        print("\n9. Проверка запрещённых тем (весь кеш)...")
        sanitized = 0
        for t in topics:
            if not t.get('_sanitized') and is_forbidden_topic(t):
                sanitize_topic(t)
                sanitized += 1
                print(f"  {t.get('movie_title','')}: запрещённая тема, скрыто")
        if sanitized:
            print(f"  Всего скрыто: {sanitized}")
        else:
            print("  Чисто")

        print("  Синхронизация hidden_topics с _sanitized...")
        hidden_ids = load_hidden_topic_ids()
        changed = False
        for t in topics:
            tid = str(t.get('topic_id', ''))
            if t.get('_sanitized') and tid not in hidden_ids:
                hidden_ids.add(tid)
                changed = True
                print(f"  {t.get('movie_title','')}: добавлен в скрытые")
        if changed:
            save_hidden_topic_ids(hidden_ids)
            print("  Скрытые обновлены")
        else:
            print("  ОК")

        if skip_kp_posters:
            mode = "нет новых тем" if not all_new_topics else "--fast"
            print(f"\n10. Кинопоиск постеры пропущены ({mode})")
        else:
            print("\n10. Кинопоиск постеры (для всех)...")
            kp_count = 0
            for t in topics:
                if not has_real_poster(t):
                    if resolve_existing_local_poster(t):
                        continue
                    if t.get('kp_id'):
                        kp_local = download_kinopoisk_poster(t['kp_id'])
                        if kp_local:
                            t['poster_url'] = kp_local
                            kp_count += 1
                            print(f"  {t.get('movie_title','')}: KP постер ✓")
            if kp_count:
                print(f"  Загружено: {kp_count}")
            else:
                print("  Нет нуждающихся")

        coll_order = {k: i for i, k in enumerate(COLLECTIONS.keys())}
        topics.sort(key=lambda t: (coll_order.get(t.get('collection', ''), 999), t.get('listing_order', 999)))

        save_json(TORRENTS_CACHE, topics)

    print("\nИсправление битых заголовков (весь кеш)...")
    fix_bad_topics(topics)
    
    print("\nВосстановление названий world-тем...")
    repair_world_titles(topics)

    # World deduplication: merge duplicates, keep best seeders, preserve enrich
    # Must run AFTER title repair so clean titles match correctly
    if any(is_world_topic(t) for t in topics):
        before = len(topics)
        topics = merge_world_duplicates(topics)
        removed = before - len(topics)
        if removed:
            print(f"  Мировые дубликаты: удалено {removed}, объединены enrich-поля")

    # Re-enrich world topics that lost enrich data due to title repair
    need_enrich = [t for t in topics if is_world_topic(t) and not t.get('_sanitized')
                   and (not t.get('imdb_id') or not t.get('kp_id') or not has_real_poster(t))]
    if need_enrich:
        print(f"\nПовторное обогащение {len(need_enrich)} world-тем (восстановленные названия)...")
        for topic in need_enrich:
            title = topic.get('movie_title', '?')
            print(f"  {title[:50]}...", end=' ', flush=True)
            enrich_topic(topic, force_poster_retry=True, include_trailer=False)
            if has_real_poster(topic):
                print("OK")
            else:
                print("poster ne najden")

    # Advanced dedup: merge topics with identical imdb_id or kp_id
    # Must run AFTER re-enrich so IDs are populated
    if any(is_world_topic(t) for t in topics):
        before = len(topics)
        topics = merge_world_by_id(topics)
        removed = before - len(topics)
        if removed:
            print(f"  Мировые дубликаты по ID: удалено {removed}, объединены enrich-поля")

    save_json(TORRENTS_CACHE, topics)

    hidden_ids = load_hidden_topic_ids()
    display_topics = filter_world_top(topics)
    print(f"\n{'='*60}")
    print(f"Генерация HTML ({len(display_topics)}/{len(topics)} фильмов, скрыто: {len(hidden_ids)})...")
    output = generate_html(display_topics, hidden_ids=hidden_ids)
    atomic_write_text(OUTPUT_FILE, output)
    print(f"Готово: {OUTPUT_FILE}")
    print(f"\nЗапусти: python stream_server.py")
    if collection_summaries:
        print("\nИтог refresh по коллекциям:")
        for s in collection_summaries:
            cache_note = ", page1 cache" if s.get('page1_cache') else ""
            print(
                f"  {s['collection']}: {s['status']}, "
                f"listing errors={s['listing_errors']}, fresh={s['fresh']}, new={s['new']}, "
                f"magnet ok={s['magnet_ok']}, magnet failed={s['magnet_failed']}{cache_note}"
            )
    if refresh_failed:
        if refresh:
            print("Откат данных к состоянию до refresh...")
            topics = original_topics_snapshot
            save_json(TORRENTS_CACHE, topics)
            output = generate_html(topics, hidden_ids=load_hidden_topic_ids())
            atomic_write_text(OUTPUT_FILE, output)
        print("\nREFRESH_FAILED: есть критические ошибки, staging нельзя публиковать")
        sys.exit(2)


if __name__ == '__main__':
    main()


