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
READY_START_PIECES = 4
READY_MIN_PROGRESS = 0.02
READY_SPEED_FACTOR = 1.3
READY_MIN_BUFFER_SECONDS = 90
READY_MIN_BUFFER_BYTES = 32 * 1024 * 1024
READY_MAX_BUFFER_BYTES = 256 * 1024 * 1024
READY_SPEED_FALLBACK_BYTES = 2 * 1024 * 1024
MIN_PLAUSIBLE_BITRATE = 128 * 1024


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
            'alert_mask': lt.alert_category.error,
        }
        self.ses.apply_settings(settings)
        self.handles: dict[str, lt.torrent_handle] = {}
        self.local_files: dict[str, str] = {}
        self._readahead_cache: dict[str, tuple[int, int]] = {}
        self._bitrate_cache: dict[str, float] = {}

    def add_magnet(self, magnet_url: str, timeout: float = 30) -> str:
        match = re.search(r'btih:([A-Fa-f0-9]{40})', magnet_url)
        if match:
            info_hash = match.group(1).lower()
            self._register_existing_file(info_hash, magnet_url)
            existing = self.handles.get(info_hash)
            if existing:
                try:
                    existing.status()
                    if self._video_file_missing(existing):
                        self.ses.remove_torrent(existing)
                        self.handles.pop(info_hash, None)
                    else:
                        self._start_handle(existing)
                        return info_hash
                except RuntimeError:
                    pass

        params = self._magnet_params(magnet_url)
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
        self._start_handle(handle)

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
                if self._video_file_missing(existing):
                    self.ses.remove_torrent(existing)
                    self.handles.pop(info_hash, None)
                else:
                    self._start_handle(existing)
                    return info_hash
            except RuntimeError:
                self.handles.pop(info_hash, None)

        params = self._magnet_params(magnet_url)
        handle = self.ses.add_torrent(params)
        self.handles[info_hash] = handle

        def _wait_metadata():
            deadline = time.monotonic() + 30
            try:
                while not handle.status().has_metadata:
                    if self.handles.get(info_hash) is not handle:
                        return
                    if time.monotonic() > deadline:
                        self.handles.pop(info_hash, None)
                        self.ses.remove_torrent(handle)
                        return
                    time.sleep(0.1)
                if self.handles.get(info_hash) is not handle:
                    return
                self._prioritize_streaming_file(handle)
                self._start_handle(handle)
            except RuntimeError:
                if self.handles.get(info_hash) is handle:
                    self.handles.pop(info_hash, None)
                return

        t = threading.Thread(target=_wait_metadata, daemon=True)
        t.start()
        return info_hash

    def _magnet_params(self, magnet_url: str):
        params = lt.parse_magnet_uri(magnet_url)
        params.save_path = self.temp_dir
        params.storage_mode = lt.storage_mode_t.storage_mode_sparse
        params.flags &= ~lt.torrent_flags.paused
        params.flags &= ~lt.torrent_flags.auto_managed
        params.flags &= ~lt.torrent_flags.stop_when_ready
        params.flags |= lt.torrent_flags.sequential_download
        return params

    @staticmethod
    def _start_handle(handle: lt.torrent_handle):
        try:
            handle.auto_managed(False)
            handle.resume()
        except RuntimeError:
            pass

    def _video_file_missing(self, handle: lt.torrent_handle) -> bool:
        try:
            if not handle.status().has_metadata:
                return False
            video_path = self._get_video_path(handle)
            return bool(video_path and not os.path.exists(video_path))
        except RuntimeError:
            return False

    def get_status(self, info_hash: str) -> dict | None:
        handle = self.handles.get(info_hash)
        if not handle:
            return None

        self.ensure_file_integrity(info_hash)

        s = handle.status()
        if not s.has_metadata:
            return {
                'progress': 0,
                'downloaded': 0,
                'total': 1,
                'download_rate': s.download_rate,
                'upload_rate': s.upload_rate,
                'num_peers': s.num_peers,
                'num_seeds': s.num_seeds,
                'state': 'pending',
                'ready': False,
                'video_path': None,
                'file_size': 0,
                'name': 'Получение метаданных...',
                'format': None,
                'transcode': False,
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
            if s.total_wanted_done > 0 and not os.path.exists(video_path):
                return {
                    'progress': 0,
                    'downloaded': 0,
                    'total': total if total > 0 else 1,
                    'download_rate': 0,
                    'upload_rate': s.upload_rate,
                    'num_peers': s.num_peers,
                    'num_seeds': s.num_seeds,
                    'state': 'file_missing',
                    'ready': False,
                    'video_path': video_path,
                    'file_size': 0,
                    'name': handle.torrent_file().name(),
                    'format': video_format,
                    'transcode': video_format not in ('mp4', 'mkv') if video_format else False,
                }

        streamable = video_format in ('mkv', 'mp4')
        num_pieces = handle.torrent_file().num_pieces()
        video = self._get_video_file(handle)
        buffered_bytes = 0
        min_buffer_bytes = 0
        speed_ok = False
        estimated_bitrate = 0
        if video:
            vindex, vsize, _ = video
            tm = handle.torrent_file()
            vstart = tm.map_file(vindex, 0, 1).piece
            vend = tm.map_file(vindex, max(0, vsize - 1), 1).piece
            check_count = min(HIGH_PRIORITY_COUNT, vend - vstart + 1)
            first_done = all(handle.have_piece(i) for i in range(vstart, vstart + check_count))
            last_done = all(handle.have_piece(i) for i in range(vend - check_count + 1, vend + 1))
            buffered_bytes = self._contiguous_done_bytes(handle, vstart, vend, vsize)
            estimated_bitrate = self._estimate_bitrate(handle, total, video_path)
            start_buffer_bytes = self._piece_buffer_bytes(tm, READY_START_PIECES, vsize)
            min_buffer_bytes = self._ready_buffer_bytes(estimated_bitrate, total, start_buffer_bytes)
            speed_ok = self._ready_speed_ok(s.download_rate, estimated_bitrate, progress)
        else:
            check_count = min(HIGH_PRIORITY_COUNT, num_pieces)
            first_done = all(handle.have_piece(i) for i in range(check_count))
            last_done = all(handle.have_piece(i) for i in range(num_pieces - check_count, num_pieces))
            buffered_bytes = self._contiguous_done_bytes(handle, 0, max(0, num_pieces - 1), total)
            start_buffer_bytes = self._piece_buffer_bytes(handle.torrent_file(), READY_START_PIECES, total)
            min_buffer_bytes = self._ready_buffer_bytes(0, total, start_buffer_bytes)
            speed_ok = self._ready_speed_ok(s.download_rate, 0, progress)
        enough_overall = progress >= READY_MIN_PROGRESS
        startup_buffer_ready = buffered_bytes >= start_buffer_bytes
        enough_buffer = buffered_bytes >= min_buffer_bytes
        ready = (
            progress >= 1.0
            if not streamable
            else (first_done and startup_buffer_ready and (speed_ok or enough_buffer) and (last_done or enough_overall))
        )
        state = 'downloading' if s.download_rate > 0 else str(s.state)

        return {
            'progress': round(progress, 4),
            'downloaded': downloaded,
            'total': total,
            'download_rate': s.download_rate,
            'upload_rate': s.upload_rate,
            'num_peers': s.num_peers,
            'num_seeds': s.num_seeds,
            'state': state,
            'ready': ready,
            'video_path': video_path,
            'file_size': file_size,
            'name': handle.torrent_file().name(),
            'format': video_format,
            'transcode': video_format not in ('mp4', 'mkv') if video_format else False,
            'buffered_bytes': buffered_bytes,
            'start_buffer_bytes': start_buffer_bytes,
            'min_buffer_bytes': min_buffer_bytes,
            'estimated_bitrate': int(estimated_bitrate),
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

    def ensure_file_integrity(self, info_hash: str):
        handle = self.handles.get(info_hash)
        if not handle:
            return
        try:
            s = handle.status()
            if not s.has_metadata or s.total_wanted_done <= 0:
                return
            if s.checking_files or s.queued_for_checking:
                return
            video_path = self._get_video_path(handle)
            if not video_path:
                return
            if not os.path.exists(video_path):
                handle.force_recheck()
                return
            if os.path.getsize(video_path) == 0:
                handle.force_recheck()
                return
        except RuntimeError:
            pass

    def _contiguous_done_bytes(self, handle: lt.torrent_handle, start_piece: int, end_piece: int, size: int) -> int:
        try:
            tm = handle.torrent_file()
            piece_len = tm.piece_length()
            done_pieces = 0
            for piece in range(start_piece, end_piece + 1):
                if not handle.have_piece(piece):
                    break
                done_pieces += 1
            if done_pieces <= 0:
                return 0
            return min(done_pieces * piece_len, size)
        except RuntimeError:
            return 0

    def _estimate_bitrate(self, handle: lt.torrent_handle, total_size: int, video_path: str | None) -> float:
        try:
            info_hash = str(handle.torrent_file().info_hash())
        except RuntimeError:
            return 0
        cached = self._bitrate_cache.get(info_hash)
        if cached is not None:
            return cached
        bitrate = 0.0
        if video_path and os.path.exists(video_path):
            try:
                r = subprocess.run(
                    ['ffprobe', '-v', 'error',
                     '-show_entries', 'format=duration',
                     '-of', 'json',
                     video_path],
                    capture_output=True, text=True, timeout=5,
                )
                if r.returncode == 0:
                    data = json.loads(r.stdout)
                    duration = float(data.get('format', {}).get('duration', 0))
                    if duration > 0:
                        bitrate = total_size / duration
            except (subprocess.TimeoutExpired, json.JSONDecodeError, ValueError, OSError):
                pass
        if 0 < bitrate < MIN_PLAUSIBLE_BITRATE:
            bitrate = 0.0
        if bitrate > 0:
            self._bitrate_cache[info_hash] = bitrate
        return bitrate

    @staticmethod
    def _ready_buffer_bytes(estimated_bitrate: float, total_size: int, start_buffer_bytes: int) -> int:
        if estimated_bitrate > 0:
            wanted = int(estimated_bitrate * READY_MIN_BUFFER_SECONDS)
        else:
            wanted = max(READY_MIN_BUFFER_BYTES, int(total_size * READY_MIN_PROGRESS))
        return min(max(wanted, READY_MIN_BUFFER_BYTES, start_buffer_bytes), READY_MAX_BUFFER_BYTES)

    @staticmethod
    def _ready_speed_ok(download_rate: int, estimated_bitrate: float, progress: float) -> bool:
        if progress >= 0.15:
            return True
        if estimated_bitrate > 0:
            return download_rate >= estimated_bitrate * READY_SPEED_FACTOR
        return download_rate >= READY_SPEED_FALLBACK_BYTES

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

            priorities = [PIECE_PRIORITY_LOW] * num_pieces

            video = self._get_video_file(handle)
            if video:
                vindex, vsize, _ = video
                tm = handle.torrent_file()
                vstart = tm.map_file(vindex, 0, 1).piece
                vend = tm.map_file(vindex, max(0, vsize - 1), 1).piece
                startup_pieces = self._startup_buffer_pieces(tm, vsize)
                startup_end = min(vstart + startup_pieces - 1, vend)
                for i in range(vstart, startup_end + 1):
                    priorities[i] = PIECE_PRIORITY_HIGH
                for i in range(max(vstart, vend - HIGH_PRIORITY_COUNT + 1), vend + 1):
                    priorities[i] = PIECE_PRIORITY_HIGH
                self._set_piece_deadlines(handle, vstart, startup_end)

            for i in range(block_start, horizon + 1):
                priorities[i] = PIECE_PRIORITY_HIGH
            handle.prioritize_pieces(priorities)
            self._set_piece_deadlines(handle, block_start, horizon)
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

    def stop_download(self, info_hash: str):
        handle = self.handles.pop(info_hash, None)
        if not handle:
            return
        try:
            self.ses.remove_torrent(handle)
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
        priorities = [PIECE_PRIORITY_NONE] * num_pieces
        video = self._get_video_file(handle)
        if not video:
            handle.prioritize_pieces(priorities)
            return

        file_index, file_size, _path = video

        start_piece = info.map_file(file_index, 0, 1).piece
        end_piece = info.map_file(file_index, max(0, file_size - 1), 1).piece

        for i in range(start_piece, end_piece + 1):
            priorities[i] = PIECE_PRIORITY_LOW
        startup_pieces = self._startup_buffer_pieces(info, file_size)
        startup_end = min(start_piece + startup_pieces - 1, end_piece)
        for i in range(start_piece, startup_end + 1):
            priorities[i] = PIECE_PRIORITY_HIGH
        for i in range(max(start_piece, end_piece - HIGH_PRIORITY_COUNT + 1), end_piece + 1):
            priorities[i] = PIECE_PRIORITY_HIGH

        handle.prioritize_pieces(priorities)
        self._set_piece_deadlines(handle, start_piece, startup_end)

    @staticmethod
    def _startup_buffer_pieces(info: lt.torrent_info, file_size: int) -> int:
        piece_len = max(info.piece_length(), 1)
        wanted = min(max(READY_MIN_BUFFER_BYTES, int(file_size * READY_MIN_PROGRESS)), READY_MAX_BUFFER_BYTES)
        return max(READY_START_PIECES, int((wanted + piece_len - 1) / piece_len))

    @staticmethod
    def _piece_buffer_bytes(info: lt.torrent_info, piece_count: int, file_size: int) -> int:
        return min(max(piece_count, 1) * max(info.piece_length(), 1), file_size)

    @staticmethod
    def _set_piece_deadlines(handle: lt.torrent_handle, start_piece: int, end_piece: int):
        try:
            for offset, piece in enumerate(range(start_piece, end_piece + 1)):
                handle.set_piece_deadline(piece, offset * 150)
        except RuntimeError:
            pass

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
