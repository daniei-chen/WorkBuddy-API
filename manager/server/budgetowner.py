"""Lifetime OS locks fence local SQLite workers. Never reclaim a live owner.

Linux production uses flock, including after SIGKILL. Remote/shared filesystem
deployments are unsupported and must keep a single worker. Unknown old owners
are frozen instead of treating lease age as proof that upstream spending stopped.
"""
import os
import socket
import uuid
import re
from pathlib import Path
from . import config

_owner = None
_pid = None
_file = None
_database = None

def lock(handle):
    if os.name == 'nt':
        import msvcrt
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

def directory():
    target = Path(config.DB_PATH).parent / '.budget-owners'
    target.mkdir(mode=0o700, exist_ok=True)
    if target.is_symlink():
        raise RuntimeError('unsafe owner directory')
    return target

def identity():
    global _owner, _pid, _file, _database
    if _pid != os.getpid() or _owner is None or _database != str(config.DB_PATH):
        # Forked workers must not reuse their parent's owner identity.
        if _file is not None:
            _file.close()
        _owner = socket.gethostname() + ':' + uuid.uuid4().hex
        _pid = os.getpid()
        _database = str(config.DB_PATH)
        path = directory() / _owner.replace(':', '_')
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
        _file = os.fdopen(fd, 'r+b')
        _file.write(b'1'); _file.flush()
        lock(_file)
    return _owner

def alive(owner):
    if not owner or owner.split(':', 1)[0] != socket.gethostname():
        return None
    if not re.fullmatch(r'[A-Za-z0-9_.-]+:[0-9a-f]{32}', owner):
        return None
    path = directory() / owner.replace(':', '_')
    try:
        fd = os.open(path, os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0))
        with os.fdopen(fd, 'r+b') as handle:
            try:
                lock(handle)
            except (BlockingIOError, OSError):
                return True
            return False
    except FileNotFoundError:
        return None

def close():
    global _owner, _pid, _file, _database
    if _file is not None:
        _file.close()
    _owner = _pid = _file = _database = None
