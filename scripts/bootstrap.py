#!/usr/bin/env python3
# SPDX-License-Identifier: 0BSD
"""Explicit, pinned free-data fetch into a private development tree."""
import sys
sys.dont_write_bytecode = True
import argparse
import functools
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import stat
import tempfile
import time
import urllib.request
import zipfile

DATA_URL = 'https://github.com/lavenderdotpet/LibreQuake/releases/download/v0.09-beta/full.zip'
DATA_SHA256 = '623e463b35811216244f9ba15e0c45abc1765288650b5e3d36a899b35e4bcf3d'
DATA_BYTES = 115678161
DOWNLOAD_SECONDS = 240
READ_SECONDS = 15
CHUNK_BYTES = 65536
MAX_MEMBERS = 20000
MAX_EXTRACTED_BYTES = 512 * 1024 * 1024


def closed_environment(work):
    """Only subprocesses see the private HOME; the parent environment is intact."""
    return {'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8', 'HOME': str(work),
            'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': '/dev/null',
            'GIT_TERMINAL_PROMPT': '0',
            'PYTHONDONTWRITEBYTECODE': '1'}


def verified_archive(path):
    """Return an open verified archive, checking bounds before reading payload."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    archive = os.fdopen(fd, 'rb')
    try:
        info = os.fstat(archive.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_size != DATA_BYTES):
            raise ValueError('Cached archive must be an owned regular file with the exact pinned size')
        digest, total = hashlib.sha256(), 0
        while total <= DATA_BYTES:
            chunk = archive.read(min(CHUNK_BYTES, DATA_BYTES + 1 - total))
            if not chunk:
                break
            digest.update(chunk)
            total += len(chunk)
        if total != DATA_BYTES or digest.hexdigest() != DATA_SHA256:
            raise ValueError('Cached LibreQuake archive failed pinned length or SHA-256 verification')
        after = os.fstat(archive.fileno())
        if (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns) != (
                after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
            raise ValueError('Cached archive changed during verification')
        archive.seek(0)
        return archive
    except BaseException:
        archive.close()
        raise


def download_archive(work):
    """Stream a pinned download; never install an unverified or partial cache."""
    archive = work / 'librequake-full.zip'
    fd, temporary = tempfile.mkstemp(dir=work, prefix='.librequake-download-')
    try:
        deadline = time.monotonic() + DOWNLOAD_SECONDS
        digest, total = hashlib.sha256(), 0
        request = urllib.request.Request(DATA_URL, headers={'User-Agent': 'LiveQuake-prototype'})
        with os.fdopen(fd, 'wb') as output:
            with urllib.request.urlopen(request, timeout=min(READ_SECONDS, DOWNLOAD_SECONDS)) as response:
                read_some = getattr(response, 'read1', None)
                if not callable(read_some):
                    raise ValueError('Download response does not support bounded streaming')
                while True:
                    if time.monotonic() >= deadline:
                        raise TimeoutError('LibreQuake download exceeded its total deadline')
                    chunk = read_some(min(CHUNK_BYTES, DATA_BYTES + 1 - total))
                    if time.monotonic() >= deadline:
                        raise TimeoutError('LibreQuake download exceeded its total deadline')
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > DATA_BYTES:
                        raise ValueError('LibreQuake archive exceeds the pinned byte count')
                    digest.update(chunk)
                    output.write(chunk)
            if total != DATA_BYTES or digest.hexdigest() != DATA_SHA256:
                raise ValueError('LibreQuake archive failed pinned length or SHA-256 verification')
            output.flush()
            os.fsync(output.fileno())
        # Unlike rename, link refuses any existing cache rather than replacing
        # it. The temporary and final paths are in the same private directory.
        os.link(temporary, archive, follow_symlinks=False)
        directory = os.open(work, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        return archive
    finally:
        Path(temporary).unlink(missing_ok=True)


def archive_members(package):
    """Preflight the whole bounded ZIP before creating the data directory."""
    members = package.infolist()
    if len(members) > MAX_MEMBERS or sum(entry.file_size for entry in members) > MAX_EXTRACTED_BYTES:
        raise ValueError('Archive exceeds extraction limits')
    files, directories, seen = set(), set(), set()
    for entry in members:
        raw = entry.orig_filename  # ZipInfo.filename truncates embedded NUL.
        mode = entry.external_attr >> 16
        if (not raw or len(raw) > 4096 or raw.startswith('/') or '\\' in raw
                or any(ord(char) < 32 or ord(char) == 127 for char in raw)
                or entry.flag_bits & 1 or entry.file_size < 0):
            raise ValueError('Unsafe archive member')
        parts = raw[:-1].split('/') if entry.is_dir() else raw.split('/')
        if (any(part in ('', '.', '..') for part in parts) or ':' in parts[0]
                or stat.S_IFMT(mode) not in (0, stat.S_IFDIR, stat.S_IFREG)
                or (stat.S_ISDIR(mode) and not entry.is_dir())
                or (stat.S_ISREG(mode) and entry.is_dir())
                or (entry.is_dir() and entry.file_size != 0)):
            raise ValueError('Unsafe archive member')
        name = '/'.join(parts)
        parents = {'/'.join(parts[:index]) for index in range(1, len(parts))}
        if name in seen or parents & files or (not entry.is_dir() and name in directories):
            raise ValueError('Duplicate or conflicting archive member')
        seen.add(name)
        directories.update(parents)
        (directories if entry.is_dir() else files).add(name)
    return members


@functools.cache
def dm_pack_module():
    """The shared pinned DM-maps pack builder in src/, loaded without changing sys.path."""
    spec = importlib.util.spec_from_file_location('live_quake_dm_pack', Path(__file__).resolve().parents[1] / 'src/dm_pack.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def write_dm_pack(work, package):
    """Build the DM-maps pack next to the development tree's bot pack (ironwail/frikbot/)."""
    dm_pack = dm_pack_module()
    payload = dm_pack.pack_from_archive(package)
    source = work / 'ironwail'
    if source.is_symlink() or not source.is_dir():
        print('DM-maps pack skipped: run bootstrap-ironwail.py --fetch-sources, then --fetch-data again',
              file=sys.stderr, flush=True)
        return None
    target = work / dm_pack.PACK_RELATIVE
    if not os.path.lexists(target.parent):
        target.parent.mkdir(mode=0o700)
    private_dir(target.parent)
    fd, temporary = tempfile.mkstemp(dir=target.parent, prefix='.dm-maps-')
    try:
        with os.fdopen(fd, 'wb') as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, target)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return {'path': str(target), 'sha256': hashlib.sha256(payload).hexdigest()}


def private_dir(path):
    path = Path(path).absolute()
    if path.resolve() != path or not path.is_dir():
        raise ValueError('Use an existing canonical directory, created with mktemp -d')
    info = path.stat()
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise ValueError('Working directory must be user-owned and mode 0700')
    return path

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--work-dir', required=True, type=private_dir)
    parser.add_argument('--fetch-data', action='store_true')
    args = parser.parse_args()
    work = args.work_dir
    os.umask(0o077)
    if args.fetch_data:
        archive = work / 'librequake-full.zip'
        if not os.path.lexists(archive):
            download_archive(work)
        destination = work / 'data'
        if os.path.lexists(destination) and (destination.is_symlink() or not destination.is_dir()):
            raise ValueError('Data path exists and is not a directory; use a fresh directory')
        with verified_archive(archive) as stream:
            with zipfile.ZipFile(stream) as package:
                members = archive_members(package)
                if not os.path.lexists(destination):  # Existing data is kept; the DM pack is always rebuilt.
                    destination.mkdir(mode=0o700)
                    package.extractall(destination, members)
                dm_maps = write_dm_pack(work, package)
        print(json.dumps({'data': str(destination), 'sha256': DATA_SHA256, 'dm_maps': dm_maps}), flush=True)

if __name__ == '__main__':
    main()
