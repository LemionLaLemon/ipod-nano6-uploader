"""Device write coordination, SQLite transactions, and reversible file changes."""
from contextlib import contextmanager
from functools import wraps
from pathlib import Path
import fcntl
import hashlib
import os
import sqlite3
import tempfile
import threading

_local = threading.local()


def device_write(function):
    @wraps(function)
    def wrapped(mount, *args, **kwargs):
        key = str(Path(mount).resolve())
        held = getattr(_local, 'held', set())
        if key in held:
            return function(mount, *args, **kwargs)
        lockdir = Path.home() / '.cache/nano6g-uploader/locks'
        lockdir.mkdir(parents=True, exist_ok=True)
        path = lockdir / (hashlib.sha256(key.encode()).hexdigest() + '.lock')
        with path.open('a') as stream:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RuntimeError('Another transfer is writing to this iPod. Wait for it to finish.') from None
            _local.held = held | {key}
            try:
                return function(mount, *args, **kwargs)
            finally:
                _local.held = held
    return wrapped


@contextmanager
def database(mount, attached=()):
    from ipod_lib import itlp
    directory = Path(itlp(mount))
    paths = {'main': directory / 'Library.itdb', **{name.lower(): directory / (name + '.itdb') for name in attached}}
    for path in paths.values():
        if not path.is_file(): raise RuntimeError(f'Missing iPod database: {path.name}')
    con = sqlite3.connect(paths['main'])
    try:
        for name, path in paths.items():
            if name != 'main': con.execute(f'ATTACH DATABASE ? AS {name}', (str(path),))
            mode = con.execute(f'PRAGMA {name}.journal_mode').fetchone()[0]
            if mode in ('wal', 'off', 'memory'):
                raise RuntimeError(f'Unsupported {name} journal mode {mode}; no changes made.')
            con.execute(f'PRAGMA {name}.synchronous=FULL')
        with con:
            con.execute('BEGIN IMMEDIATE')
            yield con
    finally:
        con.close()


def atomic_write(path, data):
    path = Path(path)
    fd, temporary = tempfile.mkstemp(prefix='.nano-write-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data); stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary): os.remove(temporary)


class FileChanges:
    """Restore published files when the surrounding SQLite transaction fails."""
    def __init__(self):
        self.originals = {}

    def __enter__(self):
        return self

    def write(self, path, data):
        path = Path(path)
        if path not in self.originals:
            self.originals[path] = path.read_bytes() if path.exists() else None
        atomic_write(path, data)

    def __exit__(self, kind, error, traceback):
        if kind is not None:
            for path, data in reversed(list(self.originals.items())):
                if data is None:
                    path.unlink(missing_ok=True)
                elif not path.exists() or path.read_bytes() != data:
                    atomic_write(path, data)


def music_path(mount, base, relative):
    root = Path(mount).resolve()
    path = root / str(base).lstrip('/') / str(relative)
    path = path.resolve()
    music = root / 'iPod_Control/Music'
    if not path.is_relative_to(music) or path == music:
        raise ValueError('Audio location is outside this iPod music folder.')
    return path
