# SPDX-License-Identifier: 0BSD
"""Bounded private runtime locking without controller or desktop imports."""
from __future__ import annotations

import fcntl
import os
from pathlib import Path
import stat


def absolute_path(value, label):
    raw = os.fspath(value)
    if (not isinstance(raw, str) or not raw.startswith('/') or raw.startswith('//')
            or any(ord(c) < 32 or ord(c) == 127 for c in raw)
            or any(part in ('', '.', '..') for part in raw.split('/')[1:])):
        raise ValueError(label + ' must be an absolute canonical path')
    path = Path(raw)
    if path.resolve() != path:
        raise ValueError(label + ' cannot contain symbolic links')
    return path



class RuntimeLock:
    """Single controller per private runtime, before sockets or observers start."""
    def __init__(self, runtime, *, create=True, record_pid=True, shared=False):
        if any(type(value) is not bool for value in (create, record_pid, shared)):
            raise ValueError('runtime lock options must be explicit booleans')
        if shared and (create or record_pid):
            raise ValueError('shared runtime probes cannot create or record a PID')
        self.runtime = absolute_path(runtime, 'runtime directory')
        self.path = self.runtime / 'daemon.lock'
        self.create, self.record_pid, self.shared = create, record_pid, shared
        self.fd = None
        self.parent = None

    def __enter__(self):
        # Walk every component without following aliases. Cleanup callers open
        # only an existing lock, without rewriting the retired owner's PID.
        parent = os.open('/', os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY)
        try:
            for component in self.runtime.parts[1:]:
                child = os.open(component, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW
                                | os.O_NONBLOCK | os.O_DIRECTORY, dir_fd=parent)
                os.close(parent)
                parent = child
            info = os.fstat(parent)
            if (info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700):
                raise ValueError('unsafe runtime directory')
            flags = (os.O_RDONLY if self.shared else os.O_RDWR) | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK
            fd = os.open('daemon.lock', flags | (os.O_CREAT if self.create else 0), 0o600, dir_fd=parent)
        except BaseException:
            os.close(parent)
            raise
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1
                    or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size > 4096):
                raise ValueError('unsafe daemon lock')
            try:
                fcntl.flock(fd, (fcntl.LOCK_SH if self.shared else fcntl.LOCK_EX) | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise RuntimeError('live quake controller is already running') from error
            self.fd, self.parent = fd, parent
            self.verify()
            if self.record_pid:
                os.ftruncate(fd, 0)
                os.write(fd, (str(os.getpid()) + '\n').encode())
            return self
        except BaseException:
            self.fd = self.parent = None
            os.close(fd)
            os.close(parent)
            raise

    def verify(self):
        """The held lock and its private directory still have their names."""
        if self.fd is None or self.parent is None:
            raise ValueError('runtime lock is not held')
        current = os.stat('daemon.lock', dir_fd=self.parent, follow_symlinks=False)
        held = os.fstat(self.fd)
        directory = os.stat(self.runtime, follow_symlinks=False)
        opened = os.fstat(self.parent)
        if ((current.st_dev, current.st_ino) != (held.st_dev, held.st_ino)
                or not stat.S_ISREG(current.st_mode) or current.st_nlink != 1
                or current.st_uid != os.getuid() or stat.S_IMODE(current.st_mode) != 0o600
                or (directory.st_dev, directory.st_ino) != (opened.st_dev, opened.st_ino)
                or not stat.S_ISDIR(directory.st_mode) or directory.st_uid != os.getuid()
                or stat.S_IMODE(directory.st_mode) != 0o700):
            raise ValueError('runtime lock changed')

    def __exit__(self, *unused):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
        if self.parent is not None:
            os.close(self.parent)
            self.parent = None
