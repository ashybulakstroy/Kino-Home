"""Topic enrichment.

Normal enrichment only fills missing fields. Validation and replacement of
existing values belongs to the explicit repair path.
"""

from __future__ import annotations

import json
import re
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from difflib import SequenceMatcher
from html import unescape

from config import DATA_DIR, ENRICH_NO_CHANGE_RETRY_DAYS
from project_io import atomic_write_json


def _backend():
    # Imported lazily to keep generate_page's public API backward compatible.
    import generate_page

    return generate_page


def _fill(topic, field, value):
    if topic.get(field) or value in (None, ''):
        return False
    topic[field] = value
    return True


def _fill_kinopoisk(topic, title, year):
    gp = _backend()
    needs_id = not topic.get('kp_id')
    needs_rating = not topic.get('kp_rating')
    needs_votes = not topic.get('kp_votes')
    if not title or not (needs_id or needs_rating or needs_votes):
        return None

    if topic.get('kp_id'):
        result = gp.fetch_kinopoisk_by_id(topic['kp_id'], title, year, verify=False)
    else:
        result = gp.find_kinopoisk_for_topic(topic, title, year)
    if not result:
        return None

    _fill(topic, 'kp_id', result.get('kp_id'))
    _fill(topic, 'kp_rating', result.get('kp_rating'))
    _fill(topic, 'kp_votes', result.get('kp_votes'))
    return result


def _fill_imdb_search(topic, title, raw_name, year):
    gp = _backend()
    if topic.get('imdb_id') or not title:
        return None
    result = gp.search_imdb(title, year)
    if result is None:
        result = gp.search_imdb_deep(raw_name)
    if not result:
        return None
    if isinstance(result, str):
        _fill(topic, 'imdb_id', result)
        return {'id': result}
    _fill(topic, 'imdb_id', result.get('id'))
    _fill(topic, 'cast', result.get('cast'))
    return result


def _download_imdb_poster(topic, imdb_id, poster_url):
    gp = _backend()
    if gp.has_real_poster(topic) or not imdb_id or not poster_url:
        return False
    local_url = gp.download_poster(imdb_id, poster_url)
    if not local_url:
        return False
    topic['poster_url'] = local_url
    gp.clear_poster_failed(topic)
    return True


def _download_kp_poster(topic):
    gp = _backend()
    if gp.has_real_poster(topic) or not topic.get('kp_id'):
        return False
    local_url = gp.download_kinopoisk_poster(topic['kp_id'])
    if not local_url:
        return False
    topic['poster_url'] = local_url
    gp.clear_poster_failed(topic)
    return True


def enrich_topic(topic, force_poster_retry=False, include_trailer=True):
    """Fill missing topic attributes without replacing populated values."""
    gp = _backend()
    if topic.get('_sanitized') or topic.get('imdb_id') == '0':
        return topic

    title = topic.get('orig_title') or topic.get('movie_title') or ''
    russian_title = topic.get('movie_title') or title
    raw_name = topic.get('title') or title
    year = topic.get('movie_year') or ''
    if not title:
        return topic

    is_world = gp.is_world_topic(topic)
    poster_missing = not gp.has_real_poster(topic)
    poster_allowed = poster_missing

    if poster_missing:
        gp.resolve_existing_local_poster(topic)
    if not gp.has_real_poster(topic) and poster_allowed:
        gp.localize_existing_poster(topic)

    needs_source_page = (
        not topic.get('magnet')
        or not topic.get('format')
        or (poster_allowed and not gp.has_real_poster(topic))
        or (is_world and not topic.get('imdb_id'))
    )
    source_html = ''
    if needs_source_page and topic.get('topic_url'):
        try:
            source_html = gp.get_topic_html(
                topic.get('topic_id', ''),
                topic['topic_url'],
                timeout=10,
            )
        except Exception:
            source_html = ''

    if source_html:
        try:
            source_data = gp.parse_topic_for_magnet(source_html)
        except Exception:
            source_data = {}
        if not topic.get('magnet'):
            if _fill(topic, 'magnet', source_data.get('magnet')):
                topic.pop('_magnet_failed', None)
            else:
                topic['_magnet_failed'] = True
        _fill(topic, 'format', source_data.get('format'))
        _fill(topic, 'imdb_id', source_data.get('imdb'))
        if is_world:
            file_info = gp.parse_world_file_info(source_html)
            _fill(topic, 'format', file_info.get('format'))
            _fill(topic, 'size_str', file_info.get('size_str'))
            _fill(topic, 'size_bytes', file_info.get('size_bytes'))
        if poster_allowed and not gp.has_real_poster(topic) and source_data.get('poster'):
            gp.set_local_poster_from_url(topic, source_data['poster'])
    elif not topic.get('magnet'):
        topic['_magnet_failed'] = True

    if is_world and source_html and not topic.get('imdb_id'):
        try:
            _fill(topic, 'imdb_id', gp.parse_piratebay_detail(source_html))
        except Exception:
            pass

    if not topic.get('format') and topic.get('magnet'):
        try:
            if is_world and topic.get('source') == 'piratebay' and topic.get('topic_url'):
                detected_format = gp.fetch_piratebay_format(
                    topic.get('topic_id', ''),
                    topic['topic_url'],
                    timeout=10,
                    metadata_timeout=20,
                )
            else:
                detected_format = gp._format_from_magnet_metadata(
                    topic['magnet'],
                    timeout=20,
                )
            _fill(topic, 'format', detected_format)
        except Exception:
            pass

    imdb_search = None
    if is_world:
        imdb_search = _fill_imdb_search(topic, title, raw_name, year)
        _fill_kinopoisk(topic, russian_title, year)
    else:
        _fill_kinopoisk(topic, russian_title, year)
        imdb_search = _fill_imdb_search(topic, title, raw_name, year)

    if (
        poster_allowed
        and imdb_search
        and isinstance(imdb_search, dict)
        and imdb_search.get('poster')
    ):
        _download_imdb_poster(topic, topic.get('imdb_id'), imdb_search['poster'])

    imdb_id = topic.get('imdb_id')
    imdb_page = {}
    if imdb_id:
        if not topic.get('genre'):
            basics = gp.load_basics({imdb_id}).get(imdb_id)
            genres = basics.get('genres', '') if isinstance(basics, dict) else ''
            if genres:
                _fill(topic, 'genre', gp.clean_and_translate_genre(genres))

        if not topic.get('imdb_rating'):
            ratings = gp.load_ratings({imdb_id}).get(imdb_id)
            if isinstance(ratings, dict):
                _fill(topic, 'imdb_rating', ratings.get('rating'))
                _fill(topic, 'imdb_votes', ratings.get('votes'))

        if (
            not topic.get('genre')
            or not topic.get('imdb_rating')
            or (poster_allowed and not gp.has_real_poster(topic))
        ):
            imdb_page = gp.fetch_imdb_rating(imdb_id)
            if not topic.get('genre') and imdb_page.get('genres'):
                _fill(topic, 'genre', gp.clean_and_translate_genre(imdb_page['genres']))
            _fill(topic, 'imdb_rating', imdb_page.get('rating'))
            _fill(topic, 'imdb_votes', imdb_page.get('votes'))

    if poster_allowed and not gp.has_real_poster(topic):
        if is_world:
            _download_imdb_poster(topic, imdb_id, imdb_page.get('poster'))
            _download_kp_poster(topic)
        else:
            _download_kp_poster(topic)
            _download_imdb_poster(topic, imdb_id, imdb_page.get('poster'))

    if is_world and poster_allowed and not gp.has_real_poster(topic):
        local_url = gp.download_impawards_poster(topic)
        if local_url:
            topic['poster_url'] = local_url
            gp.clear_poster_failed(topic)

    if poster_allowed and not gp.has_real_poster(topic):
        gp.mark_poster_failed(topic)

    if include_trailer and not topic.get('youtube_url'):
        cache_key = f"{title}|{year}".lower()
        youtube_cache = gp.load_json(gp.YOUTUBE_CACHE) or {}
        cached_url = youtube_cache.get(cache_key)
        cached_ok = bool(cached_url) and (
            gp.is_verified_world_youtube_trailer(
                gp.SESSION,
                cached_url,
                title,
                year,
            )
            if is_world
            else gp._validate_youtube_url(cached_url, title, year)
        )
        if cached_ok:
            _fill(topic, 'youtube_url', cached_url)
        else:
            if cached_url:
                youtube_cache[cache_key] = None
                gp.save_json(gp.YOUTUBE_CACHE, youtube_cache)
            trailer_url = gp.resolve_topic_trailer_url(topic)
            _fill(topic, 'youtube_url', trailer_url)
            youtube_cache[cache_key] = trailer_url
            gp.save_json(gp.YOUTUBE_CACHE, youtube_cache)

    return topic


def enrich_topics(topics, ratings, basics):
    """Bulk missing-field enrichment used by a full refresh."""
    gp = _backend()
    total = len(topics)
    for index, topic in enumerate(topics, 1):
        title = topic.get('orig_title') or topic.get('movie_title') or ''
        if not title:
            continue
        imdb_id = topic.get('imdb_id')
        print(f"  [{index}/{total}] {topic.get('movie_title') or title}...", end=' ', flush=True)

        if imdb_id and not topic.get('genre'):
            basic = basics.get(imdb_id)
            genres = basic.get('genres', '') if isinstance(basic, dict) else ''
            if genres:
                _fill(topic, 'genre', gp.clean_and_translate_genre(genres))
        if imdb_id and not topic.get('imdb_rating'):
            rating = ratings.get(imdb_id)
            if isinstance(rating, dict):
                _fill(topic, 'imdb_rating', rating.get('rating'))
                _fill(topic, 'imdb_votes', rating.get('votes'))

        before = {
            key: topic.get(key)
            for key in (
                'poster_url',
                'imdb_id',
                'imdb_rating',
                'kp_id',
                'kp_rating',
                'genre',
                'format',
                'youtube_url',
            )
        }
        enrich_topic(topic, include_trailer=True)
        added = [key for key, old in before.items() if not old and topic.get(key)]
        print('добавлено: ' + ', '.join(added) if added else 'без изменений')
        time.sleep(0.1)
    return topics


def enrich_collection_formats(topics, collection=None, worker_count=None, metadata_timeout=20):
    """Fill missing video formats for one collection using page file lists first."""
    gp = _backend()
    targets = [
        topic
        for topic in topics
        if not topic.get('format')
        and (not collection or topic.get('collection') == collection)
        and not topic.get('_sanitized')
    ]
    workers = max(1, int(worker_count or gp.WORKER_COUNT))
    stats = {
        'total': len(targets),
        'page': 0,
        'metadata': 0,
        'missing': 0,
    }
    if not targets:
        print('  Форматы: пустых полей нет')
        return stats

    print(f'  Форматы: задач {len(targets)}, воркеров {min(workers, len(targets))}')

    def process(topic):
        html = ''
        if topic.get('topic_url'):
            html = gp.get_topic_html(
                topic.get('topic_id', ''),
                topic['topic_url'],
                timeout=10,
            ) or ''
        file_info = gp.parse_world_file_info(html) if html else {}
        detected_format = file_info.get('format') or (
            gp.parse_piratebay_format(html) if html else ''
        )
        source = 'page' if detected_format else ''
        if not detected_format and topic.get('magnet'):
            detected_format = gp._format_from_magnet_metadata(
                topic['magnet'],
                timeout=metadata_timeout,
            )
            source = 'metadata' if detected_format else ''
        if detected_format:
            _fill(topic, 'format', detected_format)
            _fill(topic, 'size_str', file_info.get('size_str'))
            _fill(topic, 'size_bytes', file_info.get('size_bytes'))
        return (
            topic.get('topic_id'),
            topic.get('movie_title') or topic.get('title') or '?',
            detected_format,
            source,
            file_info.get('size_str') or topic.get('size_str') or '',
        )

    with ThreadPoolExecutor(max_workers=min(workers, len(targets))) as executor:
        futures = {executor.submit(process, topic): topic for topic in targets}
        for future in as_completed(futures):
            try:
                topic_id, title, detected_format, source, size_text = future.result()
            except Exception as exc:
                topic = futures[future]
                topic_id = topic.get('topic_id')
                title = topic.get('movie_title') or topic.get('title') or '?'
                print(f'    #{topic_id} {title}: ошибка {exc}')
                stats['missing'] += 1
                continue
            if detected_format:
                stats[source] += 1
                size_suffix = f', {size_text}' if size_text else ''
                print(f'    #{topic_id} {title}: {detected_format}{size_suffix} ({source})')
            else:
                stats['missing'] += 1
                print(f'    #{topic_id} {title}: формат не найден')

    print(
        f"  Форматы: страница={stats['page']}, metadata={stats['metadata']}, "
        f"не найдено={stats['missing']}"
    )
    return stats


def repair_topic(topic, include_trailer=True):
    """Explicitly validate selected existing fields, then fill resulting gaps."""
    gp = _backend()
    if topic.get('_sanitized') or topic.get('imdb_id') == '0':
        return topic

    title = topic.get('orig_title') or topic.get('movie_title') or ''
    russian_title = topic.get('movie_title') or title
    year = topic.get('movie_year') or ''

    if gp.is_listing_category_genre(topic.get('genre')):
        topic['genre'] = ''

    if gp.is_world_topic(topic) and topic.get('topic_url'):
        try:
            html = gp.get_topic_html(topic.get('topic_id', ''), topic['topic_url'], timeout=10)
            verified_imdb = gp.parse_piratebay_detail(html) if html else None
            if verified_imdb and verified_imdb != topic.get('imdb_id'):
                topic['imdb_id'] = verified_imdb
                if str(topic.get('poster_url') or '').startswith('data/posters/tt'):
                    topic['poster_url'] = ''
        except Exception:
            pass

    if title:
        gp.search_topic_kinopoisk(topic, russian_title, year)
    return enrich_topic(topic, force_poster_retry=True, include_trailer=include_trailer)


# Trailer fallback

TRAILER_POSITIVE_WORDS = ("trailer", "трейлер", "teaser", "тизер")
TRAILER_NEGATIVE_WORDS = (
    "review", "reaction", "explained", "ending", "soundtrack", "song",
    "clip", "scene", "interview", "behind the scenes", "gameplay",
    "season", "series", "episode", "ps4", "ps5", "xbox", "nintendo",
    "обзор", "реакция", "разбор", "концовка", "саундтрек", "песня",
    "клип", "сцена", "интервью", "со съемок", "серия", "эпизод",
    "сезон", "сериал", "игра",
)
TRAILER_TRUSTED_CHANNEL_WORDS = (
    "kinopoisk", "кинопоиск", "netflix", "disney", "warner",
    "paramount", "universal", "sony", "film festival", "киноафиша",
    "трейлеры", "что в кино", "arrow video",
)

_TRAILER_CYRILLIC_TO_LATIN = str.maketrans({
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d",
    "е": "e", "ё": "yo", "ж": "zh", "з": "z", "и": "i",
    "й": "y", "к": "k", "л": "l", "м": "m", "н": "n",
    "о": "o", "п": "p", "р": "r", "с": "s", "т": "t",
    "у": "u", "ф": "f", "х": "kh", "ц": "ts", "ч": "ch",
    "ш": "sh", "щ": "shch", "ъ": "", "ы": "y", "ь": "",
    "э": "e", "ю": "yu", "я": "ya",
})


def _trailer_json_text(value):
    if not value:
        return ""
    try:
        return unescape(json.loads(f'"{value}"'))
    except Exception:
        return unescape(value)


def _trailer_compact(value):
    value = unescape(str(value or "")).lower().replace("ё", "е")
    value = value.replace("khz", "kilohertz").replace("кгц", "килогерц")
    value = re.sub(r"[^a-zа-я0-9]+", " ", value, flags=re.I)
    return re.sub(r"\s+", " ", value).strip()


def _trailer_latin(value):
    return _trailer_compact(value).translate(_TRAILER_CYRILLIC_TO_LATIN)


def _trailer_identity_tokens(value):
    ignored = {
        "official", "trailer", "teaser", "movie", "film", "hd", "russkiy",
        "ofitsialnyy", "treyler", "tizer", "kino", "subtitry",
    }
    return {
        token for token in _trailer_latin(value).split()
        if token not in ignored and not re.fullmatch(r"(?:19|20)\d{2}", token)
        and len(token) >= 2
    }


def trailer_title_variants(topic):
    """Return distinct parsed names without changing persisted topic titles."""
    values = [topic.get("movie_title"), topic.get("orig_title")]
    raw = str(topic.get("title") or "")
    title_part = raw.split("(", 1)[0].split("[", 1)[0]
    values.extend(part.strip() for part in title_part.split("/"))

    result = []
    seen = set()
    for value in values:
        value = re.sub(r"\s+", " ", str(value or "")).strip(" ._-")
        key = _trailer_compact(value)
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(value)
    return result[:5]


def _trailer_youtube_candidates(html):
    candidates = []
    seen = set()
    for match in re.finditer(r'"videoId":"([a-zA-Z0-9_-]{11})"', html or ""):
        video_id = match.group(1)
        if video_id in seen:
            continue
        seen.add(video_id)
        chunk = html[match.start():match.start() + 3500]
        title_match = re.search(r'"title":\{"runs":\[\{"text":"(.*?)"', chunk)
        if not title_match:
            title_match = re.search(r'"title":\{"simpleText":"(.*?)"', chunk)
        if not title_match:
            continue
        channel_match = re.search(r'"ownerText":\{"runs":\[\{"text":"(.*?)"', chunk)
        candidates.append({
            "video_id": video_id,
            "title": _trailer_json_text(title_match.group(1)),
            "channel": (
                _trailer_json_text(channel_match.group(1))
                if channel_match else ""
            ),
        })
        if len(candidates) >= 20:
            break
    return candidates


def score_trailer_candidate(candidate, title_variants, year):
    """Score identity and trailer intent; return zero for unsafe matches."""
    candidate_title = _trailer_compact(candidate.get("title"))
    candidate_latin = _trailer_latin(candidate_title)
    channel = _trailer_compact(candidate.get("channel"))
    if (
        not candidate_title
        or any(word in candidate_title for word in TRAILER_NEGATIVE_WORDS)
    ):
        return 0
    if not any(word in candidate_title for word in TRAILER_POSITIVE_WORDS):
        return 0

    candidate_tokens = _trailer_identity_tokens(candidate_title)
    best_identity = 0
    best_variant_tokens = set()
    for variant in title_variants:
        variant_latin = _trailer_latin(variant)
        variant_tokens = _trailer_identity_tokens(variant)
        if not variant_latin or not variant_tokens:
            continue
        score = 0
        if variant_latin in candidate_latin:
            score += 60
        overlap = len(variant_tokens & candidate_tokens) / max(
            len(variant_tokens),
            1,
        )
        score += int(overlap * 45)
        score += int(
            SequenceMatcher(None, variant_latin, candidate_latin).ratio() * 20
        )
        if score > best_identity:
            best_identity = score
            best_variant_tokens = variant_tokens

    if best_identity < 45:
        return 0

    candidate_years = {
        int(value)
        for value in re.findall(
            r"(?<!\d)(?:19|20)\d{2}(?!\d)",
            candidate_title,
        )
    }
    try:
        wanted_year = int(year or 0)
    except (TypeError, ValueError):
        wanted_year = 0

    year_distance = None
    if wanted_year and candidate_years:
        year_distance = min(abs(value - wanted_year) for value in candidate_years)
        if year_distance > 1:
            return 0

    trusted_channel = any(
        word in channel for word in TRAILER_TRUSTED_CHANNEL_WORDS
    )
    if len(best_variant_tokens) == 1:
        extra_tokens = candidate_tokens - best_variant_tokens
        exact_year_match = year_distance == 0 and len(extra_tokens) <= 1
        trusted_exact_match = (
            not candidate_years and not extra_tokens and trusted_channel
        )
        if not exact_year_match and not trusted_exact_match:
            return 0

    score = best_identity
    if "trailer" in candidate_title or "трейлер" in candidate_title:
        score += 30
    else:
        score += 22
    if "official" in candidate_title or "официальн" in candidate_title:
        score += 8
    if trusted_channel:
        score += 8
    if wanted_year and candidate_years:
        if year_distance == 0:
            score += 15
        elif year_distance == 1:
            score -= 5
    return score


def _trailer_oembed(session, video_url):
    url = (
        "https://www.youtube.com/oembed?format=json&url="
        + urllib.parse.quote(video_url, safe="")
    )
    response = session.get(url, timeout=8)
    if response.status_code != 200:
        return None
    return response.json()


def search_topic_trailer_fallback(session, topic):
    """Search all known title variants and return a verified YouTube URL."""
    variants = trailer_title_variants(topic)
    if not variants:
        return None
    year = topic.get("movie_year") or topic.get("year") or ""

    queries = []
    for variant in variants[:3]:
        if re.search(r"[а-яё]", variant, flags=re.I):
            queries.append(f'"{variant}" {year} трейлер тизер')
        queries.append(f'"{variant}" {year} trailer teaser')

    ranked = {}
    for query in queries[:6]:
        try:
            url = (
                "https://www.youtube.com/results?search_query="
                + urllib.parse.quote(query)
            )
            response = session.get(url, timeout=10)
            if response.status_code != 200:
                continue
        except Exception:
            continue
        for candidate in _trailer_youtube_candidates(response.text):
            score = score_trailer_candidate(candidate, variants, year)
            video_id = candidate["video_id"]
            if score > ranked.get(video_id, {}).get("score", 0):
                ranked[video_id] = {**candidate, "score": score}

    ranked_candidates = sorted(
        ranked.values(),
        key=lambda item: item["score"],
        reverse=True,
    )[:3]
    for candidate in ranked_candidates:
        if candidate["score"] < 95:
            continue
        video_url = f'https://www.youtube.com/watch?v={candidate["video_id"]}'
        try:
            metadata = _trailer_oembed(session, video_url)
        except Exception:
            continue
        if not metadata:
            continue
        verified = {
            "title": metadata.get("title", ""),
            "channel": metadata.get("author_name", ""),
        }
        if score_trailer_candidate(verified, variants, year) >= 85:
            return video_url
    return None


# Kinopoisk ID fallback

KINOPOISK_SPARQL_URL = "https://query.wikidata.org/sparql"
KINOPOISK_FALLBACK_CACHE_PATH = DATA_DIR / "kp_fallback_cache.json"
KINOPOISK_USER_AGENT = "KinoGallery/1.0 (local media catalog)"

_KINOPOISK_REQUEST_SLOTS = threading.BoundedSemaphore(3)
_KINOPOISK_CACHE_LOCK = threading.Lock()
_KINOPOISK_KEY_LOCKS: dict[str, threading.Lock] = {}
_KINOPOISK_KEY_LOCKS_GUARD = threading.Lock()


def _kinopoisk_normalize(value):
    value = str(value or "").lower().replace("ё", "е")
    value = re.sub(r"[^a-zа-я0-9]+", " ", value, flags=re.I)
    return re.sub(r"\s+", " ", value).strip()


def _kinopoisk_title_tokens(value):
    ignored = {"film", "movie", "фильм", "кино"}
    return {
        token for token in _kinopoisk_normalize(value).split()
        if token not in ignored and len(token) >= 2
    }


def kinopoisk_title_variants(topic, fallback_title=""):
    values = [
        topic.get("movie_title"),
        topic.get("orig_title"),
        fallback_title,
    ]
    raw = str(topic.get("title") or "")
    title_part = raw.split("(", 1)[0].split("[", 1)[0]
    values.extend(part.strip() for part in title_part.split("/"))

    result = []
    seen = set()
    for value in values:
        value = re.sub(r"\s+", " ", str(value or "")).strip(" ._-")
        key = _kinopoisk_normalize(value)
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(value)
    return result[:5]


def score_kinopoisk_title_candidate(wanted_title, wanted_year, candidate):
    label = candidate.get("label") or ""
    wanted = _kinopoisk_normalize(wanted_title)
    actual = _kinopoisk_normalize(label)
    wanted_tokens = _kinopoisk_title_tokens(wanted)
    actual_tokens = _kinopoisk_title_tokens(actual)
    if not wanted or not actual or not wanted_tokens or not actual_tokens:
        return 0

    try:
        year = int(wanted_year or 0)
    except (TypeError, ValueError):
        year = 0
    years = {
        int(value)
        for value in candidate.get("years", [])
        if str(value).isdigit()
    }
    if year and (
        not years or min(abs(value - year) for value in years) > 1
    ):
        return 0

    overlap = len(wanted_tokens & actual_tokens) / max(len(wanted_tokens), 1)
    exact_tokens = wanted_tokens == actual_tokens
    if len(wanted_tokens) == 1 and not exact_tokens:
        return 0
    if not exact_tokens and overlap < 0.8:
        return 0

    score = (
        100 if wanted == actual
        else 75 if exact_tokens
        else int(overlap * 70)
    )
    score += int(SequenceMatcher(None, wanted, actual).ratio() * 20)
    if year and years:
        distance = min(abs(value - year) for value in years)
        score += 20 if distance == 0 else 10
    return score


def select_kinopoisk_title_candidate(title_variants, year, candidates):
    ranked = []
    for candidate in candidates:
        kp_id = str(candidate.get("kp_id") or "")
        if not kp_id.isdigit():
            continue
        score = max(
            (
                score_kinopoisk_title_candidate(title, year, candidate)
                for title in title_variants
            ),
            default=0,
        )
        if score:
            ranked.append((score, kp_id, candidate))
    if not ranked:
        return None

    ranked.sort(key=lambda item: item[0], reverse=True)
    best_score, best_id, best = ranked[0]
    competing_ids = {
        kp_id
        for score, kp_id, _candidate in ranked
        if score >= best_score - 5
    }
    if len(competing_ids) > 1:
        return None
    return {**best, "kp_id": best_id, "score": best_score}


def _kinopoisk_sparql_rows(session, query):
    headers = {
        "User-Agent": KINOPOISK_USER_AGENT,
        "Accept": "application/sparql-results+json",
    }
    for attempt in range(3):
        try:
            with _KINOPOISK_REQUEST_SLOTS:
                response = session.get(
                    KINOPOISK_SPARQL_URL,
                    params={"query": query, "format": "json"},
                    headers=headers,
                    timeout=60,
                )
            if response.status_code == 200:
                data = response.json()
                return True, data.get("results", {}).get("bindings", [])
            if response.status_code not in (429, 500, 502, 503, 504):
                return True, []
        except Exception:
            pass
        time.sleep(attempt + 1)
    return False, []


def _kinopoisk_sparql_literal(value):
    return json.dumps(str(value or ""), ensure_ascii=False)


def _kinopoisk_lookup_by_imdb(session, imdb_id):
    query = (
        "SELECT ?item ?kp WHERE { "
        f"?item wdt:P345 {_kinopoisk_sparql_literal(imdb_id)}; "
        "wdt:P2603 ?kp. "
        "} LIMIT 5"
    )
    ok, rows = _kinopoisk_sparql_rows(session, query)
    if not ok:
        return False, None
    candidates = []
    for row in rows:
        kp_id = str(row.get("kp", {}).get("value") or "")
        qid = str(row.get("item", {}).get("value") or "").rsplit("/", 1)[-1]
        if kp_id.isdigit():
            candidates.append((kp_id, qid))
    unique_ids = {kp_id for kp_id, _qid in candidates}
    if len(unique_ids) != 1:
        return True, None
    kp_id, qid = candidates[0]
    return True, {
        "kp_id": kp_id,
        "wikidata_id": qid,
        "source": "wikidata-imdb",
    }


def _kinopoisk_entity_search_query(title):
    return f"""
SELECT ?item ?itemLabel ?kp ?date WHERE {{
  SERVICE wikibase:mwapi {{
    bd:serviceParam wikibase:endpoint "www.wikidata.org";
                    wikibase:api "EntitySearch";
                    mwapi:search {_kinopoisk_sparql_literal(title)};
                    mwapi:language "ru".
    ?item wikibase:apiOutputItem mwapi:item.
  }}
  ?item wdt:P2603 ?kp.
  OPTIONAL {{ ?item wdt:P577 ?date. }}
  SERVICE wikibase:label {{ bd:serviceParam wikibase:language "ru,en". }}
}}
LIMIT 12
""".strip()


def _kinopoisk_rows_to_candidates(rows):
    grouped = {}
    for row in rows:
        kp_id = str(row.get("kp", {}).get("value") or "")
        qid = str(row.get("item", {}).get("value") or "").rsplit("/", 1)[-1]
        if not kp_id.isdigit() or not qid:
            continue
        key = (qid, kp_id)
        candidate = grouped.setdefault(key, {
            "kp_id": kp_id,
            "wikidata_id": qid,
            "label": str(row.get("itemLabel", {}).get("value") or ""),
            "years": set(),
            "source": "wikidata-title",
        })
        date_value = str(row.get("date", {}).get("value") or "")
        match = re.match(r"([+-]?\d{4})", date_value)
        if match:
            candidate["years"].add(abs(int(match.group(1))))
    result = []
    for candidate in grouped.values():
        candidate["years"] = sorted(candidate["years"])
        result.append(candidate)
    return result


def _kinopoisk_lookup_by_title(session, variants, year):
    all_candidates = []
    request_succeeded = False
    for title in variants[:3]:
        ok, rows = _kinopoisk_sparql_rows(
            session,
            _kinopoisk_entity_search_query(title),
        )
        request_succeeded = request_succeeded or ok
        if ok:
            all_candidates.extend(_kinopoisk_rows_to_candidates(rows))
        candidate = select_kinopoisk_title_candidate(
            variants,
            year,
            all_candidates,
        )
        if candidate and candidate.get("score", 0) >= 90:
            return True, candidate
    return request_succeeded, select_kinopoisk_title_candidate(
        variants,
        year,
        all_candidates,
    )


def _kinopoisk_cache_key(topic, title, year):
    imdb_id = str(topic.get("imdb_id") or "").strip()
    if imdb_id and imdb_id != "0":
        return f"imdb:{imdb_id}"
    return f"title:{_kinopoisk_normalize(title)}|{year or ''}"


def _kinopoisk_key_lock(key):
    with _KINOPOISK_KEY_LOCKS_GUARD:
        return _KINOPOISK_KEY_LOCKS.setdefault(key, threading.Lock())


def _kinopoisk_read_cache_entry(key):
    with _KINOPOISK_CACHE_LOCK:
        try:
            cache = json.loads(
                KINOPOISK_FALLBACK_CACHE_PATH.read_text("utf-8")
            )
        except (OSError, json.JSONDecodeError):
            return False, None
        entry = cache.get(key)
    if not isinstance(entry, dict):
        return False, None
    try:
        checked = datetime.fromisoformat(str(entry.get("checked_at"))).date()
    except (TypeError, ValueError):
        return False, None
    ttl = max(1, ENRICH_NO_CHANGE_RETRY_DAYS)
    if date.today() - checked >= timedelta(days=ttl):
        return False, None
    return True, entry.get("result")


def _kinopoisk_write_cache_entry(key, result):
    with _KINOPOISK_CACHE_LOCK:
        try:
            cache = json.loads(
                KINOPOISK_FALLBACK_CACHE_PATH.read_text("utf-8")
            )
        except (OSError, json.JSONDecodeError):
            cache = {}
        cache[key] = {
            "checked_at": date.today().isoformat(),
            "result": result,
        }
        atomic_write_json(KINOPOISK_FALLBACK_CACHE_PATH, cache)


def find_kinopoisk_id_fallback(
    session,
    topic,
    title,
    year,
    use_cache=True,
):
    variants = kinopoisk_title_variants(topic, title)
    if not variants:
        return None
    key = _kinopoisk_cache_key(topic, variants[0], year)

    with _kinopoisk_key_lock(key):
        if use_cache:
            cached, result = _kinopoisk_read_cache_entry(key)
            if cached:
                return result

        imdb_id = str(topic.get("imdb_id") or "").strip()
        request_ok = False
        result = None
        if imdb_id and imdb_id != "0":
            request_ok, result = _kinopoisk_lookup_by_imdb(
                session,
                imdb_id,
            )
        else:
            title_ok, result = _kinopoisk_lookup_by_title(
                session,
                variants,
                year,
            )
            request_ok = request_ok or title_ok

        if use_cache and request_ok:
            _kinopoisk_write_cache_entry(key, result)
        return result
