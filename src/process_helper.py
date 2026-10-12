# SPDX-License-Identifier: 0BSD
"""Internal lifetime guard for one transient, explicitly supplied command.

Runs as its own process, not in the threaded controller. Linux subreaping
keeps double-forked/setsid descendants inside this ancestry root. Only those
children are inspected or signalled; no ambient process inventory is read.
"""
from __future__ import annotations

import ctypes
import math
import os
import resource
import signal
import subprocess
import sys
import time

CLEANUP_BUDGET = .3
_stopped = False


def _stop(_number, _frame):
    global _stopped
    _stopped = True


def _identity(pid):
    try:
        with open(f'/proc/{pid}/stat', 'rb') as stream:
            raw = stream.read(4097)
        if len(raw) > 4096:
            return None
        fields = raw.rsplit(b') ', 1)[1].split()
        return pid, int(fields[19]), int(fields[1])
    except (OSError, ValueError, IndexError):
        return None


def _descendants():
    root = _identity(os.getpid())
    if root is None:
        raise RuntimeError('Transient process identity unavailable')
    parents, found = [root], {}
    while parents:
        parent = parents.pop()
        current = _identity(parent[0])
        if current is None or current[:2] != parent[:2]:
            continue
        try:
            with open(f'/proc/{parent[0]}/task/{parent[0]}/children', 'rb') as stream:
                raw = stream.read(65537)
            if len(raw) > 65536:
                raise RuntimeError('Transient process child bound exceeded')
            children = [int(value) for value in raw.split()]
        except (FileNotFoundError, ProcessLookupError):
            continue
        for pid in children:
            child = _identity(pid)
            if child is not None and child[2] == parent[0] and pid not in found:
                found[pid] = child
                parents.append(child)
    return tuple(found.values())


def _kill(identity):
    descriptor = None
    try:
        descriptor = os.pidfd_open(identity[0])
        # Opening a pidfd first pins the target; the start-time check rejects
        # a PID reused before the open. Reparenting within our root is safe.
        current = _identity(identity[0])
        if current is not None and current[:2] == identity[:2]:
            signal.pidfd_send_signal(descriptor, signal.SIGKILL)
    except ProcessLookupError:
        pass
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _reap():
    while True:
        try:
            pid, _ = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return
        if pid == 0:
            return


def _cleanup():
    deadline = time.monotonic() + CLEANUP_BUDGET
    while True:
        # A killed intermediate parent reparents its survivors to this
        # subreaper, so repeat until no descendants remain, even after setsid.
        for identity in _descendants():
            _kill(identity)
        _reap()
        if not _descendants():
            return True
        if time.monotonic() >= deadline:
            return False  # A kernel task that cannot be killed must not hang us.
        time.sleep(.005)


def _run(parent, deadline, command):
    global _stopped
    resource.setrlimit(resource.RLIMIT_CORE, (1, 1))
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(36, 1, 0, 0, 0) != 0:  # PR_SET_CHILD_SUBREAPER
        raise RuntimeError('Cannot guard transient descendants')
    if libc.prctl(1, signal.SIGTERM, 0, 0, 0) != 0:  # PR_SET_PDEATHSIG
        raise RuntimeError('Cannot guard transient parent lifetime')
    if os.getppid() != parent:
        _stopped = True
    child = None
    status = 124
    try:
        if not _stopped and time.monotonic() < deadline:
            child = subprocess.Popen(command, close_fds=True)
            while not _stopped and time.monotonic() < deadline:
                exited = os.waitid(os.P_PID, child.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
                if exited is not None:
                    status = (exited.si_status if exited.si_code == os.CLD_EXITED
                              else 128 + exited.si_status)
                    break
                time.sleep(.005)
    finally:
        clean = _cleanup()
        # _cleanup reaps the Popen child along with adopted descendants.
        if child is not None:
            child.returncode = status
    return status if clean else 125


def main(arguments):
    if len(arguments) < 6 or arguments[0] != '--parent' or arguments[2] != '--deadline' or arguments[4] != '--':
        raise RuntimeError('Invalid transient process invocation')
    parent, deadline, command = int(arguments[1]), float(arguments[3]), arguments[5:]
    if parent <= 0 or not math.isfinite(deadline) or not command or not os.path.isabs(command[0]):
        raise RuntimeError('Invalid transient process invocation')
    return _run(parent, deadline, command)


if __name__ == '__main__':
    try:
        sys.exit(main(sys.argv[1:]))
    except Exception:
        # Errors are intentionally generic: command output can be private.
        sys.exit(125)
