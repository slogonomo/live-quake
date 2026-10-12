#!/usr/bin/env python3
# SPDX-License-Identifier: 0BSD
"""Shared safe build outputs; the only engine builder is Ironwail."""
import os
from pathlib import Path
import stat
import tempfile

def replace_bytes(path, payload):
    fd, name = tempfile.mkstemp(dir=path.parent, prefix='.' + path.name + '-')
    try:
        with os.fdopen(fd, 'wb') as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def private_log(path):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
        os.close(fd)
        raise ValueError('Build log must be an owned regular file')
    os.ftruncate(fd, 0)
    return os.fdopen(fd, 'w')
