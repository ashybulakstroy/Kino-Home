import os
import re
import sys
import time
import json
import mimetypes
import socket
import threading
import subprocess
import shutil
from datetime import date, datetime, timedelta
from flask import Flask, request, Response, jsonify, send_file, send_from_directory, abort, redirect

from config import BASE_DIR, DATA_DIR, TEMP_DIR, MAX_TEMP_SIZE_BYTES, TEMP_MAX_AGE_SECS, MAX_TEMP_FILES, SERVER_PORT, ENRICH_INTERVAL_MINUTES, TOPIC_MAX_AGE_DAYS, PUBLIC_MODE

import generate_page as gp
from project_io import atomic_write_json_unlocked, atomic_write_text, atomic_write_text_unlocked, file_lock


class _LazyEngine:
    _engine = None
    _lock = threading.Lock()

    def _ensure(self):
        if self._engine is None:
            with self._lock:
                if self._engine is None:
                    from engine import TorrentEngine
                    self._engine = TorrentEngine(temp_dir=str(TEMP_DIR))
        return self._engine

    def __getattr__(self, name):
        return getattr(self._ensure(), name)


engine = _LazyEngine()

app = Flask(__name__, static_folder=None)

STREAM_IDLE_TIMEOUT = 30
SESSION_SWEEP_INTERVAL = 10
MAX_STREAM_SESSIONS = 10
_sessions_lock = threading.Lock()
_stream_sessions: dict[str, dict[str, float | str]] = {}
_session_monitor_started = False

_rate_limit_store: dict[str, float] = {}
RATE_LIMIT_SECONDS = 1.0

_enrich_status: dict[str, str] = {}
_enrich_lock = threading.Lock()
_daily_refresh_lock = threading.Lock()
_daily_refresh_started = False
DAILY_REFRESH_STAMP = DATA_DIR / 'last_refresh_date.txt'
DAILY_REFRESH_CHECK_SECONDS = 12 * 60 * 60
REFRESH_STAGING_DIR = DATA_DIR / 'staging_refresh'
REFRESH_FILES = [
    'torrents_data.json',
    'imdb_basics_cache.json',
    'imdb_ratings_cache.json',
    'imdb_search_cache.json',
    'kp_search_cache.json',
    'youtube_cache.json',
    'hidden_topics.json',
    'index-kino.html',
]
REFRESH_DIRS = ['posters', 'topic_cache', 'imdb']


def _today_stamp() -> str:
    return date.today().isoformat()


def _daily_refresh_due() -> bool:
    try:
        return DAILY_REFRESH_STAMP.read_text('utf-8').strip() != _today_stamp()
    except FileNotFoundError:
        return True


def _write_daily_refresh_stamp():
    atomic_write_text(DAILY_REFRESH_STAMP, _today_stamp())


def _copy_existing_refresh_data(staging_dir):
    if staging_dir.exists():
        shutil.rmtree(staging_dir)
    staging_dir.mkdir(parents=True, exist_ok=True)
    for name in REFRESH_FILES:
        src = DATA_DIR / name
        if src.exists():
            shutil.copy2(src, staging_dir / name)
    for name in REFRESH_DIRS:
        src = DATA_DIR / name
        if src.exists():
            shutil.copytree(src, staging_dir / name, dirs_exist_ok=True)


def _publish_staging_refresh(staging_dir):
    for name in REFRESH_DIRS:
        src = staging_dir / name
        dst = DATA_DIR / name
        if src.exists():
            shutil.copytree(src, dst, dirs_exist_ok=True)
    for name in REFRESH_FILES:
        src = staging_dir / name
        dst = DATA_DIR / name
        if src.exists():
            with file_lock(dst):
                os.replace(src, dst)


def _run_refresh_process(collection=None):
    env = os.environ.copy()
    env['LOCAL_KINO_DATA_DIR'] = str(REFRESH_STAGING_DIR)
    args = [sys.executable, 'generate_page.py', '--refresh']
    if collection:
        args.append(f'--collection={collection}')
    return subprocess.Popen(
        args,
        cwd=str(BASE_DIR),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding='utf-8',
        errors='replace',
        env=env,
    )


def _run_all_collections_refresh():
    _copy_existing_refresh_data(REFRESH_STAGING_DIR)
    for collection in gp.COLLECTIONS:
        print(f'Автообновление: коллекция {collection}')
        proc = _run_refresh_process(collection)
        assert proc.stdout is not None
        for line in proc.stdout:
            print(line, end='')
        code = proc.wait()
        if code != 0:
            return code
    return 0


def _iter_collections_refresh_output(collections=None):
    if collections is None:
        collections = gp.COLLECTIONS.keys()
    _copy_existing_refresh_data(REFRESH_STAGING_DIR)
    for collection in collections:
        yield f'Автообновление: коллекция {collection}\n'
        proc = _run_refresh_process(collection)
        assert proc.stdout is not None
        for line in proc.stdout:
            yield line
        code = proc.wait()
        if code != 0:
            return code
    return 0


def _iter_all_collections_refresh_output():
    return _iter_collections_refresh_output()


def _run_daily_refresh_if_due(reason: str = 'timer'):
    if not _daily_refresh_due():
        print(f'Автообновление: сегодня уже выполнено ({DAILY_REFRESH_STAMP})')
        return
    if not _daily_refresh_lock.acquire(blocking=False):
        print('Автообновление: refresh уже выполняется')
        return
    try:
        if not _daily_refresh_due():
            print(f'Автообновление: сегодня уже выполнено ({DAILY_REFRESH_STAMP})')
            return
        print(f'Автообновление: запускаю refresh ({reason})')
        code = _run_all_collections_refresh()
        if code == 0:
            _publish_staging_refresh(REFRESH_STAGING_DIR)
            _write_daily_refresh_stamp()
            print(f'Автообновление: готово, метка {_today_stamp()}')
        else:
            print(f'Автообновление: ошибка refresh, код {code}; метка не обновлена')
    except Exception as e:
        print(f'Автообновление: ошибка {e}; метка не обновлена')
    finally:
        _daily_refresh_lock.release()


def _daily_refresh_loop():
    while True:
        time.sleep(DAILY_REFRESH_CHECK_SECONDS)
        _run_daily_refresh_if_due('timer')


def _ensure_daily_refresh_loop():
    global _daily_refresh_started
    if _daily_refresh_started:
        return
    _daily_refresh_started = True
    threading.Thread(target=_daily_refresh_loop, daemon=True).start()


def _parse_topic_date(value: str):
    if not value:
        return None
    value = str(value).strip()
    for fmt in ('%Y-%m-%d %H:%M', '%Y-%m-%d'):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    return None


def _poster_file_from_url(poster_url: str):
    if not poster_url:
        return None
    poster_url = str(poster_url).replace('\\', '/')
    marker = 'posters/'
    if marker not in poster_url:
        return None
    filename = poster_url.rsplit(marker, 1)[-1].split('?', 1)[0].split('#', 1)[0]
    if not filename or '/' in filename or '..' in filename:
        return None
    return DATA_DIR / 'posters' / filename


def cleanup_old_topics(max_age_days: int = TOPIC_MAX_AGE_DAYS):
    if max_age_days <= 0:
        return {'removed_topics': 0, 'removed_cache': 0, 'removed_posters': 0, 'dated_topics': 0, 'skipped_topics': 0}
    data_path = DATA_DIR / 'torrents_data.json'
    if not data_path.exists():
        return {'removed_topics': 0, 'removed_cache': 0, 'removed_posters': 0, 'dated_topics': 0, 'skipped_topics': 0}

    cutoff = datetime.now() - timedelta(days=max_age_days)
    now_text = datetime.now().strftime('%Y-%m-%d %H:%M')
    removed = []
    dated_topics = 0
    skipped_topics = 0
    with file_lock(data_path):
        try:
            topics = json.loads(data_path.read_text('utf-8'))
        except Exception:
            return {'removed_topics': 0, 'removed_cache': 0, 'removed_posters': 0, 'dated_topics': 0, 'skipped_topics': 0}

        collection_counts = {}
        for topic in topics:
            collection = topic.get('collection', 'nashe_kino')
            collection_counts[collection] = collection_counts.get(collection, 0) + 1
        keep = []
        for topic in topics:
            collection = topic.get('collection', 'nashe_kino')
            topic_date = _parse_topic_date(topic.get('added_at', ''))
            if not topic_date:
                topic['added_at'] = now_text
                dated_topics += 1
                keep.append(topic)
            elif topic_date < cutoff:
                collection_info = gp.COLLECTIONS.get(collection, {})
                age_cleanup = bool(collection_info.get('age_cleanup', False))
                if age_cleanup and collection_counts.get(collection, 0) > gp.MAX_TOPICS:
                    removed.append(topic)
                    collection_counts[collection] -= 1
                else:
                    skipped_topics += 1
                    keep.append(topic)
            else:
                keep.append(topic)

        if not removed and not dated_topics:
            return {'removed_topics': 0, 'removed_cache': 0, 'removed_posters': 0, 'dated_topics': 0, 'skipped_topics': skipped_topics}

        atomic_write_json_unlocked(data_path, keep)
        atomic_write_text_unlocked(DATA_DIR / 'index-kino.html', gp.generate_html(keep))

    if removed:
        try:
            archive_path = DATA_DIR / 'topics_archive.json'
            archive = []
            if archive_path.exists():
                archive = json.loads(archive_path.read_text('utf-8'))
                if not isinstance(archive, list):
                    archive = []
            archived_ids = {str(t.get('topic_id')) for t in archive if isinstance(t, dict)}
            archived_at = datetime.now().strftime('%Y-%m-%d %H:%M')
            for topic in removed:
                topic_id = str(topic.get('topic_id') or '')
                if topic_id in archived_ids:
                    continue
                archived_topic = dict(topic)
                archived_topic['archived_at'] = archived_at
                archived_topic['archive_reason'] = f'age>{max_age_days}'
                archive.append(archived_topic)
            atomic_write_json_unlocked(archive_path, archive)
        except Exception:
            pass

    used_posters = {
        str(t.get('poster_url') or '').replace('\\', '/')
        for t in keep
        if t.get('poster_url')
    }
    removed_cache = 0
    removed_posters = 0
    for topic in removed:
        topic_id = str(topic.get('topic_id') or '').strip()
        if topic_id:
            cache_path = DATA_DIR / 'topic_cache' / f'{topic_id}.html'
            try:
                if cache_path.exists():
                    cache_path.unlink()
                    removed_cache += 1
            except OSError:
                pass

        poster_url = str(topic.get('poster_url') or '').replace('\\', '/')
        if poster_url and poster_url not in used_posters:
            poster_path = _poster_file_from_url(poster_url)
            try:
                if poster_path and poster_path.exists():
                    poster_path.unlink()
                    removed_posters += 1
            except OSError:
                pass

    return {
        'removed_topics': len(removed),
        'removed_cache': removed_cache,
        'removed_posters': removed_posters,
        'dated_topics': dated_topics,
        'skipped_topics': skipped_topics,
        'max_age_days': max_age_days,
    }
ENRICH_QUEUE: list[str] = []


def _enrich_worker():
    while True:
        time.sleep(1)
        topic_id = None
        with _enrich_lock:
            if ENRICH_QUEUE:
                topic_id = ENRICH_QUEUE.pop(0)
                _enrich_status[topic_id] = 'in_progress'
        if not topic_id:
            continue
        try:
            data_path = DATA_DIR / 'torrents_data.json'
            if not data_path.exists():
                with _enrich_lock:
                    _enrich_status[topic_id] = 'error: no data'
                continue
            with file_lock(data_path):
                topics = json.loads(data_path.read_text('utf-8'))
                topic = next((t for t in topics if t.get('topic_id') == topic_id), None)
                if not topic:
                    with _enrich_lock:
                        _enrich_status[topic_id] = 'error: not found'
                    continue
                gp.enrich_topic(topic, force_poster_retry=True)
                atomic_write_json_unlocked(data_path, topics)
                gen_path = DATA_DIR / 'index-kino.html'
                html = gp.generate_html(topics)
                atomic_write_text(gen_path, html)
            with _enrich_lock:
                _enrich_status[topic_id] = 'done'
        except Exception as e:
            with _enrich_lock:
                _enrich_status[topic_id] = f'error: {e}'


def _ensure_enrich_worker():
    t = threading.Thread(target=_enrich_worker, daemon=True)
    t.start()


def _enrich_missing(force: bool = False):
    data_path = DATA_DIR / 'torrents_data.json'
    if not data_path.exists():
        return
    try:
        with file_lock(data_path):
            topics = json.loads(data_path.read_text('utf-8'))
            changed = False
            cleaned_topics = gp.clean_catalog_topics(topics)
            if len(cleaned_topics) != len(topics):
                topics = cleaned_topics
                changed = True
            MAX_RETRIES = 3
            for topic in topics:
                poster_due = (
                    not gp.has_real_poster(topic)
                    and (gp.should_retry_poster(topic) or gp.should_try_external_poster_fallback(topic))
                )
                rating_due = not topic.get('kp_rating') and not topic.get('imdb_rating')
                need = (not topic.get('magnet') or topic.get('_magnet_failed')
                        or poster_due
                        or rating_due
                        or not topic.get('youtube_url')
                        or not topic.get('format'))
                if not need:
                    if topic.get('_enrich_retries'):
                        topic.pop('_enrich_retries', None)
                        changed = True
                    continue
                retries = topic.get('_enrich_retries', 0)
                if not force and retries >= MAX_RETRIES and not poster_due:
                    continue
                title = topic.get('movie_title') or topic.get('title', '?')
                print(f'  [enrich] #{topic["topic_id"]} {title} (retry {retries})')
                topic['_enrich_retries'] = retries + 1
                gp.enrich_topic(topic, force_poster_retry=force)
                changed = True
                poster_still_due = (
                    not gp.has_real_poster(topic)
                    and (gp.should_retry_poster(topic) or gp.should_try_external_poster_fallback(topic))
                )
                rating_still_due = not topic.get('kp_rating') and not topic.get('imdb_rating')
                still_missing = (not topic.get('magnet') or topic.get('_magnet_failed')
                                 or poster_still_due
                                 or rating_still_due
                                 or not topic.get('youtube_url')
                                 or not topic.get('format'))
                if not still_missing:
                    topic.pop('_enrich_retries', None)
                    print(f'    -> OK')
                else:
                    print(f'    -> ещё не все данные')
            if changed:
                atomic_write_json_unlocked(data_path, topics)
                gen_path = DATA_DIR / 'index-kino.html'
                atomic_write_text_unlocked(gen_path, gp.generate_html(topics))
                print(f'  [enrich] сохранено ({sum(1 for t in topics if not t.get("_enrich_retries"))}/{len(topics)} готово)')
    except Exception:
        return


def _periodic_enrich():
    print(f'Автообогащение запущено, интервал {ENRICH_INTERVAL_MINUTES} мин')
    while True:
        _enrich_missing()
        time.sleep(ENRICH_INTERVAL_MINUTES * 60)


def _ensure_periodic_enrich():
    t = threading.Thread(target=_periodic_enrich, daemon=True)
    t.start()


def _rate_limit(key: str, seconds: float = RATE_LIMIT_SECONDS) -> bool:
    now = time.monotonic()
    last = _rate_limit_store.get(key)
    if last and now - last < seconds:
        return False
    _rate_limit_store[key] = now
    return True


INFO_HASH_RE = re.compile(r'btih:([A-Fa-f0-9]{40})')


def _info_hash_from_magnet(magnet: str) -> str:
    match = INFO_HASH_RE.search(magnet or '')
    return match.group(1).lower() if match else ''


def _catalog_info_hashes() -> set[str]:
    data_path = DATA_DIR / 'torrents_data.json'
    if not data_path.exists():
        return set()
    try:
        topics = json.loads(data_path.read_text('utf-8'))
    except (OSError, json.JSONDecodeError):
        return set()
    hashes = set()
    for topic in topics:
        info_hash = _info_hash_from_magnet(str(topic.get('magnet') or ''))
        if info_hash:
            hashes.add(info_hash)
    return hashes


def _catalog_allows_magnet(magnet: str) -> bool:
    info_hash = _info_hash_from_magnet(magnet)
    return bool(info_hash and info_hash in _catalog_info_hashes())


def _catalog_allows_hash(info_hash: str) -> bool:
    return bool(info_hash and info_hash.lower() in _catalog_info_hashes())


def _reject_public_admin():
    if PUBLIC_MODE:
        abort(403, description='Disabled in public mode')


def _mark_stream_session(info_hash: str) -> str:
    with _sessions_lock:
        if len(_stream_sessions) >= MAX_STREAM_SESSIONS:
            if info_hash not in {s['hash'] for s in _stream_sessions.values()}:
                raise RuntimeError(f'Too many active streams ({MAX_STREAM_SESSIONS})')
    sid = request.args.get('sid') or request.headers.get('X-Player-Session')
    if not sid:
        sid = f'{request.remote_addr or "local"}:{info_hash}'
    now = time.monotonic()
    with _sessions_lock:
        _stream_sessions[sid] = {'hash': info_hash, 'last_seen': now}
    engine.resume(info_hash)
    return sid


def _touch_stream_session(sid: str):
    now = time.monotonic()
    with _sessions_lock:
        item = _stream_sessions.get(sid)
        if item:
            item['last_seen'] = now


def _stop_stream_session(sid: str | None):
    if not sid:
        return
    stopped_hash = None
    with _sessions_lock:
        item = _stream_sessions.pop(sid, None)
        if item:
            stopped_hash = str(item.get('hash') or '')
            still_active = any(v.get('hash') == stopped_hash for v in _stream_sessions.values())
        else:
            still_active = True
    if stopped_hash and not still_active:
        engine.pause(stopped_hash)


def _sweep_stream_sessions():
    while True:
        time.sleep(SESSION_SWEEP_INTERVAL)
        now = time.monotonic()
        pause_hashes: set[str] = set()
        with _sessions_lock:
            expired = [
                sid for sid, item in _stream_sessions.items()
                if now - float(item.get('last_seen') or 0) > STREAM_IDLE_TIMEOUT
            ]
            for sid in expired:
                item = _stream_sessions.pop(sid, None)
                if item and item.get('hash'):
                    pause_hashes.add(str(item['hash']))
            active_hashes = {str(item['hash']) for item in _stream_sessions.values() if item.get('hash')}
        for info_hash in pause_hashes - active_hashes:
            engine.pause(info_hash)


def _ensure_session_monitor():
    global _session_monitor_started
    if _session_monitor_started:
        return
    _session_monitor_started = True
    threading.Thread(target=_sweep_stream_sessions, daemon=True).start()


@app.before_request
def _start_session_monitor():
    _ensure_session_monitor()


@app.route('/watch', methods=['POST'])
def watch():
    data = request.get_json(silent=True)
    if not data or 'magnet' not in data:
        return jsonify(error='magnet required'), 400

    magnet = data['magnet']
    if PUBLIC_MODE and not _catalog_allows_magnet(magnet):
        return jsonify(error='magnet not allowed'), 403
    try:
        info_hash = engine.add_magnet(magnet)
    except TimeoutError as e:
        return jsonify(error=str(e)), 504

    return jsonify(info_hash=info_hash)


@app.route('/start', methods=['POST'])
def start_torrent():
    magnet = request.form.get('magnet')
    if not magnet:
        return 'magnet required', 400
    if PUBLIC_MODE and not _catalog_allows_magnet(magnet):
        return 'magnet not allowed', 403
    try:
        info_hash = engine.add_magnet_async(magnet)
    except ValueError as e:
        return str(e), 400
    return redirect(f'/player.html#{info_hash}')


@app.route('/watch_sync', methods=['POST'])
def watch_sync():
    data = request.get_json(silent=True)
    if not data or 'magnet' not in data:
        return jsonify(error='magnet required'), 400
    magnet = data['magnet']
    if PUBLIC_MODE and not _catalog_allows_magnet(magnet):
        return jsonify(error='magnet not allowed'), 403
    try:
        info_hash = engine.add_magnet(magnet, timeout=7)
    except TimeoutError:
        info_hash = engine.add_magnet_async(magnet)
        return jsonify(info_hash=info_hash, async_mode=True)
    return jsonify(info_hash=info_hash, async_mode=False)


@app.route('/status/<info_hash>')
def status(info_hash):
    if PUBLIC_MODE and not _catalog_allows_hash(info_hash):
        return jsonify(error='not allowed'), 403
    s = engine.get_status(info_hash)
    if s is None:
        return jsonify(error='not found'), 404
    return jsonify(s)


INITIAL_CHUNK = 8 * 1024 * 1024  # 8 MB - covers full moov for most files
MAX_RANGE_CHUNK = 16 * 1024 * 1024
STREAM_READ_CHUNK = 1024 * 1024


@app.route('/stream/<info_hash>')
def stream(info_hash):
    if PUBLIC_MODE and not _catalog_allows_hash(info_hash):
        abort(403, description='Not allowed')
    try:
        sid = _mark_stream_session(info_hash)
    except RuntimeError as e:
        return jsonify(error=str(e)), 503
    touch_session = lambda: _touch_stream_session(sid)
    handle = engine.get_handle(info_hash)
    if not handle:
        handle = engine.wait_for_handle(info_hash, timeout=30)
    local_file = engine.wait_for_local_file(info_hash, timeout=0.1)
    if not handle:
        if not local_file:
            abort(404, description='Torrent not found')

    deadline = time.monotonic() + 60
    if handle:
        while not handle.status().has_metadata:
            touch_session()
            if time.monotonic() > deadline:
                abort(503, description='Metadata timeout')
            time.sleep(0.3)

    filepath = engine.get_file_path(info_hash)
    while not filepath:
        touch_session()
        if time.monotonic() > deadline:
            abort(404, description='File path not found')
        time.sleep(0.3)
        filepath = engine.get_file_path(info_hash)

    video_info = engine.get_video_file_info(info_hash)
    if not video_info:
        abort(404, description='Video file not found')

    while not os.path.exists(filepath):
        touch_session()
        if time.monotonic() > deadline:
            abort(404, description='File not found on disk')
        time.sleep(0.3)

    file_size = video_info['size']

    ext = os.path.splitext(filepath)[1].lower()
    streamable = {'.mkv', '.mp4'}
    if ext not in streamable and handle:
        status = handle.status()
        if status.progress < 1.0:
            abort(503, description='Формат ' + ext.upper().lstrip('.') + ' требует полной загрузки. Прогресс: ' + str(round(status.progress * 100)) + '%')

    range_header = request.headers.get('Range')
    start, end = 0, file_size - 1

    if range_header:
        m = re.match(r'bytes=(\d*)-(\d*)', range_header)
        if m:
            start_str = m.group(1)
            req_end_str = m.group(2)
            if not start_str and req_end_str:
                suffix_len = int(req_end_str)
                if suffix_len <= 0:
                    abort(416)
                start = max(file_size - suffix_len, 0)
                end = file_size - 1
            else:
                start = int(start_str) if start_str else 0
                if start >= file_size:
                    abort(416)
                if req_end_str:
                    end = min(int(req_end_str), file_size - 1)
                else:
                    end = min(start + INITIAL_CHUNK - 1, file_size - 1)
        else:
            abort(416)
        if start > end:
            abort(416)
    else:
        end = min(start + INITIAL_CHUNK - 1, file_size - 1)

    if end - start + 1 > MAX_RANGE_CHUNK:
        end = min(start + MAX_RANGE_CHUNK - 1, file_size - 1)

    if handle and not video_info.get('local_only'):
        info = handle.torrent_file()
        try:
            start_piece = info.map_file(video_info['index'], start, 1).piece
            end_piece = info.map_file(video_info['index'], end, 1).piece
        except RuntimeError:
            abort(416)

        engine.prioritize_piece_range(info_hash, start_piece, end_piece)

        try:
            _wait_for_pieces(handle, start_piece, end_piece, touch=touch_session)
        except TimeoutError:
            abort(503, description='Download in progress, retry later')

    content_length = end - start + 1
    content_type = mimetypes.guess_type(filepath)[0] or 'video/mp4'

    def generate_file():
        remaining = content_length
        with open(filepath, 'rb') as f:
            f.seek(start)
            while remaining > 0:
                _touch_stream_session(sid)
                chunk = f.read(min(STREAM_READ_CHUNK, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk

    resp = Response(generate_file(), status=206, content_type=content_type, direct_passthrough=True)
    resp.headers['Content-Range'] = f'bytes {start}-{end}/{file_size}'
    resp.headers['Accept-Ranges'] = 'bytes'
    resp.headers['Content-Length'] = str(content_length)
    resp.headers['Cache-Control'] = 'no-cache'
    return resp


def _wait_for_pieces(handle, start_piece, end_piece, timeout=120, interval=0.5, touch=None):
    deadline = time.monotonic() + timeout
    num_pieces = handle.torrent_file().num_pieces()
    while time.monotonic() < deadline:
        if touch:
            touch()
        s = handle.status()
        if s.state in (0, 1, 6):
            time.sleep(interval)
            continue
        all_have = True
        for i in range(start_piece, min(end_piece + 1, num_pieces)):
            if not handle.have_piece(i):
                all_have = False
                break
        if all_have:
            handle.flush_cache()
            return
        time.sleep(interval)
    raise TimeoutError(f'Pieces {start_piece}-{end_piece} not available after {timeout}s')


@app.route('/transcode/<info_hash>')
def transcode(info_hash):
    if PUBLIC_MODE and not _catalog_allows_hash(info_hash):
        abort(403, description='Not allowed')
    sid = _mark_stream_session(info_hash)
    filepath = engine.get_file_path(info_hash)
    if not filepath:
        filepath = engine.wait_for_local_file(info_hash, timeout=10)
    if not filepath or not os.path.exists(filepath):
        abort(404, description='File not found')

    cmd = [
        'ffmpeg',
        '-hide_banner',
        '-loglevel', 'error',
        '-i', filepath,
        '-map', '0:v:0',
        '-map', '0:a:0',
        '-c:v', 'copy',
        '-c:a', 'aac',
        '-b:a', '192k',
        '-f', 'mp4',
        '-movflags', 'frag_keyframe+empty_moov+default_base_moof',
        'pipe:1',
    ]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def generate():
        try:
            while True:
                _touch_stream_session(sid)
                chunk = proc.stdout.read(STREAM_READ_CHUNK)
                if not chunk:
                    break
                yield chunk
        finally:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.kill()

    resp = Response(generate(), content_type='video/mp4', direct_passthrough=True)
    resp.headers['Cache-Control'] = 'no-cache'
    return resp


@app.route('/stop_session', methods=['POST'])
def stop_session():
    data = request.get_json(silent=True) or {}
    sid = data.get('sid') or request.form.get('sid')
    _stop_stream_session(str(sid) if sid else None)
    return jsonify(ok=True)


@app.route('/player.html')
def player():
    return send_file(str(BASE_DIR / 'player.html'))


@app.route('/data/posters/<path:filename>')
def poster_asset(filename):
    return send_from_directory(str(DATA_DIR / 'posters'), filename)


@app.route('/')
def index():
    index_path = DATA_DIR / 'index-kino.html'

    if not index_path.exists():
        return '<h1>LocaL-Kino</h1><p>index-kino.html not found. Run generate_page.py first.</p>'

    stat = index_path.stat()
    INJECT_VER = 'v7'
    etag_val = f'{stat.st_mtime}-{stat.st_size}-{INJECT_VER}'

    if request.if_none_match.contains(etag_val):
        return Response(status=304)

    html = index_path.read_text('utf-8')
    refresh_btn = '' if PUBLIC_MODE else '<a class="rf" href="/refresh" title="Обновить данные" style="font-size:14px;margin-left:8px;text-decoration:none;cursor:pointer" onclick="var s=document.getElementById(\'cs\'),c=s?s.value:\'\';this.href=c?\'/refresh?collection=\'+encodeURIComponent(c):\'/refresh\'">🔄</a>'
    html = html.replace('</span>', f'{refresh_btn}</span>', 1)
    browse_links = '''<div class="bl"><a href="/test">Каталог</a><a href="/browse/carousel">Карусель</a><a href="/browse/random">Случайный</a><a href="/browse/filter">Фильтр</a><a href="/browse/timeline">Хронология</a><a href="/browse/shuffle">ТВ</a><a href="/browse/duel">Дуэль</a><a href="/browse/matrix">Матрица</a><a href="/browse/stats">Статистика</a><a href="/browse/search">Поиск</a><a href="/browse/top">Топ</a><a href="/browse/collections">Коллекции</a></div>\n'''
    if 'class="bl"' not in html:
        html = html.replace('<table id="tbl">', f'{browse_links}<table id="tbl">', 1)
    public_style = '.rmv,.eb{display:none!important}' if PUBLIC_MODE else ''
    bl_style = f'<style>.bl{{display:flex;flex-wrap:wrap;gap:8px;padding:10px 14px;background:#1a1a2e;border-bottom:2px solid #8ab4f8;margin-bottom:4px}}.bl a{{color:#fff;text-decoration:none;font-size:14px;font-weight:600;padding:7px 16px;border-radius:6px;background:#16213e;border:1px solid #0f3460;transition:all .2s}}.bl a:hover{{background:#0f3460;border-color:#8ab4f8;transform:translateY(-1px)}}{public_style}</style>'
    html = html.replace('</head>', f'{bl_style}</head>', 1)

    resp = Response(html, content_type='text/html')
    resp.set_etag(etag_val)
    resp.cache_control.public = True
    resp.cache_control.max_age = 0
    resp.cache_control.must_revalidate = True
    return resp


@app.route('/refresh')
def refresh():
    _reject_public_admin()
    collection = (request.args.get('collection') or '').strip()
    if collection and collection not in gp.COLLECTIONS:
        return Response(
            '<!DOCTYPE html><html lang="ru"><head><meta charset="utf-8"><title>Обновление</title></head>'
            '<body><h2>Неизвестная коллекция</h2><p><a href="/">Назад</a></p></body></html>',
            status=400,
            content_type='text/html; charset=utf-8',
        )

    def generate():
        if not _daily_refresh_lock.acquire(blocking=False):
            yield '<!DOCTYPE html><html lang="ru"><head><meta charset="utf-8"><title>Обновление</title></head><body><h2>Обновление уже выполняется</h2><p><a href="/">Назад</a></p></body></html>'
            return
        label = gp.COLLECTIONS[collection]['name'] if collection else 'все коллекции'
        yield f'<!DOCTYPE html><html lang="ru"><head><meta charset="utf-8"><title>Обновление</title></head><body><h2>Обновляю: {label}</h2><pre>'
        try:
            refresh_output = _iter_collections_refresh_output([collection] if collection else None)
            code = 0
            while True:
                try:
                    yield next(refresh_output)
                except StopIteration as done:
                    code = done.value or 0
                    break
            if code == 0:
                _publish_staging_refresh(REFRESH_STAGING_DIR)
                if collection:
                    yield '</pre><p>Обновлена выбранная коллекция. Общая дневная метка не изменялась.</p><p><a href="/">Готово</a></p></body></html>'
                else:
                    _write_daily_refresh_stamp()
                    yield f'</pre><p>Метка обновления: {_today_stamp()}</p><p><a href="/">Готово</a></p></body></html>'
            else:
                yield f'</pre><p>Ошибка refresh, код {code}. Метка не обновлена.</p><p><a href="/">Назад</a></p></body></html>'
        finally:
            _daily_refresh_lock.release()
    return Response(generate(), content_type='text/html; charset=utf-8')


@app.route('/cleanup')
def cleanup_trigger():
    _reject_public_admin()
    if not _rate_limit(f'cleanup:{request.remote_addr}', seconds=10):
        return jsonify(error='rate limited'), 429
    removed = engine.cleanup(MAX_TEMP_SIZE_BYTES, TEMP_MAX_AGE_SECS, MAX_TEMP_FILES)
    topics = cleanup_old_topics()
    return jsonify(removed=removed, count=len(removed), topics=topics)


@app.route('/torrents_data.json')
def torrents_data():
    if PUBLIC_MODE:
        abort(404)
    return send_file(str(DATA_DIR / 'torrents_data.json'))


@app.route('/enrich/all', methods=['POST'])
def enrich_all():
    _reject_public_admin()
    if not _rate_limit(f'enrich:{request.remote_addr}', seconds=30):
        return jsonify(error='rate limited'), 429
    threading.Thread(target=_enrich_missing, args=[True], daemon=True).start()
    return jsonify(status='started')


@app.route('/enrich/<topic_id>', methods=['POST'])
def enrich_topic(topic_id):
    _reject_public_admin()
    with _enrich_lock:
        if _enrich_status.get(topic_id) == 'in_progress':
            return jsonify(status='in_progress')
        if topic_id in ENRICH_QUEUE:
            return jsonify(status='queued')
        ENRICH_QUEUE.append(topic_id)
        _enrich_status[topic_id] = 'queued'
    return jsonify(status='queued')


@app.route('/enrich/status/<topic_id>')
def enrich_status(topic_id):
    _reject_public_admin()
    with _enrich_lock:
        status = _enrich_status.get(topic_id, 'unknown')
    return jsonify(status=status)


@app.route('/hide/<topic_id>', methods=['POST'])
def hide_topic(topic_id):
    _reject_public_admin()
    if not _rate_limit(f'hide:{request.remote_addr}'):
        return jsonify(error='rate limited'), 429
    gp.add_hidden_topic(topic_id)
    data_path = DATA_DIR / 'torrents_data.json'
    if data_path.exists():
        html = gp.generate_html(json.loads(data_path.read_text('utf-8')))
        atomic_write_text(DATA_DIR / 'index-kino.html', html)
    return jsonify(ok=True)


@app.route('/unhide/<topic_id>', methods=['POST'])
def unhide_topic(topic_id):
    _reject_public_admin()
    if not _rate_limit(f'unhide:{request.remote_addr}'):
        return jsonify(error='rate limited'), 429
    gp.remove_hidden_topic(topic_id)
    data_path = DATA_DIR / 'torrents_data.json'
    if data_path.exists():
        html = gp.generate_html(json.loads(data_path.read_text('utf-8')))
        atomic_write_text(DATA_DIR / 'index-kino.html', html)
    return jsonify(ok=True)


@app.after_request
def add_cors(response):
    response.headers['Access-Control-Allow-Origin'] = '*'
    if request.path.startswith('/data/posters/'):
        response.headers['Cache-Control'] = 'public, max-age=604800, immutable'
    return response


# ---------------------------------------------------------------------------
# Browse modes
# ---------------------------------------------------------------------------
def _get_movies():
    p = DATA_DIR / 'torrents_data.json'
    if not p.exists():
        return []
    return json.loads(p.read_text('utf-8'))


BROWSE_CSS = '''
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;background:#141414;color:#fff;min-height:100vh}
a{color:#e50914;text-decoration:none}
h1{font-size:24px;font-weight:700;padding:20px 30px 10px}
h2{font-size:18px;font-weight:600;padding:10px 30px;color:#ccc}
.back{position:fixed;top:15px;right:20px;z-index:100;background:rgba(0,0,0,.7);color:#fff;border:1px solid #555;padding:6px 14px;border-radius:4px;font-size:13px;cursor:pointer}
.back:hover{background:#e50914;border-color:#e50914}
'''


@app.route('/test')
def browse_test():
    movies = _get_movies()
    hidden = gp.load_hidden_topic_ids()
    movies = [m for m in movies if m['topic_id'] not in hidden]
    count = len(movies)
    genres = sorted({g.strip() for m in movies for g in m.get('genre', '').split(',') if g.strip()})
    years = sorted({m['movie_year'] for m in movies if m.get('movie_year')}, reverse=True)
    return f'''<!DOCTYPE html>
<html lang="ru"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>LocaL-Kino — Тест режимов</title>
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;background:#141414;color:#fff;padding:30px;min-height:100vh}}
h1{{font-size:28px;margin-bottom:6px}}
.sub{{color:#888;margin-bottom:30px;font-size:14px}}
.grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:16px}}
.card{{background:#1f1f1f;border-radius:10px;padding:20px;transition:transform .2s,box-shadow .2s}}
.card:hover{{transform:translateY(-2px);box-shadow:0 8px 25px rgba(0,0,0,.4)}}
.card h3{{font-size:18px;margin-bottom:8px}}
.card p{{font-size:13px;color:#aaa;margin-bottom:12px;line-height:1.4}}
.card a{{display:inline-block;background:#e50914;color:#fff;padding:8px 18px;border-radius:4px;font-size:13px;text-decoration:none;margin-right:8px}}
.card a:hover{{background:#f40612}}
.tag{{display:inline-block;background:#333;color:#aaa;font-size:11px;padding:2px 8px;border-radius:3px;margin-right:4px;margin-top:6px}}
</style></head><body>
<h1>🧪 Режимы просмотра</h1>
<p class="sub">{count} фильмов, {len(genres)} жанров, {len(years)} годов выпуска</p>
<div class="grid">
<div class="card"><h3>🎠 Carousel</h3><p>Netflix-стиль: горизонтальные ряды по жанрам. Скролл колёсиком мыши или стрелками.</p><a href="/browse/carousel">Открыть</a><span class="tag">постеры</span><span class="tag">жанры</span></div>
<div class="card"><h3>🎲 Random</h3><p>Случайный фильм на весь экран: постер, описание, трейлер. Клавиши ← → для навигации.</p><a href="/browse/random">Открыть</a><span class="tag">одна карточка</span><span class="tag">клавиши</span></div>
<div class="card"><h3>🔍 Filter</h3><p>Фильтры слева: жанр, год, рейтинг, коллекция, сиды. Результаты обновляются мгновенно.</p><a href="/browse/filter">Открыть</a><span class="tag">фильтрация</span><span class="tag">реальное время</span></div>
<div class="card"><h3>📅 Timeline</h3><p>Фильмы на временной шкале по годам. Клик по году → фильмы этого года.</p><a href="/browse/timeline">Открыть</a><span class="tag">годы</span><span class="tag">коллекции</span></div>
<div class="card"><h3>📺 Shuffle TV</h3><p>Автоматическая карусель: фильмы переключаются каждые 30 секунд. Как телеканал.</p><a href="/browse/shuffle">Открыть</a><span class="tag">авто</span><span class="tag">полный экран</span></div>
<div class="card"><h3>🤺 Дуэль</h3><p>Два фильма — выбирай лучший. VS-режим, счётчик побед.</p><a href="/browse/duel">Открыть</a><span class="tag">выбор</span><span class="tag">vs</span></div>
<div class="card"><h3>🔲 Матрица</h3><p>Случайные 16 постеров в сетке 4×4. Клик — плеер. Перемешать заново.</p><a href="/browse/matrix">Открыть</a><span class="tag">сетка</span><span class="tag">постеры</span></div>
<div class="card"><h3>📊 Статистика</h3><p>Графики: жанры, годы, коллекции. Количество, рейтинги, распределение.</p><a href="/browse/stats">Открыть</a><span class="tag">чарты</span><span class="tag">данные</span></div>
<div class="card"><h3>🔎 Поиск</h3><p>Поиск фильмов по названию (русскому или оригинальному). Результаты мгновенно по мере ввода.</p><a href="/browse/search">Открыть</a><span class="tag">поиск</span><span class="tag">название</span></div>
<div class="card"><h3>🏆 Топ</h3><p>Топ-50 фильмов по рейтингу Кинопоиска и IMDB. Сортировка, бейджи, номера мест.</p><a href="/browse/top">Открыть</a><span class="tag">рейтинг</span><span class="tag">топ</span></div>
<div class="card"><h3>📂 Коллекции</h3><p>Фильмы сгруппированные по коллекциям: Наше кино, Новинки, Кино СНГ и другие.</p><a href="/browse/collections">Открыть</a><span class="tag">группы</span><span class="tag">коллекции</span></div>
</div>
<p style="margin-top:30px;font-size:13px;color:#555"><a href="/" style="color:#e50914">← На главную</a></p>
</body></html>'''


def _poster_style(poster_url: str) -> str:
    if not poster_url:
        return 'background-image:url(/data/posters/placeholder.png)'
    if not poster_url.startswith('data/'):
        poster_url = 'data/' + poster_url
    full_path = str(DATA_DIR / poster_url.removeprefix('data/'))
    if not os.path.exists(full_path):
        return 'background-image:url(/data/posters/placeholder.png)'
    return f'background-image:url(/{poster_url})'


@app.route('/browse/carousel')
def browse_carousel():
    movies = _get_movies()
    hidden = gp.load_hidden_topic_ids()
    movies = [m for m in movies if m['topic_id'] not in hidden]
    genre_map: dict[str, list] = {}
    for m in movies:
        for g in m.get('genre', '').split(','):
            g = g.strip()
            if not g:
                continue
            genre_map.setdefault(g, []).append(m)
    rows_html = ''
    sorted_genres = sorted(genre_map.items(), key=lambda x: -len(x[1]))
    for genre, items in sorted_genres:
        items.sort(key=lambda m: (0 if 'background-image' in _poster_style(m.get('poster_url', '')) else 1, -(m.get('seeders') or 0)))
        cards = ''
        for m in items:
            poster_style = _poster_style(m.get('poster_url', ''))
            yr = m.get('movie_year', '')
            rt = m.get('kp_rating') or m.get('imdb_rating') or ''
            cards += f'''<div class="cc">
<div class="cp" style="{poster_style}"></div>
<div class="ci"><strong>{m.get('orig_title') or m.get('movie_title','')}</strong>{" "+yr if yr else ""}{" · "+rt if rt else ""}</div>
</div>'''
        rows_html += f'<h2>{genre} ({len(items)})</h2><div class="cr">{cards}</div>'
    return f'''<!DOCTYPE html>
<html lang="ru"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0"><title>Carousel</title>
<style>
{BROWSE_CSS}
.cr{{display:flex;gap:12px;overflow-x:auto;padding:10px 30px 20px;scroll-behavior:smooth;-webkit-overflow-scrolling:touch}}
.cr::-webkit-scrollbar{{height:6px}}
.cr::-webkit-scrollbar-thumb{{background:#555;border-radius:3px}}
.cc{{flex:0 0 auto;width:180px;cursor:pointer;transition:transform .2s}}
.cc:hover{{transform:scale(1.05)}}
.cp{{width:180px;height:270px;background-size:cover;background-position:center;border-radius:6px}}
.ci{{font-size:12px;color:#ccc;padding:6px 2px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}}
</style></head><body>
<a href="/test" class="back">← Тест</a>
<h1>🎠 По жанрам</h1>
{rows_html}
</body></html>'''


@app.route('/browse/random')
def browse_random():
    movies = _get_movies()
    hidden = gp.load_hidden_topic_ids()
    movies = [m for m in movies if m['topic_id'] not in hidden]
    movies_json = json.dumps(movies, ensure_ascii=False).replace('</script>', '<\\/script>')
    return f'''<!DOCTYPE html>
<html lang="ru"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0"><title>Random</title>
<style>
{BROWSE_CSS}
body{{background:#000;display:flex;align-items:center;justify-content:center}}
#rc{{position:relative;width:100%;height:100vh;display:flex;align-items:center;justify-content:center;overflow:hidden}}
#rc .bg{{position:absolute;inset:0;background-size:cover;background-position:center;filter:blur(20px) brightness(.3);transition:background-image .5s}}
#rc .poster{{position:relative;z-index:1;width:300px;height:450px;background-size:cover;background-position:center;border-radius:10px;box-shadow:0 10px 40px rgba(0,0,0,.6);transition:background-image .5s}}
#rc .info{{position:relative;z-index:1;margin-left:40px;max-width:500px}}
#rc .info h2{{font-size:28px;padding:0;margin-bottom:8px}}
#rc .info .meta{{color:#aaa;font-size:14px;margin-bottom:12px}}
#rc .info .desc{{color:#ccc;font-size:14px;line-height:1.5;margin-bottom:20px}}
.btns a{{display:inline-block;background:#e50914;color:#fff;padding:10px 24px;border-radius:4px;font-size:14px;text-decoration:none;margin-right:10px}}
.btns a:hover{{background:#f40612}}
.btns button{{background:#333;color:#fff;border:none;padding:10px 24px;border-radius:4px;font-size:14px;cursor:pointer}}
.btns button:hover{{background:#555}}
#nav{{position:fixed;bottom:30px;left:50%;transform:translateX(-50%);display:flex;gap:12px;z-index:10}}
#nav button{{background:rgba(255,255,255,.1);color:#fff;border:1px solid #555;padding:8px 20px;border-radius:4px;font-size:16px;cursor:pointer}}
#nav button:hover{{background:rgba(255,255,255,.2)}}
</style></head><body>
<a href="/test" class="back">← Тест</a>
<div id="rc"><div class="bg" id="bg"></div><div class="poster" id="poster"></div><div class="info"><h2 id="title"></h2><div class="meta" id="meta"></div><div class="desc" id="cast"></div><div class="btns" id="btns"></div></div></div>
<div id="nav"><button onclick="prev()">←</button><button onclick="next()">→</button></div>
 <script>
const MOVIES = {movies_json};
function posterUrl(m){{return (m.poster_url||'').indexOf('data/')===0?'/'+m.poster_url:m.poster_url?'/data/'+m.poster_url:'/data/posters/placeholder.png'}}
let idx = Math.floor(Math.random()*MOVIES.length);
function show(i){{
const m=MOVIES[i];if(!m)return;
const p=posterUrl(m);
document.getElementById('bg').style.backgroundImage='url('+p+')';
document.getElementById('poster').style.backgroundImage='url('+p+')';
document.getElementById('title').textContent=m.orig_title||m.movie_title||'';
document.getElementById('meta').textContent=[m.movie_year,m.genre,m.kp_rating?'KP '+m.kp_rating:'',m.imdb_rating?'IMDB '+m.imdb_rating:''].filter(Boolean).join(' · ');
document.getElementById('cast').textContent=m.cast||'';
document.getElementById('btns').innerHTML=(m.magnet?'<a href="player.html#'+m.magnet.match(/btih:([A-Fa-f0-9]+)/)[1].toLowerCase()+'">▶ Смотреть</a>':'')+(m.youtube_url?'<a href="'+m.youtube_url+'" target="_blank">▶ Трейлер</a>':'');
}}
function next(){{idx=(idx+1)%MOVIES.length;show(idx)}}
function prev(){{idx=(idx-1+MOVIES.length)%MOVIES.length;show(idx)}}
document.addEventListener('keydown',e=>{{if(e.key==='ArrowLeft')prev();if(e.key==='ArrowRight')next()}});
show(idx);
</script></body></html>'''


@app.route('/browse/filter')
def browse_filter():
    from html import escape as h_esc
    movies = _get_movies()
    hidden = gp.load_hidden_topic_ids()
    movies = [m for m in movies if m['topic_id'] not in hidden]
    genre_counts: dict[str, int] = {}
    for m in movies:
        for g in m.get('genre', '').split(','):
            g = g.strip().lower()
            if g:
                genre_counts[g] = genre_counts.get(g, 0) + 1
    top_genres = sorted(genre_counts.keys(), key=lambda g: -genre_counts[g])[:20]
    years = sorted({m['movie_year'] for m in movies if m.get('movie_year')}, reverse=True)
    collections = sorted({m.get('collection', '') for m in movies if m.get('collection')})
    formats = sorted({m.get('format', '').upper().strip() for m in movies if m.get('format') and m.get('format').strip()})
    movies_json = json.dumps(movies, ensure_ascii=False).replace('</script>', '<\\/script>')
    genre_opts = ''.join(f'<label><input type="checkbox" class="fg" value="{g}" checked> {g.capitalize()}</label>' for g in top_genres)
    year_opts = ''.join(f'<option value="{y}">{y}</option>' for y in years)
    coll_opts = ''.join(f'<option value="{h_esc(c)}">{h_esc(c)}</option>' for c in collections)
    fmt_opts = ''.join(f'<option value="{f}">{f}</option>' for f in formats)
    return f'''<!DOCTYPE html>
<html lang="ru"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0"><title>Filter</title>
<style>
{BROWSE_CSS}
body{{display:flex;padding:0}}
#sidebar{{width:260px;min-width:260px;background:#1a1a1a;padding:20px;height:100vh;overflow-y:auto;position:sticky;top:0}}
#sidebar h3{{font-size:14px;margin:14px 0 8px;color:#aaa}}
#sidebar label{{display:block;font-size:13px;margin:3px 0;cursor:pointer}}
#sidebar input[type=checkbox]{{margin-right:6px}}
#sidebar select,#sidebar input[type=range]{{width:100%;margin:4px 0 8px;padding:4px;background:#333;color:#fff;border:1px solid #555;border-radius:3px}}
#sidebar input[type=range]{{padding:0}}
#results{{flex:1;padding:20px;display:grid;grid-template-columns:repeat(auto-fill,minmax(160px,1fr));gap:12px;align-content:start}}
.fr{{width:160px;cursor:pointer;transition:transform .2s}}
.fr:hover{{transform:scale(1.05)}}
.fr .fp{{width:160px;height:240px;background-size:cover;background-position:center;border-radius:6px}}
.fr .fi{{font-size:12px;color:#ccc;padding:4px 2px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}}
#count{{position:fixed;bottom:10px;right:20px;background:rgba(0,0,0,.7);padding:6px 14px;border-radius:4px;font-size:13px;z-index:100}}
</style></head><body>
<a href="/test" class="back">← Тест</a>
<div id="sidebar"><h3>Жанры</h3>{genre_opts}<h3>Год от</h3><select id="yrFrom"><option value="">Все</option>{year_opts}</select><h3>Год до</h3><select id="yrTo"><option value="">Все</option>{year_opts}</select><h3>Рейтинг ≥</h3><input type="range" id="minRt" min="0" max="10" step="0.5" value="0"><span id="rtVal">0</span><h3>Коллекция</h3><select id="coll"><option value="">Все</option>{coll_opts}</select><h3>Формат</h3><select id="fmt"><option value="">Все</option>{fmt_opts}</select><h3>Сиды ≥</h3><input type="range" id="minSd" min="0" max="100" step="1" value="0"><span id="sdVal">0</span></div>
<div id="results"></div><div id="count"></div>
 <script>
const MOVIES = {movies_json};
function posterUrl(m){{return (m.poster_url||'').indexOf('data/')===0?'/'+m.poster_url:m.poster_url?'/data/'+m.poster_url:'/data/posters/placeholder.png'}}
function filter(){{
const selGenres=new Set([...document.querySelectorAll('.fg:checked')].map(c=>c.value));
const yrFrom=document.getElementById('yrFrom').value;
const yrTo=document.getElementById('yrTo').value;
const minRt=parseFloat(document.getElementById('minRt').value);
const coll=document.getElementById('coll').value;
const fmt=document.getElementById('fmt').value;
const minSd=parseInt(document.getElementById('minSd').value);
document.getElementById('rtVal').textContent=minRt;
document.getElementById('sdVal').textContent=minSd;
const out=MOVIES.filter(m=>{{
const g=(m.genre||'').split(',').map(s=>s.trim().toLowerCase()).filter(Boolean);
if(!g.some(x=>selGenres.has(x)))return false;
if(yrFrom&&m.movie_year<yrFrom)return false;
if(yrTo&&m.movie_year>yrTo)return false;
const r=parseFloat(m.kp_rating||m.imdb_rating||'0');
if(r<minRt)return false;
if(coll&&m.collection!==coll)return false;
if(fmt&&(m.format||'').toUpperCase()!==fmt)return false;
if((m.seeders||0)<minSd)return false;
return true;
}});
document.getElementById('count').textContent=out.length+' фильмов';
document.getElementById('results').innerHTML=out.map(m=>{{
const p=posterUrl(m)?'background-image:url('+posterUrl(m)+')':'background:#333';
const hash=(m.magnet||'').match(/btih:([A-Fa-f0-9]+)/)?.[1]?.toLowerCase();
const fmtBadge=m.format?'<span style="float:right;font-size:10px;color:#888">'+m.format.toUpperCase()+'</span>':'';
return '<div class=fr data-href="player.html#'+(hash||'')+'"><div class=fp style="'+p+'"></div><div class=fi>'+(m.orig_title||m.movie_title||'')+fmtBadge+'</div></div>';
}}).join('');
}}
document.querySelectorAll('.fg').forEach(c=>c.addEventListener('change',filter));
document.getElementById('yrFrom').addEventListener('change',filter);
document.getElementById('yrTo').addEventListener('change',filter);
document.getElementById('minRt').addEventListener('input',filter);
document.getElementById('coll').addEventListener('change',filter);
document.getElementById('fmt').addEventListener('change',filter);
document.getElementById('minSd').addEventListener('input',filter);
document.getElementById('results').addEventListener('click',function(e){{var t=e.target.closest('.fr');if(t&&t.dataset.href)location=t.dataset.href}});
filter();
</script></body></html>'''


@app.route('/browse/timeline')
def browse_timeline():
    import re
    movies = _get_movies()
    hidden = gp.load_hidden_topic_ids()
    movies = [m for m in movies if m['topic_id'] not in hidden]
    year_map: dict[str, list] = {}
    for m in movies:
        y = m.get('movie_year', '')
        if y:
            year_map.setdefault(y, []).append(m)
    years = sorted(year_map.keys(), reverse=True)
    rows_html = ''
    for y in years:
        items = year_map[y]
        cards = ''
        for m in items:
            rt = m.get('kp_rating') or m.get('imdb_rating') or '—'
            poster_style = _poster_style(m.get('poster_url', ''))
            match = re.search(r'btih:([A-Fa-f0-9]{40})', m.get('magnet', ''))
            player_url = f'player.html#{match.group(1).lower()}' if match else '#'
            title = m.get('orig_title') or m.get('movie_title', '')
            from html import escape as h_esc
            cards += f'''<div class="tc" onclick="location=\'{h_esc(player_url)}\'">
<div class="tp" style="{poster_style}"></div>
<div class="ti"><strong>{h_esc(title)}</strong> <span class="tr">{rt}</span></div>
</div>'''
        rows_html += f'''<div class="ty" onclick="this.classList.toggle(\'open\')">
<div class="yh"><span class="yl">{y}</span> <span class="yc">{len(items)}</span> <span class="ya">▼</span></div>
<div class="yg">{cards}</div>
</div>'''
    return f'''<!DOCTYPE html>
<html lang="ru"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0"><title>Timeline</title>
<style>
{BROWSE_CSS}
body{{padding:20px 30px}}
.ty{{margin-bottom:4px;overflow:hidden;border-radius:6px;background:#1a1a1a;transition:background .2s}}
.ty:hover{{background:#222}}
.yh{{padding:12px 16px;cursor:pointer;display:flex;align-items:center;gap:12px;user-select:none}}
.yl{{font-size:22px;font-weight:700;min-width:50px}}
.yc{{font-size:13px;color:#888}}
.ya{{margin-left:auto;color:#555;transition:transform .3s}}
.ty.open .ya{{transform:rotate(180deg)}}
.yg{{display:none;grid-template-columns:repeat(auto-fill,minmax(140px,1fr));gap:10px;padding:0 16px 16px}}
.ty.open .yg{{display:grid}}
.tc{{cursor:pointer;transition:transform .2s}}
.tc:hover{{transform:scale(1.05)}}
.tp{{width:140px;height:210px;background-size:cover;background-position:center;border-radius:5px}}
.ti{{font-size:11px;color:#ccc;padding:4px 0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}}
.tr{{color:#888}}
</style></head><body>
<a href="/test" class="back">← Тест</a>
<h1>📅 По годам</h1>
{rows_html}
</body></html>'''


@app.route('/browse/shuffle')
def browse_shuffle():
    movies = _get_movies()
    hidden = gp.load_hidden_topic_ids()
    hidden_set = set(str(k) for k in hidden)
    movies = [m for m in movies if str(m['topic_id']) not in hidden_set]
    movies_json = json.dumps(movies, ensure_ascii=False).replace('</script>', '<\\/script>')
    return f'''<!DOCTYPE html>
<html lang="ru"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0"><title>Киноплёнка</title>
<style>
body{{background:#000;overflow:hidden;margin:0;cursor:none;font-family:system-ui,sans-serif}}
#bg{{position:fixed;inset:0;background-size:cover;background-position:center;filter:blur(40px) brightness(.2);transition:background-image 1s}}
#main-w{{position:fixed;top:50%;left:50%;transform:translate(-50%,-50%);z-index:3;width:300px;height:440px;overflow:visible;border-radius:12px;box-shadow:0 10px 50px rgba(0,0,0,.8)}}
#main-w .mp{{position:absolute;inset:0;background-size:cover;background-position:center;background-color:#222;border-radius:12px;transform-origin:center center;transition:transform .5s ease,opacity .5s ease}}
#main-w .mp.next-idle{{visibility:hidden;opacity:0;transform:translateX(120%)}}
#main-w .mp.out{{transform:translateX(-220px) scale(.34);opacity:.9}}
#main-w .mp.out-right{{transform:translateX(220px) scale(.34);opacity:.9}}
#main-w .mp.in{{transform:translateX(220px) scale(.34)}}
#main-w .mp.in-left{{transform:translateX(-220px) scale(.34)}}
#main-w .mp.in.go{{transform:translateX(0);opacity:1}}
#main-w .mp.in-left.go{{transform:translateX(0);opacity:1}}
#info{{position:fixed;top:calc(50% + 230px);left:50%;transform:translateX(-50%);z-index:4;text-align:center;color:#fff;text-shadow:0 2px 8px rgba(0,0,0,.8);pointer-events:none}}
#info h2{{font-size:20px;margin:0 0 2px;font-weight:600}}
#info .meta{{font-size:12px;color:#aaa}}
#strip-wrap{{position:fixed;top:50%;left:0;right:0;transform:translateY(-50%);z-index:2;height:190px;overflow:hidden;pointer-events:none}}
.side-strip{{position:absolute;top:20px;height:150px;display:flex;align-items:center;gap:6px;transition:transform .5s cubic-bezier(.4,0,.2,1)}}
.side-strip::before,.side-strip::after{{content:"";position:absolute;left:0;right:0;height:20px;z-index:3;background-color:#050505;background-image:repeating-linear-gradient(90deg,transparent 0 20px,rgba(230,230,210,.72) 20px 34px,transparent 34px 54px);box-shadow:0 0 10px rgba(0,0,0,.8)}}
.side-strip::before{{top:-20px}}
.side-strip::after{{bottom:-20px}}
#strip-left{{right:calc(50% + 170px)}}
#strip-right{{left:calc(50% + 170px)}}
.side-strip .fr{{flex:none;width:100px;height:150px;background-size:cover;background-position:center;border-radius:4px;border:2px solid #333;box-sizing:border-box;background-color:#222;opacity:.64;transition:opacity .3s}}
.film-slot{{position:absolute;top:20px;width:100px;height:150px;z-index:4;box-sizing:border-box;border:2px solid #333;border-radius:4px;background:rgba(0,0,0,.24);visibility:hidden;opacity:0;transition:opacity .12s linear}}
.film-slot.show{{visibility:visible;opacity:1}}
.film-slot::before,.film-slot::after{{content:"";position:absolute;left:-2px;right:-2px;height:20px;background-color:#050505;background-image:repeating-linear-gradient(90deg,transparent 0 20px,rgba(230,230,210,.72) 20px 34px,transparent 34px 54px);box-shadow:0 0 10px rgba(0,0,0,.8)}}
.film-slot::before{{top:-22px}}
.film-slot::after{{bottom:-22px}}
#slot-left{{left:calc(50% - 270px)}}
#slot-right{{left:calc(50% + 170px)}}
.side-strip .fr.blank{{background:rgba(0,0,0,.18)}}
#ctrl{{position:fixed;bottom:5%;left:50%;transform:translateX(-50%);z-index:10;display:flex;gap:8px;opacity:0;transition:opacity .3s}}
#ctrl.show{{opacity:1}}
#ctrl button{{background:rgba(255,255,255,.08);color:#fff;border:1px solid #555;padding:6px 14px;border-radius:4px;font-size:13px;cursor:pointer}}
#ctrl button:hover{{background:rgba(255,255,255,.2)}}
#timer{{position:fixed;top:12px;left:16px;z-index:10;font-size:13px;color:#555;font-variant-numeric:tabular-nums}}
.back{{position:fixed;top:12px;right:16px;z-index:20;color:#888;text-decoration:none;font-size:13px;padding:4px 10px;border-radius:4px;background:rgba(0,0,0,.4)}}
.back:hover{{color:#fff}}
</style></head><body>
<a href="/test" class="back">← Тест</a>
<div id="timer"></div>
<div id="bg"></div>
<div id="main-w"><div class="mp" id="mpCur"></div><div class="mp next-idle" id="mpNext"></div></div>
<div id="info"><h2 id="stitle"></h2><div class="meta" id="smeta"></div></div>
<div id="strip-wrap"><div id="strip-left" class="side-strip"></div><div id="strip-right" class="side-strip"></div><div id="slot-left" class="film-slot"></div><div id="slot-right" class="film-slot"></div></div>
<div id="ctrl" class="show"><button id="playBtn" onclick="togglePause()">⏸</button><button onclick="next()">→</button><button onclick="setSpeed(5)">5с</button><button onclick="setSpeed(10)">10с</button><button onclick="setSpeed(15)">15с</button><button onclick="setSpeed(30)">30с</button></div>
 <script>
const MOVIES = {movies_json};
function pu(m){{return (m.poster_url||'').indexOf('data/')===0?'/'+m.poster_url:m.poster_url?'/data/'+m.poster_url:'/data/posters/placeholder.png'}}
let idx=Math.floor(Math.random()*MOVIES.length),paused=false,speed=5,timer=0,animating=false;
const FRAME=106; // width+gap (100+6)
const HALF=12;
const ANIM_MS=520;

function movieAt(i){{return MOVIES[(i+MOVIES.length)%MOVIES.length]}}
function setInfo(m){{
document.getElementById('stitle').textContent=m.orig_title||m.movie_title||'';
document.getElementById('smeta').textContent=[m.movie_year,m.kp_rating?'KP '+m.kp_rating:'',m.imdb_rating?'IMDB '+m.imdb_rating:''].filter(Boolean).join(' · ');
}}
function setMainPoster(m){{
const p=pu(m);
document.getElementById('bg').style.backgroundImage='url('+p+')';
document.getElementById('mpCur').style.backgroundImage='url('+p+')';
setInfo(m);
}}
function setStrip(center, direction){{
const total=MOVIES.length;
const left=document.getElementById('strip-left');
const right=document.getElementById('strip-right');
const leftHtml=[];
const rightHtml=[];
for(let o=-HALF;o<=-1;o++){{
if(direction==='backward'&&o===-1){{
leftHtml.push('<div class="fr blank"></div>');
continue;
}}
const fi=(center+o+total)%total;
leftHtml.push('<div class="fr" style="background-image:url('+pu(MOVIES[fi])+')"></div>');
}}
for(let o=1;o<=HALF;o++){{
if(direction==='forward'&&o===1){{
rightHtml.push('<div class="fr blank"></div>');
continue;
}}
const fi=(center+o+total)%total;
rightHtml.push('<div class="fr" style="background-image:url('+pu(MOVIES[fi])+')"></div>');
}}
left.innerHTML=leftHtml.join('');
right.innerHTML=rightHtml.join('');
left.style.transition='none';
right.style.transition='none';
left.style.transform='translateX(0)';
right.style.transform='translateX(0)';
void left.offsetHeight;
}}
function render(i){{
const m=MOVIES[i];if(!m)return;
setMainPoster(m);
setStrip(i,'idle');
timer=0;
}}

function move(delta){{
if(animating)return;
if(!MOVIES.length)return;
animating=true;
const direction=delta>0?'forward':'backward';
const ni=(idx+delta+MOVIES.length)%MOVIES.length;
const nm=MOVIES[ni]; if(!nm){{animating=false;return;}}
const newP=pu(nm);
const mpCur=document.getElementById('mpCur');
const mpNext=document.getElementById('mpNext');
const left=document.getElementById('strip-left');
const right=document.getElementById('strip-right');
const slot=document.getElementById(direction==='forward'?'slot-left':'slot-right');
setStrip(idx,direction);
document.getElementById('slot-left').className='film-slot';
document.getElementById('slot-right').className='film-slot';
slot.className='film-slot show';
document.getElementById('bg').style.backgroundImage='url('+newP+')';
// Reset next poster off-screen in the same direction as the filmstrip movement
mpNext.style.transition='none';
mpNext.style.backgroundImage='url('+newP+')';
mpNext.className=direction==='forward'?'mp in':'mp in-left';
mpNext.style.visibility='visible';
void mpNext.offsetHeight; // reflow
// Slide the strip and the large frame as one timed movement
left.style.transition='transform '+ANIM_MS+'ms cubic-bezier(.4,0,.2,1)';
right.style.transition='transform '+ANIM_MS+'ms cubic-bezier(.4,0,.2,1)';
left.style.transform='translateX('+(direction==='forward'?-FRAME:FRAME)+'px)';
right.style.transform='translateX('+(direction==='forward'?-FRAME:FRAME)+'px)';
mpCur.style.transition='transform '+ANIM_MS+'ms ease,opacity '+ANIM_MS+'ms ease';
mpCur.className=direction==='forward'?'mp out':'mp out-right';
mpNext.style.transition='transform '+ANIM_MS+'ms ease,opacity '+ANIM_MS+'ms ease';
mpNext.className=(direction==='forward'?'mp in go':'mp in-left go');
setTimeout(()=>{{
idx=ni;
// Set current poster to new image, reset positions
mpCur.style.transition='none';
mpCur.style.backgroundImage='url('+newP+')';
mpCur.className='mp';
mpNext.style.transition='none';
mpNext.style.backgroundImage='';
mpNext.className='mp next-idle';
mpNext.style.visibility='';
slot.className='film-slot';
setInfo(nm);
setStrip(idx,'idle');
timer=0;
animating=false;
}},ANIM_MS+40);
showCtrl();
}}

function next(){{move(1)}}
function prev(){{
move(-1);
}}

function togglePause(){{paused=!paused;document.getElementById('playBtn').textContent=paused?'▶':'⏸';showCtrl()}}
function setSpeed(s){{speed=s;timer=0}}
function showCtrl(){{document.getElementById('ctrl').classList.add('show');clearTimeout(window._hc);window._hc=setTimeout(()=>document.getElementById('ctrl').classList.remove('show'),2000)}}
document.addEventListener('mousemove',showCtrl);
document.addEventListener('keydown',e=>{{if(e.key==='ArrowRight')next();if(e.key==='ArrowLeft')prev();if(e.key===' '||e.key==='Space'){{e.preventDefault();togglePause()}}}});
setInterval(()=>{{if(paused)return;timer++;const s=speed-timer;document.getElementById('timer').textContent=(s>0?s+'с':'сейчас')+' | '+(paused?'⏸':'▶')+' '+speed+'с';if(timer>=speed)next()}},1000);
// Initial render
if(MOVIES.length){{render(idx);}}
else{{document.getElementById('stitle').textContent='Нет фильмов';}}
</script></body></html>'''
@app.route('/browse/duel')
def browse_duel():
    movies = _get_movies()
    hidden = gp.load_hidden_topic_ids()
    movies = [m for m in movies if m['topic_id'] not in hidden]
    movies_json = json.dumps(movies, ensure_ascii=False).replace('</script>', '<\\/script>')
    return f'''<!DOCTYPE html>
<html lang="ru"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0"><title>Дуэль</title>
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{background:#141414;color:#fff;font-family:system-ui,sans-serif;overflow:hidden;height:100vh;display:flex;flex-direction:column;align-items:center;justify-content:center}}
h1{{font-size:22px;margin-bottom:20px;color:#888}}
#arena{{display:flex;gap:40px;align-items:center}}
.fighter{{width:240px;cursor:pointer;transition:transform .3s,box-shadow .3s;border-radius:12px;overflow:hidden;text-align:center}}
.fighter:hover{{transform:scale(1.05);box-shadow:0 0 40px rgba(229,9,20,.4)}}
.fighter .poster{{width:240px;height:360px;background-size:cover;background-position:center;border-radius:10px;background-color:#222}}
.fighter .title{{font-size:14px;margin-top:8px;padding:0 4px}}
.fighter .meta{{font-size:12px;color:#888;margin-top:2px}}
.vs{{font-size:48px;color:#e50914;font-weight:900;text-shadow:0 0 20px rgba(229,9,20,.5)}}
#score{{position:fixed;bottom:30px;color:#555;font-size:13px}}
#skip{{position:fixed;top:20px;right:20px;z-index:10;background:rgba(255,255,255,.08);color:#aaa;border:1px solid #444;padding:8px 18px;border-radius:4px;font-size:13px;cursor:pointer}}
#skip:hover{{background:rgba(255,255,255,.15)}}
.back{{position:fixed;top:20px;left:20px;z-index:10;color:#888;text-decoration:none;font-size:13px;padding:4px 10px;border-radius:4px;background:rgba(0,0,0,.4)}}
.back:hover{{color:#fff}}
</style></head><body>
<a href="/test" class="back">← Тест</a>
<button id="skip" onclick="next()">Пропустить →</button>
<h1>Какой фильм лучше?</h1>
<div id="arena"><div class="fighter" id="fa" onclick="vote(0)"><div class="poster" id="pa"></div><div class="title" id="ta"></div><div class="meta" id="ma"></div></div><div class="vs">VS</div><div class="fighter" id="fb" onclick="vote(1)"><div class="poster" id="pb"></div><div class="title" id="tb"></div><div class="meta" id="mb"></div></div></div>
<div id="score">👍 <span id="sc">0</span></div>
<script>
const MOVIES={movies_json};
let i=Math.floor(Math.random()*MOVIES.length),score=0;
function pu(m){{return (m.poster_url||'').indexOf('data/')===0?'/'+m.poster_url:m.poster_url?'/data/'+m.poster_url:'/data/posters/placeholder.png'}}
function pick(n){{return MOVIES[(i+n)%MOVIES.length]}}
function show(){{
const a=pick(0),b=pick(1+Math.floor(Math.random()*(MOVIES.length-2)));
const set=(el,poster,title,meta)=>{{
el.style.backgroundImage='url('+pu(poster)+')';
document.getElementById('t'+el.id[1]).textContent=poster.orig_title||poster.movie_title||'';
document.getElementById('m'+el.id[1]).textContent=[poster.movie_year,poster.kp_rating?'KP '+poster.kp_rating:'',poster.imdb_rating?'IMDB '+poster.imdb_rating:''].filter(Boolean).join(' · ');
}};
set(document.getElementById('pa'),a);set(document.getElementById('pb'),b);
i=(i+1)%MOVIES.length;
}}
function vote(n){{score++;document.getElementById('sc').textContent=score;next()}}
function next(){{show()}}
show();
</script></body></html>'''

@app.route('/browse/matrix')
def browse_matrix():
    movies = _get_movies()
    hidden = gp.load_hidden_topic_ids()
    movies = [m for m in movies if m['topic_id'] not in hidden]
    movies_json = json.dumps(movies, ensure_ascii=False).replace('</script>', '<\\/script>')
    return f'''<!DOCTYPE html>
<html lang="ru"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0"><title>Матрица</title>
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{background:#0a0a0a;color:#fff;font-family:system-ui,sans-serif;overflow-y:auto;height:100vh}}
#grid{{display:grid;grid-template-columns:repeat(4,1fr);gap:16px;padding:28px}}
#grid .cell{{position:relative;background-size:cover;background-position:center;aspect-ratio:2/3;border-radius:6px;cursor:pointer;transition:transform .3s;overflow:hidden;background-color:#1a1a1a}}
#grid .cell:hover{{transform:scale(1.02);z-index:2;box-shadow:0 0 12px rgba(229,9,20,.25)}}
#grid .cell .label{{position:absolute;bottom:0;left:0;right:0;padding:6px 8px;background:linear-gradient(transparent,rgba(0,0,0,.8));font-size:12px;opacity:0;transition:opacity .2s}}
#grid .cell:hover .label{{opacity:1}}
#shuffle{{position:fixed;top:20px;right:100px;z-index:10;background:rgba(229,9,14,.8);color:#fff;border:0;padding:10px 24px;border-radius:6px;font-size:14px;cursor:pointer}}
#shuffle:hover{{background:#e50914}}
#pauseBtn{{position:fixed;top:20px;right:20px;z-index:10;background:rgba(80,80,80,.8);color:#fff;border:0;padding:10px 24px;border-radius:6px;font-size:14px;cursor:pointer}}
#pauseBtn:hover{{background:rgba(120,120,120,.8)}}
.back{{position:fixed;top:20px;left:20px;z-index:10;color:#888;text-decoration:none;font-size:13px;padding:4px 10px;border-radius:4px;background:rgba(0,0,0,.4)}}
.back:hover{{color:#fff}}
</style></head><body>
<a href="/test" class="back">← Тест</a>
<button id="shuffle" onclick="sc()" style="right:20px">🔀 Перемешать</button><button id="pauseBtn" onclick="togglePause()" style="right:100px">⏸ Пауза</button>
<div id="grid"></div>
<script>
const MOVIES={movies_json};
function pu(m){{return (m.poster_url||'').indexOf('data/')===0?'/'+m.poster_url:m.poster_url?'/data/'+m.poster_url:'/data/posters/placeholder.png'}}
function sc(){{
const a=[...MOVIES];
for(let i=a.length-1;i>0;i--){{const j=Math.floor(Math.random()*(i+1));[a[i],a[j]]=[a[j],a[i]];}}
a.length=8;
const g=document.getElementById('grid');
    g.innerHTML=a.map(m=>'<div class="cell" data-hash="'+((m.magnet||'').match(/btih:([A-Fa-f0-9]+)/)?.[1]?.toLowerCase()||'')+'" style="background-image:url('+pu(m)+')"><div class="label">'+(m.orig_title||m.movie_title||'')+'</div></div>').join('');
    g.onclick=function(e){{var c=e.target.closest(\'.cell\');if(c&&c.dataset.hash){{window.open(\'player.html#\'+c.dataset.hash,\'_blank\');}}}}
}}
sc();
var _ti=setInterval(sc,10000);
function togglePause(){{
if(_ti){{clearInterval(_ti);_ti=null;document.getElementById('pauseBtn').textContent='▶ Пуск';}}
else{{_ti=setInterval(sc,10000);document.getElementById('pauseBtn').textContent='⏸ Пауза';}}
}}
</script></body></html>'''

@app.route('/browse/stats')
def browse_stats():
    movies = _get_movies()
    hidden = gp.load_hidden_topic_ids()
    movies = [m for m in movies if m['topic_id'] not in hidden]
    genre_counts: dict[str, int] = {}
    for m in movies:
        for g in m.get('genre', '').split(','):
            g = g.strip().lower()
            if g:
                genre_counts[g] = genre_counts.get(g, 0) + 1
    top_genres = sorted(genre_counts.items(), key=lambda x: -x[1])[:15]
    total = len(movies)
    top = {k: v for k, v in top_genres}
    genre_json = json.dumps(top, ensure_ascii=False)
    year_groups: dict[int, int] = {}
    for m in movies:
        y = m.get('movie_year')
        if y:
            year_groups[y] = year_groups.get(y, 0) + 1
    years_sorted = sorted(year_groups.items())
    years_json = json.dumps(dict(years_sorted), ensure_ascii=False)
    rated = sum(1 for m in movies if m.get('kp_rating') or m.get('imdb_rating'))
    avg_rating = 0
    ratings_list = [float(m.get('kp_rating') or m.get('imdb_rating') or 0) for m in movies if m.get('kp_rating') or m.get('imdb_rating')]
    if ratings_list:
        avg_rating = round(sum(ratings_list) / len(ratings_list), 1)
    coll_counts: dict[str, int] = {}
    for m in movies:
        c = m.get('collection', '')
        if c:
            coll_counts[c] = coll_counts.get(c, 0) + 1
    coll_json = json.dumps(coll_counts, ensure_ascii=False)
    return f'''<!DOCTYPE html>
<html lang="ru"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0"><title>Статистика</title>
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{background:#141414;color:#fff;font-family:system-ui,sans-serif;padding:30px}}
h1{{font-size:24px;margin-bottom:4px}}
.sub{{color:#888;font-size:14px;margin-bottom:30px}}
.row{{display:flex;gap:30px;flex-wrap:wrap}}
.box{{background:#1f1f1f;border-radius:10px;padding:20px;flex:1;min-width:300px}}
.box h2{{font-size:16px;color:#aaa;margin-bottom:12px}}
.stat{{font-size:32px;font-weight:700}}
.stat-label{{font-size:13px;color:#888}}
canvas{{max-width:100%;height:auto!important}}
.bar{{display:flex;align-items:center;margin:4px 0;gap:8px}}
.bar-label{{font-size:12px;width:100px;text-align:right;color:#aaa;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}
.bar-fill{{height:16px;border-radius:3px;min-width:2px;transition:width .5s}}
.bar-val{{font-size:11px;color:#666}}
.back{{position:fixed;top:15px;right:20px;z-index:100;background:rgba(0,0,0,.7);color:#fff;border:1px solid #555;padding:6px 14px;border-radius:4px;font-size:13px;cursor:pointer}}
.back:hover{{background:#e50914;border-color:#e50914}}
</style></head><body>
<a href="/test" class="back">← Тест</a>
<h1>📊 Статистика</h1>
<p class="sub">{total} фильмов, {rated} с рейтингом, средний {avg_rating}</p>
<div class="row">
<div class="box"><h2>🎭 Жанры</h2><div id="genre-bars"></div></div>
<div class="box"><h2>📅 Годы</h2><canvas id="yearChart" height="200"></canvas></div>
<div class="box"><h2>📂 Коллекции</h2><div id="coll-bars"></div></div>
</div>
<script>
const GENRES={genre_json};
const YEARS={years_json};
const COLLS={coll_json};
const COLORS=['#e50914','#f5c518','#46d369','#0072eb','#e87c03','#b9090b','#1a73e8','#34a853','#ea4335','#fbbc04','#ff6d01','#c44601','#564d4d','#808080','#a0a0a0'];
const maxG=Math.max(...Object.values(GENRES));
document.getElementById('genre-bars').innerHTML=Object.entries(GENRES).map(([g,n],i)=>'<div class="bar"><span class="bar-label">'+g+'</span><div class="bar-fill" style="width:'+(n/maxG*100)+'%;background:'+COLORS[i%COLORS.length]+'"></div><span class="bar-val">'+n+'</span></div>').join('');
const maxC=Math.max(...Object.values(COLLS));
document.getElementById('coll-bars').innerHTML=Object.entries(COLLS).map(([c,n],i)=>'<div class="bar"><span class="bar-label">'+c+'</span><div class="bar-fill" style="width:'+(n/maxC*100)+'%;background:'+COLORS[i%COLORS.length]+'"></div><span class="bar-val">'+n+'</span></div>').join('');
const canvas=document.getElementById('yearChart');
const ctx=canvas.getContext('2d');
const years=Object.keys(YEARS);
const vals=Object.values(YEARS);
canvas.width=canvas.parentElement.offsetWidth-40;canvas.height=200;
const w=canvas.width,y=200,h=160,b=30;
const max=Math.max(...vals);
ctx.clearRect(0,0,w,y);
years.forEach((yr,i)=>{{
const barW=Math.max(4,w/years.length-2);
const barH=(vals[i]/max)*h;
const x=i*(barW+2)+b;
ctx.fillStyle=COLORS[i%COLORS.length];
ctx.fillRect(x,y-25-barH,barW,barH);
ctx.fillStyle='#888';
ctx.font='9px sans-serif';
ctx.textAlign='center';
ctx.fillText(yr,x+barW/2,y-27-barH);
}});
</script></body></html>'''

@app.route('/browse/search')
def browse_search():
    movies = _get_movies()
    hidden = gp.load_hidden_topic_ids()
    movies = [m for m in movies if m['topic_id'] not in hidden]
    movies_json = json.dumps(movies, ensure_ascii=False).replace('</script>', '<\\/script>')
    return f'''<!DOCTYPE html>
<html lang="ru"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0"><title>Поиск</title>
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{background:#141414;color:#fff;font-family:system-ui,sans-serif;padding:20px}}
input[type=text]{{width:100%;padding:14px 18px;font-size:18px;border:0;border-radius:8px;background:#2a2a2a;color:#fff;outline:0}}
input[type=text]:focus{{box-shadow:0 0 0 2px #e50914}}
input[type=text]::placeholder{{color:#666}}
#results{{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:12px;margin-top:20px}}
.card{{position:relative;aspect-ratio:2/3;background-size:cover;background-position:center;border-radius:6px;cursor:pointer;overflow:hidden;background-color:#1a1a1a;transition:transform .2s,box-shadow .2s}}
.card:hover{{transform:scale(1.03);box-shadow:0 0 20px rgba(229,9,20,.3);z-index:2}}
.card .info{{position:absolute;bottom:0;left:0;right:0;padding:8px;background:linear-gradient(transparent,rgba(0,0,0,.9));font-size:12px;opacity:0;transition:opacity .2s}}
.card:hover .info{{opacity:1}}
.no{{color:#666;text-align:center;margin-top:40px;font-size:16px}}
.cnt{{color:#888;font-size:13px;margin-top:8px}}
.back{{position:fixed;top:15px;right:20px;z-index:100;background:rgba(0,0,0,.7);color:#fff;border:1px solid #555;padding:6px 14px;border-radius:4px;font-size:13px;cursor:pointer;text-decoration:none}}
.back:hover{{background:#e50914;border-color:#e50914}}
</style></head><body>
<a href="/test" class="back">← Тест</a>
<input type="text" id="q" placeholder="Название фильма..." autofocus>
<p class="cnt" id="cnt"></p>
<div id="results"></div>
<script>
const MOVIES={movies_json};
function pu(m){{return (m.poster_url||'').indexOf('data/')===0?'/'+m.poster_url:m.poster_url?'/data/'+m.poster_url:'/data/posters/placeholder.png'}}
function esc(s){{return String(s||'').replace(/[&<>"']/g,c=>({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[c]))}}
function hash(m){{const x=(m.magnet||'').match(/btih:([A-Fa-f0-9]+)/);return x?x[1].toLowerCase():''}}
function openMovie(h){{if(h) window.open('/player.html#'+h,'_blank')}}
function card(m){{const h=hash(m);return '<div class="card" data-hash="'+h+'" style="background-image:url('+pu(m)+')"><div class="info">'+esc(m.movie_title||m.orig_title||'')+'<br>'+esc(m.movie_year||'')+'</div></div>'}}
function render(q){{
const ql=q.toLowerCase().trim();
const filtered=ql?MOVIES.filter(m=>(m.movie_title||'').toLowerCase().includes(ql)||(m.orig_title||'').toLowerCase().includes(ql)):MOVIES;
document.getElementById('cnt').textContent='Найдено: '+filtered.length;
document.getElementById('results').innerHTML=filtered.length?filtered.map(card).join(''):'<div class="no">Ничего не найдено</div>';
}}
document.getElementById('results').addEventListener('click',e=>{{const c=e.target.closest('.card');if(c)openMovie(c.dataset.hash)}});
document.getElementById('q').addEventListener('input',function(){{render(this.value);}});
render('');
</script></body></html>'''


@app.route('/browse/top')
def browse_top():
    movies = _get_movies()
    hidden = gp.load_hidden_topic_ids()
    movies = [m for m in movies if m['topic_id'] not in hidden]
    def _rating(m):
        kp = float(m.get('kp_rating') or 0)
        imdb = float(m.get('imdb_rating') or 0)
        return max(kp, imdb)
    movies.sort(key=_rating, reverse=True)
    top = movies[:50]
    movies_json = json.dumps(top, ensure_ascii=False).replace('</script>', '<\\/script>')
    return f'''<!DOCTYPE html>
<html lang="ru"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0"><title>Топ-50</title>
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{background:#141414;color:#fff;font-family:system-ui,sans-serif;padding:20px}}
h1{{font-size:24px}}
.sub{{color:#888;font-size:14px;margin-bottom:20px}}
#grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:12px}}
.card{{position:relative;aspect-ratio:2/3;background-size:cover;background-position:center;border-radius:6px;cursor:pointer;overflow:hidden;background-color:#1a1a1a;transition:transform .2s,box-shadow .2s}}
.card:hover{{transform:scale(1.03);box-shadow:0 0 20px rgba(229,9,20,.3);z-index:2}}
.card .info{{position:absolute;bottom:0;left:0;right:0;padding:8px;background:linear-gradient(transparent,rgba(0,0,0,.9));font-size:12px;opacity:0;transition:opacity .2s}}
.card:hover .info{{opacity:1}}
.rank{{position:absolute;top:6px;left:6px;width:26px;height:26px;background:#e50914;color:#fff;font-size:13px;font-weight:700;border-radius:50%;display:flex;align-items:center;justify-content:center;z-index:3}}
.badge{{position:absolute;top:6px;right:6px;background:rgba(0,0,0,.7);color:#f5c518;font-size:12px;font-weight:600;padding:2px 6px;border-radius:4px;z-index:3}}
.back{{position:fixed;top:15px;right:20px;z-index:100;background:rgba(0,0,0,.7);color:#fff;border:1px solid #555;padding:6px 14px;border-radius:4px;font-size:13px;cursor:pointer;text-decoration:none}}
.back:hover{{background:#e50914;border-color:#e50914}}
</style></head><body>
<a href="/test" class="back">← Тест</a>
<h1>🏆 Топ-50</h1>
<p class="sub">По рейтингу Кинопоиска и IMDB</p>
<div id="grid"></div>
<script>
const MOVIES={movies_json};
function pu(m){{return (m.poster_url||'').indexOf('data/')===0?'/'+m.poster_url:m.poster_url?'/data/'+m.poster_url:'/data/posters/placeholder.png'}}
function rt(m){{return Math.max(parseFloat(m.kp_rating)||0,parseFloat(m.imdb_rating)||0)}}
function esc(s){{return String(s||'').replace(/[&<>"']/g,c=>({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[c]))}}
function hash(m){{const x=(m.magnet||'').match(/btih:([A-Fa-f0-9]+)/);return x?x[1].toLowerCase():''}}
function openMovie(h){{if(h) window.open('/player.html#'+h,'_blank')}}
document.getElementById('grid').innerHTML=MOVIES.map((m,i)=>'<div class="card" data-hash="'+hash(m)+'" style="background-image:url('+pu(m)+')"><span class="rank">'+(i+1)+'</span><span class="badge">★ '+(rt(m)||0).toFixed(1)+'</span><div class="info">'+esc(m.movie_title||m.orig_title||'')+'<br>'+esc(m.movie_year||'')+'</div></div>').join('');
document.getElementById('grid').addEventListener('click',e=>{{const c=e.target.closest('.card');if(c)openMovie(c.dataset.hash)}});
</script></body></html>'''


@app.route('/browse/collections')
def browse_collections():
    movies = _get_movies()
    hidden = gp.load_hidden_topic_ids()
    movies = [m for m in movies if m['topic_id'] not in hidden]
    groups: dict[str, list] = {}
    for m in movies:
        c = m.get('collection', 'unknown')
        groups.setdefault(c, []).append(m)
    coll_json = json.dumps(groups, ensure_ascii=False, default=str).replace('</script>', '<\\/script>')
    labels = {'nashe_kino': 'Наше кино', 'kino_sng': 'Кино СНГ', 'novinki_2026': 'Новинки 2026', 'kino_sng_hd': 'Кино СНГ HD'}
    labels_json = json.dumps(labels, ensure_ascii=False)
    return f'''<!DOCTYPE html>
<html lang="ru"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0"><title>Коллекции</title>
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{background:#141414;color:#fff;font-family:system-ui,sans-serif;padding:20px}}
h1{{font-size:24px;margin-bottom:4px}}
.sub{{color:#888;font-size:14px;margin-bottom:20px}}
.section{{margin-bottom:24px}}
.section h2{{font-size:18px;margin-bottom:4px;cursor:pointer;display:flex;align-items:center;gap:8px;user-select:none}}
.section h2:hover{{color:#e50914}}
.section h2 .arrow{{transition:transform .2s;font-size:14px}}
.section h2 .arrow.open{{transform:rotate(180deg)}}
.section h2 .cnt{{color:#888;font-size:13px;font-weight:400}}
.grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(140px,1fr));gap:10px;margin-top:10px}}
.card{{position:relative;aspect-ratio:2/3;background-size:cover;background-position:center;border-radius:6px;cursor:pointer;overflow:hidden;background-color:#1a1a1a;transition:transform .2s,box-shadow .2s}}
.card:hover{{transform:scale(1.03);box-shadow:0 0 15px rgba(229,9,20,.3);z-index:2}}
.card .info{{position:absolute;bottom:0;left:0;right:0;padding:6px;background:linear-gradient(transparent,rgba(0,0,0,.9));font-size:11px;opacity:0;transition:opacity .2s}}
.card:hover .info{{opacity:1}}
.back{{position:fixed;top:15px;right:20px;z-index:100;background:rgba(0,0,0,.7);color:#fff;border:1px solid #555;padding:6px 14px;border-radius:4px;font-size:13px;cursor:pointer;text-decoration:none}}
.back:hover{{background:#e50914;border-color:#e50914}}
</style></head><body>
<a href="/test" class="back">← Тест</a>
<h1>📂 Коллекции</h1>
<p class="sub">{len(movies)} фильмов, {len(groups)} коллекций</p>
<div id="root"></div>
<script>
const GROUPS={coll_json};
const LABELS={labels_json};
function pu(m){{return (m.poster_url||'').indexOf('data/')===0?'/'+m.poster_url:m.poster_url?'/data/'+m.poster_url:'/data/posters/placeholder.png'}}
function label(k){{return LABELS[k]||k}}
function esc(s){{return String(s||'').replace(/[&<>"']/g,c=>({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[c]))}}
function hash(m){{const x=(m.magnet||'').match(/btih:([A-Fa-f0-9]+)/);return x?x[1].toLowerCase():''}}
function openMovie(h){{if(h) window.open('/player.html#'+h,'_blank')}}
function card(m){{return '<div class="card" data-hash="'+hash(m)+'" style="background-image:url('+pu(m)+')"><div class="info">'+esc(m.movie_title||m.orig_title||'')+'<br>'+esc(m.movie_year||'')+'</div></div>'}}
document.getElementById('root').innerHTML=Object.entries(GROUPS).map(([key,items],gi)=>'<div class="section"><h2><span class="arrow'+(gi===0?' open':'')+'">&#9660;</span>'+esc(label(key))+' <span class="cnt">('+items.length+')</span></h2><div class="grid"'+(gi>0?' style="display:none"':'')+'>'+items.map(card).join('')+'</div></div>').join('');
document.getElementById('root').addEventListener('click',e=>{{const h=e.target.closest('h2');if(h){{const g=h.nextElementSibling;g.style.display=g.style.display==='none'?'':'none';h.querySelector('.arrow').classList.toggle('open');return}}const c=e.target.closest('.card');if(c)openMovie(c.dataset.hash)}});
</script></body></html>'''


if __name__ == '__main__':
    import webbrowser
    port = int(sys.argv[1]) if len(sys.argv) > 1 else SERVER_PORT
    print(f'LocaL-Kino server: http://localhost:{port}')
    print(f'Test with: http://localhost:{port}/player.html')

    def _deferred_cleanup():
        print('Запускаю очистку temp...')
        removed = engine.cleanup(MAX_TEMP_SIZE_BYTES, TEMP_MAX_AGE_SECS, MAX_TEMP_FILES)
        if removed:
            print(f'  Удалено папок: {len(removed)}')
            for p in removed:
                print(f'    - {p}')
        else:
            print('  Всё в порядке, ничего не удалено.')
        topic_cleanup = cleanup_old_topics()
        if topic_cleanup.get('removed_topics') or topic_cleanup.get('dated_topics') or topic_cleanup.get('skipped_topics'):
            print('Очистка старых тем:')
            print(f'  Тем: {topic_cleanup["removed_topics"]}')
            print(f'  Тем с новой датой: {topic_cleanup["dated_topics"]}')
            print(f'  Старых оставлено политикой коллекций: {topic_cleanup["skipped_topics"]}')
            print(f'  Кешей: {topic_cleanup["removed_cache"]}')
            print(f'  Постеров: {topic_cleanup["removed_posters"]}')
        else:
            print(f'Старых тем старше {TOPIC_MAX_AGE_DAYS} дней нет.')
        _run_daily_refresh_if_due('startup')

    threading.Thread(target=_deferred_cleanup, daemon=True).start()

    print()
    webbrowser.open(f'http://localhost:{port}')
    _ensure_session_monitor()
    _ensure_enrich_worker()
    _ensure_periodic_enrich()
    _ensure_daily_refresh_loop()
    sock = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
    sock.bind(('::', port))
    sock.listen(128)
    from werkzeug.serving import make_server
    server = make_server('::', port, app, threaded=True, fd=sock.fileno())
    sock.close()
    print(f' * Running on http://127.0.0.1:{port}')
    server.serve_forever()
