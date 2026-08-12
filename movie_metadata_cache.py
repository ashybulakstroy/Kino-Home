import copy
import json
import re
from datetime import datetime
from pathlib import Path

from project_io import atomic_write_json_unlocked, file_lock


CACHE_VERSION = 2
SHARED_FIELDS = (
    'imdb_id',
    'kp_id',
    'imdb_rating',
    'imdb_votes',
    'kp_rating',
    'kp_votes',
    'poster_url',
    'youtube_url',
    'genre',
    'cast',
)
ID_FIELDS = ('imdb_id', 'kp_id')


def _has_value(value):
    return value not in (None, '', 0, '0', [], {})


def _normalize_title(value):
    text = str(value or '').casefold().replace('ё', 'е')
    text = re.sub(r'[^0-9a-zа-я]+', ' ', text)
    return re.sub(r'\s+', ' ', text).strip()


def _normalize_year(value):
    match = re.search(r'\b(19\d{2}|20\d{2})\b', str(value or ''))
    return match.group(1) if match else ''


def _normalize_imdb_id(value):
    match = re.fullmatch(r'tt\d+', str(value or '').strip(), flags=re.I)
    if not match:
        return ''
    digits = match.group(0)[2:]
    return f'tt{digits.zfill(7)}'


def _normalize_kp_id(value):
    value = str(value or '').strip()
    return value if value.isdigit() and value != '0' else ''


def topic_identity_keys(topic):
    keys = []
    imdb_id = _normalize_imdb_id(topic.get('imdb_id'))
    kp_id = _normalize_kp_id(topic.get('kp_id'))
    if imdb_id:
        keys.append(f'imdb:{imdb_id}')
    if kp_id:
        keys.append(f'kp:{kp_id}')

    year = _normalize_year(topic.get('movie_year') or topic.get('year'))
    if year:
        seen_titles = set()
        for field in ('movie_title', 'orig_title'):
            title = _normalize_title(topic.get(field))
            if len(title) < 2 or title in seen_titles:
                continue
            seen_titles.add(title)
            keys.append(f'title:{title}|{year}')
    topic_id = str(topic.get('topic_id') or '').strip()
    if topic_id:
        keys.append(f'topic:{topic_id}')
    return keys


def _valid_poster_url(value, cache_path):
    value = str(value or '').strip().replace('\\', '/')
    if not value or value == '0' or value.endswith('/placeholder.png'):
        return False
    if value.startswith('data/posters/'):
        poster_path = Path(cache_path).parent / 'posters' / value.rsplit('/', 1)[-1]
        return poster_path.is_file()
    return value.startswith(('http://', 'https://'))


def _field_value(topic, field, cache_path):
    value = topic.get(field)
    if not _has_value(value):
        return ''
    if field == 'imdb_id':
        return _normalize_imdb_id(value)
    if field == 'kp_id':
        return _normalize_kp_id(value)
    if field == 'poster_url' and not _valid_poster_url(value, cache_path):
        return ''
    return value


def _empty_cache():
    return {'version': CACHE_VERSION, 'movies': [], '_migrated': False}


def _normalize_identity_key(key):
    key = str(key or '')
    if key.startswith('imdb:'):
        imdb_id = _normalize_imdb_id(key.split(':', 1)[1])
        return f'imdb:{imdb_id}' if imdb_id else ''
    return key


def _load_cache_unlocked(cache_path):
    try:
        data = json.loads(Path(cache_path).read_text('utf-8'))
    except (OSError, ValueError):
        return _empty_cache()
    if not isinstance(data, dict) or not isinstance(data.get('movies'), list):
        return _empty_cache()
    migrated = data.get('version') != CACHE_VERSION
    movies = []
    for raw in data['movies']:
        if not isinstance(raw, dict):
            continue
        raw_keys = {str(key) for key in raw.get('keys', []) if key}
        keys = sorted({
            normalized for key in raw_keys
            if (normalized := _normalize_identity_key(key))
        })
        if set(keys) != raw_keys:
            migrated = True
        metadata = raw.get('metadata') if isinstance(raw.get('metadata'), dict) else {}
        if not keys:
            continue
        clean_metadata = {
            field: value
            for field in SHARED_FIELDS
            if _has_value(value := metadata.get(field))
        }
        if clean_metadata.get('imdb_id'):
            normalized_imdb = _normalize_imdb_id(clean_metadata['imdb_id'])
            if normalized_imdb != clean_metadata['imdb_id']:
                migrated = True
            clean_metadata['imdb_id'] = normalized_imdb
        if clean_metadata.get('kp_id'):
            clean_metadata['kp_id'] = _normalize_kp_id(clean_metadata['kp_id'])
        movies.append({
            'keys': keys,
            'metadata': clean_metadata,
            'blocked_fields': sorted({
                str(field) for field in raw.get('blocked_fields', [])
                if field in SHARED_FIELDS
            }),
            'updated_at': str(raw.get('updated_at') or ''),
        })
    return {'version': CACHE_VERSION, 'movies': movies, '_migrated': migrated}


def load_movie_metadata_cache(cache_path):
    with file_lock(cache_path):
        cache = _load_cache_unlocked(cache_path)
        cache.pop('_migrated', None)
        return cache


def _record_compatible(record, topic):
    metadata = record.get('metadata', {})
    topic_imdb = _normalize_imdb_id(topic.get('imdb_id'))
    topic_kp = _normalize_kp_id(topic.get('kp_id'))
    record_imdb = _normalize_imdb_id(metadata.get('imdb_id'))
    record_kp = _normalize_kp_id(metadata.get('kp_id'))
    if topic_imdb and record_imdb:
        return topic_imdb == record_imdb
    if topic_kp and record_kp and topic_kp != record_kp:
        return False
    return True


def _build_key_index(records):
    index = {}
    for record_index, record in enumerate(records):
        for key in record.get('keys', []):
            index.setdefault(key, set()).add(record_index)
    return index


def _matching_record_indexes(records, topic):
    keys = topic_identity_keys(topic)
    if not keys:
        return [], keys, ''
    index = _build_key_index(records)
    title_keys = [key for key in keys if key.startswith('title:')]
    topic_keys = [key for key in keys if key.startswith('topic:')]
    topic_matches = {
        record_index
        for key in topic_keys
        for record_index in index.get(key, set())
        if _record_compatible(records[record_index], topic)
    }
    imdb_keys = [key for key in keys if key.startswith('imdb:')]
    imdb_matches = {
        record_index
        for key in imdb_keys
        for record_index in index.get(key, set())
        if _record_compatible(records[record_index], topic)
    }
    if imdb_matches:
        return sorted(imdb_matches | topic_matches), keys, 'imdb'

    topic_title_keys = set(title_keys)
    kp_keys = [key for key in keys if key.startswith('kp:')]
    kp_matches = {
        record_index
        for key in kp_keys
        for record_index in index.get(key, set())
        if _record_compatible(records[record_index], topic)
        and topic_title_keys.intersection(records[record_index].get('keys', []))
    }
    if kp_matches:
        return sorted(kp_matches | topic_matches), keys, 'kp'

    title_matches = {
        record_index
        for key in title_keys
        for record_index in index.get(key, set())
        if _record_compatible(records[record_index], topic)
    }
    title_records = [records[record_index] for record_index in sorted(title_matches)]
    title_is_ambiguous = any(
        not _records_compatible(left, right)
        for index, left in enumerate(title_records)
        for right in title_records[index + 1:]
    )
    if title_is_ambiguous:
        title_matches = set()
    if title_matches:
        return sorted(title_matches | topic_matches), keys, 'title'
    if topic_matches:
        return sorted(topic_matches), keys, 'topic'
    return [], keys, ''


def _records_compatible(left, right):
    left_meta = left.get('metadata', {})
    right_meta = right.get('metadata', {})
    left_imdb = _normalize_imdb_id(left_meta.get('imdb_id'))
    right_imdb = _normalize_imdb_id(right_meta.get('imdb_id'))
    if left_imdb and right_imdb:
        return left_imdb == right_imdb
    left_kp = _normalize_kp_id(left_meta.get('kp_id'))
    right_kp = _normalize_kp_id(right_meta.get('kp_id'))
    if left_kp and right_kp and left_kp != right_kp:
        return False
    return True


def _merge_record_indexes(records, indexes):
    if not indexes:
        return None, 0
    primary_index = indexes[0]
    primary = records[primary_index]
    merged_count = 0
    for other_index in reversed(indexes[1:]):
        other = records[other_index]
        if not _records_compatible(primary, other):
            continue
        primary_meta = primary.setdefault('metadata', {})
        other_meta = other.get('metadata', {})
        same_imdb = (
            _normalize_imdb_id(primary_meta.get('imdb_id'))
            and _normalize_imdb_id(primary_meta.get('imdb_id'))
            == _normalize_imdb_id(other_meta.get('imdb_id'))
        )
        conflicting_kp = (
            _normalize_kp_id(primary_meta.get('kp_id'))
            and _normalize_kp_id(other_meta.get('kp_id'))
            and _normalize_kp_id(primary_meta.get('kp_id'))
            != _normalize_kp_id(other_meta.get('kp_id'))
        )
        primary['keys'] = sorted(set(primary.get('keys', [])) | set(other.get('keys', [])))
        blocked_fields = (
            set(primary.get('blocked_fields', []))
            | set(other.get('blocked_fields', []))
        )
        if same_imdb and conflicting_kp:
            blocked_fields.update(('kp_id', 'kp_rating', 'kp_votes'))
            for field in ('kp_id', 'kp_rating', 'kp_votes'):
                primary_meta.pop(field, None)
        primary['blocked_fields'] = sorted(blocked_fields)
        for field, value in other_meta.items():
            if field in blocked_fields:
                continue
            if not _has_value(primary.get('metadata', {}).get(field)) and _has_value(value):
                primary.setdefault('metadata', {})[field] = value
        records.pop(other_index)
        if other_index < primary_index:
            primary_index -= 1
        merged_count += 1
    return records[primary_index], merged_count


def _attach_unambiguous_keys(records, record, keys):
    changed = False
    index = _build_key_index(records)
    own_index = next(i for i, item in enumerate(records) if item is record)
    for key in keys:
        conflicting = [
            records[i]
            for i in index.get(key, set())
            if i != own_index and not _records_compatible(record, records[i])
        ]
        if (conflicting and not key.startswith('title:')) or key in record.get('keys', []):
            continue
        record.setdefault('keys', []).append(key)
        changed = True
    if changed:
        record['keys'] = sorted(set(record['keys']))
    return changed


def _apply_record_to_topic(record, topic, cache_path, replace_fields=()):
    if topic.get('_sanitized'):
        return []
    applied = []
    metadata = record.get('metadata', {})
    replace_fields = set(replace_fields or ())
    for field in SHARED_FIELDS:
        if _has_value(topic.get(field)) and field not in replace_fields:
            continue
        value = _field_value(metadata, field, cache_path)
        if not _has_value(value):
            continue
        if topic.get(field) == value:
            continue
        topic[field] = copy.deepcopy(value)
        applied.append(field)
    return applied


def _update_record_from_topic(record, topic, cache_path, replace_fields=()):
    changed = False
    metadata = record.setdefault('metadata', {})
    replace_fields = set(replace_fields or ())
    blocked_fields = set(record.get('blocked_fields', []))
    for field in SHARED_FIELDS:
        value = _field_value(topic, field, cache_path)
        if not _has_value(value):
            continue
        if (
            field == 'kp_id'
            and _has_value(metadata.get('kp_id'))
            and metadata.get('kp_id') != value
            and _normalize_imdb_id(metadata.get('imdb_id'))
            and _normalize_imdb_id(metadata.get('imdb_id'))
            == _normalize_imdb_id(topic.get('imdb_id'))
            and field not in replace_fields
        ):
            for kp_field in ('kp_id', 'kp_rating', 'kp_votes'):
                metadata.pop(kp_field, None)
                blocked_fields.add(kp_field)
            changed = True
            continue
        if field in blocked_fields and field not in replace_fields:
            continue
        if _has_value(metadata.get(field)):
            if field not in replace_fields or metadata.get(field) == value:
                continue
        metadata[field] = copy.deepcopy(value)
        if field in blocked_fields:
            blocked_fields.remove(field)
        changed = True
    record['blocked_fields'] = sorted(blocked_fields)
    if changed:
        record['updated_at'] = datetime.now().isoformat(timespec='seconds')
    return changed


def _record_for_topic(records, topic, create=False):
    indexes, keys, match_kind = _matching_record_indexes(records, topic)
    record, merged_count = _merge_record_indexes(records, indexes)
    created = False
    if record is None and create and keys:
        record = {'keys': [], 'metadata': {}, 'blocked_fields': [], 'updated_at': ''}
        records.append(record)
        created = True
    keys_changed = False
    if record is not None:
        attach_keys = keys
        if match_kind == 'kp':
            attach_keys = [key for key in keys if not key.startswith('title:')]
        keys_changed = _attach_unambiguous_keys(records, record, attach_keys)
        if keys_changed and not record.get('updated_at'):
            record['updated_at'] = datetime.now().isoformat(timespec='seconds')
    return record, created, merged_count, keys_changed, match_kind


def apply_cached_movie_metadata(topic, cache_path, replace_fields=()):
    if not isinstance(topic, dict) or topic.get('_sanitized'):
        return []
    with file_lock(cache_path):
        cache = _load_cache_unlocked(cache_path)
        record, _created, _merged, _keys_changed, _match_kind = _record_for_topic(
            cache['movies'], topic, create=False
        )
        if record is None:
            return []
        return _apply_record_to_topic(
            record,
            topic,
            cache_path,
            replace_fields=replace_fields,
        )


def invalidate_cached_movie_metadata(topic, cache_path, fields):
    fields = {field for field in fields or () if field in SHARED_FIELDS}
    if not fields or not isinstance(topic, dict):
        return []
    with file_lock(cache_path):
        cache = _load_cache_unlocked(cache_path)
        record, _created, _merged, keys_changed, _match_kind = _record_for_topic(
            cache['movies'], topic, create=False
        )
        if record is None:
            return []
        metadata = record.setdefault('metadata', {})
        removed = [field for field in fields if field in metadata]
        for field in removed:
            metadata.pop(field, None)
        blocked_fields = set(record.get('blocked_fields', []))
        newly_blocked = fields - blocked_fields
        record['blocked_fields'] = sorted(blocked_fields | fields)
        if not removed and not newly_blocked and not keys_changed:
            return []
        record['updated_at'] = datetime.now().isoformat(timespec='seconds')
        atomic_write_json_unlocked(cache_path, {
            'version': CACHE_VERSION,
            'updated_at': datetime.now().isoformat(timespec='seconds'),
            'movies': cache['movies'],
        })
        return sorted(removed)


def sync_movie_metadata_cache(topics, cache_path, replace_fields=()):
    valid_topics = [
        topic for topic in topics or []
        if isinstance(topic, dict) and not topic.get('_sanitized')
    ]
    stats = {
        'records': 0,
        'records_created': 0,
        'records_merged': 0,
        'topics_updated': 0,
        'fields_applied': 0,
        'cache_changed': False,
    }
    with file_lock(cache_path):
        cache = _load_cache_unlocked(cache_path)
        records = cache['movies']
        stats['cache_changed'] = bool(cache.pop('_migrated', False))
        changed_topics = set()

        for topic in valid_topics:
            record, created, merged, keys_changed, _match_kind = _record_for_topic(
                records, topic, create=True
            )
            if record is None:
                continue
            stats['records_created'] += int(created)
            stats['records_merged'] += merged
            applied = _apply_record_to_topic(record, topic, cache_path)
            if applied:
                changed_topics.add(id(topic))
                stats['fields_applied'] += len(applied)
            metadata_changed = _update_record_from_topic(
                record, topic, cache_path, replace_fields=replace_fields
            )
            stats['cache_changed'] = (
                stats['cache_changed'] or created or bool(merged)
                or keys_changed or metadata_changed
            )

        # A complete duplicate may appear after an incomplete one in the catalog.
        for topic in valid_topics:
            record, _created, merged, keys_changed, _match_kind = _record_for_topic(
                records, topic, create=False
            )
            if record is None:
                continue
            stats['records_merged'] += merged
            applied = _apply_record_to_topic(record, topic, cache_path)
            if applied:
                changed_topics.add(id(topic))
                stats['fields_applied'] += len(applied)
                if _update_record_from_topic(
                    record, topic, cache_path, replace_fields=replace_fields
                ):
                    stats['cache_changed'] = True
            stats['cache_changed'] = stats['cache_changed'] or bool(merged) or keys_changed

        records.sort(key=lambda record: record.get('keys', ['~'])[0])
        stats['records'] = len(records)
        stats['topics_updated'] = len(changed_topics)
        if stats['cache_changed']:
            atomic_write_json_unlocked(cache_path, {
                'version': CACHE_VERSION,
                'updated_at': datetime.now().isoformat(timespec='seconds'),
                'movies': records,
            })
    return stats
