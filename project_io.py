import contextlib
import json
import os
import tempfile
from pathlib import Path


@contextlib.contextmanager
def file_lock(path):
    lock_path = Path(str(path) + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "a+b") as lock_file:
        if os.name == "nt":
            import msvcrt

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


def atomic_write_text_unlocked(path, text, encoding="utf-8"):
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding=encoding, newline="") as tmp:
            tmp.write(text)
            tmp.flush()
            os.fsync(tmp.fileno())
        os.replace(tmp_name, target)
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
