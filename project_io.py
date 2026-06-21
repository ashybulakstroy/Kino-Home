import contextlib
import hashlib
import json
import os
import tempfile
import threading
import time
from pathlib import Path


_IN_PROCESS_LOCKS: dict[str, threading.RLock] = {}
_IN_PROCESS_LOCKS_GUARD = threading.Lock()


def _lock_key(path):
    return str(Path(path).resolve())


def _get_in_process_lock(path):
    key = _lock_key(path)
    with _IN_PROCESS_LOCKS_GUARD:
        lock = _IN_PROCESS_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _IN_PROCESS_LOCKS[key] = lock
        return lock


def _fallback_lock_path(path):
    base_dir = os.environ.get("LOCAL_KINO_LOCK_DIR")
    if base_dir:
        root = Path(base_dir)
    else:
        root = Path(tempfile.gettempdir()) / "local_kino_file_locks"
    digest = hashlib.sha1(str(Path(path).resolve()).encode("utf-8")).hexdigest()
    name = f"{Path(path).name}.{digest}.lock"
    return root / name


@contextlib.contextmanager
def file_lock(path):
    primary_lock_path = Path(str(path) + ".lock")
    lock_path = primary_lock_path
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        open(lock_path, "a+b").close()
    except PermissionError:
        lock_path = _fallback_lock_path(path)
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        open(lock_path, "a+b").close()
    in_process_lock = _get_in_process_lock(lock_path)
    with in_process_lock:
        with open(lock_path, "a+b") as lock_file:
            if os.name == "nt":
                import msvcrt

                lock_file.seek(0)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_LOCK, 1)
                try:
                    yield
                finally:
                    lock_file.seek(0)
                    msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _replace_with_retry(src, dst, attempts=5):
    for attempt in range(attempts):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if os.name != "nt" or attempt == attempts - 1:
                raise
            time.sleep(0.05 * (attempt + 1))


def atomic_write_text_unlocked(path, text, encoding="utf-8"):
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent)
        )
    except PermissionError:
        fd, tmp_name = tempfile.mkstemp(
            prefix=f"{target.name}.", suffix=".tmp", dir=tempfile.gettempdir()
        )
    try:
        with os.fdopen(fd, "w", encoding=encoding, newline="") as tmp:
            tmp.write(text)
            tmp.flush()
            os.fsync(tmp.fileno())
        _replace_with_retry(tmp_name, target)
    finally:
        if os.path.exists(tmp_name):
            os.remove(tmp_name)


def atomic_write_text(path, text, encoding="utf-8"):
    with file_lock(path):
        atomic_write_text_unlocked(path, text, encoding=encoding)


def atomic_write_json(path, data):
    text = json.dumps(data, ensure_ascii=False, indent=2)
    atomic_write_text(path, text, encoding="utf-8")


def atomic_write_json_unlocked(path, data):
    text = json.dumps(data, ensure_ascii=False, indent=2)
    atomic_write_text_unlocked(path, text, encoding="utf-8")
