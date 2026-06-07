import json
import os
import re
import shutil
import subprocess
import time
import threading
import urllib.parse
import libtorrent as lt

from config import READAHEAD_SECONDS, READAHEAD_RECHARGE_SECONDS, READAHEAD_MAX_BYTES


def _get_dir_size(path: str) -> int:
    total = 0
    for dirpath, _dirnames, filenames in os.walk(path):
        for fn in filenames:
            fp = os.path.join(dirpath, fn)
            try:
                total += os.path.getsize(fp)
            except OSError:
                continue
    return total


def _remove_dir(path: str):
    def _onerror(func, p, _exc_info):
        os.chmod(p, 0o777)
        func(p)
    shutil.rmtree(path, onexc=_onerror)


def _remove_path(path: str):
    if os.path.isdir(path):
        _remove_dir(path)
    else:
        try:
            os.chmod(path, 0o777)
        except OSError:
            pass
        os.remove(path)


PIECE_PRIORITY_NONE = 0
PIECE_PRIORITY_HIGH = 7
PIECE_PRIORITY_LOW = 1
HIGH_PRIORITY_COUNT = 2
READY_THRESHOLD = 0.03


class TorrentEngine:
    def __init__(self, temp_dir="temp"):
        self.temp_dir = temp_dir
        os.makedirs(temp_dir, exist_ok=True)

        self.ses = lt.session()
        self.ses.listen_on(6881, 6891)
        settings = {
            'enable_dht': True,
            'enable_lsd': True,
            'enable_upnp': True,
            'enable_natpmp': True,
            'dht_bootstrap_nodes': 'dht.transmissionbt.com:6881',
            'alert_mask': lt.alert.category_t.error_notification,
        }
        self.ses.apply_settings(settings)
        self.handles: dict[str, lt.torrent_handle] = {}
        self.local_files: dict[str, str] = {}
        self._readahead_cache: dict[str, tuple[int, int]] = {}

    def add_magnet(self, magnet_url: str, timeout: float = 30) -> str:
        match = re.search(r'btih:([A-Fa-f0-9]{40})', magnet_url)
        if match:
            info_hash = match.group(1).lower()
            self._register_existing_file(info_hash, magnet_url)
            existing = self.handles.get(info_hash)
            if existing:
                try:
                    existing.status()
                    return info_hash
                except RuntimeError:
                    pass

        params = lt.parse_magnet_uri(magnet_url)
        params.save_path = self.temp_dir
        handle = self.ses.add_torrent(params)
        deadline = time.monotonic() + timeout
        while not handle.status().has_metadata:
            if time.monotonic() > deadline:
                self.ses.remove_torrent(handle)
                raise TimeoutError(f'Metadata not received within {timeout}s')
            time.sleep(0.1)
        info = handle.torrent_file()
        info_hash = str(info.info_hash())
        self._prioritize_streaming_file(handle)

        self.handles[info_hash] = handle
        return info_hash

    def add_magnet_async(self, magnet_url: str) -> str:
        match = re.search(r'btih:([A-Fa-f0-9]{40})', magnet_url)
        if not match:
            raise ValueError('Cannot extract info hash from magnet')
        info_hash = match.group(1).lower()
        self._register_existing_file(info_hash, magnet_url)
        existing = self.handles.get(info_hash)
        if existing:
            try:
                existing.status()
                return info_hash
            except RuntimeError:
                self.handles.pop(info_hash, None)

        params = lt.parse_magnet_uri(magnet_url)
        params.save_path = self.temp_dir
        handle = self.ses.add_torrent(params)
        self.handles[info_hash] = handle

        def _wait_metadata():
            deadline = time.monotonic() + 30
            while not handle.status().has_metadata:
                if time.monotonic() > deadline:
                    self.handles.pop(info_hash, None)
                    self.ses.remove_torrent(handle)
                    return
                time.sleep(0.1)
            info = handle.torrent_file()
            self._prioritize_streaming_file(handle)

        t = threading.Thread(target=_wait_metadata, daemon=True)
        t.start()
        return info_hash

    def get_status(self, info_hash: str) -> dict | None:
        handle = self.handles.get(info_hash)
        if not handle:
            return None
        s = handle.status()
        if not s.has_metadata:
            return {
                'progress': 0,
                'downloaded': 0,
                'total': 1,
                'download_rate': 0,
                'upload_rate': 0,
                'num_peers': 0,
                'num_seeds': 0,
                'state': 'pending',
                'ready': False,
                'video_path': None,
                'file_size': 0,
                'name': 'Получение метаданных...',
            }
        total = s.total_wanted
        downloaded = s.total_wanted_done
        progress = (downloaded / total) if total > 0 else 0

        video_path = self._get_video_path(handle)
        file_size = 0
        video_format = None
        if video_path:
            file_size = os.path.getsize(video_path) if os.path.exists(video_path) else 0
            ext = os.path.splitext(video_path)[1].lower()
            if ext:
                video_format = ext.lstrip('.')

        streamable = video_format in ('mkv', 'mp4')
        num_pieces = handle.torrent_file().num_pieces()
        check_count = min(HIGH_PRIORITY_COUNT, num_pieces)
        first_done = all(handle.have_piece(i) for i in range(check_count))
        last_done = all(handle.have_piece(i) for i in range(num_pieces - check_count, num_pieces))
        enough_overall = progress >= READY_THRESHOLD
        ready = (progress >= 1.0) if not streamable else (first_done and (last_done or enough_overall))

        return {
            'progress': round(progress, 4),
            'downloaded': downloaded,
            'total': total,
            'download_rate': s.download_rate,
            'upload_rate': s.upload_rate,
            'num_peers': s.num_peers,
            'num_seeds': s.num_seeds,
            'state': str(s.state),
            'ready': ready,
            'video_path': video_path,
            'file_size': file_size,
            'name': handle.torrent_file().name(),
            'format': video_format,
        }

    def get_handle(self, info_hash: str) -> lt.torrent_handle | None:
        return self.handles.get(info_hash)

    def wait_for_handle(self, info_hash: str, timeout: float = 30) -> lt.torrent_handle | None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            h = self.handles.get(info_hash)
            if h:
                return h
            time.sleep(0.3)
        return None

    def get_file_path(self, info_hash: str) -> str | None:
        handle = self.handles.get(info_hash)
        if not handle:
            return self.local_files.get(info_hash)
        return self._get_video_path(handle) or self.local_files.get(info_hash)

    def have_piece(self, info_hash: str, piece_index: int) -> bool:
        handle = self.handles.get(info_hash)
        if not handle:
            return False
        if piece_index < 0 or piece_index >= handle.torrent_file().num_pieces():
            return False
        return handle.have_piece(piece_index)

    def prioritize_piece_range(self, info_hash: str, start_piece: int, end_piece: int):
        handle = self.handles.get(info_hash)
        if not handle:
            return
        try:
            num_pieces = handle.torrent_file().num_pieces()
            start_piece = max(0, min(start_piece, num_pieces - 1))
            end_piece = max(start_piece, min(end_piece, num_pieces - 1))

            total_size = handle.torrent_file().total_size()
            avg_piece_size = total_size / num_pieces if num_pieces > 0 else 1

            readahead_bytes, recharge_bytes = self._calc_readahead_bytes(
                handle, total_size
            )
            readahead_pieces = int(readahead_bytes / avg_piece_size) if readahead_bytes > 0 else 0
            recharge_pieces = int(recharge_bytes / avg_piece_size) if recharge_bytes > 0 else 0

            if readahead_pieces < 1:
                readahead_pieces = 1

            block_index = start_piece // readahead_pieces
            block_start = block_index * readahead_pieces
            block_end = min(block_start + readahead_pieces - 1, num_pieces - 1)

            position_in_block = start_piece - block_start
            need_next_block = (end_piece > block_end) or (
                position_in_block >= (readahead_pieces - recharge_pieces)
            )

            horizon = block_end
            if need_next_block and block_end < num_pieces - 1:
                horizon = min(block_end + readahead_pieces, num_pieces - 1)

            priorities = [PIECE_PRIORITY_NONE] * num_pieces
            for i in range(block_start, horizon + 1):
                priorities[i] = PIECE_PRIORITY_HIGH
            handle.prioritize_pieces(priorities)
        except RuntimeError:
            return

    READAHEAD_FALLBACK_FRACTION = 0.10

    def _calc_readahead_bytes(self, handle: lt.torrent_handle, total_size: int) -> tuple[int, int]:
        info_hash = str(handle.torrent_file().info_hash())
        cached = self._readahead_cache.get(info_hash)
        if cached is not None:
            return cached
        fallback_readahead = min(int(total_size * self.READAHEAD_FALLBACK_FRACTION), READAHEAD_MAX_BYTES)
        fallback_recharge = min(int(total_size * self.READAHEAD_FALLBACK_FRACTION / 3), READAHEAD_MAX_BYTES)
        result = (fallback_readahead, fallback_recharge)
        video_path = self._get_video_path(handle)
        if video_path and os.path.exists(video_path):
            try:
                r = subprocess.run(
                    ['ffprobe', '-v', 'error',
                     '-show_entries', 'format=duration',
                     '-of', 'json',
                     video_path],
                    capture_output=True, text=True, timeout=10,
                )
                if r.returncode == 0:
                    data = json.loads(r.stdout)
                    duration = float(data.get('format', {}).get('duration', 0))
                    if duration > 0:
                        bitrate = total_size / duration
                        readahead = int(bitrate * READAHEAD_SECONDS)
                        recharge = int(bitrate * READAHEAD_RECHARGE_SECONDS)
                        result = (
                            min(readahead, READAHEAD_MAX_BYTES),
                            min(recharge, READAHEAD_MAX_BYTES),
                        )
            except (subprocess.TimeoutExpired, json.JSONDecodeError, ValueError, OSError):
                pass
        self._readahead_cache[info_hash] = result
        return result

    def resume(self, info_hash: str):
        handle = self.handles.get(info_hash)
        if not handle:
            return
        try:
            handle.resume()
        except RuntimeError:
            return

    def pause(self, info_hash: str):
        handle = self.handles.get(info_hash)
        if not handle:
            return
        try:
            handle.pause()
        except RuntimeError:
            return

    def remove(self, info_hash: str):
        handle = self.handles.pop(info_hash, None)
        if handle:
            self.ses.remove_torrent(handle, lt.options_t.delete_files)

    def stop_all(self):
        for h in list(self.handles.values()):
            self.ses.remove_torrent(h)
        self.handles.clear()

    def close(self):
        self.stop_all()

    def get_active_folder_names(self) -> set[str]:
        names: set[str] = set()
        for h in list(self.handles.values()):
            try:
                if h.status().has_metadata:
                    names.add(h.torrent_file().name().lower())
                    video_path = self._get_video_path(h)
                    if video_path:
                        names.add(os.path.basename(video_path).lower())
            except RuntimeError:
                continue
        return names

    def cleanup(self, max_size_bytes: int, max_age_secs: float, max_files: int = 0) -> list[str]:
        active = self.get_active_folder_names()
        candidates: list[tuple[str, float, int]] = []

        for entry in os.listdir(self.temp_dir):
            full_path = os.path.join(self.temp_dir, entry)
            if entry.lower() in active:
                continue
            try:
                mtime = os.path.getmtime(full_path)
                size = _get_dir_size(full_path) if os.path.isdir(full_path) else os.path.getsize(full_path)
            except OSError:
                continue
            candidates.append((full_path, mtime, size))

        removed: list[str] = []
        now = time.time()

        for path, mtime, size in candidates:
            if now - mtime > max_age_secs:
                _remove_path(path)
                removed.append(path)

        candidates = [(p, m, s) for p, m, s in candidates if os.path.exists(p)]
        candidates.sort(key=lambda x: x[1])
        total = sum(s for _, _, s in candidates)
        for path, mtime, size in candidates:
            if total <= max_size_bytes:
                break
            _remove_path(path)
            total -= size
            removed.append(path)

        if max_files > 0:
            candidates = [(p, m, s) for p, m, s in candidates if os.path.exists(p)]
            while len(candidates) > max_files:
                path, _mtime, _size = candidates.pop(0)
                _remove_path(path)
                removed.append(path)

        return removed

    def _get_video_path(self, handle: lt.torrent_handle) -> str | None:
        video = self._get_video_file(handle)
        if not video:
            return None
        _index, _size, path = video
        save_path = handle.status().save_path
        return os.path.join(save_path, path)

    def get_video_file_info(self, info_hash: str) -> dict | None:
        handle = self.handles.get(info_hash)
        if not handle:
            path = self.local_files.get(info_hash)
            if not path or not os.path.exists(path):
                return None
            return {'index': None, 'size': os.path.getsize(path), 'path': path, 'local_only': True}
        video = self._get_video_file(handle)
        if not video:
            path = self.local_files.get(info_hash)
            if path and os.path.exists(path):
                return {'index': None, 'size': os.path.getsize(path), 'path': path, 'local_only': True}
            return None
        index, size, path = video
        return {'index': index, 'size': size, 'path': path}

    def wait_for_local_file(self, info_hash: str, timeout: float = 30) -> str | None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            path = self.local_files.get(info_hash)
            if path and os.path.exists(path):
                return path
            time.sleep(0.3)
        return None

    def _get_video_file(self, handle: lt.torrent_handle) -> tuple[int, int, str] | None:
        if not handle.status().has_metadata:
            return None
        files = handle.torrent_file().files()
        video_exts = {'.mp4', '.mkv', '.avi', '.webm', '.m4v', '.mov', '.ts'}
        video_files = []
        for i in range(files.num_files()):
            path = files.file_path(i)
            ext = os.path.splitext(path)[1].lower()
            if ext in video_exts:
                video_files.append((i, files.file_size(i), path))
        if not video_files:
            return None
        best = max(video_files, key=lambda x: x[1])
        return best

    def _prioritize_streaming_file(self, handle: lt.torrent_handle):
        info = handle.torrent_file()
        num_pieces = info.num_pieces()
        priorities = [PIECE_PRIORITY_LOW] * num_pieces
        video = self._get_video_file(handle)
        if not video:
            handle.prioritize_pieces(priorities)
            return

        file_index, file_size, _path = video
        file_priorities = [0] * info.files().num_files()
        file_priorities[file_index] = PIECE_PRIORITY_LOW
        handle.prioritize_files(file_priorities)

        start_piece = info.map_file(file_index, 0, 1).piece
        end_piece = info.map_file(file_index, max(0, file_size - 1), 1).piece

        for i in range(start_piece, min(start_piece + HIGH_PRIORITY_COUNT, end_piece + 1)):
            priorities[i] = PIECE_PRIORITY_HIGH
        for i in range(max(start_piece, end_piece - HIGH_PRIORITY_COUNT + 1), end_piece + 1):
            priorities[i] = PIECE_PRIORITY_HIGH

        handle.prioritize_pieces(priorities)

    def _register_existing_file(self, info_hash: str, magnet_url: str):
        path = self._find_existing_video_file(magnet_url)
        if path:
            self.local_files[info_hash] = path

    def _find_existing_video_file(self, magnet_url: str) -> str | None:
        try:
            query = urllib.parse.urlsplit(magnet_url).query
            name = (urllib.parse.parse_qs(query).get('dn') or [''])[0]
        except Exception:
            name = ''
        if not name:
            return None

        wanted = self._normalize_name(name)
        best: tuple[int, str] | None = None
        video_exts = {'.mp4', '.mkv', '.avi', '.webm', '.m4v', '.mov', '.ts'}

        for entry in os.listdir(self.temp_dir):
            full_path = os.path.join(self.temp_dir, entry)
            entry_norm = self._normalize_name(entry)
            if wanted not in entry_norm and entry_norm not in wanted:
                continue

            if os.path.isdir(full_path):
                for dirpath, _dirnames, filenames in os.walk(full_path):
                    for fn in filenames:
                        if os.path.splitext(fn)[1].lower() not in video_exts:
                            continue
                        candidate = os.path.join(dirpath, fn)
                        try:
                            size = os.path.getsize(candidate)
                        except OSError:
                            continue
                        if best is None or size > best[0]:
                            best = (size, candidate)
            elif os.path.splitext(full_path)[1].lower() in video_exts:
                try:
                    size = os.path.getsize(full_path)
                except OSError:
                    continue
                if best is None or size > best[0]:
                    best = (size, full_path)

        return best[1] if best else None

    @staticmethod
    def _normalize_name(name: str) -> str:
        name = urllib.parse.unquote_plus(name).lower()
        name = re.sub(r'\[[^\]]*\]|\([^\)]*\)', ' ', name)
        name = re.sub(r'[^a-z0-9а-яё]+', ' ', name, flags=re.I)
        return re.sub(r'\s+', ' ', name).strip()
