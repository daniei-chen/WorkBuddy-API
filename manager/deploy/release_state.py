"""Switch one release pointer, and restore it if readiness fails.

The caller owns service stop/start and validates signed content before entry.
This module never restores databases, account tokens or private configuration.
"""
import os
from pathlib import Path

def point(pointer, target):
    pointer=Path(pointer)
    staged=pointer.with_name(pointer.name+'.next')
    if staged.exists() or staged.is_symlink():
        raise RuntimeError('unfinished pointer transaction retained')
    staged.symlink_to(target, target_is_directory=True)
    os.replace(staged,pointer)
    fd=os.open(pointer.parent,os.O_RDONLY)
    try:os.fsync(fd)
    finally:os.close(fd)

def activate(pointer,target,stop,start,healthy):
    pointer=Path(pointer)
    if not pointer.is_symlink():raise RuntimeError('current release pointer missing')
    previous=os.readlink(pointer)
    stop()
    try:
        point(pointer,target)
        start()
        if not healthy():raise RuntimeError('candidate readiness failed')
    except BaseException:
        stop()
        point(pointer,previous)
        start()
        if not healthy():raise RuntimeError('rollback readiness failed; operator action required')
        raise
    return previous
