import os
import re
import sys
import time
import json
import mimetypes
import threading
import subprocess
from flask import Flask, request, Response, jsonify, send_file, abort, redirect

from config import BASE_DIR, TEMP_DIR, MAX_TEMP_SIZE_BYTES, TEMP_MAX_AGE_SECS, MAX_TEMP_FILES, SERVER_PORT, ENRICH_INTERVAL_MINUTES

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

app = Flask(__name__, static_folder=str(BASE_DIR), static_url_path='')

STREAM_IDLE_TIMEOUT = 30
SESSION_SWEEP_INTERVAL = 10
_sessions_lock = threading.Lock()
_stream_sessions: dict[str, dict[str, float | str]] = {}
_session_monitor_started = False

_enrich_status: dict[str, str] = {}
_enrich_lock = threading.Lock()
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
            data_path = BASE_DIR / 'torrents_data.json'
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
                gp.enrich_topic(topic)
                atomic_write_json_unlocked(data_path, topics)
                gen_path = BASE_DIR / 'index-kino.html'
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
    data_path = BASE_DIR / 'torrents_data.json'
    if not data_path.exists():
        return
    try:
        with file_lock(data_path):
            topics = json.loads(data_path.read_text('utf-8'))
            changed = False
            MAX_RETRIES = 3
            for topic in topics:
                need = (not topic.get('magnet') or topic.get('_magnet_failed')
                        or not topic.get('poster_url')
                        or not topic.get('kp_rating')
                        or not topic.get('youtube_url')
                        or not topic.get('imdb_id')
                        or not topic.get('format'))
                if not need:
                    continue
                retries = topic.get('_enrich_retries', 0)
                if not force and retries >= MAX_RETRIES:
                    continue
                title = topic.get('movie_title') or topic.get('title', '?')
                print(f'  [enrich] #{topic["topic_id"]} {title} (retry {retries})')
                topic['_enrich_retries'] = retries + 1
                gp.enrich_topic(topic)
                changed = True
                still_missing = (not topic.get('magnet') or topic.get('_magnet_failed')
                                 or not topic.get('poster_url')
                                 or not topic.get('kp_rating')
                                 or not topic.get('youtube_url')
                                 or not topic.get('imdb_id')
                                 or not topic.get('format'))
                if not still_missing:
                    topic.pop('_enrich_retries', None)
                    print(f'    -> OK')
                else:
                    print(f'    -> ещё не все данные')
            if changed:
                atomic_write_json_unlocked(data_path, topics)
                gen_path = BASE_DIR / 'index-kino.html'
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


def _mark_stream_session(info_hash: str) -> str:
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
    try:
        info_hash = engine.add_magnet(magnet, timeout=7)
    except TimeoutError:
        info_hash = engine.add_magnet_async(magnet)
        return jsonify(info_hash=info_hash, async_mode=True)
    return jsonify(info_hash=info_hash, async_mode=False)


@app.route('/status/<info_hash>')
def status(info_hash):
    s = engine.get_status(info_hash)
    if s is None:
        return jsonify(error='not found'), 404
    return jsonify(s)


INITIAL_CHUNK = 8 * 1024 * 1024  # 8 MB - covers full moov for most files
MAX_RANGE_CHUNK = 16 * 1024 * 1024
STREAM_READ_CHUNK = 1024 * 1024


@app.route('/stream/<info_hash>')
def stream(info_hash):
    sid = _mark_stream_session(info_hash)
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


@app.route('/')
def index():
    index_path = BASE_DIR / 'index-kino.html'

    if not index_path.exists():
        return '<h1>HomeKino</h1><p>index-kino.html not found. Run generate_page.py first.</p>'
    html = index_path.read_text('utf-8')
    refresh_btn = '<a class="rf" href="/refresh" title="Обновить данные с Pirate Bay" style="font-size:14px;margin-left:8px;text-decoration:none;cursor:pointer">🔄</a>'
    html = html.replace('</span>', f'{refresh_btn}</span>', 1)
    return html


@app.route('/refresh')
def refresh():
    import subprocess, sys
    def generate():
        yield '<html><body><h2>Обновляю данные...</h2><pre>'
        proc = subprocess.Popen(
            [sys.executable, 'generate_page.py', '--refresh'],
            cwd=str(BASE_DIR),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        for line in proc.stdout:
            yield line
        proc.wait()
        yield '</pre><p><a href="/">Готово</a></p></body></html>'
    return Response(generate(), content_type='text/html')


@app.route('/cleanup')
def cleanup_trigger():
    removed = engine.cleanup(MAX_TEMP_SIZE_BYTES, TEMP_MAX_AGE_SECS, MAX_TEMP_FILES)
    return jsonify(removed=removed, count=len(removed))


@app.route('/torrents_data.json')
def torrents_data():
    return send_file(str(BASE_DIR / 'torrents_data.json'))


@app.route('/enrich/all', methods=['POST'])
def enrich_all():
    threading.Thread(target=_enrich_missing, args=[True], daemon=True).start()
    return jsonify(status='started')


@app.route('/enrich/<topic_id>', methods=['POST'])
def enrich_topic(topic_id):
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
    with _enrich_lock:
        status = _enrich_status.get(topic_id, 'unknown')
    return jsonify(status=status)


@app.after_request
def add_cors(response):
    response.headers['Access-Control-Allow-Origin'] = '*'
    return response


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

    threading.Thread(target=_deferred_cleanup, daemon=True).start()

    print()
    webbrowser.open(f'http://localhost:{port}')
    _ensure_session_monitor()
    _ensure_enrich_worker()
    _ensure_periodic_enrich()
    app.run(host='0.0.0.0', port=port, threaded=True)
