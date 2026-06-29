import re
from datetime import datetime

from config import DATA_DIR
from project_io import atomic_write_json_unlocked, file_lock


CATALOG_FILE = DATA_DIR / 'torrents_data.json'

DISCOVERED_COLLECTION = 'discovered'
WATCHED_COLLECTION = 'watched'

ACTIVITY_COLLECTIONS = {
    DISCOVERED_COLLECTION: {'name': 'Discovered', 'activity': True},
    WATCHED_COLLECTION: {'name': 'Watched', 'activity': True},
}


def _now_text():
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def _load_catalog():
    try:
        import json
        return json.loads(CATALOG_FILE.read_text('utf-8'))
    except (OSError, ValueError):
        return []


def _info_hash(magnet):
    match = re.search(r'btih:([A-Fa-f0-9]{40})', str(magnet or ''), re.I)
    return match.group(1).lower() if match else ''


def activity_key(topic):
    return (
        _info_hash(topic.get('magnet'))
        or str(topic.get('imdb_id') or '')
        or str(topic.get('kp_id') or '')
        or str(topic.get('source_topic_id') or '')
        or str(topic.get('original_topic_id') or '')
        or str(topic.get('topic_id') or '')
        or str(topic.get('title') or topic.get('movie_title') or '').strip().lower()
    )


def activity_topic_id(collection, topic):
    key = activity_key(topic)
    safe = re.sub(r'[^0-9a-zA-Z_]+', '_', key).strip('_').lower()[:80] or 'movie'
    return f'{collection}_{safe}'


def _first(*values):
    for value in values:
        if value not in (None, '', [], {}):
            return value
    return ''


def normalize_activity_topic(source, collection):
    topic = dict(source or {})
    now = _now_text()
    source_topic_id = str(topic.get('source_topic_id') or topic.get('original_topic_id') or topic.get('topic_id') or '')
    title = _first(topic.get('title'), topic.get('raw_title'), topic.get('movie_title'), topic.get('orig_title'))
    movie_title = _first(topic.get('movie_title'), topic.get('title'), topic.get('raw_title'), topic.get('orig_title'))
    year = _first(topic.get('movie_year'), topic.get('year'))
    poster_url = _first(topic.get('poster_url'), topic.get('poster'))
    trailer_url = _first(topic.get('youtube_url'), topic.get('trailer_url'))
    topic.update({
        'source_topic_id': source_topic_id,
        'original_topic_id': source_topic_id,
        'topic_id': activity_topic_id(collection, topic),
        'collection': collection,
        'source': 'activity',
        'title': str(title or movie_title or '').strip(),
        'movie_title': str(movie_title or title or '').strip(),
        'orig_title': str(topic.get('orig_title') or ''),
        'movie_year': str(year or ''),
        'author': str(topic.get('author') or 'Kino Gallery'),
        'date_str': now,
        'added_at': now,
        'activity_at': now,
        'topic_url': str(topic.get('topic_url') or '#'),
        'magnet': str(topic.get('magnet') or ''),
        'poster_url': str(poster_url or ''),
        'youtube_url': str(trailer_url or ''),
        'genre': str(topic.get('genre') or ''),
        'format': str(topic.get('format') or ''),
        'size_str': str(_first(topic.get('size_str'), topic.get('size')) or ''),
        'size_bytes': int(topic.get('size_bytes') or 0),
        'seeders': int(topic.get('seeders') or 0),
        'kp_id': str(topic.get('kp_id') or ''),
        'kp_rating': str(topic.get('kp_rating') or ''),
        'kp_votes': str(topic.get('kp_votes') or ''),
        'imdb_id': str(topic.get('imdb_id') or ''),
        'imdb_rating': str(topic.get('imdb_rating') or ''),
        'imdb_votes': str(topic.get('imdb_votes') or ''),
        'cast': str(topic.get('cast') or ''),
    })
    return topic


def upsert_activity_topic(source, collection):
    topic = normalize_activity_topic(source, collection)
    key = activity_key(topic)
    if not key or not topic.get('magnet'):
        return None
    with file_lock(CATALOG_FILE):
        catalog = _load_catalog()
        kept = []
        for item in catalog:
            if not isinstance(item, dict):
                continue
            same_collection = item.get('collection') == collection
            if same_collection and activity_key(item) == key:
                continue
            kept.append(item)
        kept.append(topic)
        atomic_write_json_unlocked(CATALOG_FILE, kept)
    return topic


def find_catalog_topic_by_magnet(magnet):
    wanted = _info_hash(magnet)
    if not wanted:
        return None
    for topic in _load_catalog():
        if isinstance(topic, dict) and _info_hash(topic.get('magnet')) == wanted:
            return dict(topic)
    return None


def record_discovered(source):
    return upsert_activity_topic(source, DISCOVERED_COLLECTION)


def record_watched(source):
    return upsert_activity_topic(source, WATCHED_COLLECTION)


def record_watched_magnet(magnet, extra=None):
    topic = find_catalog_topic_by_magnet(magnet) or {}
    topic.update(extra or {})
    topic['magnet'] = magnet
    return record_watched(topic)
