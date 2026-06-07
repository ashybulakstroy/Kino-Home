#!/usr/bin/env python3
"""Парсинг rutracker f=22, обогащение IMDB, генерация index-kino.html."""

import gzip
import io
import json
import os
import re
import sys
import time
import urllib.parse
from datetime import datetime, timedelta, timezone
from html import escape

import requests
from bs4 import BeautifulSoup

from project_io import atomic_write_json, atomic_write_text

COLLECTIONS = {
    'nashe_kino':          {'name': 'Наше кино',                       'url': 'https://rutracker.net/forum/viewforum.php?f=22'},
    'kino_sng':            {'name': 'Фильмы ближнего зарубежья',        'url': 'https://rutracker.net/forum/viewforum.php?f=2540'},
    'novinki_2026':        {'name': 'Новинки 2026',                    'url': 'https://rutracker.net/forum/viewforum.php?f=252'},
    'kino_sng_hd':         {'name': 'Фильмы Ближнего Зарубежья (HD Video)', 'url': 'https://rutracker.net/forum/viewforum.php?f=1247'},
}
FORUM_URL = COLLECTIONS['nashe_kino']['url']
TOPIC_URL_T = "https://rutracker.net/forum/viewtopic.php?t={}"
MAX_TOPICS = 100
PAGE_SIZE = 50

DATA_DIR = "data"
RATINGS_URL = "https://datasets.imdbws.com/title.ratings.tsv.gz"
BASICS_URL = "https://datasets.imdbws.com/title.basics.tsv.gz"
RATINGS_CACHE = os.path.join(DATA_DIR, "imdb_ratings_cache.json")
BASICS_CACHE = os.path.join(DATA_DIR, "imdb_basics_cache.json")
SEARCH_CACHE = os.path.join(DATA_DIR, "imdb_search_cache.json")
KP_SEARCH_CACHE = os.path.join(DATA_DIR, "kp_search_cache.json")
YOUTUBE_CACHE = os.path.join(DATA_DIR, "youtube_cache.json")
OUTPUT_FILE = os.path.join(DATA_DIR, "index-kino.html")
TORRENTS_CACHE = os.path.join(DATA_DIR, "torrents_data.json")
POSTERS_DIR = os.path.join(DATA_DIR, "posters")
POSTERS_URL = "data/posters"
TOPIC_CACHE_DIR = os.path.join(DATA_DIR, "topic_cache")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36",
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
}
SESSION = requests.Session()
SESSION.headers.update(HEADERS)

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
              'WEB-DL', 'WEBRip', 'BDRip', 'AVI', 'MKV', 'MP4', 'Россия', 'Украина',
              'США', 'Великобритания', 'Франция', 'Германия',
              'ужасы', 'эротика', 'порно', 'для взрослых', 'взрослый',
              'horror', 'erotica', 'porn', 'adult'}
HIDDEN_GENRES = {'ужасы', 'эротика', 'порно', 'для взрослых', 'взрослый',
                 'horror', 'erotica', 'porn', 'adult'}


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
        try:
            r = SESSION.get(topic_url, timeout=timeout)
            r.raise_for_status()
            raw = r.content
            with open(cache_path, 'wb') as f:
                f.write(raw)
        except Exception:
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
    m = re.match(r'([\d.]+)\s*(TB|GB|MB|KB)', text, re.I)
    if not m:
        return 0, text
    val = float(m.group(1))
    unit = m.group(2).upper()
    multipliers = {'KB': 1024, 'MB': 1024**2, 'GB': 1024**3, 'TB': 1024**4}
    return int(val * multipliers.get(unit, 1)), text


def parse_rutracker_title(raw):
    t = raw.strip()
    suffix_parts = re.split(r'\s+(?<!\w)(Original|Rus|Sub|Line|AVC|HEVC|MEGA|PRoFX|HDTV)(?!\w)\s*', t, maxsplit=1, flags=re.I)
    t = suffix_parts[0].strip()

    year = ''
    genre = ''
    quality = ''
    bracket_m = re.search(r'\[([^\]]*\d{4}[^\]]*)\]', t)
    if bracket_m:
        meta_text = bracket_m.group(1)
        parts = [p.strip() for p in meta_text.split(',')]
        genre_parts = []
        quality_parts = []
        quality_keywords = re.compile(
            r'^(WEB|BluRay|HDTV|DVDRip|HDRip|WEB-DL|WEBRip|BDRip|DVD|SATRip|TVRip|CamRip|'
            r'TS|TC|Screener|WEB-DLRip|BDRip-AVC|DVDRip-AVC|HDTVRip|'
            r'Betacam\s*SP|DVDRemux|DVDRip-AVC|BDRemux|WEBRip-AVC|'
            r'AVI|MKV|MP4|MPEG|TS|M2TS|BluRay\s*Remux|WEB\s*Rip|'
            r'BDRip-AVC|BDRemux)', re.I)
        for p in parts:
            p = p.strip()
            if re.match(r'^(19\d{2}|20\d{2})$', p):
                year = p
            elif quality_keywords.match(p):
                quality_parts.append(p)
            elif p in GENRE_STOP:
                continue
            else:
                genre_parts.append(p)
        genre = ', '.join(genre_parts)
        quality = ' / '.join(quality_parts)
        title_part = t[:bracket_m.start()].strip()
    else:
        title_part = t

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
    year_m = re.search(r'\((\d{4})\)', t) or re.search(r'\b(19\d{2}|20\d{2})\b', t)
    year = year_m.group(1) if year_m else ''
    t = re.sub(r'\s*\(\d{4}\)\s*', ' ', t)
    t = re.sub(r'\(.*?\)', ' ', t)
    t = re.sub(r'\[.*?\]', ' ', t)
    t = re.sub(r'(?i)\b(1080p|720p|2160p|480p|WEBRip|WEB-DL|WEB|BluRay|BRRip|HDRip|DVDRip|DCPRip|'
               r'x264|x265|h264|h265|HEVC|AVC|AAC|AC3|DDP|DTS|MP4|MKV|AVI|'
               r'10bit|8bit|5\s*[. ]\s*1|2\s*[. ]\s*0|6CH|'
               r'REPACK|PROPER|READNFO|iNTERNAL|EXTENDED|UNRATED|DC|FINAL|COMPLETE|'
               r'YTS|RARBG|RMTeam|NeoNoir|SupaCvnt|FLUX|BTM|'
               r'WEBRip|WEB\s*[.-]\s*DL|WEB\s*Line|AMZN|DSNP|NF|MA|PMNTP|PLAY|Early\s*Release|'
               r'CAM|TELESYNC|HDTS|TS|TC|SCREENER'
               r')\b', ' ', t, flags=re.I)
    t = re.sub(r'[._]', ' ', t)
    t = re.sub(r'\s+', ' ', t).strip()
    t = re.sub(r'\s*-\s*\w+$', '', t)
    t = re.sub(r'(?i)\b(LEAK|PLAY|DUAL|LINKS|SCREENER|TS|CAM|HDRip)\b', '', t)
    t = re.sub(r'\s+', ' ', t).strip()
    return t, year


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
               r'x264|x265|h264|h265|HEVC|AVC|AAC|AC3|DDP|DTS|MP4|MKV|AVI|'
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
    for i, t in enumerate(topics, 1):
        title = t.get('orig_title') or t['movie_title']
        year = t['movie_year']
        raw_name = t['title']
        if not title:
            continue
        cache_key = f"{title}|{year}".lower()
        result = None
        if cache_key in imdb_cache and imdb_cache[cache_key] is not None:
            result = imdb_cache[cache_key]
        else:
            print(f"  [{i}/{total}] {title}...", end=' ', flush=True)
            result = search_imdb(title, year)
            if result is None:
                deep_cache_key = f"deep:{raw_name.lower().strip()}"
                if deep_cache_key in imdb_cache and imdb_cache[deep_cache_key] is not None:
                    result = imdb_cache[deep_cache_key]
                else:
                    result = search_imdb_deep(raw_name)
                    if result is not None:
                        imdb_cache[deep_cache_key] = result
            if result is not None:
                imdb_cache[cache_key] = result
                save_json(SEARCH_CACHE, imdb_cache)
            time.sleep(0.1)
        if result:
            if isinstance(result, str):
                imdb_id = result
                poster, cast = '', ''
            else:
                imdb_id = result.get('id')
                poster = result.get('poster', '')
                cast = result.get('cast', '')
            t['imdb_id'] = imdb_id
            if not t.get('poster_url') and poster:
                t['poster_url'] = download_poster(imdb_id, poster)
            t['cast'] = cast
            print(f"ID {imdb_id}", end='')
        else:
            print("не найдено", end='')
        print()
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

    query = f"{title} {year}" if year else title
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


def search_kinopoisk_ids(topics):
    total = len(topics)
    kp_cache = load_json(KP_SEARCH_CACHE) or {}
    for i, t in enumerate(topics, 1):
        title, year = t['movie_title'], t['movie_year']
        if not title:
            continue
        cache_key = f"{title}|{year}".lower()
        if cache_key in kp_cache and kp_cache[cache_key] is not None:
            result = kp_cache[cache_key]
        else:
            print(f"  [{i}/{total}] {title}...", end=' ', flush=True)
            result = search_kinopoisk(title, year)
            kp_cache[cache_key] = result
            if result:
                save_json(KP_SEARCH_CACHE, kp_cache)
            time.sleep(0.3)
        if result:
            t['kp_id'] = result['kp_id']
            t['kp_rating'] = result['kp_rating']
            t['kp_votes'] = result['kp_votes']
            print(f"КП {result['kp_rating'] or '—'}", end='')
        else:
            print("не найдено", end='')
        print()
    return topics


def search_youtube_trailer(title, year):
    query = f"{title} {year} official trailer"
    url = f"https://www.youtube.com/results?search_query={urllib.parse.quote(query)}"
    try:
        r = SESSION.get(url, timeout=10)
        m = re.search(r'/watch\?v=([a-zA-Z0-9_-]{11})', r.text)
        if m:
            return f"https://www.youtube.com/watch?v={m.group(1)}"
    except Exception:
        pass
    return None


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
    return url


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


def load_ratings(needed_ids):
    cached = load_json(RATINGS_CACHE) or {}
    if cached and needed_ids.issubset(cached.keys()):
        return {k: v for k, v in cached.items() if k in needed_ids and v is not None}
    try:
        print("Скачиваю IMDB ratings dataset (~8MB)...")
        r = SESSION.get(RATINGS_URL, stream=True, timeout=120)
        r.raise_for_status()
        keep_ids = set(cached.keys()) | needed_ids
        ratings = {}
        buf = io.BytesIO(r.content)
        with gzip.GzipFile(fileobj=buf, mode='rb') as gz:
            with io.TextIOWrapper(gz, encoding='utf-8') as f:
                f.readline()
                for line in f:
                    parts = line.strip().split('\t')
                    if len(parts) >= 3:
                        tid = parts[0]
                        if tid in keep_ids:
                            ratings[tid] = {'rating': parts[1], 'votes': parts[2]}
        for tid in keep_ids:
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
    try:
        print("Скачиваю IMDB basics dataset (жанры, ~8MB)...")
        r = SESSION.get(BASICS_URL, stream=True, timeout=120)
        r.raise_for_status()
        keep_ids = set(cached.keys()) | needed_ids
        basics = {}
        buf = io.BytesIO(r.content)
        with gzip.GzipFile(fileobj=buf, mode='rb') as gz:
            with io.TextIOWrapper(gz, encoding='utf-8') as f:
                f.readline()
                for line in f:
                    parts = line.strip().split('\t')
                    if len(parts) >= 9:
                        tid = parts[0]
                        g = parts[8] if parts[8] != r'\N' else ''
                        if tid in keep_ids:
                            basics[tid] = {'type': parts[1], 'genres': g}
        for tid in keep_ids:
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


def parse_forum_page(html, collection='nashe_kino'):
    soup = BeautifulSoup(html, 'html.parser')
    topics = []
    rows = soup.select('tr.hl-tr')
    for row in rows:
        topic_id = row.get('data-topic_id')
        if not topic_id:
            continue
        size_el = row.select_one('a.dl-stub')
        if not size_el:
            continue
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
            'topic_url': TOPIC_URL_T.format(topic_id),
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
    m = re.search(r'Формат(?:\s+видео)?\s*:\s*(\S+)', text)
    if not m:
        m = re.search(r'Format(?:\s+video)?\s*:\s*(\S+)', text)
    if m:
        candidate = m.group(1).strip().rstrip(':').rstrip(',')
        if candidate.lower() in known:
            fmt = known_map.get(candidate, candidate)
    if not fmt:
        # fallback: split at known separators (br, newline) and check before
        part = re.sub(r'<br\s*/?>', '\n', html, flags=re.I)
        part = re.sub(r'<[^>]+>', '', part)
        for line in part.split('\n'):
            ck = re.search(r'(?:Формат|Format)(?:\s+(?:видео|video))?\s*:\s*(\S+)', line)
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
    return url


def fetch_magnets(topics):
    total = len(topics)
    for i, t in enumerate(topics, 1):
        if t.get('magnet') and t.get('poster_url'):
            continue
        if t.get('_magnet_failed'):
            continue
        print(f"  [{i}/{total}] Загрузка магнета t={t['topic_id']}...", end=' ', flush=True)
        try:
            html = get_topic_html(t['topic_id'], t['topic_url'], timeout=10)
            if html is None:
                raise RuntimeError('fetch failed')
            data = parse_topic_for_magnet(html)
            t['magnet'] = data['magnet']
            if data['magnet']:
                print("OK", end='')
                if data.get('poster') and not t.get('poster_url'):
                    t['poster_url'] = download_rutracker_poster(t['topic_id'], data['poster'])
                    if t['poster_url']:
                        print(", постер ✓", end='')
            else:
                print("нет магнета", end='')
            if data.get('imdb') and not t.get('imdb_id'):
                t['imdb_id'] = data['imdb']
            if data.get('format') and not t.get('format'):
                t['format'] = data['format']
        except Exception as e:
            print(f"ошибка: {e}", end='')
            t['_magnet_failed'] = True
        print()
        time.sleep(1.5)
    return topics


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
        if imdb_id:
            bdata = basics.get(imdb_id)
            genre = bdata.get('genres', '') if isinstance(bdata, dict) else ''
            if not genre:
                rating_data = fetch_imdb_rating(imdb_id)
                if rating_data.get('genres'):
                    genre = rating_data['genres']
                if not t.get('poster_url') and rating_data.get('poster'):
                    t['poster_url'] = download_poster(imdb_id, rating_data['poster'])
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
        if cache_key in yt_cache:
            t['youtube_url'] = yt_cache[cache_key]
        else:
            yt_url = search_youtube_trailer(eng_title, year)
            t['youtube_url'] = yt_url
            yt_cache[cache_key] = yt_url
            save_json(YOUTUBE_CACHE, yt_cache)
            if yt_url:
                print(f", трейлер ✓", end='')
            time.sleep(0.1)
        print()
    return topics


def generate_html(topics):
    rows = []
    tiles = []

    coll_opts = ''.join(f'<option value="{k}">{v["name"]}</option>' for k, v in COLLECTIONS.items())
    filter_bar = '''<div class="gf"><span class="gl">Коллекция:</span><select class="gs" onchange="af()" id="cs"><option value="">Все</option>''' + coll_opts + '''</select>
<span class="gl">Жанр:</span><select class="gs" onchange="af()" id="gs"><option value="">Все</option></select>
<span class="gl">Дата:</span><select class="gs" onchange="af()" id="ds"><option value="0">Все</option><option value="7">Неделя</option><option value="14">2 недели</option><option value="30">Месяц</option><option value="60">2 месяца</option><option value="180">Полгода</option></select></div>'''

    for t in topics:
        rating = t['kp_rating'] or t['imdb_rating'] or '—'
        rating_label = 'КП' if t['kp_rating'] else 'IMDB' if t['imdb_rating'] else ''
        rating_cls = ''
        if rating and rating != '—':
            r = float(rating)
            rating_cls = 'rh' if r >= 8 else 'rm' if r >= 7 else 'rl'
        if t.get('kp_id'):
            rating_url = f"https://www.kinopoisk.ru/film/{t['kp_id']}/"
        elif t.get('imdb_id'):
            rating_url = f"https://www.imdb.com/title/{t['imdb_id']}/"
        else:
            rating_url = '#'
        votes = t['kp_votes'] or t['imdb_votes'] or ''
        votes_str = f" ({votes})" if votes else ''
        votes_title = f' title="{votes} голосов"' if votes else ''
        trailer_q = urllib.parse.quote(f"{t['movie_title']} {t['movie_year']} official trailer")
        trailer_url = t.get('youtube_url') or f"https://www.youtube.com/results?search_query={trailer_q}"
        clean_t = escape(t['movie_title'].lower().strip())

        poster = t.get('poster_url', '') or ''
        if poster.startswith('posters/'):
            poster = f"data/{poster}"
        cast_str = t.get('cast', '') or ''
        raw_genre = t.get('genre', '') or ''
        if any(h in raw_genre.lower() for h in HIDDEN_GENRES):
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
        poster_html = f'<div class="pc" data-yt="{escape(trailer_url)}" onclick="pt(this)"><img src="{escape(poster)}" class="ps" alt=""><span class="pb">▶</span></div>' if poster else ''
        cast_html = f'<p class="ca">{escape(cast_str)}</p>' if cast_str else ''
        genre_html = f'<p class="gn">{escape(genre)}</p>' if genre else ''
        fmt_row = f'<p class="ff">Формат: {escape(cont)}</p>' if cont else ''
        esize = escape(t['size_str'])
        magnet = t.get('magnet', '')
        collection = escape(t.get('collection', 'nashe_kino'))
        movie_year = escape(str(t.get('movie_year') or ''))
        seeders = int(t.get('seeders') or 0)
        topic_title = escape(t.get('title', ''))
        missing = not poster or not t.get('kp_rating') or not t.get('youtube_url')
        enrich_btn = f'<button class="eb" data-tid="{escape(t["topic_id"])}" onclick="enrich(this)">◈</button>' if missing else ''

        watch_attrs = f'data-magnet="{escape(magnet)}" data-title="{clean_t}" data-year="{movie_year}" data-container="{escape(container)}" data-seeders="{seeders}" data-topic-title="{topic_title}" data-collection="{collection}"'

        rows.append(f'''<tr data-date="0" data-title="{clean_t}" data-year="{movie_year}" data-container="{escape(container)}" data-seeders="{seeders}" data-genre="{escape(genre.lower())}" data-collection="{collection}">
<td><a href="{escape(t['topic_url'])}" class="tn" target="_blank">{escape(t['title'])}</a>
<div class="ml">
<span class="tg" onclick="td(this)">+</span>
<span class="un">{escape(t['author'])}</span>
<a href="{trailer_url}" onclick="window.open(this.href,'tr','width=960,height=540,menubar=no,toolbar=no,location=no');return false" class="bt">▶ Трейлер</a>
<a href="{rating_url}" target="_blank"{votes_title}><span class="rb {rating_cls}">{rating_label} {escape(str(rating))}{escape(votes_str)}</span></a>
{enrich_btn}
<span class="rmv" onclick="hm(this)">✕</span>
</div>
<div class="dtc" style="display:none"><div class="dc">{poster_html}<div class="dx">{genre_html}{fmt_row}<div class="rbi"><span class="rb {rating_cls}">{rating_label} {escape(str(rating))}{escape(votes_str)}</span></div>{cast_html}</div></div></div>
</td>
<td>{esize}</td>
<td>{escape(t['date_str'])}</td>
<td data-s="{rating or '0'}"><a href="{escape(magnet)}" class="bm" title="Скачать kino">🧲</a><button class="wb" {watch_attrs} onclick="watch(this)">▶ Смотреть</button></td>
</tr>''')

        poster_card = f'<div class="pc" data-yt="{escape(trailer_url)}" onclick="pt(this)"><img src="{escape(poster)}" class="tps" alt=""><span class="pb">▶</span></div>' if poster else ''
        cast_short = escape(cast_str)[:120] + '…' if len(cast_str) > 120 else escape(cast_str)

        tiles.append(f'''<div class="tile-card" data-date="0" data-title="{clean_t}" data-year="{movie_year}" data-container="{escape(container)}" data-seeders="{seeders}" data-genre="{escape(genre.lower())}" data-size="{esize}" data-rating="{rating or '0'}" data-collection="{collection}">
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
<a href="{trailer_url}" onclick="window.open(this.href,'tr','width=960,height=540,menubar=no,toolbar=no,location=no');return false" class="bt">▶ Трейлер</a>
<button class="wb" {watch_attrs} onclick="watch(this)">▶ Смотреть</button>
<a href="{escape(magnet)}" class="bm" title="Скачать kino">🧲</a>
<a href="{rating_url}" target="_blank" class="tile-imdb">{rating_label}</a>
<span class="rmv" onclick="htm(this)">✕</span>
</div>
</div>
</div>''')

    with_r = sum(1 for t in topics if t['kp_rating'] or t['imdb_rating'])

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
.rb{{display:inline-block;padding:2px 8px;font-size:11px;font-weight:700;border-radius:4px;white-space:nowrap;text-decoration:none}}
.rbi .rb{{font-size:33px}}
.rh{{background:#f5c518;color:#000}}
.rm{{background:#b0881a;color:#fff}}
.rl{{background:#e0e0e0;color:#666}}
.un{{font-size:12px;color:#888}}
.rmv{{display:inline-block;width:18px;height:18px;line-height:16px;text-align:center;font-size:11px;font-weight:700;color:#fff;background:#da3633;border-radius:3px;cursor:pointer;user-select:none;flex-shrink:0;margin-left:6px}}
.rmv:hover{{background:#b62324}}
.bm{{font-size:16px;text-decoration:none;color:#333}}
.wb{{display:inline-block;padding:3px 10px;font-size:11px;font-weight:700;color:#fff;background:#e94560;border:none;border-radius:4px;cursor:pointer;white-space:nowrap;vertical-align:middle}}
.wb:hover{{background:#d63850}}
.eb{{display:inline-block;padding:2px 6px;font-size:12px;font-weight:700;color:#fff;background:#7c4dff;border:none;border-radius:4px;cursor:pointer;white-space:nowrap;vertical-align:middle;line-height:1.4}}
.eb:hover{{background:#651fff}}
.eb:disabled{{opacity:.4;cursor:wait}}
.st{{margin:16px 0;padding:12px 16px;background:#f5f5f5;border-radius:6px;font-size:13px;color:#666;border:1px solid #e0e0e0}}
.tile-grid{{display:none;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:16px}}
.tile-card{{border:1px solid #e0e0e0;border-radius:8px;overflow:hidden;transition:box-shadow .2s}}
.tile-card:hover{{box-shadow:0 2px 12px rgba(0,0,0,.1)}}
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
@media(max-width:768px){{body{{padding:10px}}h1{{font-size:22px}}.sub{{font-size:12px}}.tn{{font-size:20px}}.ca{{font-size:18px}}.gn{{font-size:18px}}.ps{{max-width:260px}}td{{padding:8px 10px;font-size:13px}}tr{{padding:6px 0}}.tile-title{{font-size:17px}}.tile-genre,.tile-size,.tile-format,.tile-cast,.tile-imdb{{font-size:14px}}.tile-grid{{grid-template-columns:1fr}}.tps{{max-height:200px;object-fit:cover}}table,thead,tbody,tr,td,th{{display:block}}thead{{display:none}}}}
body.mobile{{padding:10px}}body.mobile h1{{font-size:22px}}body.mobile .sub{{font-size:12px}}body.mobile .tn{{font-size:20px}}body.mobile .ca{{font-size:18px}}body.mobile .gn{{font-size:18px}}body.mobile .ps{{max-width:260px}}body.mobile td{{padding:8px 10px;font-size:13px}}body.mobile tr{{padding:6px 0}}body.mobile .tile-title{{font-size:17px}}body.mobile .tile-genre,body.mobile .tile-size,body.mobile .tile-cast,body.mobile .tile-imdb{{font-size:14px}}body.mobile .tile-grid{{grid-template-columns:1fr}}body.mobile .tps{{max-height:200px;object-fit:cover}}body.mobile table,body.mobile thead,body.mobile tbody,body.mobile tr,body.mobile td,body.mobile th{{display:block}}body.mobile thead{{display:none}}body.tile.mobile table{{display:none}}
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
<span>• Всего: <strong>{len(topics)}</strong> kino
• С рейтингом: <strong>{with_r}</strong></span>
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
function sortTiles(){{var tg=document.getElementById('tile-grid'),cards=Array.from(tg.children),a=sd.d;if(sd.i===0){{cards.sort(function(x,y){{return a*((x.getAttribute('data-title')||'').localeCompare(y.getAttribute('data-title')||''))}})}}else if(sd.i===1){{cards.sort(function(x,y){{return a*((x.getAttribute('data-size')||'').localeCompare(y.getAttribute('data-size')||''))}})}}else if(sd.i===3){{cards.sort(function(x,y){{return a*(parseFloat(x.getAttribute('data-rating')||'0')-parseFloat(y.getAttribute('data-rating')||'0'))}})}}else{{cards.sort(function(x,y){{return a*(parseFloat(x.getAttribute('data-date')||'0')-parseFloat(y.getAttribute('data-date')||'0'))}})}}cards.forEach(function(c){{tg.appendChild(c)}})}}
function st(c,t){{var tb=document.querySelector('#tbl tbody'),r=Array.from(tb.children),a=sd.i===c?sd.d*-1:1;
r.sort(function(x,y){{var va=x.children[c].getAttribute('data-s')||(t==='n'?x.getAttribute('data-date')||'0':x.children[c].textContent.trim()),vb=y.children[c].getAttribute('data-s')||(t==='n'?y.getAttribute('data-date')||'0':y.children[c].textContent.trim());if(t==='n'){{return a*(parseFloat(va)-parseFloat(vb))}}return a*va.localeCompare(vb)}});
r.forEach(function(r){{tb.appendChild(r)}});sd.i=c;sd.d=a;
document.querySelectorAll('th .ar').forEach(function(e){{e.textContent=''}});document.querySelectorAll('th')[c].querySelector('.ar').textContent=a>0?'▲':'▼';sortTiles()}}
function td(el){{var r=el.closest('td').querySelector('.dtc');if(!r)return;var on=r.style.display!=='none';r.style.display=on?'none':'';el.textContent=on?'+':'−'}}
function pt(el){{var u=el.getAttribute('data-yt');if(!u)return;window.open(u,'tr','width=960,height=540,menubar=no,toolbar=no,location=no')}}
function hm(el){{var t=el.closest('tr').getAttribute('data-title');if(!t)return;var h=JSON.parse(localStorage.getItem('ph')||'[]');if(h.indexOf(t)===-1)h.push(t);localStorage.setItem('ph',JSON.stringify(h));af()}}
function htm(el){{var t=el.closest('.tile-card').getAttribute('data-title');if(!t)return;var h=JSON.parse(localStorage.getItem('ph')||'[]');if(h.indexOf(t)===-1)h.push(t);localStorage.setItem('ph',JSON.stringify(h));af()}}
function hideSaved(sel){{var h=JSON.parse(localStorage.getItem('ph')||'[]');[].forEach.call(document.querySelectorAll(sel),function(r){{if(h.indexOf(r.getAttribute('data-title'))!==-1)r.style.display='none'}})}}
function fh(){{hideSaved('#tbl tbody tr')}}
function fht(){{hideSaved('.tile-card')}}
var sx=/(?:\\bhorror\\b|\\b(?:sex|porn|xxx|erotic|adult|nsfw|onlyfans)\\b)/i;
(function(){{var dv=localStorage.getItem('dv');if(dv)document.getElementById('ds').value=dv;af();sortTiles();
var isTile=localStorage.getItem('tv')==='tile';if(isTile){{document.body.classList.add('tile');document.getElementById('tvb').textContent='Вид: список';sortTiles()}}
var isMob=localStorage.getItem('mb')==='1';if(isMob){{document.body.classList.add('mobile');document.getElementById('mdb').textContent='🖥'}}}})()
function tv(){{var b=document.body;b.classList.toggle('tile');var isTile=b.classList.contains('tile');localStorage.setItem('tv',isTile?'tile':'list');document.getElementById('tvb').textContent=isTile?'Вид: список':'Вид: плитка';if(isTile)sortTiles()}}
function md(){{var b=document.body;b.classList.toggle('mobile');var isMob=b.classList.contains('mobile');localStorage.setItem('mb',isMob?'1':'0');document.getElementById('mdb').textContent=isMob?'🖥':'📱'}}
function bgf(){{var gs={{}};[].forEach.call(document.querySelectorAll('#tbl tbody tr,.tile-card'),function(r){{if(r.style.display==='none')return;var rg=(r.getAttribute('data-genre')||'').toLowerCase();rg.split(',').forEach(function(g){{g=g.trim();if(g)gs[g]=1}})}});var sel=document.getElementById('gs'),v=sel.value;sel.innerHTML='<option value=\\"\\">Все</option>';Object.keys(gs).sort().forEach(function(g){{var s=g.charAt(0).toUpperCase()+g.slice(1);sel.innerHTML+='<option value=\\"'+g+'\\">'+s+'</option>'}});sel.value=v}}
var playerPoll=null;
var currentHash='';
var currentSession='';
function fmtBytes(b){{if(!b)return'0 B';var u=['B','KB','MB','GB','TB'],i=0,v=b;while(v>=1024&&i<u.length-1){{v/=1024;i++}}return v.toFixed(1)+' '+u[i]}}
function hashFromMagnet(m){{var x=(m||'').match(/btih:([A-Fa-f0-9]{{40}})/i);return x?x[1].toLowerCase():''}}
function isStreamContainer(c){{c=(c||'').toLowerCase();return c==='mkv'||c==='mp4'}}
function findStreamReplacement(el){{var src={{magnet:el.getAttribute('data-magnet')||'',title:el.getAttribute('data-title')||'',year:el.getAttribute('data-year')||'',container:(el.getAttribute('data-container')||'').toLowerCase(),collection:el.getAttribute('data-collection')||'',hash:hashFromMagnet(el.getAttribute('data-magnet')||'')}};if(src.container!=='avi'||!src.title)return null;var best=null,bestScore=-1;[].forEach.call(document.querySelectorAll('button.wb[data-magnet]'),function(b){{var m=b.getAttribute('data-magnet')||'',h=hashFromMagnet(m),c=(b.getAttribute('data-container')||'').toLowerCase(),title=b.getAttribute('data-title')||'',year=b.getAttribute('data-year')||'';if(!m||!h||h===src.hash||!isStreamContainer(c)||title!==src.title)return;if(src.year&&year!==src.year)return;var score=parseInt(b.getAttribute('data-seeders')||'0',10)||0;if(c==='mp4')score+=5;if(b.getAttribute('data-collection')===src.collection)score+=3;if(score>bestScore){{bestScore=score;best=b}}}});if(!best)return null;return {{magnet:best.getAttribute('data-magnet')||'',container:(best.getAttribute('data-container')||'').toUpperCase(),title:best.getAttribute('data-topic-title')||best.getAttribute('data-title')||'',seeders:parseInt(best.getAttribute('data-seeders')||'0',10)||0}}}}
function newSession(h){{return h+'-'+Date.now()+'-'+Math.random().toString(36).slice(2)}}
function streamUrl(kind,h){{var sid=encodeURIComponent(currentSession||'');return '/'+kind+'/'+h+(sid?'?sid='+sid:'')}}
function stopCurrentSession(){{if(!currentSession)return;var sid=currentSession;currentSession='';try{{if(navigator.sendBeacon){{var blob=new Blob([JSON.stringify({{sid:sid}})],{{type:'application/json'}});navigator.sendBeacon('/stop_session',blob);return}}}}catch(_e){{}}fetch('/stop_session',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{sid:sid}}),keepalive:true}}).catch(function(){{}})}}
function unmutePlayer(ev){{if(ev){{ev.preventDefault();ev.stopPropagation()}}var p=document.getElementById('inline-player'),s=document.getElementById('player-status'),b=document.getElementById('sound-button');p.muted=false;p.defaultMuted=false;p.volume=1;p.removeAttribute('muted');if(b)b.textContent='Звук включён';s.textContent='Звук включён';p.play().catch(function(){{s.textContent='Нажмите ▶ в плеере для запуска со звуком'}})}}
function playPlayer(ev){{if(ev){{ev.preventDefault();ev.stopPropagation()}}var p=document.getElementById('inline-player'),s=document.getElementById('player-status');p.muted=false;p.defaultMuted=false;p.volume=1;p.removeAttribute('muted');p.play().then(function(){{s.textContent='Воспроизведение запущено'}}).catch(function(err){{s.textContent='Не удалось запустить: '+(err&&err.name?err.name:'ошибка')}})}}
function useAacAudio(ev){{if(ev){{ev.preventDefault();ev.stopPropagation()}}if(!currentHash)return;var p=document.getElementById('inline-player'),s=document.getElementById('player-status'),b=document.getElementById('aac-button');if(p.dataset.mode==='aac')return;p.dataset.mode='aac';p.pause();p.muted=false;p.defaultMuted=false;p.volume=1;p.removeAttribute('muted');p.src=streamUrl('transcode',currentHash);if(b)b.textContent='AAC включён';s.textContent='Запускаю совместимый AAC-звук...';p.play().catch(function(){{s.textContent='Нажмите ▶ в плеере для запуска AAC-звука'}})}}
function closePlayer(){{if(playerPoll){{clearInterval(playerPoll);playerPoll=null}}var p=document.getElementById('inline-player');p.pause();p.removeAttribute('src');p.load();stopCurrentSession();document.getElementById('player-overlay').classList.add('hidden')}}
function startStream(p, h) {{p.dataset.mode='stream';p.muted=false;p.defaultMuted=false;p.volume=1;p.removeAttribute('muted');p.src=streamUrl('stream',h);var pp=p.play();if(pp&&pp.catch)pp.catch(function(){{document.getElementById('player-status').textContent='Нажмите ▶ в плеере для запуска'}})}}
function pollPlayer(h){{var s=document.getElementById('player-status'),p=document.getElementById('inline-player');if(playerPoll)clearInterval(playerPoll);playerPoll=setInterval(async function(){{try{{var r=await fetch('/status/'+h);if(!r.ok){{s.textContent='Ожидание добавления...';return}}var d=await r.json();if(d.state==='pending'){{s.textContent='Получаю метаданные...';return}}var pct=Math.round((d.progress||0)*1000)/10;s.textContent=(d.ready?'Видео готово, запускаю...':'Буферизация...')+' '+pct+'% · '+fmtBytes(d.downloaded)+' / '+fmtBytes(d.total)+' · '+fmtBytes(d.download_rate)+'/с · peers '+(d.num_peers||0);if(d.ready){{clearInterval(playerPoll);playerPoll=null;startStream(p,h)}}}}catch(_e){{s.textContent='Нет связи с сервером'}}}},1500)}}
async function watch(el){{var m=el.getAttribute('data-magnet'),replacement=findStreamReplacement(el),replacementText='';if(replacement){{m=replacement.magnet;replacementText='AVI заменён на '+replacement.container+' для онлайн-просмотра · сиды '+replacement.seeders}}var h=hashFromMagnet(m);if(!m)return;if(!h){{window.open(m);return}}stopCurrentSession();currentHash=h;currentSession=newSession(h);var o=document.getElementById('player-overlay'),p=document.getElementById('inline-player'),s=document.getElementById('player-status'),e=document.getElementById('player-error'),b=document.getElementById('sound-button'),ab=document.getElementById('aac-button');o.classList.remove('hidden');p.dataset.mode='stream';if(b)b.textContent='Звук';if(ab)ab.textContent='AAC-звук';s.textContent=replacementText||'Запускаю поток...';e.textContent='';pollPlayer(h);try{{var r=await fetch('/watch_sync',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{magnet:m}})}});var d=await r.json().catch(function(){{return {{}}}});if(!r.ok||!d.info_hash){{e.textContent=d.error||'Не удалось добавить kino';return}}if(d.info_hash.toLowerCase()!==h){{h=d.info_hash;currentHash=h;currentSession=newSession(h);pollPlayer(h)}}s.textContent=replacementText||(d.async_mode?'Получаю метаданные...':'Буферизация...')}}catch(_err){{e.textContent='Ошибка соединения с сервером'}}}}
function ac(){{var c=document.getElementById('cs').value;[].forEach.call(document.querySelectorAll('#tbl tbody tr,.tile-card'),function(r){{if(c)r.style.display=r.getAttribute('data-collection')===c?'':'none'}})}}
function af(){{var d=document.getElementById('ds').value,g=document.getElementById('gs').value;localStorage.setItem('dv',d);var n=Date.now()/1000,cut=d>0?n-d*86400:0;
[].forEach.call(document.querySelectorAll('#tbl tbody tr,.tile-card'),function(r){{r.style.display='';var dt=parseFloat(r.getAttribute('data-date')||'0');if(cut&&dt<cut)r.style.display='none'}});
if(g){{[].forEach.call(document.querySelectorAll('#tbl tbody tr,.tile-card'),function(r){{if(r.style.display!=='none'){{var rg=(r.getAttribute('data-genre')||'').toLowerCase();if(rg.indexOf(g)===-1)r.style.display='none'}}}})}}
[].forEach.call(document.querySelectorAll('#tbl tbody tr,.tile-card'),function(r){{if(r.style.display!=='none'){{var rg=r.getAttribute('data-genre')||'',t=r.getAttribute('data-title')||'';if(sx.test(rg)||sx.test(t))r.style.display='none'}}}});
ac();fh();fht();bgf()}}
['pointerdown','mousedown','click'].forEach(function(n){{document.addEventListener(n,function(e){{if(e.target&&e.target.id==='sound-button')unmutePlayer(e)}},true)}});
document.addEventListener('click',function(e){{if(e.target&&e.target.id==='aac-button')useAacAudio(e)}},true);
['pointerdown','mousedown','click'].forEach(function(n){{document.addEventListener(n,function(e){{if(e.target&&e.target.id==='play-button')playPlayer(e)}},true)}});
document.getElementById('inline-player').addEventListener('pointerdown',function(){{this.muted=false;this.defaultMuted=false;this.volume=1;this.removeAttribute('muted')}});
document.getElementById('inline-player').addEventListener('playing',function(){{document.getElementById('player-status').textContent='Воспроизведение запущено'}});
window.addEventListener('beforeunload',stopCurrentSession);
async function enrich(el){{var tid=el.getAttribute('data-tid');if(!tid)return;el.disabled=true;el.textContent='◈';fetch('/enrich/'+tid,{{method:'POST'}}).catch(function(){{}});var iv=setInterval(async function(){{try{{var r=await fetch('/enrich/status/'+tid);if(!r.ok)return;var d=await r.json();if(d.status==='done'){{clearInterval(iv);el.textContent='✓';el.disabled=false;setTimeout(function(){{location.reload()}},1500)}}else if(d.status==='in_progress'){{el.textContent='◈'}}else if(d.status==='queued'){{el.textContent='⌛'}}else if(d.status&&d.status.startsWith('error')){{clearInterval(iv);el.textContent='✗';el.disabled=false;console.error('Enrich error:',d.status)}}}}catch(_e){{}}}},2000)}}
</script>
</body>
</html>'''
    return html


def enrich_topic(topic):
    """Enrich a single topic dict with missing data (poster, magnet, ratings, trailers)."""
    title = topic.get('orig_title') or topic['movie_title']
    russian_title = topic['movie_title']
    year = topic['movie_year']
    raw_name = topic['title']
    if not title:
        return topic
    kp_cache = load_json(KP_SEARCH_CACHE) or {}

    if not topic.get('magnet') or topic.get('_magnet_failed'):
        try:
            html = get_topic_html(topic['topic_id'], topic['topic_url'], timeout=10)
            if html:
                data = parse_topic_for_magnet(html)
                if data.get('magnet'):
                    topic['magnet'] = data['magnet']
                    topic.pop('_magnet_failed', None)
                    if data.get('poster') and not topic.get('poster_url'):
                        topic['poster_url'] = download_rutracker_poster(topic['topic_id'], data['poster'])
                    if data.get('imdb') and not topic.get('imdb_id'):
                        topic['imdb_id'] = data['imdb']
                    if data.get('format') and not topic.get('format'):
                        topic['format'] = data['format']
        except Exception:
            topic['_magnet_failed'] = True

    if not topic.get('poster_url') or not topic.get('format'):
        try:
            html = get_topic_html(topic['topic_id'], topic['topic_url'], timeout=10)
            if html:
                data = parse_topic_for_magnet(html)
                if data.get('poster') and not topic.get('poster_url'):
                    topic['poster_url'] = download_rutracker_poster(topic['topic_id'], data['poster'])
                if data.get('format') and not topic.get('format'):
                    topic['format'] = data['format']
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
                if not topic.get('poster_url') and result.get('poster'):
                    topic['poster_url'] = download_poster(topic['imdb_id'], result['poster'])
                topic['cast'] = result.get('cast', '')

    cache_key = f"{title}|{year}".lower()
    kp_key = f"{russian_title}|{year}".lower()
    if not topic.get('kp_rating') and russian_title:
        if kp_key in kp_cache and kp_cache[kp_key] is not None:
            result = kp_cache[kp_key]
        else:
            result = search_kinopoisk(russian_title, year)
            kp_cache[kp_key] = result
            save_json(KP_SEARCH_CACHE, kp_cache)
        if result:
            topic['kp_id'] = result['kp_id']
            topic['kp_rating'] = result['kp_rating']
            topic['kp_votes'] = result['kp_votes']

    if not topic.get('youtube_url'):
        yt_cache = load_json(YOUTUBE_CACHE) or {}
        if cache_key in yt_cache:
            topic['youtube_url'] = yt_cache[cache_key]
        else:
            yt_url = search_youtube_trailer(title, year)
            topic['youtube_url'] = yt_url
            yt_cache[cache_key] = yt_url
            save_json(YOUTUBE_CACHE, yt_cache)

    return topic


def main():
    refresh = '--refresh' in sys.argv
    quick = '--quick' in sys.argv
    pages_flag = False
    pages_count = 10
    topics_limit = MAX_TOPICS
    collection = 'nashe_kino'

    limit = 0
    for arg in sys.argv[1:]:
        if arg.startswith('--collection='):
            collection = arg.split('=')[1]
        elif arg.startswith('--limit='):
            limit = int(arg.split('=')[1])
        elif arg == '--limit':
            idx = sys.argv.index(arg)
            if idx + 1 < len(sys.argv) and not sys.argv[idx + 1].startswith('--'):
                limit = int(sys.argv[idx + 1])
        elif arg.startswith('--pages='):
            pages_count = int(arg.split('=')[1])
            pages_flag = True
            topics_limit = 0
        elif arg == '--pages' and not pages_flag:
            idx = sys.argv.index(arg)
            if idx + 1 < len(sys.argv) and not sys.argv[idx + 1].startswith('--'):
                pages_count = int(sys.argv[idx + 1])
                pages_flag = True
                topics_limit = 0

    if collection not in COLLECTIONS:
        print(f"Неизвестная коллекция '{collection}'. Доступны: {', '.join(COLLECTIONS.keys())}")
        sys.exit(1)

    if quick:
        pages_count = 1
        topics_limit = 0

    coll_info = COLLECTIONS[collection]
    print(f"Коллекция: {coll_info['name']}")

    if not refresh and os.path.exists(TORRENTS_CACHE):
        print("Загружаю кеш...")
        topics = load_json(TORRENTS_CACHE)
    else:
        print(f"1. Парсинг {coll_info['name']}...")
        all_topics = []
        base_url = coll_info['url']
        # extract forum id for cache naming
        m_fid = re.search(r'f=(\d+)', base_url)
        forum_id = m_fid.group(1) if m_fid else collection
        for page in range(pages_count):
            start = page * PAGE_SIZE
            url = base_url if start == 0 else f"{base_url}&start={start}"
            listing_cache_path = os.path.join(TOPIC_CACHE_DIR, f'f{forum_id}_p{page}.html')
            if not os.path.exists(listing_cache_path):
                print(f"  Страница {page + 1} (start={start})...", end=' ', flush=True)
                try:
                    r = SESSION.get(url, timeout=30)
                    r.raise_for_status()
                    raw = r.content
                    html = raw.decode('cp1251', errors='replace')
                    page_topics = parse_forum_page(html, collection=collection)
                    if page_topics:
                        with open(listing_cache_path, 'wb') as f:
                            f.write(raw)
                except Exception as e:
                    print(f"ошибка: {e}")
                    continue
            else:
                with open(listing_cache_path, 'rb') as f:
                    raw = f.read()
                html = raw.decode('cp1251', errors='replace')
                page_topics = parse_forum_page(html, collection=collection)
                print(f"  Страница {page + 1} (start={start}) — из кеша", end=' ', flush=True)
            print(f"{len(page_topics)} тем")
            all_topics.extend(page_topics)
            if topics_limit and len(all_topics) >= topics_limit:
                all_topics = all_topics[:topics_limit]
                print(f"  Достигнут лимит в {topics_limit} топиков")
                break
            time.sleep(0.5)

        print(f"\nВсего найдено тем: {len(all_topics)}")

        if not all_topics:
            print("  Новых тем нет, сохраняем кеш как есть")
            topics = load_json(TORRENTS_CACHE) or []
            save_json(TORRENTS_CACHE, topics)
            generate_html_only = True
        else:
            generate_html_only = False

        print("2. Слияние с кешем...")
        cached = load_json(TORRENTS_CACHE) or []
        cache_by_id = {t['topic_id']: t for t in cached if t.get('topic_id')}

        # Keep cached topics from OTHER collections, merge current topics
        other = [t for t in cached if t.get('collection', 'nashe_kino') != collection]

        new_current = []
        merged_current = []
        for t in all_topics:
            tid = t['topic_id']
            if tid in cache_by_id:
                old = cache_by_id[tid]
                old['seeders'] = t['seeders']
                old['leechers'] = t['leechers']
                old['size_str'] = t['size_str']
                old['size_bytes'] = t['size_bytes']
                old['date_str'] = t['date_str']
                old['title'] = t['title']
                old['movie_title'] = t['movie_title']
                if t.get('orig_title'):
                    old['orig_title'] = t['orig_title']
                old['collection'] = collection
                merged_current.append(old)
            else:
                new_current.append(t)
                merged_current.append(t)

        if limit and len(merged_current) > limit:
            merged_current = merged_current[:limit]
            new_current = [t for t in merged_current if t['topic_id'] not in cache_by_id]
            print(f"  (лимит --limit={limit})")

        merged = other + merged_current
        new_topics = new_current
        print(f"  В кеше: {len(cached)}, новых: {len(new_current)}, "
              f"из других коллекций: {len(other)}, всего: {len(merged)}")

        if not generate_html_only:
            need_fetch = [t for t in merged if not t.get('_magnet_failed') and (not t.get('magnet') or not t.get('poster_url'))]
            if need_fetch:
                print(f"\n3. Загрузка магнетов и постеров для {len(need_fetch)} тем...")
                fetch_magnets(need_fetch)

        topics = merged if not generate_html_only else topics
        save_json(TORRENTS_CACHE, topics)

        if not generate_html_only:
            print("\n4. Поиск IMDB ID...")
            search_imdb_ids(new_topics if new_topics else topics)

            print("\n5. Поиск Кинопоиск рейтинга...")
            search_kinopoisk_ids(new_topics if new_topics else topics)

            needed_ids = set()
            for t in topics:
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

            targets = new_topics if new_topics else topics
            if targets:
                print(f"\n8. Обогащение данных...")
                enrich(targets, ratings, basics)

            topics.sort(key=lambda t: t.get('seeders', 0) or 0, reverse=True)

            save_json(TORRENTS_CACHE, topics)

    print(f"\n9. Генерация HTML ({len(topics)} фильмов)...")
    output = generate_html(topics)
    atomic_write_text(OUTPUT_FILE, output)
    print(f"Готово: {OUTPUT_FILE}")
    print(f"\nЗапусти: python stream_server.py")


if __name__ == '__main__':
    main()
