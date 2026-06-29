import re
import json
import html as html_lib
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path

from flask import Blueprint, request, jsonify
from bs4 import BeautifulSoup

from config import DATA_DIR, WORKER_COUNT, DISCOVER_SEARCH_CACHE_TTL_DAYS
from project_io import atomic_write_json
import generate_page as gp
import rutracker_search as rtsearch

HISTORY_FILE = DATA_DIR / 'discover_history.json'
SEARCH_CACHE_FILE = DATA_DIR / 'discover_search_cache.json'
SEARCH_CACHE_VERSION = 'v2'
ENRICH_BATCH_LIMIT = max(1, min(WORKER_COUNT, 8))

bp = Blueprint('discover', __name__)


def _load_full_topics():
    path = DATA_DIR / 'torrents_data.json'
    try:
        return json.loads(path.read_text('utf-8'))
    except (OSError, json.JSONDecodeError):
        return []


def _cache_key(scope, query):
    normalized = re.sub(r'\s+', ' ', query.strip().lower())
    return f'{SEARCH_CACHE_VERSION}:{scope}:{normalized}'


def _load_search_cache():
    try:
        data = json.loads(SEARCH_CACHE_FILE.read_text('utf-8'))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _save_search_cache(data):
    atomic_write_json(SEARCH_CACHE_FILE, data)


def _get_cached_search(scope, query):
    entry = _load_search_cache().get(_cache_key(scope, query))
    if not isinstance(entry, dict):
        return None
    try:
        created = datetime.fromisoformat(entry.get('created_at', ''))
    except ValueError:
        return None
    if datetime.now() - created > timedelta(days=DISCOVER_SEARCH_CACHE_TTL_DAYS):
        return None
    payload = entry.get('payload')
    if not isinstance(payload, dict):
        return None
    payload = dict(payload)
    payload['cached'] = True
    return payload


def _put_cached_search(scope, query, payload):
    data = _load_search_cache()
    stored = dict(payload)
    stored.pop('cached', None)
    data[_cache_key(scope, query)] = {
        'created_at': datetime.now().isoformat(timespec='seconds'),
        'payload': stored,
    }
    if len(data) > 200:
        items = sorted(data.items(), key=lambda kv: kv[1].get('created_at', ''))
        data = dict(items[-200:])
    _save_search_cache(data)


def _load_discover_history():
    try:
        return json.loads(HISTORY_FILE.read_text('utf-8'))
    except (OSError, json.JSONDecodeError):
        return []


def _search_history_results(query, limit=20):
    q = query.lower().strip()
    results = []
    seen = set()
    for item in reversed(_load_discover_history()):
        haystack = ' '.join(str(item.get(k) or '') for k in (
            'topic_id', 'title', 'raw_title', 'orig_title', 'year',
            'imdb_id', 'kp_id', 'source', 'collection',
        )).lower()
        key = item.get('topic_id') or item.get('magnet') or item.get('title')
        if q and q in haystack and key and key not in seen:
            seen.add(key)
            cached = dict(item)
            cached['status'] = 'В кеше поиска'
            results.append(cached)
            if len(results) >= limit:
                break
    return results


def _search_cached_payload_results(query, limit=20):
    q = re.sub(r'\s+', ' ', query.strip().lower())
    results = []
    seen = set()
    cache = _load_search_cache()
    for key, entry in sorted(cache.items(), key=lambda kv: kv[1].get('created_at', ''), reverse=True):
        payload = entry.get('payload') if isinstance(entry, dict) else None
        if not isinstance(payload, dict):
            continue
        key_match = q and q in key.lower()
        for item in payload.get('results') or []:
            haystack = ' '.join(str(item.get(k) or '') for k in (
                'topic_id', 'title', 'raw_title', 'orig_title', 'year',
                'imdb_id', 'kp_id', 'source', 'collection',
            )).lower()
            if not key_match and (not q or q not in haystack):
                continue
            item_key = item.get('topic_id') or item.get('magnet') or item.get('imdb_id') or item.get('kp_id') or item.get('title')
            if not item_key or item_key in seen:
                continue
            seen.add(item_key)
            cached = dict(item)
            cached['status'] = 'В кеше поискового запроса'
            results.append(cached)
            if len(results) >= limit:
                return results
    return results


def _missing_topic_reason(topic, display_ids, hidden_ids):
    reasons = []
    topic_id = str(topic.get('topic_id') or '')
    if topic_id in hidden_ids:
        reasons.append('hidden')
    if topic.get('_sanitized'):
        reasons.append('sanitized')
    if not topic.get('magnet') or topic.get('magnet') == '0':
        reasons.append('no magnet')
    if topic_id and topic_id not in display_ids:
        reasons.append('outside display set')
    hidden_source = f"{topic.get('genre', '')} {topic.get('title', '')}".lower()
    if any(h in hidden_source for h in gp.HIDDEN_GENRES):
        reasons.append('forbidden text/genre')
    if not gp.has_real_poster(topic):
        reasons.append('missing poster')
    return reasons


def _discover_poster_url(topic):
    gp.resolve_existing_local_poster(topic)
    poster_url = topic.get('poster_url') or ''
    if gp.is_external_poster_url(poster_url):
        return poster_url
    return gp.display_poster_url(topic)


def _format_size_bytes(size_bytes):
    try:
        size_bytes = int(size_bytes or 0)
    except (TypeError, ValueError):
        size_bytes = 0
    if size_bytes <= 0:
        return ''
    units = ('B', 'KB', 'MB', 'GB', 'TB')
    value = float(size_bytes)
    unit = units[0]
    for unit in units:
        if value < 1024 or unit == units[-1]:
            break
        value /= 1024
    if unit == 'B':
        return f'{int(value)} {unit}'
    return f'{value:.1f} {unit}'


def _ensure_discover_poster(topic):
    gp.resolve_existing_local_poster(topic)
    if gp.has_real_poster(topic):
        return
    poster_url = topic.get('poster_url') or ''
    if gp.is_external_poster_url(poster_url) and topic.get('imdb_id'):
        local_url = gp.download_poster(topic.get('imdb_id'), poster_url)
        if local_url:
            topic['poster_url'] = local_url
            return
    kp_id = topic.get('kp_id')
    if kp_id:
        local_url = gp.download_kinopoisk_poster(kp_id)
        if local_url:
            topic['poster_url'] = local_url
            return
    imdb_id = topic.get('imdb_id')
    if imdb_id:
        rating_data = gp.fetch_imdb_rating(imdb_id)
        if rating_data.get('poster'):
            local_url = gp.download_poster(imdb_id, rating_data['poster'])
            if local_url:
                topic['poster_url'] = local_url


def _has_real_topic_url(topic):
    topic_id = str(topic.get('topic_id') or '')
    topic_url = str(topic.get('topic_url') or '')
    if topic_id.startswith('discover_'):
        return False
    if 'rutracker.net/forum/viewtopic.php' in topic_url:
        return bool(re.fullmatch(r'\d+', topic_id))
    if 'piratebay' in topic_url.lower():
        return True
    return False


def _enrich_discover_identity(topic):
    title = topic.get('orig_title') or topic.get('movie_title') or topic.get('title') or ''
    year = topic.get('movie_year') or ''
    if not topic.get('kp_id') and title:
        gp.search_topic_kinopoisk(topic, title, year)
    if not topic.get('imdb_id') and title:
        result = gp.search_imdb(title, year)
        if result is None:
            result = gp.search_imdb_deep(topic.get('title') or title)
        if result:
            if isinstance(result, str):
                topic['imdb_id'] = result
            else:
                topic['imdb_id'] = result.get('id')
                if result.get('poster'):
                    local_url = gp.download_poster(topic['imdb_id'], result['poster'])
                    if local_url:
                        topic['poster_url'] = local_url
                topic['cast'] = result.get('cast', '')
    imdb_id = topic.get('imdb_id')
    if imdb_id:
        rdata = gp.load_ratings({imdb_id}).get(imdb_id)
        if isinstance(rdata, dict):
            topic['imdb_rating'] = rdata.get('rating')
            topic['imdb_votes'] = rdata.get('votes', '')
        bdata = gp.load_basics({imdb_id}).get(imdb_id)
        if isinstance(bdata, dict) and bdata.get('genres'):
            topic['genre'] = gp.clean_and_translate_genre(bdata.get('genres', ''))
    _ensure_discover_poster(topic)
    if not topic.get('youtube_url'):
        topic['youtube_url'] = gp.resolve_trailer_url(title, year, topic.get('kp_id'), topic.get('imdb_id'))
    return topic


def _topic_card_payload(topic, status, source_label=''):
    collection = topic.get('collection') or ''
    size = topic.get('size_str') or topic.get('size') or ''
    if not size:
        size = _format_size_bytes(topic.get('size_bytes'))
    fmt = topic.get('format') or gp.detect_format_from_text(
        topic.get('title') or topic.get('movie_title') or topic.get('raw_title') or ''
    )
    return {
        'status': status,
        'source': source_label or gp.COLLECTIONS.get(collection, {}).get('name', collection) or collection,
        'collection': collection,
        'source_kind': topic.get('source') or '',
        'topic_id': str(topic.get('topic_id') or ''),
        'title': topic.get('movie_title') or topic.get('title') or '',
        'raw_title': topic.get('title') or '',
        'orig_title': topic.get('orig_title') or '',
        'year': topic.get('movie_year') or '',
        'seeders': topic.get('seeders') or 0,
        'size': size,
        'size_bytes': int(topic.get('size_bytes') or 0),
        'format': (fmt or '').upper(),
        'poster_url': _discover_poster_url(topic),
        'has_poster': gp.has_real_poster(topic),
        'has_magnet': bool(topic.get('magnet') and topic.get('magnet') != '0'),
        'magnet': topic.get('magnet') or '',
        'topic_url': topic.get('topic_url') or '',
        'imdb_id': topic.get('imdb_id') or '',
        'kp_id': topic.get('kp_id') or '',
        'imdb_rating': topic.get('imdb_rating') or '',
        'kp_rating': topic.get('kp_rating') or '',
        'genre': topic.get('genre') or '',
        'trailer_url': topic.get('trailer_url') or topic.get('youtube_url') or '',
    }


def _discover_local(query):
    q = query.lower().strip()
    topics = _load_full_topics()
    hidden_ids = gp.load_hidden_topic_ids()
    display_topics = gp.filter_world_top(gp.clean_catalog_topics(topics))
    display_ids = {str(t.get('topic_id') or '') for t in display_topics}
    results = []
    for topic in topics:
        haystack = ' '.join(str(v or '') for v in (
            topic.get('topic_id'), topic.get('movie_title'), topic.get('orig_title'),
            topic.get('title'), topic.get('movie_year'), topic.get('imdb_id'),
            topic.get('kp_id'), topic.get('collection'),
        )).lower()
        if q and q not in haystack:
            continue
        tid = str(topic.get('topic_id') or '')
        if tid in display_ids and tid not in hidden_ids and not topic.get('_sanitized'):
            status = 'В витрине'
        else:
            reasons = _missing_topic_reason(topic, display_ids, hidden_ids)
            status = 'В базе, не на витрине' + (f": {', '.join(reasons)}" if reasons else '')
        results.append(_topic_card_payload(topic, status))
    for cached in _search_history_results(query):
        key = cached.get('topic_id') or cached.get('magnet') or cached.get('title')
        if key and all((r.get('topic_id') or r.get('magnet') or r.get('title')) != key for r in results):
            results.append(cached)
    for cached in _search_cached_payload_results(query):
        key = cached.get('topic_id') or cached.get('magnet') or cached.get('imdb_id') or cached.get('kp_id') or cached.get('title')
        if key and all((r.get('topic_id') or r.get('magnet') or r.get('imdb_id') or r.get('kp_id') or r.get('title')) != key for r in results):
            results.append(cached)
    results.sort(key=lambda item: (0 if item['status'] == 'В витрине' else 1, -(item.get('seeders') or 0), item.get('title') or ''))
    return {'stage': 'local', 'results': results[:40], 'count': len(results)}


def _discover_identity(query):
    title, year = gp.clean_title(query)
    title = title or query.strip()
    imdb = gp.search_imdb(title, year) if title else None
    kp = gp.search_kinopoisk(title, year) if title else None
    results = []
    if imdb:
        poster_url = imdb.get('poster', '') if isinstance(imdb, dict) else ''
        results.append({
            'status': 'Фильм найден в IMDb',
            'source': 'IMDb',
            'title': title,
            'year': year,
            'imdb_id': imdb.get('id') if isinstance(imdb, dict) else imdb,
            'poster_url': poster_url,
            'has_magnet': False,
        })
    if kp:
        poster_url = ''
        kp_id = kp.get('kp_id', '')
        if kp_id:
            poster_url = gp.download_kinopoisk_poster(kp_id) or ''
        results.append({
            'status': 'Фильм найден в Кинопоиске',
            'source': 'Кинопоиск',
            'title': title,
            'year': year,
            'kp_id': kp_id,
            'kp_rating': kp.get('kp_rating', ''),
            'poster_url': poster_url,
            'has_magnet': False,
        })
    return {'stage': 'identity', 'results': results, 'count': len(results)}


def _discover_world(query):
    urls = []
    encoded = urllib.parse.quote(query.strip())
    for collection, info in gp.COLLECTIONS.items():
        source = info.get('source', '')
        if source == 'piratebay':
            urls.append((collection, info, f'https://1.piratebays.to/s/?q={encoded}&category=207'))
        elif source == 'tpbparty':
            urls.append((collection, info, f'https://thepiratebay.party/search/{encoded}/1/99/207'))
    results = []
    seen = set()
    for collection, info, url in urls:
        try:
            r = gp.SESSION.get(url, timeout=12)
            if r.status_code != 200:
                continue
            topics = gp.parse_world_page(
                r.text, collection, info.get('source', ''), gp.world_identity_clean_title,
                gp.parse_size, gp.now_text, gp.info_hash_from_magnet, gp.detect_format_from_text,
            )
            topics = _fill_world_search_formats(topics)
            for topic in gp.deduplicate_world_topics(topics):
                tid = str(topic.get('topic_id') or '')
                if not tid or tid in seen:
                    continue
                seen.add(tid)
                results.append(_topic_card_payload(topic, 'Найдено во внешнем World-источнике', info.get('name', collection)))
        except Exception:
            continue
    results.sort(key=lambda item: -(item.get('seeders') or 0))
    return {'stage': 'world', 'results': results[:30], 'count': len(results)}


def _fill_world_search_formats(topics):
    targets = [t for t in topics[:12] if not t.get('format') and t.get('topic_url')]
    if not targets:
        return topics

    def fetch_fmt(topic):
        try:
            return topic, gp.fetch_piratebay_format(topic.get('topic_id'), topic.get('topic_url'), timeout=8)
        except Exception:
            return topic, ''

    with ThreadPoolExecutor(max_workers=min(WORKER_COUNT, len(targets))) as executor:
        futures = [executor.submit(fetch_fmt, t) for t in targets]
        for future in as_completed(futures):
            topic, fmt = future.result()
            if fmt:
                topic['format'] = fmt
    return topics


RUTRACKER_FORUMS = [
    ('22', 'Наше кино'),
    ('2540', 'Кино СНГ'),
    ('1247', 'Кино СНГ HD'),
    ('252', 'Новинки 2026'),
]


def _search_rutracker_forums(query):
    found = []
    seen = set()
    q = query.strip().lower()
    if not q:
        return found
    for fid, fname in RUTRACKER_FORUMS:
        try:
            url = f'https://rutracker.net/forum/viewforum.php?f={fid}'
            r = gp.SESSION.get(url, timeout=12)
            if r.status_code != 200:
                continue
            html = r.content.decode('cp1251', errors='replace')
            soup = BeautifulSoup(html, 'html.parser')
            for a in soup.select('a[href*="viewtopic.php"]'):
                href = a.get('href', '')
                m = re.search(r'viewtopic\.php\?t=(\d+)', href)
                if not m or m.group(1) in seen:
                    continue
                title = a.get_text(strip=True)
                if not title:
                    continue
                if q in title.lower():
                    seen.add(m.group(1))
                    found.append({
                        'topic_id': m.group(1),
                        'title': title,
                        'forum': fname,
                    })
        except Exception:
            continue
    return found


def _search_google_rutracker(query):
    found = []
    seen = set()
    for domain in ('rutracker.org', 'rutracker.net'):
        try:
            url = f'https://www.google.com/search?q={urllib.parse.quote(query.strip())}+site:{domain}&gbv=1&hl=ru'
            r = gp.SESSION.get(url, timeout=12, headers={
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36',
                'Accept-Language': 'ru-RU,ru;q=0.9',
                'Accept': 'text/html,application/xhtml+xml',
            })
            if r.status_code != 200:
                continue
            soup = BeautifulSoup(r.text, 'html.parser')
            for a in soup.select('a[href*="rutracker"]'):
                href = a.get('href', '')
                m = re.search(r'viewtopic\.php\?t=(\d+)', href)
                if m and m.group(1) not in seen:
                    seen.add(m.group(1))
                    title = a.get_text(strip=True) or a.get('title', '') or f'Topic {m.group(1)}'
                    found.append({'topic_id': m.group(1), 'title': title})
        except Exception:
            continue
    return found


def _search_yandex_rutracker(query):
    found = []
    seen = set()
    try:
        url = f'https://yandex.kz/search/?text={urllib.parse.quote(query.strip())}+site%3Arutracker.org'
        r = gp.SESSION.get(url, timeout=12, headers={
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36',
            'Accept-Language': 'ru-RU,ru;q=0.9',
            'Accept': 'text/html,application/xhtml+xml',
        })
        if r.status_code != 200:
            return found
        soup = BeautifulSoup(r.text, 'html.parser')
        for a in soup.select('a[href*="rutracker"]'):
            href = a.get('href', '')
            m = re.search(r'viewtopic\.php\?t=(\d+)', href)
            if not m or m.group(1) in seen:
                continue
            title = a.get_text(strip=True) or a.get('title', '')
            if not title:
                parent = a.find_parent(['div', 'li'], class_=lambda c: c and 'OrganicTitle' in c)
                if parent:
                    title = parent.get_text(strip=True)
            if not title:
                continue
            seen.add(m.group(1))
            found.append({'topic_id': m.group(1), 'title': title})
    except Exception:
        pass
    return found


def _discover_rutracker(query):
    def from_topic_id(topic_id, title, idx):
        topic_url = gp.TOPIC_URL_T.format(topic_id)
        return {
            'status': 'Найдено через поисковик (Яндекс/Google)',
        'source': 'Rutracker',
        'collection': 'rutracker_search',
        'source_kind': 'rutracker',
        'topic_id': topic_id,
            'title': title,
            'orig_title': '',
            'year': '',
            'seeders': 0,
            'size': '',
            'poster_url': '/data/posters/placeholder.png',
            'has_poster': False,
            'has_magnet': False,
            'magnet': '',
            'topic_url': topic_url,
            'imdb_id': '',
            'kp_id': '',
            'listing_order': idx,
        }

    url = f'https://rutracker.net/forum/tracker.php?nm={urllib.parse.quote(query.strip())}'
    results = []
    seen_topic_ids = set()
    errors = []
    try:
        r = gp.SESSION.get(url, timeout=12)
        if r.status_code != 200:
            errors.append(f'Rutracker HTTP {r.status_code}')
            html = ''
        else:
            html = r.content.decode('cp1251', errors='replace')
            if 'login.php' in r.url or 'action="login.php"' in html:
                errors.append('Rutracker login required')
        topics = gp.parse_forum_page(html, collection='rutracker_search')
    except Exception as e:
        errors.append(f'Rutracker: {e}')
        topics = []
    for topic in topics[:30]:
        seen_topic_ids.add(str(topic.get('topic_id') or ''))
        results.append(_topic_card_payload(topic, 'Найдено в Rutracker', 'Rutracker'))

    # If tracker.php failed or returned too few results, try forum index
    if len(results) < 5:
        if rtsearch.index_exists():
            idx_results = rtsearch.search(query, limit=20)
            for r in idx_results:
                if r['topic_id'] in seen_topic_ids:
                    continue
                seen_topic_ids.add(r['topic_id'])
                card = _topic_card_payload({
                    'topic_id': r['topic_id'],
                    'movie_title': r.get('movie_title', r.get('title', '')),
                    'title': r.get('title', ''),
                    'magnet': '',
                    'topic_url': f'https://rutracker.net/forum/viewtopic.php?t={r["topic_id"]}',
                    'seeders': r.get('seeders', 0),
                    'size_str': r.get('size_str', ''),
                    'collection': '',
                    'imdb_id': '',
                    'kp_id': '',
                    'orig_title': '',
                    'movie_year': '',
                }, 'Найдено в индексе RuTracker', f'RuTracker — {r.get("forum_name", "")}')
                results.append(card)
            if idx_results:
                errors.append('Результаты из индекса RuTracker')
        else:
            errors.append('Индекс RuTracker не найден. Запустите: python rutracker_search.py')

    if len(results) < 5:
        all_search = []
        all_search.extend(_search_rutracker_forums(query))
        all_search.extend(_search_google_rutracker(query))
        all_search.extend(_search_yandex_rutracker(query))

        seen_keys = set()
        deduped = []
        for item in all_search:
            key = item['topic_id']
            if key not in seen_keys:
                seen_keys.add(key)
                deduped.append(item)

        for item in deduped[:20]:
            if item['topic_id'] in seen_topic_ids:
                continue
            seen_topic_ids.add(item['topic_id'])
            title = item.get('title', '')
            forum_tag = f' [{item["forum"]}]' if item.get('forum') else ''
            results.append(from_topic_id(item['topic_id'], title + forum_tag, len(results)))

        if not deduped:
            google_url = 'https://www.google.com/search?q=' + urllib.parse.quote(f'{query.strip()} site:rutracker.org')
            yandex_url = 'https://yandex.kz/search/?text=' + urllib.parse.quote(f'{query.strip()} site:rutracker.org')
            results.append({
                'status': 'Открыть поиск в браузере',
            'source': 'Rutracker',
            'collection': 'rutracker_search',
            'source_kind': 'rutracker',
            'topic_id': '',
                'title': f'{query.strip()} site:rutracker.org',
                'orig_title': '',
                'year': '',
                'seeders': 0,
                'size': '',
                'poster_url': '/data/posters/placeholder.png',
                'has_poster': False,
                'magnet': '',
                'topic_url': google_url,
                'imdb_id': '',
                'kp_id': '',
            })
            results.append({
                'status': 'Открыть поиск в браузере',
            'source': 'Яндекс',
            'collection': 'rutracker_search',
            'source_kind': 'rutracker',
            'topic_id': '',
                'title': f'{query.strip()} site:rutracker.org',
                'orig_title': '',
                'year': '',
                'seeders': 0,
                'size': '',
                'poster_url': '/data/posters/placeholder.png',
                'has_poster': False,
                'magnet': '',
                'topic_url': yandex_url,
                'imdb_id': '',
                'kp_id': '',
            })

    payload = {'stage': 'rutracker', 'results': results[:30], 'count': len(results)}
    if errors:
        payload['error'] = '; '.join(errors)
    return payload


@bp.route('/api/discover')
def api_discover():
    query = (request.args.get('q') or '').strip()
    scope = (request.args.get('scope') or 'local').strip().lower()
    if len(query) < 2:
        return jsonify(error='query too short', stage=scope, results=[], count=0), 400
    if scope == 'local':
        payload = _discover_local(query)
        return jsonify(payload)
    cached = _get_cached_search(scope, query)
    if cached is not None:
        return jsonify(cached)
    if scope == 'identity':
        payload = _discover_identity(query)
        _put_cached_search(scope, query, payload)
        return jsonify(payload)
    if scope == 'world':
        payload = _discover_world(query)
        _put_cached_search(scope, query, payload)
        return jsonify(payload)
    if scope == 'rutracker':
        payload = _discover_rutracker(query)
        _put_cached_search(scope, query, payload)
        return jsonify(payload)
    if scope == 'all':
        payload = {
            'stage': 'all',
            'local': _discover_local(query),
            'identity': _discover_identity(query),
            'world': _discover_world(query),
            'rutracker': _discover_rutracker(query),
        }
        _put_cached_search(scope, query, payload)
        return jsonify(payload)
    return jsonify(error='unknown scope', stage=scope, results=[], count=0), 400


def _save_discover_history(entry):
    history = _load_discover_history()
    entry_id = entry.get('topic_id') or ''
    history = [h for h in history if h.get('topic_id') != entry_id]
    history.append(entry)
    atomic_write_json(HISTORY_FILE, history)


def _make_discover_topic(result):
    topic_id = str(result.get('topic_id') or '')
    title = (result.get('title') or result.get('orig_title') or '').strip()
    clean_title, year = gp.clean_title(title)
    size_str = result.get('size') or ''
    size_bytes = result.get('size_bytes') or 0
    if not size_bytes and size_str:
        size_bytes, size_str = gp.parse_size(size_str)
    collection = result.get('collection') or 'rutracker_search'
    source_kind = result.get('source_kind') or ''
    if not source_kind and collection in gp.COLLECTIONS:
        source_kind = gp.COLLECTIONS.get(collection, {}).get('source', '')
    synthetic_identity = not topic_id
    if not topic_id:
        id_part = result.get('imdb_id') or result.get('kp_id') or re.sub(r'\W+', '_', title.lower()).strip('_')[:40]
        topic_id = f'discover_{id_part or "movie"}'
    topic_url = result.get('topic_url') or ''
    if not topic_url and not synthetic_identity:
        topic_url = gp.TOPIC_URL_T.format(topic_id)
    return {
        'topic_id': topic_id,
        'title': title,
        'movie_title': clean_title or title,
        'orig_title': result.get('orig_title') or '',
        'movie_year': year or result.get('year') or '',
        'collection': collection,
        'source': source_kind,
        'topic_url': topic_url,
        'magnet': result.get('magnet') or '',
        'poster_url': result.get('poster_url') or '',
        'seeders': int(result.get('seeders') or 0),
        'size_str': size_str,
        'size_bytes': int(size_bytes or 0),
        'imdb_id': result.get('imdb_id') or '',
        'kp_id': result.get('kp_id') or '',
        'kp_rating': result.get('kp_rating') or '',
        'imdb_rating': result.get('imdb_rating') or '',
        'genre': '',
        'format': result.get('format') or gp.detect_format_from_text(title),
    }


@bp.route('/api/discover/enrich', methods=['POST'])
def api_discover_enrich():
    data = request.get_json(silent=True) or {}
    topic_id = str(data.get('topic_id') or '')
    if not topic_id and not (data.get('imdb_id') or data.get('kp_id') or data.get('title')):
        return jsonify(error='no movie identity'), 400
    return jsonify(_enrich_discover_item(data))


def _discover_item_key(item):
    return str(item.get('topic_id') or item.get('magnet') or item.get('imdb_id') or item.get('kp_id') or item.get('title') or '')


def _discover_item_keys(item):
    return {
        str(v) for v in (
            item.get('topic_id'), item.get('magnet'), item.get('imdb_id'),
            item.get('kp_id'), item.get('title'), item.get('raw_title'),
        ) if v
    }


def _enrich_discover_item(data):
    topic = _make_discover_topic(data)
    result = {'topic_id': topic.get('topic_id'), 'status': 'enriching'}
    if _has_real_topic_url(topic) and 'rutracker.net/forum/viewtopic.php' in topic.get('topic_url', ''):
        try:
            gp.fetch_magnets([topic])
        except Exception as e:
            result['magnet_error'] = str(e)
    if _has_real_topic_url(topic):
        try:
            gp.enrich_topic(topic, force_poster_retry=True, include_trailer=True)
        except Exception as e:
            result['enrich_error'] = str(e)
    else:
        try:
            _enrich_discover_identity(topic)
        except Exception as e:
            result['enrich_error'] = str(e)
    _ensure_discover_poster(topic)
    payload = _topic_card_payload(topic, 'Обогащён', 'Discover')
    payload['kp_rating'] = topic.get('kp_rating', '')
    payload['genre'] = topic.get('genre', '')
    payload['orig_title'] = topic.get('orig_title', '')
    payload['year'] = topic.get('movie_year', '')
    payload['trailer_url'] = topic.get('trailer_url') or topic.get('youtube_url') or ''
    payload['status'] = 'Обогащён'
    _save_discover_history(payload)
    result['status'] = 'done'
    result['movie'] = payload
    return result


@bp.route('/api/discover/enrich_batch', methods=['POST'])
def api_discover_enrich_batch():
    data = request.get_json(silent=True) or {}
    selected_key = str(data.get('selected_key') or '')
    items = data.get('items') or []
    if not isinstance(items, list):
        return jsonify(error='items must be list'), 400
    deduped = []
    seen = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        key = _discover_item_key(item)
        if not key or key in seen:
            continue
        seen.add(key)
        deduped.append(item)
        if len(deduped) >= ENRICH_BATCH_LIMIT:
            break
    if not deduped:
        return jsonify(error='no items'), 400
    results = []
    with ThreadPoolExecutor(max_workers=min(WORKER_COUNT, len(deduped))) as executor:
        futures = {executor.submit(_enrich_discover_item, item): item for item in deduped}
        for future in as_completed(futures):
            try:
                results.append(future.result())
            except Exception as e:
                item = futures[future]
                results.append({'status': 'error', 'error': str(e), 'topic_id': item.get('topic_id') or ''})
    selected = None
    for result in results:
        movie = result.get('movie') or {}
        if selected_key and selected_key in _discover_item_keys(movie):
            selected = movie
            break
    if selected is None:
        selected = (results[0].get('movie') if results and isinstance(results[0], dict) else None)
    return jsonify(status='done', workers=min(WORKER_COUNT, len(deduped)), movie=selected, results=results)


@bp.route('/browse/discover')
def browse_discover():
    return '''<!DOCTYPE html>
<html lang="ru"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0"><title>Найти фильм</title>
<style>
*{box-sizing:border-box}body{margin:0;background:#141414;color:#fff;font-family:system-ui,sans-serif;padding:22px}h1{font-size:26px;margin:0 0 6px}.sub{color:#999;font-size:14px;margin:0 0 18px}.search{display:grid;grid-template-columns:1fr 150px;gap:10px;margin-bottom:14px}input{background:#242424;color:#fff;border:1px solid #333;border-radius:6px;padding:13px 14px;font-size:16px}button{background:#e50914;color:#fff;border:0;border-radius:6px;padding:0 18px;font-weight:700;cursor:pointer}button:disabled{opacity:.55;cursor:wait}.progress{height:8px;background:#292929;border-radius:4px;overflow:hidden;margin:8px 0 12px}.fill{height:100%;width:0;background:#e50914;transition:width .2s}.status{display:flex;gap:8px;flex-wrap:wrap;color:#aaa;font-size:12px;margin-bottom:14px}.pill{background:#242424;border:1px solid #333;border-radius:4px;padding:4px 8px}.section{margin:18px 0}.section h2{font-size:18px;margin:0 0 10px;color:#ddd}.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(290px,1fr));gap:10px}.card{display:grid;grid-template-columns:64px 1fr;gap:10px;background:#1f1f1f;border:1px solid #333;border-radius:7px;padding:9px;min-height:104px;cursor:default;position:relative;transition:border-color .2s}.card.clickable{cursor:pointer}.card.clickable:hover{border-color:#e50914}.card.loading{opacity:.6;pointer-events:none}.card .spinner{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;background:rgba(0,0,0,.6);border-radius:7px;z-index:2;font-size:13px;color:#e50914}.card .spinner::after{content:'';width:18px;height:18px;margin-left:8px;border:2px solid #555;border-top-color:#e50914;border-radius:50%;animation:sp .6s linear infinite}@keyframes sp{to{transform:rotate(360deg)}}.poster{width:64px;aspect-ratio:2/3;background-size:cover;background-position:center;background-color:#333;border-radius:4px}.title{font-size:15px;font-weight:700;line-height:1.25}.meta{font-size:12px;color:#aaa;margin:5px 0}.tag{display:inline-block;background:#2c2c2c;color:#ddd;border:1px solid #444;border-radius:3px;padding:2px 6px;font-size:11px;margin:2px 4px 2px 0}.actions a{display:inline-block;color:#fff;background:#333;text-decoration:none;border-radius:4px;padding:5px 8px;font-size:12px;margin-right:5px}.actions a.watch{background:#e50914}.empty{color:#777;padding:14px;background:#1b1b1b;border:1px solid #282828;border-radius:7px}.back{position:fixed;top:15px;right:20px;z-index:100;background:rgba(0,0,0,.7);color:#fff;border:1px solid #555;padding:6px 14px;border-radius:4px;font-size:13px;cursor:pointer;text-decoration:none}.modal{display:none;position:fixed;inset:0;z-index:200;background:rgba(0,0,0,.8);align-items:center;justify-content:center;padding:20px}.modal.open{display:flex}.modal .mc{background:#1a1a1a;border:1px solid #333;border-radius:10px;max-width:520px;width:100%;max-height:90vh;overflow-y:auto;padding:20px;position:relative}.modal .mc h2{font-size:20px;margin:0 0 4px}.modal .mc .sub{font-size:13px;color:#999;margin:0 0 12px}.modal .mc .row{display:flex;gap:16px}.modal .mc .mposter{width:140px;aspect-ratio:2/3;background-size:cover;background-position:center;background-color:#222;border-radius:6px;flex-shrink:0}.modal .mc .minfo{flex:1;min-width:0}.modal .mc .rating{display:inline-block;background:#242424;border:1px solid #444;border-radius:4px;padding:3px 8px;margin:2px 4px 2px 0;font-size:13px;font-weight:700}.modal .mc .rating.kp{color:#ffd700}.modal .mc .rating.imdb{color:#f5c518}.modal .mc .genre{display:inline-block;background:#2a2a2a;border:1px solid #444;border-radius:3px;padding:2px 7px;font-size:11px;color:#aaa;margin:2px 3px 2px 0}.modal .mc .close{position:absolute;top:12px;right:16px;background:none;border:none;color:#888;font-size:22px;cursor:pointer}.modal .mc .close:hover{color:#fff}.modal .mc .mactions{margin-top:12px;display:flex;gap:8px}.modal .mc .mactions a{padding:8px 16px;border-radius:5px;font-size:13px;text-decoration:none;font-weight:700}.modal .mc .mactions .watch-btn{background:#e50914;color:#fff}.modal .mc .mactions .source-btn{background:#333;color:#fff}.modal .mc .mloading{text-align:center;padding:40px 20px;color:#aaa}.modal .mc .mloading .sp{width:28px;height:28px;margin:0 auto 12px;border:3px solid #333;border-top-color:#e50914;border-radius:50%;animation:sp .6s linear infinite}
</style></head><body>
<a href="/test" class="back">Назад</a>
<h1>Найти фильм</h1>
<p class="sub">Кликните по результату, чтобы обогатить (magnet, постер, рейтинги, трейлер) и открыть карточку фильма.</p>
<div class="search"><input id="q" placeholder="Например: F1 The Movie, Сваты 2, Interstellar" autofocus><button id="go">Искать</button></div>
<div class="progress"><div class="fill" id="fill"></div></div>
<div class="status" id="status"><span class="pill">Готово</span></div>
<div id="out"></div>
<div class="modal" id="modal"><div class="mc" id="mc"></div></div>
<script>
const stages=[['local','Локальный каталог'],['identity','IMDb / Кинопоиск'],['world','World источники'],['rutracker','Rutracker']];
let currentMovie=null;
let allResults=[];
function esc(s){return String(s||'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
function pu(m){const p=m.poster_url||'';return p?p.indexOf('data/')===0?'/'+p:p:'/data/posters/placeholder.png'}
function hash(m){const x=(m.magnet||'').match(/btih:([A-Fa-f0-9]+)/);return x?x[1].toLowerCase():''}
function cardActions(m){
  const h=hash(m);
  let a='';
  if(h)a+='<a class="watch" href="#" data-watch="1">\u0421\u043c\u043e\u0442\u0440\u0435\u0442\u044c</a>';
  if(m.topic_url)a+='<a href="'+esc(m.topic_url)+'" target="_blank">\u0418\u0441\u0442\u043e\u0447\u043d\u0438\u043a</a>';
  return a;
}
function card(m){
  const ids=[m.imdb_id?'IMDb '+m.imdb_id:'',m.kp_id?'KP '+m.kp_id:''].filter(Boolean).join(' \u00b7');
  const fmt=m.format||'\u0444\u043e\u0440\u043c\u0430\u0442 ?';
  const size=m.size||'\u0440\u0430\u0437\u043c\u0435\u0440 ?';
  return '<div class="card clickable" data-id="'+esc(m.topic_id||'')+'" data-json="'+esc(JSON.stringify(m))+'"><div class="poster" style="background-image:url('+pu(m)+')"></div><div><div class="title">'+esc(m.title||m.orig_title||'?')+'</div><div class="meta">'+esc([m.source,m.year,ids,m.seeders?'\u0441\u0438\u0434\u043e\u0432 '+m.seeders:''].filter(Boolean).join(' \u00b7 '))+'</div><div><span class="tag">'+esc(fmt)+'</span><span class="tag">'+esc(size)+'</span></div><span class="tag">'+esc(m.status||'')+'</span><div class="actions">'+cardActions(m)+'</div></div></div>';
}
function enrichedCard(m){
  const h=hash(m), ids=[m.imdb_id?'IMDb '+m.imdb_id:'',m.kp_id?'KP '+m.kp_id:''].filter(Boolean).join(' \u00b7');
  let ratings='';
  if(m.kp_rating)ratings+='<span class="rating kp">KP '+esc(m.kp_rating)+'</span>';
  if(m.imdb_rating)ratings+='<span class="rating imdb">IMDb '+esc(m.imdb_rating)+'</span>';
  let genres='';
  if(m.genre)genres=m.genre.split(',').map(g=>'<span class="genre">'+esc(g.trim())+'</span>').join('');
  let watchBtn='',sourceBtn='',magnetText='';
  if(h)watchBtn='<a class="watch-btn" href="#" onclick="watchMovie(currentMovie);return false">\u0421\u043c\u043e\u0442\u0440\u0435\u0442\u044c</a>';
  if(m.magnet)magnetText='<div style="margin-top:10px;font-size:11px;color:#777;word-break:break-all">Magnet: '+esc(m.magnet.slice(0,160))+(m.magnet.length>160?'...':'')+'</div>';
  if(m.topic_url)sourceBtn='<a class="source-btn" href="'+esc(m.topic_url)+'" target="_blank">\u0418\u0441\u0442\u043e\u0447\u043d\u0438\u043a</a>';
  if(m.trailer_url)sourceBtn+='<a class="source-btn" href="'+esc(m.trailer_url)+'" target="_blank">\u0422\u0440\u0435\u0439\u043b\u0435\u0440</a>';
  const tech=[m.format,m.size].filter(Boolean).join(' \u00b7 ');
  return '<button class="close" onclick="closeModal()">\u00d7</button><div class="row"><div class="mposter" style="background-image:url('+pu(m)+')"></div><div class="minfo"><h2>'+esc(m.title||m.orig_title||'?')+'</h2><div class="sub">'+esc([m.orig_title,m.year,ids,tech].filter(Boolean).join(' \u00b7 '))+'</div>'+ratings+'<div style="margin-top:6px">'+genres+'</div><div class="mactions">'+watchBtn+sourceBtn+'</div>'+magnetText+'</div></div>';
}
function section(name,data){const items=data.results||[];return '<div class="section"><h2>'+esc(name)+' <span class="tag">'+(data.count||items.length)+'</span></h2>'+(items.length?'<div class="grid">'+items.map(card).join('')+'</div>':'<div class="empty">\u041d\u0438\u0447\u0435\u0433\u043e \u043d\u0435 \u043d\u0430\u0439\u0434\u0435\u043d\u043e</div>')+'</div>'}
async function enrichItem(m){
  const modal=document.getElementById('modal'),mc=document.getElementById('mc');
  modal.classList.add('open');
  mc.innerHTML='<div class="mloading"><div class="sp"></div>\u041e\u0431\u043e\u0433\u0430\u0449\u0435\u043d\u0438\u0435...<br><span style="font-size:12px;color:#666">\u0437\u0430\u0433\u0440\u0443\u0437\u043a\u0430 magnet, \u043f\u043e\u0441\u0442\u0435\u0440\u0430, \u0440\u0435\u0439\u0442\u0438\u043d\u0433\u043e\u0432, \u0442\u0440\u0435\u0439\u043b\u0435\u0440\u0430</span></div>';
  try{
    const selectedKey=m.topic_id||m.magnet||m.imdb_id||m.kp_id||m.title||'';
    const batch=[m].concat(allResults.filter(x=>x!==m)).slice(0,8);
    const r=await fetch('/api/discover/enrich_batch',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({selected_key:selectedKey,items:batch})});
    const d=await r.json();
    if(d.movie){currentMovie=d.movie;mc.innerHTML=enrichedCard(d.movie);}
    else mc.innerHTML='<div class="mloading">\u041e\u0448\u0438\u0431\u043a\u0430 \u043e\u0431\u043e\u0433\u0430\u0449\u0435\u043d\u0438\u044f</div>';
  }catch(e){
    mc.innerHTML='<div class="mloading">\u041e\u0448\u0438\u0431\u043a\u0430: '+esc(e.message)+'</div>';
  }
}
function closeModal(){document.getElementById('modal').classList.remove('open')}
async function watchMovie(m){
  if(!m||!m.magnet)return;
  try{
    const r=await fetch('/watch_sync',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({magnet:m.magnet,async_only:true})});
    const d=await r.json().catch(()=>({}));
    if(d.info_hash)window.open('/player.html#'+d.info_hash,'_blank');
    else alert(d.error||'\u041d\u0435 \u0443\u0434\u0430\u043b\u043e\u0441\u044c \u0437\u0430\u043f\u0443\u0441\u0442\u0438\u0442\u044c');
  }catch(e){
    alert('\u041e\u0448\u0438\u0431\u043a\u0430 \u0441\u0432\u044f\u0437\u0438 \u0441 \u0441\u0435\u0440\u0432\u0435\u0440\u043e\u043c');
  }
}
document.getElementById('modal').addEventListener('click',function(e){if(e.target===this)closeModal()});
document.addEventListener('keydown',function(e){if(e.key==='Escape')closeModal()});
document.getElementById('out').addEventListener('click',function(e){
  const link=e.target.closest('a');
  if(link&&link.dataset.watch){
    e.preventDefault();
    const c=link.closest('.card.clickable');
    if(!c)return;
    try{watchMovie(JSON.parse(c.dataset.json))}catch(_e){}
    return;
  }
  if(link)return;
  const card=e.target.closest('.card.clickable');
  if(!card)return;
  const raw=card.dataset.json;
  if(!raw)return;
  try{enrichItem(JSON.parse(raw))}catch(e){}
});
async function run(){const q=document.getElementById('q').value.trim();if(q.length<2)return;const btn=document.getElementById('go'),fill=document.getElementById('fill'),st=document.getElementById('status'),out=document.getElementById('out');btn.disabled=true;out.innerHTML='';allResults=[];fill.style.width='0%';let html='';for(let i=0;i<stages.length;i++){const[scope,label]=stages[i];st.innerHTML='<span class="pill">\u042d\u0442\u0430\u043f '+(i+1)+'/'+stages.length+': '+label+'</span>';try{const r=await fetch('/api/discover?scope='+scope+'&q='+encodeURIComponent(q));const d=await r.json();if(d.results)allResults=allResults.concat(d.results);html+=section(label,d);out.innerHTML=html}catch(e){html+='<div class="section"><h2>'+esc(label)+'</h2><div class="empty">\u041e\u0448\u0438\u0431\u043a\u0430 \u043f\u043e\u0438\u0441\u043a\u0430</div></div>';out.innerHTML=html}fill.style.width=Math.round((i+1)/stages.length*100)+'%'}st.innerHTML='<span class="pill">\u0413\u043e\u0442\u043e\u0432\u043e</span>';btn.disabled=false}
document.getElementById('go').addEventListener('click',run);
document.getElementById('q').addEventListener('keydown',e=>{if(e.key==='Enter')run()});
</script></body></html>'''
