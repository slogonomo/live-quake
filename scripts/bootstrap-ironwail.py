#!/usr/bin/python3
# SPDX-License-Identifier: 0BSD
"""Explicitly fetch pinned Ironwail sources and its pinned MIT ABI header only."""
import sys
sys.dont_write_bytecode = True
import resource
resource.setrlimit(resource.RLIMIT_CORE, (1, 1))
import argparse
import hashlib
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile
import time
import urllib.request

from bootstrap import closed_environment, private_dir

ENGINE_URL = 'https://github.com/andrei-drexler/ironwail.git'
ENGINE_COMMIT = '1eabd0df21130e55d94496c7af177e2dcbed75df'
HEADER_COMMIT = 'e57bb11597e8a00380f30f2627d219da960cf69a'
HEADER_URL = ('https://raw.githubusercontent.com/libretro/tyrquake/' + HEADER_COMMIT
              + '/libretro-common/include/libretro.h')
HEADER_SHA256 = '6513c2158a4e3bb8da9f96817ac3ff78b95be0b8ccb8a8560b0da226715ea214'
HEADER_BYTES = 294997
HEADER_SECONDS = 60
FETCH_SECONDS = 240
READ_SECONDS = 15
CHUNK_BYTES = 65536
CORE_LIMIT = ['/usr/bin/prlimit', '--core=1:1', '--']


def git_command(source=None):
    command = ['/usr/bin/git', '-c', 'core.hooksPath=/dev/null', '-c', 'core.fsmonitor=false']
    return command + ['--git-dir=' + str(source / '.git'), '--work-tree=' + str(source),
                      '-C', str(source)] if source else command


def source_directory(source):
    """Reject a redirected source checkout before consulting its Git metadata."""
    info = source.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
            or source.resolve() != source):
        raise ValueError('Source checkout must be an owned canonical directory')
    metadata = (source / '.git').lstat()
    if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.getuid():
        raise ValueError('Source Git metadata must be an owned directory')
    if os.path.lexists(source / '.git/commondir'):
        raise ValueError('Source Git metadata cannot redirect to a shared worktree')


def environment(work):
    result = closed_environment(work)
    result['GIT_OPTIONAL_LOCKS'] = '0'
    result['GIT_NO_LAZY_FETCH'] = '1'
    return result


def directory_fd(path):
    """Open every absolute parent component without following a symlink."""
    path = Path(path)
    if not path.is_absolute() or len(str(path)) > 4096:
        raise ValueError('An absolute bounded canonical path is required')
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    current = os.open('/', flags)
    try:
        for part in path.parts[1:]:
            if part in ('', '.', '..'):
                raise ValueError('Path traversal is not allowed')
            child = os.open(part, flags, dir_fd=current)
            os.close(current)
            current = child
        result, current = current, -1
        return result
    finally:
        if current >= 0:
            os.close(current)


def bounded_bytes(path, maximum, *, exact_size=None):
    path = Path(path)
    parent = directory_fd(path.parent)
    try:
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=parent)
    finally:
        os.close(parent)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1
                or info.st_size > maximum or exact_size is not None and info.st_size != exact_size):
            raise ValueError('Input must be an owned, bounded, unlinked regular file')
        chunks, position = [], 0
        while position < info.st_size:
            data = os.pread(fd, min(CHUNK_BYTES, info.st_size - position), position)
            if not data:
                raise ValueError('Input changed during its bounded read')
            chunks.append(data)
            position += len(data)
        after = os.fstat(fd)
        identity = lambda value: (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns)
        if identity(info) != identity(after):
            raise ValueError('Input changed during its bounded read')
        return b''.join(chunks)
    finally:
        os.close(fd)


def verified_header(work):
    private_dir(work / 'include')
    path = work / 'include/libretro.h'
    payload = bounded_bytes(path, HEADER_BYTES, exact_size=HEADER_BYTES)
    if hashlib.sha256(payload).hexdigest() != HEADER_SHA256:
        raise ValueError('ABI header differs from its pinned SHA-256')
    return path


def safe_git_metadata(source):
    """Require a self-contained checkout; reading objects must never fetch."""
    source_directory(source)
    metadata = source / '.git'
    for name in ('objects/info/alternates', 'objects/info/http-alternates'):
        if os.path.lexists(metadata / name):
            raise ValueError('Source objects cannot redirect to an alternate store')
    configuration = bounded_bytes(metadata / 'config', 256 * 1024)
    if (re.search(rb'(?im)^\s*(?:partialclone|promisor)\s*=', configuration)
            or re.search(rb'(?im)^\s*\[\s*include(?:if)?(?:\s|\])', configuration)):
        raise ValueError('Source Git configuration cannot include or lazily fetch objects')
    pack = metadata / 'objects/pack'
    descriptor = directory_fd(pack)
    try:
        with os.scandir(descriptor) as entries:
            if any(entry.name.endswith('.promisor') for entry in entries):
                raise ValueError('Source has partial-clone promisor objects')
    finally:
        os.close(descriptor)


def fetch_header(work, *, deadline=None):
    local_deadline = time.monotonic() + HEADER_SECONDS
    deadline = local_deadline if deadline is None else min(deadline, local_deadline)
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError('Header fetch exceeded its total deadline')
    include = work / 'include'
    if not os.path.lexists(include):
        include.mkdir(mode=0o700)
    private_dir(include)
    final = include / 'libretro.h'
    if os.path.lexists(final):
        return verified_header(work)
    fd, name = tempfile.mkstemp(dir=include, prefix='.libretro-download-')
    try:
        total, digest = 0, hashlib.sha256()
        request = urllib.request.Request(HEADER_URL, headers={'User-Agent': 'LiveQuake-pinned-source'})
        with os.fdopen(fd, 'wb') as output:
            with urllib.request.urlopen(request, timeout=min(READ_SECONDS, remaining)) as response:
                read_some = getattr(response, 'read1', None)
                if not callable(read_some):
                    raise ValueError('Header response requires bounded streaming')
                while True:
                    if time.monotonic() >= deadline:
                        raise TimeoutError('Header fetch exceeded its total deadline')
                    data = read_some(min(CHUNK_BYTES, HEADER_BYTES + 1 - total))
                    if time.monotonic() >= deadline:
                        raise TimeoutError('Header fetch exceeded its total deadline')
                    if not data:
                        break
                    total += len(data)
                    if total > HEADER_BYTES:
                        raise ValueError('Header exceeds its pinned byte count')
                    digest.update(data)
                    output.write(data)
            if total != HEADER_BYTES or digest.hexdigest() != HEADER_SHA256:
                raise ValueError('Header failed pinned length or SHA-256 verification')
            output.flush()
            os.fsync(output.fileno())
        os.link(name, final, follow_symlinks=False)
        Path(name).unlink()
        directory = directory_fd(include)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        return verified_header(work)
    finally:
        Path(name).unlink(missing_ok=True)


def fetch_sources(work):
    work = private_dir(work)
    source = work / 'ironwail'
    deadline = time.monotonic() + FETCH_SECONDS
    def git(arguments, *, source=None, limit=30):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError('Source fetch exceeded its total deadline')
        return subprocess.run(CORE_LIMIT + git_command(source) + arguments,
                              env=environment(work), check=True, timeout=min(limit, remaining),
                              capture_output=True, text=True)
    if not os.path.lexists(source):
        git(['init', '--template=', str(source)], limit=15)
        safe_git_metadata(source)
        git(['fetch', '--no-tags', '--depth=1', ENGINE_URL, ENGINE_COMMIT], source=source, limit=180)
        git(['checkout', '--detach', ENGINE_COMMIT], source=source)
    safe_git_metadata(source)
    revision = git(['rev-parse', 'HEAD'], source=source, limit=10).stdout.strip()
    if revision != ENGINE_COMMIT:
        raise ValueError('Ironwail revision differs; preserve it and use a fresh private directory')
    header = fetch_header(work, deadline=deadline)
    return source, header


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--work-dir', required=True, type=private_dir)
    parser.add_argument('--fetch-sources', action='store_true')
    args = parser.parse_args(argv)
    os.umask(0o077)
    if args.fetch_sources:
        fetch_sources(args.work_dir)
        print('Pinned Ironwail source and MIT ABI header verified.')


if __name__ == '__main__':
    main()
