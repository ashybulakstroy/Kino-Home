"""Topic enrichment.

Normal enrichment only fills missing fields. Validation and replacement of
existing values belongs to the explicit repair path.
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed


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
