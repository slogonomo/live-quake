# SPDX-License-Identifier: 0BSD
"""Install verified local artifacts into an explicitly supplied private HOME.

No commands are launched, artifacts fetched, services enabled, or background
choices changed; the only exception is a read-only query of the account's own
running shell after an install that moved the menu (it never restarts the shell).
The menu is copied into a content-addressed impl-<sha256>/ folder so a changed
menu gets a new QML URL. Updates replace only exact inventory-owned files. Removal
preserves changed/unowned files and removes plugin files last. HOME and every
XDG location are explicit and never obtained from the process environment.
"""
from __future__ import annotations

import sys
sys.dont_write_bytecode = True

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import pwd
import re
import secrets
import signal
import stat
import subprocess
import time
import zipfile

import dm_pack

PLUGIN_ID = 'io.github.slogonomo.live-quake'
IRON_URL = 'https://github.com/andrei-drexler/ironwail.git'
IRON_COMMIT = '1eabd0df21130e55d94496c7af177e2dcbed75df'
IRON_HEADER_SHA256 = '6513c2158a4e3bb8da9f96817ac3ff78b95be0b8ccb8a8560b0da226715ea214'
IRON_HEADER_BYTES = 294997
IRON_SOURCE_DATE_EPOCH = 1790991502
DATA_URL = 'https://github.com/lavenderdotpet/LibreQuake/releases/download/v0.09-beta/full.zip'
DATA_SHA256 = '623e463b35811216244f9ba15e0c45abc1765288650b5e3d36a899b35e4bcf3d'
DATA_BYTES = 115678161
MAX_FILES = 512
MAX_JSON = 512 * 1024
MAX_RECOVERY_JSON = 2 * 1024 * 1024
MAX_FILE_BYTES = 256 * 1024 * 1024
MAX_TOTAL_BYTES = 512 * 1024 * 1024
_READ = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK
_IRON_SOURCE = (
    'scripts/bootstrap-ironwail.py', 'scripts/build-ironwail.py', 'patches/ironwail-desktop.patch',
    'src/ironwail/live_quake.c', 'src/ironwail/lq_egl_gbm.c',
    'src/ironwail/lq_egl_offscreen.h', 'src/ironwail/lq_libretro.c', 'src/ironwail/lq_libretro.h',
    'src/ironwail/lq_session.h', 'src/ironwail/dm_rotation.h', 'src/ironwail/dm_director.h',
    'src/ironwail/lq_sound.c', 'src/ironwail/lq_steam_disabled.c',
    'src/gpu_gbm.c', 'src/gpu_gbm.h', 'src/gpu_transport.c', 'src/gpu_transport.h',
    'src/gpu_wayland.c', 'src/gpu_wayland.h',
    'src/protocols/linux-dmabuf-v1.xml', 'src/protocols/viewporter.xml',
)
_SOURCE = (
    'README.md', 'THIRD-PARTY-NOTICES.md', 'manifest.json',
    'LICENSES/README.md', 'LICENSES/0BSD.txt', 'LICENSES/CC0-1.0.txt',
    'LICENSES/GPL-2.0-or-later.txt', 'LICENSES/BSD-3-Clause-LibreQuake.txt',
    'assets/live-quake.webp', 'assets/README.md',
    'docs/architecture.md', 'docs/engine-choice.md', 'docs/free-data.md', 'docs/steam-games.md',
    'menu/Menu.qml', 'menu/MenuCard.qml', 'menu/MenuFloat.qml', 'menu/MenuSounds.qml', 'menu/MenuWindow.qml',
    'menu/QuakeSlider.qml', 'menu/QuakeText.qml', 'menu/TextBox.qml', 'menu/pakart.py', 'menu/README.md',
    'scripts/quakectl', 'scripts/install.py', 'scripts/bootstrap.py', 'scripts/build.py', 'scripts/build-viewer.sh',
    'src/controller.py', 'src/onboarding.py', 'src/daemon.py', 'src/idle.py', 'src/idle_controller.py', 'src/idle_cleanup.py', 'src/runtime_lock.py',
    'src/data_farm.py', 'src/desktop.py', 'src/frame.py',
    'src/policy.py', 'src/policy_log.py', 'src/steam_games.py', 'src/game_catalog.py', 'src/background.py', 'src/install.py', 'src/bot_watchdog.py',
    'src/dm_pack.py',
    'src/engine.c', 'src/bridge.h', 'src/viewer.c',
    'src/protocols/pointer-constraints-unstable-v1.xml', 'src/protocols/relative-pointer-unstable-v1.xml',
    'src/protocols/wlr-layer-shell-unstable-v1.xml', 'src/protocols/xdg-output-unstable-v1.xml',
    'src/protocols/xdg-shell.xml',
) + ('src/level_bypass.h',) + _IRON_SOURCE + (
    'src/waypoints/README.md',
    'src/waypoints/lqdm1.way',
    'src/waypoints/lqdm11.wa1',
    'src/waypoints/lqdm11.way',
    'src/waypoints/lqdm12.wa1',
    'src/waypoints/lqdm12.way',
    'src/waypoints/lqdm13.wa1',
    'src/waypoints/lqdm13.way',
    'src/waypoints/lqdm2.way',
    'src/waypoints/lqdm3.wa1',
    'src/waypoints/lqdm3.way',
    'src/waypoints/lqdm4.wa1',
    'src/waypoints/lqdm4.way',
    'src/waypoints/lqdm5.wa1',
    'src/waypoints/lqdm5.way',
    'src/waypoints/lqdm7.wa1',
    'src/waypoints/lqdm7.way',
    'src/waypoints/lqdm8.way',
    'src/waypoints/lqdm9.wa1',
    'src/waypoints/lqdm9.way',
)
_OPTIONAL_SOURCE = ('LICENSE', 'src/background_controller.py', 'src/menu_launcher.py',
                    'src/process_helper.py', 'src/audio.c', 'src/audio.h')
_IRON_BUILD_SOURCE = _IRON_SOURCE + (
    'src/engine.c', 'src/audio.c', 'src/audio.h', 'src/bridge.h', 'src/viewer.c',
    'scripts/bootstrap.py', 'scripts/build.py', 'scripts/build-viewer.sh',
    'src/protocols/pointer-constraints-unstable-v1.xml', 'src/protocols/relative-pointer-unstable-v1.xml',
    'src/protocols/wlr-layer-shell-unstable-v1.xml', 'src/protocols/xdg-output-unstable-v1.xml',
    'src/protocols/xdg-shell.xml',
) + ('src/level_bypass.h',)
_IRON_BUILD_FILES = ('quake-engine', 'quake-viewer', 'ironwail/Quake/ironwail_libretro.so', 'ironwail/frikbot/pak0.pak')



def _absolute(value):
    raw = os.fspath(value)
    if (not isinstance(raw, str) or len(raw) > 4096 or not raw.startswith('/') or raw.startswith('//')
            or any(ord(char) < 32 or ord(char) == 127 for char in raw) or '\\' in raw
            or any(part in ('', '.', '..') for part in raw.split('/')[1:])):
        raise ValueError('An absolute path without traversal is required')
    return Path(raw)


def _relative(value):
    if (not isinstance(value, str) or not value or len(value) > 4096 or value.startswith('/')
            or '\\' in value or any(ord(char) < 32 or ord(char) == 127 for char in value)
            or any(part in ('', '.', '..') for part in value.split('/'))):
        raise ValueError('Invalid installation-relative path')
    return value


def _directory(path):
    """Open a canonical directory without following any parent links."""
    path = Path('/') if os.fspath(path) == '/' else _absolute(path)
    current = os.open('/', _READ | os.O_DIRECTORY)
    try:
        for name in path.parts[1:]:
            child = os.open(name, _READ | os.O_DIRECTORY, dir_fd=current)
            os.close(current)
            current = child
        return current
    except BaseException:
        os.close(current)
        raise


def _identity(info):
    return [info.st_dev, info.st_ino, info.st_mtime_ns, info.st_ctime_ns]


def _file_identity(info):
    return [info.st_ino, info.st_mtime_ns]


def _file_info(fd, maximum):
    info = os.fstat(fd)
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1
            or not 0 <= info.st_size <= maximum):
        raise ValueError('An owned single-link bounded regular file is required')
    return info


def _hash_stream(stream, size, maximum):
    if size > maximum:
        raise ValueError('File exceeds its size limit')
    digest, total = hashlib.sha256(), 0
    while total <= size:
        chunk = stream.read(min(65536, size + 1 - total))
        if not chunk:
            break
        digest.update(chunk)
        total += len(chunk)
    if total != size:
        raise ValueError('File changed or exceeded its size limit')
    return digest.hexdigest()


def _hash_chunks(stream, size):
    digest, chunks, used = hashlib.sha256(), [], 0
    while used < size:
        chunk = stream.read(min(65536, size - used))
        if not chunk:
            raise ValueError('File ended while building its recovery records')
        digest.update(chunk)
        chunks.append(hashlib.sha256(chunk).hexdigest())
        used += len(chunk)
    if stream.read(1):
        raise ValueError('File grew while building its recovery records')
    return digest.hexdigest(), chunks


@contextmanager
def _source_file(path, maximum=MAX_FILE_BYTES):
    path = _absolute(path)
    parent = _directory(path.parent)
    try:
        fd = os.open(path.name, _READ, dir_fd=parent)
    finally:
        os.close(parent)
    with os.fdopen(fd, 'rb') as stream:
        before = _file_info(stream.fileno(), maximum)
        yield stream, before
        after = os.fstat(stream.fileno())
        if _identity(before) != _identity(after) or before.st_size != after.st_size:
            raise ValueError('Source file changed while installing')


def _json_source(path):
    with _source_file(path, MAX_JSON) as (stream, info):
        payload = stream.read(MAX_JSON + 1)
        if len(payload) != info.st_size:
            raise ValueError('JSON file changed while reading')
    value = json.loads(payload)
    if not isinstance(value, dict):
        raise ValueError('JSON document must be an object')
    return value


@dataclass(frozen=True)
class InstallationPaths:
    home: Path
    config_home: Path | None = None
    data_home: Path | None = None
    state_home: Path | None = None
    cache_home: Path | None = None
    runtime_dir: Path | None = None

    def __post_init__(self):
        home = _absolute(self.home)
        fd = _directory(home)
        try:
            info = os.fstat(fd)
            if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
                raise ValueError('Supplied HOME must be owned and mode 0700')
        finally:
            os.close(fd)
        object.__setattr__(self, 'home', home)
        values = []
        defaults = (home / '.config', home / '.local/share', home / '.local/state', home / '.cache')
        for key, default in (('config_home', '.config'), ('data_home', '.local/share'),
                             ('state_home', '.local/state'), ('cache_home', '.cache')):
            value = _absolute(getattr(self, key) or home / default)
            if value == home or home not in value.parents:
                raise ValueError('XDG directories must remain inside supplied HOME')
            if value != home / default and (any(value == item or item in value.parents for item in defaults)
                    or any(part.casefold() in ('omarchy', 'live-doom', 'doom-desktop', 'io.github.slogonomo.live-doom')
                           for part in value.relative_to(home).parts)):
                raise ValueError('Custom XDG roots must be isolated from default XDG and Omarchy/app directories')
            values.append(value)
            object.__setattr__(self, key, value)
        if any(a == b or a in b.parents or b in a.parents for i, a in enumerate(values) for b in values[i + 1:]):
            raise ValueError('XDG directories must be distinct and nonoverlapping')
        with _Home(self) as filesystem:
            for value in values:
                filesystem.check_parents(self.rel(value / '.check'))
                if value not in defaults:
                    for parent in (value, *value.parents):
                        if parent == home:
                            break
                        try:
                            fd, name = filesystem.parent(self.rel(parent / 'manifest.json'))
                            try:
                                os.stat(name, dir_fd=fd, follow_symlinks=False)
                            finally:
                                os.close(fd)
                        except FileNotFoundError:
                            continue
                        raise ValueError('Custom XDG root cannot be inside another application checkout')
        if self.runtime_dir is not None:
            runtime = _absolute(self.runtime_dir)
            fd = _directory(runtime)
            try:
                info = os.fstat(fd)
                if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
                    raise ValueError('Explicit runtime directory must be owned and mode 0700')
            finally:
                os.close(fd)
            object.__setattr__(self, 'runtime_dir', runtime)

    @property
    def plugin(self):
        return self.config_home / 'omarchy/plugins' / PLUGIN_ID

    @property
    def build(self):
        return self.cache_home / 'live-quake/build'

    @property
    def data(self):
        return self.data_home / 'live-quake'

    @property
    def inventory(self):
        return self.data / 'installation.json'

    def rel(self, path):
        return _relative(str(_absolute(path).relative_to(self.home)))

    def environment(self):
        return {'HOME': str(self.home), 'XDG_CONFIG_HOME': str(self.config_home),
                'XDG_DATA_HOME': str(self.data_home), 'XDG_STATE_HOME': str(self.state_home),
                'XDG_CACHE_HOME': str(self.cache_home)}


class _Home:
    def __init__(self, paths):
        self.paths = paths
        self.fd = _directory(paths.home)
        info = os.fstat(self.fd)
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            os.close(self.fd)
            raise ValueError('Supplied HOME must remain owned and mode 0700')
        self.created = {}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        os.close(self.fd)

    def parent(self, relative, create=False):
        parts = _relative(relative).split('/')
        current = os.dup(self.fd)
        prefix = []
        try:
            for name in parts[:-1]:
                prefix.append(name)
                try:
                    child = os.open(name, _READ | os.O_DIRECTORY, dir_fd=current)
                except FileNotFoundError:
                    if not create:
                        raise
                    os.mkdir(name, 0o700, dir_fd=current)
                    os.fsync(current)
                    child = os.open(name, _READ | os.O_DIRECTORY, dir_fd=current)
                    info = os.fstat(child)
                    self.created['/'.join(prefix)] = {'identity': [info.st_ino],
                                                     'mode': stat.S_IMODE(info.st_mode)}
                info = os.fstat(child)
                if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o022:
                    os.close(child)
                    raise ValueError('Installation parent must be owned and not group/world writable')
                os.close(current)
                current = child
            return current, parts[-1]
        except BaseException:
            os.close(current)
            raise

    def check_parents(self, relative):
        try:
            fd, _ = self.parent(relative)
            os.close(fd)
        except FileNotFoundError:
            pass

    def record(self, relative):
        try:
            parent, name = self.parent(relative)
        except FileNotFoundError:
            return None
        try:
            try:
                fd = os.open(name, _READ, dir_fd=parent)
            except FileNotFoundError:
                return None
            with os.fdopen(fd, 'rb') as stream:
                before = _file_info(stream.fileno(), MAX_FILE_BYTES)
                digest = _hash_stream(stream, before.st_size, MAX_FILE_BYTES)
                after = os.fstat(stream.fileno())
                if _identity(before) != _identity(after) or before.st_size != after.st_size:
                    raise ValueError('Installed file changed during inspection')
                return {'sha256': digest, 'size': before.st_size, 'mode': stat.S_IMODE(before.st_mode),
                        'identity': _file_identity(before)}
        finally:
            os.close(parent)

    def read_json(self, relative):
        parent, name = self.parent(relative)
        try:
            fd = os.open(name, _READ, dir_fd=parent)
            with os.fdopen(fd, 'rb') as stream:
                before = _file_info(stream.fileno(), MAX_JSON)
                payload = stream.read(MAX_JSON + 1)
                if len(payload) != before.st_size or _identity(before) != _identity(os.fstat(stream.fileno())):
                    raise ValueError('Installation inventory changed during inspection')
        finally:
            os.close(parent)
        value = json.loads(payload)
        if not isinstance(value, dict):
            raise ValueError('Installation inventory must be an object')
        return value

    def unlink_exact(self, relative, expected):
        parent, name = self.parent(relative)
        try:
            actual, instant = _record_at(parent, name)
            if actual != expected:
                raise ValueError('Refusing to remove a changed installation file: ' + relative)
            if _identity(os.stat(name, dir_fd=parent, follow_symlinks=False)) != instant:
                raise ValueError('File changed at removal: ' + relative)
            os.unlink(name, dir_fd=parent)
            os.fsync(parent)
        finally:
            os.close(parent)

    def remove_empty(self, relatives):
        retained = {}
        for relative in sorted(set(relatives), key=lambda value: (value.count('/'), value), reverse=True):
            try:
                parent, name = self.parent(relative)
            except FileNotFoundError:
                continue
            except (OSError, ValueError):
                if isinstance(relatives, dict):
                    retained[relative] = relatives[relative]
                continue
            try:
                info = os.stat(name, dir_fd=parent, follow_symlinks=False)
                expected = relatives.get(relative) if isinstance(relatives, dict) else None
                if (stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid()
                        and (expected is None or expected == {'identity': [info.st_ino],
                                                            'mode': stat.S_IMODE(info.st_mode)})):
                    os.rmdir(name, dir_fd=parent)
                elif isinstance(relatives, dict):
                    retained[relative] = relatives[relative]
            except FileNotFoundError:
                pass
            except OSError:
                if isinstance(relatives, dict) and relative in relatives:
                    retained[relative] = relatives[relative]
            finally:
                os.close(parent)
        return retained


def _record_at(parent, name, allow_pair=False):
    fd = os.open(name, _READ, dir_fd=parent)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_nlink not in ((1, 2) if allow_pair else (1,))
                or not 0 <= info.st_size <= MAX_FILE_BYTES):
            raise ValueError('An owned bounded regular recovery file is required')
        digest = _hash_stream(stream, info.st_size, MAX_FILE_BYTES)
        if _identity(info) != _identity(os.fstat(stream.fileno())):
            raise ValueError('Recovery file changed while reading')
        return ({'sha256': digest, 'size': info.st_size, 'mode': stat.S_IMODE(info.st_mode),
                 'identity': _file_identity(info)}, _identity(info))


def _managed(paths, relative, external=False):
    target = paths.home / _relative(relative)
    return ((not external and paths.plugin in target.parents) or paths.build in target.parents
            or (paths.data / 'librequake/full') in target.parents
            or target == paths.data / 'build-provenance.json'
            or target == paths.config_home / 'systemd/user/live-quake.service'
            or target == paths.data_home / 'applications/live-quake.desktop')


def _inventory(filesystem):
    paths = filesystem.paths
    try:
        value = filesystem.read_json(paths.rel(paths.inventory))
    except FileNotFoundError:
        return None
    return _validate_inventory(filesystem, value)


def _validate_inventory(filesystem, value):
    paths = filesystem.paths
    if not isinstance(value, dict):
        raise ValueError('Invalid installation inventory')
    files, directories = value.get('files'), value.get('directories')
    if (type(value.get('schema')) is not int or value.get('schema') != 2 or value.get('plugin_id') != PLUGIN_ID
            or value.get('home') != str(paths.home) or value.get('xdg') != paths.environment()
            or not isinstance(files, dict) or len(files) > MAX_FILES
            or not isinstance(directories, dict) or len(directories) > MAX_FILES * 2):
        raise ValueError('Invalid installation inventory')
    mode = value.get('checkout_mode')
    if mode not in ('owned-copy', 'external-read-only'):
        raise ValueError('Invalid installation checkout ownership mode')
    external = mode == 'external-read-only'
    for relative, record in files.items():
        if (not _managed(paths, relative, external) or not isinstance(record, dict)
                or set(record) != {'sha256', 'size', 'mode', 'identity'}
                or not isinstance(record['sha256'], str) or len(record['sha256']) != 64
                or any(char not in '0123456789abcdef' for char in record['sha256'])
                or type(record['size']) is not int or not 0 <= record['size'] <= MAX_FILE_BYTES
                or type(record['mode']) is not int or record['mode'] not in (0o600, 0o644, 0o755)
                or not isinstance(record['identity'], list) or len(record['identity']) != 2
                or any(type(value) is not int or value < 0 for value in record['identity'])):
            raise ValueError('Invalid installation file record')
    # A ledger cannot authorize deleting shared XDG parents, even when empty.
    allowed_roots = (paths.cache_home / 'live-quake', paths.data / 'librequake') + (() if external else (paths.plugin,))
    for relative, record in directories.items():
        target = paths.home / _relative(relative)
        if (not any(target == root or root in target.parents for root in allowed_roots)
                or not isinstance(record, dict) or set(record) != {'identity', 'mode'}
                or not isinstance(record['identity'], list) or len(record['identity']) != 1
                or any(type(value) is not int or value < 0 for value in record['identity'])
                or record['mode'] != 0o700):
            raise ValueError('Invalid owned installation directory')
    pending = value.get('pending')
    if pending is not None and (not isinstance(pending, dict) or set(pending) != {'transaction'}
            or not _coordinator_path(paths, pending['transaction'])):
        raise ValueError('Invalid pending installation recovery reference')
    return value


def _systemd_quote(value):
    return '"' + value.replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%') + '"'


def _systemd_exec_quote(value):
    return _systemd_quote(value.replace('$', '$$'))


def _systemd_condition_path(value):
    # Condition directives do not use ExecStart's argument quoting grammar.
    return str(_absolute(value)).replace('%', '%%')


def _desktop_quote(value):
    # Desktop Exec has its own quoting grammar; no shell is involved.
    return '"' + value.replace('\\', '\\\\\\\\').replace('"', '\\\\"').replace('`', '\\\\`').replace('$', '\\\\$').replace('%', '%%') + '"'


def _generated(paths, provenance):
    environment = '\n'.join('Environment=' + _systemd_quote(key + '=' + value)
                            for key, value in paths.environment().items())
    unit = ('[Unit]\nDescription=Live Quake wallpaper controller\nAfter=graphical-session.target\n'
            'PartOf=graphical-session.target\nConditionPathExists=' + _systemd_condition_path(paths.plugin / 'src/controller.py') + '\n'
            'StartLimitIntervalSec=60\nStartLimitBurst=3\n\n[Service]\nType=simple\nEnvironment="PATH=/usr/bin:/bin"\n' + environment + '\n'
            'ExecStart=/usr/bin/python3 -E -s -B ' + _systemd_exec_quote(str(paths.plugin / 'src/controller.py')) + ' daemon --audio-mode alsa'
            + '\n'
            'ExecStopPost=-/usr/bin/timeout --kill-after=0.1s 1s /usr/bin/python3 -E -s -B '
            + _systemd_exec_quote(str(paths.plugin / 'src/idle_cleanup.py'))
            + ' --home ' + _systemd_exec_quote(str(paths.home))
            + ' --state-dir ' + _systemd_exec_quote(str(paths.state_home / 'live-quake'))
            + ' --runtime-dir "%t/live-quake"\n'
            'Restart=on-failure\nRestartSec=3\nLimitCORE=1\nKillMode=control-group\n'
            '\n[Install]\nWantedBy=graphical-session.target\n')
    desktop = ('[Desktop Entry]\nType=Application\nName=Live Quake\nComment=Open Live Quake settings\n'
               'Exec=/usr/bin/env "PATH=/usr/bin:/bin" ' + ' '.join(_desktop_quote(key + '=' + value)
                                             for key, value in paths.environment().items()) + ' '
               + '/usr/bin/python3 -E -s -B ' + _desktop_quote(str(paths.plugin / 'src/controller.py')) + ' open-menu --origin=launcher\n'
               'Terminal=false\nCategories=Game;\nStartupNotify=false\n')
    return {
        paths.rel(paths.config_home / 'systemd/user/live-quake.service'): (unit.encode(), 0o644),
        paths.rel(paths.data_home / 'applications/live-quake.desktop'): (desktop.encode(), 0o644),
        paths.rel(paths.data / 'build-provenance.json'): (
            (json.dumps(provenance, indent=2, allow_nan=False) + '\n').encode(), 0o600),
    }


def _source_root(root):
    root = _absolute(root)
    fd = _directory(root)
    try:
        if os.fstat(fd).st_uid != os.getuid():
            raise ValueError('Artifact source directory must be owned')
    finally:
        os.close(fd)
    return root


def _sources(root):
    manifest = _json_source(root / 'manifest.json')
    if manifest.get('id') != PLUGIN_ID:
        raise ValueError('Unexpected plugin manifest id')
    result = list(_SOURCE)
    for relative in _OPTIONAL_SOURCE:
        try:
            with _source_file(root / relative, 2 * 1024 * 1024):
                pass
        except FileNotFoundError:
            continue
        result.append(relative)
    return result


def _iron_build(root, work, record):
    """Accept only the closed complete Iron recipe and three exact binaries."""
    hashes, sources, artifacts = record.get('files'), record.get('source_hashes'), record.get('artifacts')
    if record.get('components') == ['core', 'frontend', 'viewer']:
        raise ValueError('Build lacks the local bot pack; rebuild with build-ironwail.py --frikbot-pack and --frikbot-receipt')
    if (record.get('backend') != 'ironwail' or record.get('engine_commit') != IRON_COMMIT
            or type(record.get('source_date_epoch')) is not int or record['source_date_epoch'] != IRON_SOURCE_DATE_EPOCH
            or record.get('header_sha256') != IRON_HEADER_SHA256
            or record.get('components') != ['core', 'frontend', 'viewer', 'frikbot']
            or not isinstance(hashes, dict) or len(hashes) > 64
            or not isinstance(sources, dict) or set(sources) != set(_IRON_BUILD_SOURCE)
            or not isinstance(artifacts, dict) or set(artifacts) != {'core', 'frontend', 'viewer', 'frikbot'}):
        raise ValueError('Build record does not match the complete pinned OpenGL engine')
    neutral, recognized = {}, set()
    for relative in _IRON_BUILD_SOURCE:
        keys = [key for key in hashes if isinstance(key, str) and key.endswith('/' + relative)]
        if len(keys) != 1:
            raise ValueError('OpenGL build record lacks a unique source hash: ' + relative)
        _absolute(keys[0])
        with _source_file(root / relative) as (stream, info):
            digest = _hash_stream(stream, info.st_size, MAX_FILE_BYTES)
        if sources[relative] != digest or hashes[keys[0]] != digest:
            raise ValueError('OpenGL native source hash mismatch: ' + relative)
        neutral['repository/' + relative] = digest
        recognized.add(keys[0])
    if record.get('patch_sha256') != sources['patches/ironwail-desktop.patch']:
        raise ValueError('OpenGL patch receipt differs from the reviewed source')
    for kind, relative in (('frontend', _IRON_BUILD_FILES[0]), ('viewer', _IRON_BUILD_FILES[1]), ('core', _IRON_BUILD_FILES[2]), ('frikbot', _IRON_BUILD_FILES[3])):
        entry = artifacts[kind]
        if not isinstance(entry, dict) or set(entry) != {'path', 'sha256'} or entry['path'] != relative:
            raise ValueError('OpenGL build artifact path is not a closed runtime target')
        path = work / relative
        with _source_file(path) as (stream, info):
            digest = _hash_stream(stream, info.st_size, MAX_FILE_BYTES)
        if entry['sha256'] != digest or hashes.get(str(path)) != digest:
            raise ValueError('OpenGL native artifact hash mismatch: ' + relative)
        neutral['native/' + relative] = digest
        recognized.add(str(path))
    bot_receipt = record.get('frikbot')
    if (not isinstance(bot_receipt, dict)
            or bot_receipt.get('local_pack_sha256') != artifacts['frikbot']['sha256']
            or bot_receipt.get('base_pin') != 'c0d1b91c74eb654365ac7755bc837e497caaca73'
            or bot_receipt.get('compiler_pin') != 'f937b9d88f71fc4429db5fe56c6a98d922711b2e'):
        raise ValueError('Local bot pack lacks its pinned source and compiler receipt')
    header = work / 'include/libretro.h'
    with _source_file(header, IRON_HEADER_BYTES) as (stream, info):
        if info.st_size != IRON_HEADER_BYTES or _hash_stream(stream, info.st_size, IRON_HEADER_BYTES) != IRON_HEADER_SHA256:
            raise ValueError('OpenGL ABI header differs from its exact pin')
    if hashes.get(str(header)) != IRON_HEADER_SHA256 or set(hashes) != recognized | {str(header)}:
        raise ValueError('OpenGL receipt has unrecognized file targets or an invalid ABI header')
    return {'backend': 'ironwail', 'renderer': 'opengl', 'engine_commit': IRON_COMMIT,
            'source_date_epoch': IRON_SOURCE_DATE_EPOCH, 'header_sha256': IRON_HEADER_SHA256, 'files': neutral}


def _build(root, work):
    record = _json_source(work / 'build-record.json')
    return _iron_build(root, work, record)


def _archive_members(package):
    members = package.infolist()
    if len(members) > 20000 or sum(member.file_size for member in members) > MAX_TOTAL_BYTES:
        raise ValueError('LibreQuake archive exceeds its limits')
    seen, files, directories = set(), set(), set()
    selected = []
    for member in members:
        raw, mode = member.orig_filename, member.external_attr >> 16
        name = raw[:-1] if member.is_dir() else raw
        _relative(name)
        parents = {'/'.join(name.split('/')[:index]) for index in range(1, len(name.split('/')))}
        if (':' in name.split('/')[0] or member.flag_bits & 1
                or stat.S_IFMT(mode) not in (0, stat.S_IFDIR, stat.S_IFREG)
                or (stat.S_ISDIR(mode) and not member.is_dir())
                or (stat.S_ISREG(mode) and member.is_dir())
                or (member.is_dir() and member.file_size != 0)
                or not 0 <= member.file_size <= MAX_FILE_BYTES
                or name in seen or parents & files or (not member.is_dir() and name in directories)):
            raise ValueError('Unsafe LibreQuake archive member')
        seen.add(name)
        directories.update(parents)
        (directories if member.is_dir() else files).add(name)
        if not member.is_dir() and (name in ('full/id1/pak0.pak', 'full/id1/pak1.pak')
                                   or name.startswith('full/id1/docs/')):
            selected.append(member)
    if not {'full/id1/pak0.pak', 'full/id1/pak1.pak', 'full/id1/docs/COPYING'} <= {m.filename for m in selected}:
        raise ValueError('LibreQuake archive lacks required PAKs or licence')
    if len(selected) > MAX_FILES:
        raise ValueError('LibreQuake selected data exceeds file count limit')
    return selected


def _artifacts(paths, root, work, package, sources, build, provenance, external):
    """Verify all payload bytes without creating a lock, directory or file."""
    artifacts = {}
    source_hashes = {}
    menu, manifest = {}, None
    for relative in sources:
        with _source_file(root / relative, 2 * 1024 * 1024) as (stream, info):
            digest, chunks = _hash_chunks(stream, info.st_size)
            source_hashes['repository/' + relative] = digest
            if external and relative == 'scripts/quakectl' and not stat.S_IMODE(info.st_mode) & 0o100:
                raise ValueError('External checkout scripts/quakectl must already be executable')
            if not external:
                mode = 0o755 if relative in ('scripts/quakectl', 'scripts/install.py', 'scripts/build-viewer.sh') else 0o644
                entry = {'kind': 'source', 'path': root / relative, 'sha256': digest, 'chunks': chunks, 'size': info.st_size, 'mode': mode}
                if relative.startswith('menu/'):
                    menu[relative[len('menu/'):]] = entry
                elif relative == 'manifest.json':
                    stream.seek(0)
                    manifest = stream.read(info.st_size + 1)
                else:
                    artifacts[paths.rel(paths.plugin / relative)] = entry
    if not external:
        impl, payload = _menu_implementation(manifest, menu)
        for name, entry in menu.items():
            artifacts[paths.rel(paths.plugin / impl / name)] = entry
        artifacts[paths.rel(paths.plugin / 'manifest.json')] = {
            'kind': 'generated', 'payload': payload, 'sha256': hashlib.sha256(payload).hexdigest(),
            'chunks': _hash_chunks(io.BytesIO(payload), len(payload))[1], 'size': len(payload), 'mode': 0o644}
    for relative in _IRON_BUILD_FILES:
        with _source_file(work / relative) as (stream, info):
            magic = b'PACK' if relative == 'ironwail/frikbot/pak0.pak' else b'\x7fELF'
            if stream.read(4) != magic:
                raise ValueError('Bot artifact is not a PACK archive' if magic == b'PACK' else 'Native artifact is not an ELF binary')
            stream.seek(0)
            digest, chunks = _hash_chunks(stream, info.st_size)
            if digest != build['files']['native/' + relative]:
                raise ValueError('Native artifact changed after build verification')
            artifacts[paths.rel(paths.build / relative)] = {
                'kind': 'source', 'path': work / relative, 'sha256': digest, 'chunks': chunks,
                'size': info.st_size, 'mode': 0o644 if relative.endswith(('.so', '.pak')) else 0o755}
    for member in _archive_members(package):
        with package.open(member) as stream:
            digest, chunks = _hash_chunks(stream, member.file_size)
        artifacts[paths.rel(paths.data / 'librequake' / member.filename)] = {
            'kind': 'archive', 'member': member, 'sha256': digest, 'chunks': chunks, 'size': member.file_size, 'mode': 0o644}
    # Installer-generated data with its own pinned members, outside the closed native build receipt.
    payload = dm_pack.pack_from_archive(package)
    digest = hashlib.sha256(payload).hexdigest()
    artifacts[paths.rel(paths.build / dm_pack.PACK_RELATIVE)] = {
        'kind': 'generated', 'payload': payload, 'sha256': digest,
        'chunks': _hash_chunks(io.BytesIO(payload), len(payload))[1], 'size': len(payload), 'mode': 0o644}
    provenance['dm_maps'] = {'path': dm_pack.PACK_RELATIVE, 'sha256': digest, 'bytes': len(payload)}
    provenance['source_files'] = source_hashes
    for relative, (payload, mode) in _generated(paths, provenance).items():
        artifacts[relative] = {'kind': 'generated', 'payload': payload, 'sha256': hashlib.sha256(payload).hexdigest(),
                               'chunks': _hash_chunks(io.BytesIO(payload), len(payload))[1], 'size': len(payload), 'mode': mode}
    if len(artifacts) > MAX_FILES or sum(entry['size'] for entry in artifacts.values()) > MAX_TOTAL_BYTES:
        raise ValueError('Installation exceeds its file or byte limit')
    return artifacts


def _menu_implementation(manifest, menu):
    """Copy the menu into impl-<sha256>/ and point the manifest's entry points there.

    Quickshell caches compiled QML by URL and Omarchy's plugin reload keeps that cache, so a changed
    menu at the same path stays old until the shell restarts. Content addressing gives a changed
    menu (any sibling QML, pakart.py or mode) a new URL; an unchanged one keeps its directory.
    """
    impl = 'impl-' + _fingerprint([{'path': name, 'mode': entry['mode'], 'sha256': entry['sha256']}
                                   for name, entry in sorted(menu.items())])
    value = json.loads(manifest)
    entries = value.get('entryPoints') if isinstance(value, dict) else None
    if (not isinstance(entries, dict) or not entries
            or any(not isinstance(entry, str) or not entry.startswith('menu/') or entry[len('menu/'):] not in menu
                   for entry in entries.values())):
        raise ValueError('Plugin manifest entry points must name copied menu/ files')
    value['entryPoints'] = {kind: impl + '/' + entry[len('menu/'):] for kind, entry in entries.items()}
    return impl, (json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + '\n').encode()


def _retirement_notice():
    return {'service_retirement_verified': False, 'manual_service_action_required': True,
            'manual_prerequisite': '/usr/bin/systemctl --user disable --now live-quake.service',
            'service_commands_performed': []}


def _fingerprint(plan):
    return hashlib.sha256(json.dumps(plan, sort_keys=True, allow_nan=False).encode()).hexdigest()


def _install_plan(filesystem, artifacts, previous, external):
    owned, entries, conflicts = (previous['files'] if previous else {}), [], []
    for relative in sorted(artifacts.keys() | owned.keys()):
        filesystem.check_parents(relative)
        try:
            current = filesystem.record(relative)
        except (OSError, ValueError):
            current = 'unreadable'
        if current is not None and (relative not in owned or current != owned[relative]):
            conflicts.append(relative)
        artifact = artifacts.get(relative)
        if artifact is not None:
            same = isinstance(current, dict) and all(current[key] == artifact[key] for key in ('sha256', 'size', 'mode'))
            entries.append({'action': 'keep-owned-unchanged' if same and relative in owned else ('update' if current is not None else 'create'), 'path': relative,
                            **{key: artifact[key] for key in ('sha256', 'size', 'mode')}})
        elif current is not None:
            entries.append({'action': 'remove-obsolete-owned-file', 'path': relative,
                            **{key: owned[relative][key] for key in ('sha256', 'size', 'mode')}})
    directories = set()
    for relative in (*artifacts, filesystem.paths.rel(filesystem.paths.inventory)):
        for parent in (filesystem.paths.home / relative).parents:
            if parent == filesystem.paths.home:
                break
            if _directory_id(parent) is None or filesystem.paths.rel(parent) in filesystem.created:
                directories.add(filesystem.paths.rel(parent))
    # Owned plugin folders no file needs any more (a replaced menu impl-*/, or the old flat menu/).
    plugin = filesystem.paths.rel(filesystem.paths.plugin) + '/'
    needed = {str(parent) for relative in artifacts for parent in Path(relative).parents}
    obsolete = [relative for relative in (previous['directories'] if previous else {})
                if relative.startswith(plugin) and relative not in needed]
    plan = {'schema': 1, 'operation': 'install', 'renderer': 'opengl', 'entries': entries, 'conflicts': conflicts,
            'create_directories': sorted(directories, key=lambda value: (value.count('/'), value)),
            'remove_empty_directories': sorted(obsolete, key=lambda value: (-value.count('/'), value)),
            'inventory_path': filesystem.paths.rel(filesystem.paths.inventory),
            'checkout_mode': 'external-read-only' if external else 'owned-copy',
            'plugin_path': filesystem.paths.rel(filesystem.paths.plugin),
            'service_start_requested': False, 'service_enable_requested': False,
            'service_state': 'unknown', 'manual_activation_required': True}
    unit_path = filesystem.paths.rel(filesystem.paths.config_home / 'systemd/user/live-quake.service')
    plan['unit_changed'] = any(entry['path'] == unit_path and entry['action'] != 'keep-owned-unchanged'
                               for entry in entries)
    plan['fingerprint'] = _fingerprint(plan)
    return plan


def _blockers(paths, *, runtime_held=False):
    """Read only the supplied private HOME/runtime, never a service manager."""
    blockers = []
    folder = paths.config_home / 'systemd/user'
    with _Home(paths) as filesystem:
        filesystem.check_parents(paths.rel(folder / '.check'))
        filesystem.check_parents(paths.rel(paths.config_home / 'omarchy/shell.json'))
    try:
        parent = _directory(folder)
    except FileNotFoundError:
        parent = -1
    if parent >= 0:
        try:
            with os.scandir(parent) as scan:
                names = []
                for entry in scan:
                    if len(names) >= 4096:
                        raise ValueError('Unit directory exceeds inspection bound')
                    names.append(entry.name)
            for name in names:
                info = os.stat(name, dir_fd=parent, follow_symlinks=False)
                if name == 'live-quake.service.d' or (name == 'live-quake.service' and stat.S_ISLNK(info.st_mode)):
                    blockers.append('Unit has externally managed symlink or drop-in state')
                if stat.S_ISLNK(info.st_mode):
                    target = os.readlink(name, dir_fd=parent)
                    if len(target) > 4096:
                        raise ValueError('Unit alias exceeds inspection bound')
                    if Path(target).name == 'live-quake.service':
                        blockers.append('Unit has an externally managed alias')
                if name.endswith(('.wants', '.requires', '.upholds')):
                    child = os.open(name, _READ | os.O_DIRECTORY, dir_fd=parent)
                    try:
                        try:
                            os.stat('live-quake.service', dir_fd=child, follow_symlinks=False)
                        except FileNotFoundError:
                            pass
                        else:
                            blockers.append('live-quake.service is enabled')
                    finally:
                        os.close(child)
        finally:
            os.close(parent)
    try:
        shell = _json_source(paths.config_home / 'omarchy/shell.json')
    except FileNotFoundError:
        shell = {}
    plugins, disabled = shell.get('plugins', []), shell.get('disabledPlugins', [])
    if not isinstance(plugins, (dict, list)) or not isinstance(disabled, list):
        raise ValueError('Cannot verify plugin disablement from shell.json')
    enabled = PLUGIN_ID in plugins if isinstance(plugins, dict) else any(
        entry == PLUGIN_ID or (isinstance(entry, dict) and entry.get('id') == PLUGIN_ID) for entry in plugins)
    if enabled and PLUGIN_ID not in disabled:
        blockers.append('Omarchy plugin is enabled')
    if paths.runtime_dir is not None and not runtime_held:
        from runtime_lock import RuntimeLock
        try:
            with RuntimeLock(paths.runtime_dir / 'live-quake', create=False, record_pid=False, shared=True):
                pass
        except FileNotFoundError:
            try:
                from idle_cleanup import runtime_parent
                runtime_parent(paths.runtime_dir / 'live-quake')
            except FileNotFoundError:
                pass  # No record may proceed under manual service retirement.
            except (OSError, ValueError):
                blockers.append('Runtime parent is unverifiable; check --runtime-dir before uninstall')
        except RuntimeError:
            blockers.append('Live Quake controller is running')
        except (OSError, ValueError):
            blockers.append('Runtime lock is unverifiable; check --runtime-dir before uninstall')
    from idle import SaverOwnership
    try:
        ownership = SaverOwnership(paths.home, paths.state_home / 'live-quake').cleanup_status()
        if not ownership['safe']:
            blockers.append('Screensaver ownership is unverifiable; preserve the installation')
        elif ownership['recorded'] and paths.runtime_dir is None:
            blockers.append('Explicit runtime directory is required to retire screensaver ownership')
        elif ownership['recorded'] and not runtime_held:
            try:
                from idle_cleanup import runtime_parent
                runtime_parent(paths.runtime_dir / 'live-quake')
            except (OSError, ValueError):
                blockers.append('Runtime parent is unavailable; use the current --runtime-dir to retire screensaver ownership')
    except (OSError, ValueError, RecursionError):
        blockers.append('Screensaver ownership is unverifiable; preserve the installation')
    return sorted(set(blockers))


def _uninstall_plan(filesystem, previous, *, runtime_held=False):
    entries, preserved = [], []
    for relative, expected in (previous['files'] if previous else {}).items():
        try:
            current = filesystem.record(relative)
        except (OSError, ValueError):
            current = 'unreadable'
        if current is None:
            continue
        if current != expected:
            preserved.append(relative)
        entries.append({'action': 'preserve' if current != expected else 'remove-owned-file', 'path': relative,
                        **{key: expected[key] for key in ('sha256', 'size', 'mode')}})
    plan = {'schema': 1, 'operation': 'uninstall', 'entries': sorted(entries, key=lambda entry: entry['path']),
            'preserved': sorted(preserved), 'plugin_remove_after_uninstall': True,
            'inventory_path': filesystem.paths.rel(filesystem.paths.inventory),
            'cleanup_empty_owned_directories': sorted(previous['directories']) if previous else [], **_retirement_notice()}
    plan['blockers'] = _blockers(filesystem.paths, runtime_held=runtime_held)
    from idle import SaverOwnership
    ownership = SaverOwnership(filesystem.paths.home,
                               filesystem.paths.state_home / 'live-quake').cleanup_status()
    if ownership['safe'] and all(ownership.get(field) is True for field in
            ('preserved_changed', 'toggle_safe', 'journal_retained', 'toggle_retained')):
        plan['screensaver_preservation_note'] = "Omarchy's screensaver-off toggle was switched on after Live Quake set it, so it was left on; turn Omarchy's screensaver back on in its menu if you want it."
    plan['running_controller_checked'] = filesystem.paths.runtime_dir is not None
    plan['fingerprint'] = _fingerprint(plan)
    return plan


@contextmanager
def _lock(filesystem, create=True):
    relative = filesystem.paths.rel(filesystem.paths.data / '.lock-directory')
    parent, name = filesystem.parent(relative, create=create)
    fd = parent
    try:
        info = os.fstat(fd)
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise ValueError('Live Quake data directory must be private')
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RuntimeError('Another install/removal is in progress') from error
        parent, name = filesystem.parent(filesystem.paths.rel(filesystem.paths.data))
        try:
            actual = os.stat(name, dir_fd=parent, follow_symlinks=False)
            current = os.fstat(fd)
            if (actual.st_dev, actual.st_ino) != (current.st_dev, current.st_ino):
                raise RuntimeError('Another install/removal changed the lock directory')
        finally:
            os.close(parent)
        yield
    finally:
        os.close(fd)


class _Transaction:
    def __init__(self, filesystem, previous=None):
        self.fs = filesystem
        self.relative = filesystem.paths.rel(filesystem.paths.data / ('.install-' + secrets.token_hex(12)))
        parent, name = filesystem.parent(self.relative, create=True)
        try:
            os.mkdir(name, 0o700, dir_fd=parent)
            os.fsync(parent)
            self.fd = os.open(name, _READ | os.O_DIRECTORY, dir_fd=parent)
        finally:
            os.close(parent)
        self.entries = {}
        self.saved = {}
        self.saved_records = {}
        self.published = {}
        self.groups = {}
        self.previous = previous
        self.phase = 'staging'
        self.committed = False
        try:
            self.journal()
        except BaseException:
            os.close(self.fd)
            raise

    def journal(self):
        value = {'schema': 2, 'transaction': self.relative, 'phase': self.phase,
                 'groups': {key: {name: value for name, value in group.items() if name != 'fd'}
                            for key, group in self.groups.items()},
                 'entries': self.entries, 'previous': self.previous, 'created': self.fs.created}
        payload = (json.dumps(value, indent=2, allow_nan=False) + '\n').encode()
        if len(payload) > MAX_RECOVERY_JSON:
            raise ValueError('Recovery journal exceeds its size limit')
        temporary = 'journal-' + secrets.token_hex(12)
        fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
                     0o600, dir_fd=self.fd)
        try:
            view = memoryview(payload)
            while view:
                view = view[os.write(fd, view):]
            os.fsync(fd)
        finally:
            os.close(fd)
        if hasattr(self, 'receipt'):
            current, _ = _record_at(self.fd, 'recovery.json')
            if current != self.receipt:
                raise ValueError('Preserving changed recovery journal')
        else:
            try:
                os.stat('recovery.json', dir_fd=self.fd, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                raise ValueError('Preserving unowned recovery journal')
        os.replace(temporary, 'recovery.json', src_dir_fd=self.fd, dst_dir_fd=self.fd)
        os.fsync(self.fd)
        self.receipt, _ = _record_at(self.fd, 'recovery.json')

    def group(self, relative):
        parent_relative = relative.rsplit('/', 1)[0]
        if parent_relative in self.groups:
            return self.groups[parent_relative]
        parent, _ = self.fs.parent(relative, create=True)
        name = '.install-' + secrets.token_hex(12)
        try:
            os.mkdir(name, 0o700, dir_fd=parent)
            os.fsync(parent)
            fd = os.open(name, _READ | os.O_DIRECTORY, dir_fd=parent)
        finally:
            os.close(parent)
        info = os.fstat(fd)
        group = {'fd': fd, 'relative': parent_relative + '/' + name,
                 'identity': [info.st_ino], 'mode': 0o700}
        self.groups[parent_relative] = group
        return group

    def reuse(self, relative, current):
        self.entries[relative] = dict(current, phase='reuse', stage=None)
        self.published[relative] = current
        self.journal()

    def stage(self, relative, stream, size, mode, expected_hash=None, chunks=None):
        _relative(relative)
        if relative in self.entries or len(self.entries) >= MAX_FILES:
            raise ValueError('Duplicate or excessive installation file')
        if not 0 <= size <= MAX_FILE_BYTES:
            raise ValueError('Input file exceeds declared size limit')
        group = self.group(relative)
        name = 'new-' + str(len(self.entries))
        fd = os.open(name, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
                     0o600, dir_fd=group['fd'])
        digest, used = hashlib.sha256(), 0
        try:
            info = os.fstat(fd)
            os.fsync(group['fd'])
            self.entries[relative] = {'stage': name, 'group': relative.rsplit('/', 1)[0], 'phase': 'writing',
                                      'sha256': expected_hash, 'size': size, 'mode': mode,
                                      'ino': info.st_ino, 'chunks': chunks or []}
            self.journal()
            while used <= size:
                chunk = stream.read(min(65536, size + 1 - used))
                if not chunk:
                    break
                if used + len(chunk) > size:
                    raise ValueError('Input file exceeds declared size')
                view = memoryview(chunk)
                while view:
                    written = os.write(fd, view)
                    if written <= 0:
                        raise OSError('Staging write made no progress')
                    digest.update(view[:written])
                    used += written
                    view = view[written:]
            if used != size or size > MAX_FILE_BYTES:
                raise ValueError('Input file size changed')
            value = digest.hexdigest()
            if expected_hash is not None and value != expected_hash:
                raise ValueError('Artifact changed after validation')
            os.fchmod(fd, mode)
            os.fsync(fd)
            os.fsync(group['fd'])
            self.entries[relative].update(phase='ready', identity=_file_identity(os.fstat(fd)))
            self.journal()
        except BaseException:
            try:
                current, _ = _record_at(group['fd'], name)
                if current['identity'][0] == os.fstat(fd).st_ino and current['size'] == used and current['sha256'] == digest.hexdigest():
                    os.unlink(name, dir_fd=group['fd'])
                    self.entries.pop(relative)
                    self.journal()
            except (OSError, ValueError, RuntimeError):
                pass
            raise
        finally:
            os.close(fd)

    def preflight(self, previous):
        owned = previous['files'] if previous else {}
        for relative in self.entries.keys() | owned.keys():
            self.fs.check_parents(relative)
            current = self.fs.record(relative)
            if current is not None and (relative not in owned or current != owned[relative]):
                raise ValueError('Preserving changed or unowned file: ' + relative)
        return owned

    def publish(self, owned):
        # New files, then the manifest that points at them, then obsolete files: Omarchy reloads the
        # plugin on every change, so an entry point must never name a missing or partial menu.
        manifest = self.fs.paths.rel(self.fs.paths.plugin / 'manifest.json')
        relatives = sorted(self.entries.keys() | owned.keys(), key=lambda relative: (
            2 if relative not in self.entries else int(relative == manifest), relative))
        for index, relative in enumerate(relatives):
            current = self.fs.record(relative)
            entry = self.entries.get(relative)
            if entry is not None and entry['phase'] == 'reuse':
                continue
            self.group(relative)
            if entry is None:
                entry = self.entries[relative] = {'stage': None, 'phase': 'delete',
                                                'group': relative.rsplit('/', 1)[0]}
            entry['old'] = current
            entry['backup'] = 'old-' + str(index) if current is not None else None
        self.phase = 'publishing'
        self.journal()
        for relative in relatives:
            entry = self.entries[relative]
            if entry['phase'] == 'reuse':
                continue
            parent, name = self.fs.parent(relative, create=True)
            group = self.groups[relative.rsplit('/', 1)[0]]
            try:
                if os.fstat(parent).st_dev != os.fstat(group['fd']).st_dev:
                    raise ValueError('Target filesystem changed during installation')
                current = self.fs.record(relative)
                if current is not None:
                    if current != owned.get(relative):
                        raise ValueError('File changed before update: ' + relative)
                    backup = entry['backup']
                    os.link(name, backup, src_dir_fd=parent, dst_dir_fd=group['fd'], follow_symlinks=False)
                    self.saved[relative] = {'name': backup, 'group': relative.rsplit('/', 1)[0]}
                    self.saved_records[relative] = current
                    info = os.stat(backup, dir_fd=group['fd'], follow_symlinks=False)
                    if _file_identity(info) != current['identity']:
                        raise ValueError('File changed during update: ' + relative)
                    saved, _ = _record_at(group['fd'], backup, allow_pair=True)
                    if saved != current:
                        raise ValueError('Backup changed before replacement: ' + relative)
                    os.fsync(group['fd'])  # The prior inode must survive before its target is replaced.
                    if _identity(os.stat(name, dir_fd=parent, follow_symlinks=False)) != _identity(os.stat(backup, dir_fd=group['fd'], follow_symlinks=False)):
                        raise ValueError('Target changed after its backup was created: ' + relative)
                if entry['phase'] == 'ready':
                    if current is None:
                        os.link(entry['stage'], name, src_dir_fd=group['fd'], dst_dir_fd=parent, follow_symlinks=False)
                        os.fsync(parent)
                        os.unlink(entry['stage'], dir_fd=group['fd'])
                    else:
                        os.replace(entry['stage'], name, src_dir_fd=group['fd'], dst_dir_fd=parent)
                    self.published[relative] = self.fs.record(relative)
                elif current is not None:
                    os.unlink(name, dir_fd=parent)
                os.fsync(group['fd'])
                os.fsync(parent)
            finally:
                os.close(parent)

    def rollback(self):
        retained = []
        for relative, expected in reversed(list(self.published.items())):
            if self.entries[relative]['phase'] == 'reuse':
                continue
            if relative in self.saved:
                continue
            try:
                self.fs.unlink_exact(relative, expected)
            except (OSError, ValueError):
                retained.append(relative)
        for relative, backup in reversed(list(self.saved.items())):
            parent = -1
            try:
                parent, name = self.fs.parent(relative)
                group = self.groups[backup['group']]
                saved, saved_instant = _record_at(group['fd'], backup['name'], allow_pair=True)
                if saved != self.saved_records[relative]:
                    raise ValueError('Refusing changed backup during rollback')
                try:
                    current, current_instant = _record_at(parent, name, allow_pair=True)
                except FileNotFoundError:
                    current = None
                if (_identity(os.stat(backup['name'], dir_fd=group['fd'], follow_symlinks=False)) != saved_instant
                        or (current is not None and _identity(os.stat(name, dir_fd=parent, follow_symlinks=False)) != current_instant)):
                    raise ValueError('Target or backup changed during rollback')
                if current == saved:
                    os.unlink(backup['name'], dir_fd=group['fd'])
                elif current is None or current == self.published.get(relative):
                    os.replace(backup['name'], name, src_dir_fd=group['fd'], dst_dir_fd=parent)
                else:
                    raise ValueError('Refusing to overwrite foreign file during rollback')
                os.fsync(group['fd'])
                os.fsync(parent)
            except (OSError, ValueError):
                retained.append(relative)
            finally:
                if parent >= 0:
                    os.close(parent)
        return retained

    def close(self, preserve=False):
        directories = {}
        try:
            if not preserve:
                expected = {key: {} for key in self.groups}
                for entry in self.entries.values():
                    if entry.get('stage') is not None:
                        expected[entry['group']][entry['stage']] = entry
                for relative, backup in self.saved.items():
                    expected[backup['group']][backup['name']] = self.saved_records[relative]
                containers = [(group['fd'], group['relative'], expected[key]) for key, group in self.groups.items()]
                containers.append((self.fd, self.relative, {'recovery.json': self.receipt} if hasattr(self, 'receipt') else {}))
                cleanup = []
                for container, relative, records in containers:
                    parent, name = self.fs.parent(relative)
                    try:
                        actual = os.stat(name, dir_fd=parent, follow_symlinks=False)
                        held = os.fstat(container)
                        if (not stat.S_ISDIR(actual.st_mode) or actual.st_uid != os.getuid()
                                or stat.S_IMODE(actual.st_mode) != 0o700
                                or (actual.st_dev, actual.st_ino) != (held.st_dev, held.st_ino)):
                            raise ValueError('Preserving replaced transaction directory: ' + relative)
                    finally:
                        os.close(parent)
                    names = []
                    # These descriptors were opened before staging/journal
                    # creation. Btrfs can retain that earlier directory view;
                    # enumerate a fresh description of the same held inode.
                    scan_fd = os.open('.', _READ | os.O_DIRECTORY, dir_fd=container)
                    try:
                        with os.scandir(scan_fd) as scan:
                            for entry in scan:
                                if len(names) >= MAX_FILES * 2:
                                    raise ValueError('Preserving excessive transaction contents: ' + relative)
                                names.append(entry.name)
                    finally:
                        os.close(scan_fd)
                    for name in names:
                        if name not in records:
                            raise ValueError('Preserving unexpected transaction file: ' + relative)
                        fd = os.open(name, _READ, dir_fd=container)
                        with os.fdopen(fd, 'rb') as stream:
                            info = _file_info(stream.fileno(), MAX_FILE_BYTES)
                            record = records[name]
                            if (info.st_size != record['size'] or stat.S_IMODE(info.st_mode) != record['mode']
                                    or ('identity' in record and _file_identity(info) != record['identity'])
                                    or _hash_stream(stream, info.st_size, MAX_FILE_BYTES) != record['sha256']):
                                raise ValueError('Preserving changed transaction file: ' + relative)
                        cleanup.append((container, name, _identity(info)))
                for container, name, instant in cleanup:
                    if container == self.fd:
                        groups = {group['relative']: {key: group[key] for key in ('identity', 'mode')}
                                  for group in self.groups.values()}
                        if self.fs.remove_empty(groups):
                            raise ValueError('Preserving changed transaction directory')
                    if _identity(os.stat(name, dir_fd=container, follow_symlinks=False)) != instant:
                        raise ValueError('Preserving transaction file changed during cleanup')
                    os.unlink(name, dir_fd=container)
                    os.fsync(container)
                info = os.fstat(self.fd)
                directories[self.relative] = {'identity': [info.st_ino], 'mode': 0o700}
                directories.update({group['relative']: {key: group[key] for key in ('identity', 'mode')}
                                    for group in self.groups.values()})
        finally:
            os.close(self.fd)
            for group in self.groups.values():
                os.close(group['fd'])
        if not preserve:
            if self.fs.remove_empty(directories):
                raise RuntimeError('Transaction cleanup retained changed or nonempty directories')


def _write_inventory(filesystem, value, expected=None, *, committed=None):
    relative = filesystem.paths.rel(filesystem.paths.inventory)
    payload = (json.dumps(value, indent=2, allow_nan=False) + '\n').encode()
    if len(payload) > MAX_JSON:
        raise ValueError('Installation inventory exceeds size limit')
    if filesystem.record(relative) != expected:
        raise ValueError('Installation inventory changed before commit')
    parent, name = filesystem.parent(relative, create=True)
    temporary = '.inventory-' + secrets.token_hex(12)
    try:
        fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
                     0o600, dir_fd=parent)
        try:
            view = memoryview(payload)
            while view:
                view = view[os.write(fd, view):]
            os.fsync(fd)
        finally:
            os.close(fd)
        if filesystem.record(relative) != expected:
            raise ValueError('Installation inventory changed during commit')
        # Defer interrupts across the commit point and its in-memory marker.
        # A durability error after rename must never roll published files back
        # underneath their new ownership ledger.
        with _rollback_signals():
            os.replace(temporary, name, src_dir_fd=parent, dst_dir_fd=parent)
            if committed is not None:
                committed()
            os.fsync(parent)
    finally:
        try:
            os.unlink(temporary, dir_fd=parent)
        except FileNotFoundError:
            pass
        os.close(parent)


def _directory_id(path):
    try:
        fd = _directory(path)
    except (FileNotFoundError, NotADirectoryError):
        return None
    try:
        info = os.fstat(fd)
        return info.st_dev, info.st_ino
    finally:
        os.close(fd)


def _overlap(left, right):
    if left == right or left in right.parents or right in left.parents:
        return True
    left_id, right_id = _directory_id(left), _directory_id(right)
    return ((left_id is not None and any(_directory_id(path) == left_id for path in (right, *right.parents)))
            or (right_id is not None and any(_directory_id(path) == right_id for path in (left, *left.parents))))


def _input_paths(paths, root, work, external):
    for value in (root, work):
        if value == paths.home or value in paths.home.parents:
            raise ValueError('Artifact input cannot contain supplied HOME')
    if external:
        if _directory_id(root) != _directory_id(paths.plugin):
            raise ValueError('Marketplace source must be the existing plugin checkout; use --private-copy for a private copy')
    elif _overlap(root, paths.plugin) and root != paths.plugin:
        raise ValueError('Copied plugin and source checkout cannot overlap')
    source_destinations = (paths.cache_home / 'live-quake', paths.data,
                           paths.config_home / 'systemd/user/live-quake.service',
                           paths.data_home / 'applications/live-quake.desktop')
    if any(_overlap(root, destination) for destination in source_destinations):
        raise ValueError('Source checkout overlaps installer-managed artifacts')
    if any(_overlap(work, destination) for destination in (paths.plugin, paths.build, paths.data)):
        raise ValueError('Build input overlaps installer-managed artifacts')


def _coordinator_path(paths, relative):
    try:
        relative = _relative(relative)
    except (TypeError, ValueError):
        return False
    path = paths.home / relative
    return path.parent == paths.data and re.fullmatch(r'\.install-[0-9a-f]{24}', path.name) is not None


def _transactions(filesystem):
    paths = filesystem.paths
    try:
        fd = _directory(paths.data)
    except FileNotFoundError:
        return []
    try:
        result, count = [], 0
        with os.scandir(fd) as scan:
            for entry in scan:
                count += 1
                if count > 4096:
                    raise ValueError('Installation data directory exceeds inspection bound')
                if re.fullmatch(r'\.install-[0-9a-f]{24}', entry.name) is None:
                    continue
                info = os.stat(entry.name, dir_fd=fd, follow_symlinks=False)
                if not stat.S_ISDIR(info.st_mode):
                    raise ValueError('Unexpected recovery coordinator type')
                child = os.open(entry.name, _READ | os.O_DIRECTORY, dir_fd=fd)
                try:
                    try:
                        os.stat('recovery.json', dir_fd=child, follow_symlinks=False)
                    except FileNotFoundError:
                        continue  # A per-parent group is referenced by its coordinator.
                    result.append(paths.rel(paths.data / entry.name))
                finally:
                    os.close(child)
        if len(result) > 32:
            raise ValueError('Too many interrupted installation transactions')
        return sorted(result)
    finally:
        os.close(fd)


def _require_recovered(filesystem, previous):
    found = _transactions(filesystem)
    try:
        fd = _directory(filesystem.paths.data)
    except FileNotFoundError:
        fd = -1
    if fd >= 0:
        try:
            found.extend(name for name in _names(fd, 4096) if re.fullmatch(r'\.install-[0-9a-f]{24}', name))
        finally:
            os.close(fd)
    if found or (previous and previous.get('pending')):
        raise RuntimeError('No changes were made. Review recover --dry-run, then run recover --yes before install or uninstall')


def _new_inventory(paths, external, provenance, previous=None):
    return {'schema': 2, 'plugin_id': PLUGIN_ID, 'home': str(paths.home), 'xdg': paths.environment(),
            'checkout_mode': 'external-read-only' if external else 'owned-copy',
            'files': dict(previous['files'] if previous else {}),
            'directories': dict(previous['directories'] if previous else {}), 'provenance': provenance,
            'service_enable_requested': False, 'service_start_requested': False,
            'service_state': 'unknown', 'manual_activation_required': True}


def _capture_owned_parents(filesystem, files, external):
    paths, result = filesystem.paths, {}
    roots = (paths.build, paths.data / 'librequake') + (() if external else (paths.plugin,))
    for relative in files:
        for target in (paths.home / relative).parents:
            if target == paths.home:
                break
            if not any(target == root or root in target.parents for root in roots):
                continue
            fd = _directory(target)
            try:
                info = os.fstat(fd)
                if info.st_uid == os.getuid() and stat.S_IMODE(info.st_mode) == 0o700:
                    result[paths.rel(target)] = {'identity': [info.st_ino], 'mode': 0o700}
            finally:
                os.close(fd)
    return result


def _restore_inventory(filesystem, previous, transaction):
    relative = filesystem.paths.rel(filesystem.paths.inventory)
    current = _inventory(filesystem)
    if current is not None and isinstance(current.get('pending'), dict) and current['pending'].get('transaction') == transaction.relative:
        expected = filesystem.record(relative)
        if previous is None:
            filesystem.unlink_exact(relative, expected)
        else:
            _write_inventory(filesystem, previous, expected)
    elif current != previous:
        raise ValueError('Preserving inventory changed during rollback')


@contextmanager
def _rollback_signals():
    previous = signal.pthread_sigmask(signal.SIG_BLOCK, set())
    try:
        signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGINT, signal.SIGTERM, signal.SIGHUP})
        yield
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, previous)


def install(paths, source_root, build_dir, archive_path, *, dry_run=False, expected_plan=None, private_copy=False):
    """Plan or install verified local artifacts; never activate the integration."""
    if not isinstance(paths, InstallationPaths):
        raise TypeError('Explicit InstallationPaths are required')
    root, work, archive = _source_root(source_root), _source_root(build_dir), _absolute(archive_path)
    external = not private_copy
    _input_paths(paths, root, work, external)
    sources, build = _sources(root), _build(root, work)
    with _source_file(archive, DATA_BYTES) as (archive_stream, archive_info):
        if archive_info.st_size != DATA_BYTES or _hash_stream(archive_stream, DATA_BYTES, DATA_BYTES) != DATA_SHA256:
            raise ValueError('LibreQuake archive does not match pinned length and SHA-256')
        archive_stream.seek(0)
        with zipfile.ZipFile(archive_stream) as package, _Home(paths) as filesystem:
            previous = _inventory(filesystem)
            _require_recovered(filesystem, previous)
            mode = 'external-read-only' if external else 'owned-copy'
            if previous and previous['checkout_mode'] != mode:
                raise ValueError('Existing checkout ownership mode differs; uninstall integration first')
            if private_copy and _directory_id(paths.plugin) is not None and previous is None:
                raise ValueError('Preserving unowned plugin checkout; use that checkout as marketplace --source')
            provenance = {'schema': 1, 'renderer': 'opengl',
                          'engine': {'url': IRON_URL,
                                     'commit': IRON_COMMIT},
                          'free_data': {'url': DATA_URL, 'sha256': DATA_SHA256, 'bytes': DATA_BYTES}, 'build_record': build}
            artifacts = _artifacts(paths, root, work, package, sources, build, provenance, external)
            plan = _install_plan(filesystem, artifacts, previous, external)
            if dry_run:
                return plan
            if expected_plan is not None and expected_plan != plan['fingerprint']:
                raise ValueError('No changes were made. The installation plan changed after confirmation')
            if plan['conflicts']:
                raise ValueError('Preserving changed or unowned file: ' + plan['conflicts'][0])
            failed = False
            try:
                with _lock(filesystem):
                    if _inventory(filesystem) != previous or _install_plan(filesystem, artifacts, previous, external) != plan:
                        raise ValueError('No changes were made. The installation plan changed before locking')
                    inventory_expected = filesystem.record(paths.rel(paths.inventory))
                    transaction = _Transaction(filesystem, previous)
                    preserve, failure = False, None
                    try:
                        for relative, artifact in artifacts.items():
                            current = filesystem.record(relative)
                            if current is not None and all(current[key] == artifact[key] for key in ('sha256', 'size', 'mode')):
                                transaction.reuse(relative, current)
                                continue
                            arguments = (relative, artifact['size'], artifact['mode'], artifact['sha256'], artifact['chunks'])
                            if artifact['kind'] == 'source':
                                with _source_file(artifact['path']) as (stream, _):
                                    transaction.stage(arguments[0], stream, *arguments[1:])
                            elif artifact['kind'] == 'archive':
                                with package.open(artifact['member']) as stream:
                                    transaction.stage(arguments[0], stream, *arguments[1:])
                            else:
                                transaction.stage(arguments[0], io.BytesIO(artifact['payload']), *arguments[1:])
                        owned = transaction.preflight(previous)
                        archive_stream.seek(0)
                        if (_identity(archive_info) != _identity(os.fstat(archive_stream.fileno()))
                                or _hash_stream(archive_stream, DATA_BYTES, DATA_BYTES) != DATA_SHA256):
                            raise ValueError('LibreQuake archive changed before installation')
                        value = _new_inventory(paths, external, provenance, previous)
                        pending = dict(value, pending={'transaction': transaction.relative})
                        _write_inventory(filesystem, pending, inventory_expected)
                        transaction.publish(owned)
                        value['files'] = transaction.published
                        for relative, record in _capture_owned_parents(filesystem, value['files'], external).items():
                            value['directories'].setdefault(relative, record)
                        value['transaction'] = transaction.relative
                        _write_inventory(filesystem, value, filesystem.record(paths.rel(paths.inventory)),
                                         committed=lambda: setattr(transaction, 'committed', True))
                    except BaseException as original:
                        if transaction.committed:
                            preserve = True
                            return dict(value, warnings=[
                                'Installation committed; final durability check was interrupted. '
                                'Review recover --dry-run, then recover --yes to verify files and clean recovery records.'])
                        failure = original
                        preserve = True  # Sticky until rollback AND inventory restoration complete.
                        try:
                            with _rollback_signals():
                                retained = transaction.rollback()
                                if not retained:
                                    _restore_inventory(filesystem, previous, transaction)
                            preserve = bool(retained)
                        except BaseException as rollback_error:
                            raise RuntimeError(type(original).__name__ + ': ' + str(original)
                                               + '; rollback interrupted (' + type(rollback_error).__name__ + '); recovery preserved at '
                                               + str(paths.home / transaction.relative)) from original
                        if preserve:
                            raise RuntimeError(type(original).__name__ + ': ' + str(original)
                                               + '; rollback preserved changed files and recovery transaction: '
                                               + str(paths.home / transaction.relative)) from original
                        raise
                    finally:
                        try:
                            transaction.close(preserve)
                        except BaseException as cleanup_error:
                            if failure is not None:
                                raise RuntimeError(type(failure).__name__ + ': ' + str(failure)
                                                   + '; transaction cleanup failed (' + type(cleanup_error).__name__ + ': '
                                                   + str(cleanup_error) + '); recovery preserved at '
                                                   + str(paths.home / transaction.relative)) from failure
                            raise
                    return _prune_directories(filesystem, value, plan['remove_empty_directories'])
            except BaseException:
                failed = True
                raise
            finally:
                if failed:
                    # All and only this run's still-empty created directories.
                    # This runs after releasing the directory flock.
                    filesystem.remove_empty(filesystem.created)


def _prune_directories(filesystem, value, obsolete):
    """After commit and staging cleanup, remove the plan's obsolete owned folders when empty."""
    records = {relative: value['directories'][relative] for relative in obsolete if relative in value['directories']}
    if not records:
        return value
    try:
        retained = filesystem.remove_empty(records)
        pruned = dict(value, directories={relative: record for relative, record in value['directories'].items()
                                          if relative not in records or relative in retained})
        _write_inventory(filesystem, pruned, filesystem.record(filesystem.paths.rel(filesystem.paths.inventory)))
    except (OSError, ValueError) as error:
        return dict(value, warnings=['Installed; obsolete owned folders were not all removed: ' + str(error)])
    return pruned


def _unowned_leftovers(filesystem, directories, owned):
    unowned = []
    for relative in directories:
        try:
            fd = _directory(filesystem.paths.home / relative)
        except OSError:
            continue
        try:
            with os.scandir(fd) as scan:
                count = 0
                for entry in scan:
                    count += 1
                    if count > 4096:
                        raise ValueError('Retained directory exceeds inspection bound')
                    text = relative + '/' + entry.name
                    if text not in owned and text not in directories:
                        unowned.append(text)
                        if len(unowned) >= 20:
                            return sorted(unowned)
        finally:
            os.close(fd)
    return sorted(unowned)


def uninstall(paths, *, dry_run=False, expected_plan=None):
    """Remove exact owned artifacts; service retirement always requires manual work."""
    if not isinstance(paths, InstallationPaths):
        raise TypeError('Explicit InstallationPaths are required')
    with _Home(paths) as filesystem:
        previous = _inventory(filesystem)
        _require_recovered(filesystem, previous)
        plan = _uninstall_plan(filesystem, previous)
        if dry_run:
            return plan
        if expected_plan is not None and expected_plan != plan['fingerprint']:
            raise ValueError('No changes were made. The removal plan changed after confirmation')
        if plan['blockers']:
            raise ValueError('; '.join(plan['blockers']) + '; run /usr/bin/systemctl --user disable --now live-quake.service '
                             'and disable the Omarchy plugin before uninstall')
        def before_release(*, runtime_held):
            # Plan checks must precede ownership changes, under the actual
            # runtime lock when one exists. Never infer a lock from an outcome.
            if (_inventory(filesystem) != previous
                    or _uninstall_plan(filesystem, previous, runtime_held=runtime_held) != plan):
                raise ValueError('No changes were made. The removal plan changed before locking; '
                                 'review uninstall --dry-run and retry')

        def require_cleanup(cleanup):
            if cleanup['safe']:
                return
            if cleanup['outcome'] == 'busy':
                raise ValueError('No changes were made. Live Quake controller is running; '
                                 'stop Live Quake before uninstall')
            prefix = ('The screensaver claim was released; no installation files were removed. '
                      if cleanup['released'] else '')
            raise ValueError(prefix + 'Screensaver ownership cleanup is unavailable; preserve the installation. '
                             'Stop Live Quake, verify --runtime-dir, then review uninstall --dry-run '
                             'and the documented screensaver recovery')

        runtime = paths.runtime_dir / 'live-quake' if paths.runtime_dir is not None else None
        if previous is None:
            result = {'removed': [], 'preserved': [], 'artifacts_removed': False, 'installation_remaining': False,
                      'installed': False, 'retained_directories': [], 'unowned': [],
                      'inventory_checked': paths.rel(paths.inventory), 'same_xdg_flags_required': True,
                      'running_controller_checked': paths.runtime_dir is not None, **_retirement_notice()}
            from idle import SaverOwnership
            ownership = SaverOwnership(paths.home, paths.state_home / 'live-quake').cleanup_status()
            if not ownership['safe']:
                raise ValueError('Screensaver ownership is unverifiable; preserve the installation')
            if ownership['recorded']:
                from idle_cleanup import cleanup_guard
                with cleanup_guard(paths.home, paths.state_home / 'live-quake', runtime,
                                   purpose='uninstall', before_release=before_release) as cleanup:
                    require_cleanup(cleanup)
                    result['screensaver_cleanup'] = cleanup
                    if cleanup.get('note'):
                        result['warnings'] = [cleanup['note']]
            return result
        from idle_cleanup import cleanup_guard
        with _lock(filesystem, create=False), cleanup_guard(paths.home, paths.state_home / 'live-quake', runtime,
                                                         purpose='uninstall', before_release=before_release) as cleanup:
            require_cleanup(cleanup)
            if _inventory(filesystem) != previous:
                prefix = ('The screensaver claim was released; no installation files were removed. '
                          if cleanup['released'] else 'No changes were made. ')
                raise ValueError(prefix + 'The removal plan changed before locking; review uninstall --dry-run and retry')
            inventory_expected = filesystem.record(paths.rel(paths.inventory))
            removed, preserved, remaining = [], [], {}
            ordered = sorted(previous['files'], key=lambda relative: (
                paths.plugin in (paths.home / relative).parents,
                relative != paths.rel(paths.plugin / 'manifest.json'), relative))
            for relative in ordered:
                expected = previous['files'][relative]
                try:
                    current = filesystem.record(relative)
                    if current is None:
                        continue
                    if current != expected:
                        preserved.append(relative)
                        remaining[relative] = expected
                        continue
                    filesystem.unlink_exact(relative, expected)
                    removed.append(relative)
                except (OSError, ValueError):
                    preserved.append(relative)
                    remaining[relative] = expected
            directories = filesystem.remove_empty(previous['directories'])
            unowned = _unowned_leftovers(filesystem, directories, remaining)
            if remaining or directories:
                previous.update(files=remaining, directories=directories, preserved=preserved)
                _write_inventory(filesystem, previous, inventory_expected)
            else:
                filesystem.unlink_exact(paths.rel(paths.inventory), inventory_expected)
            return {'removed': removed, 'preserved': preserved, 'artifacts_removed': bool(removed),
                    'installation_remaining': bool(remaining or directories), 'installed': bool(remaining),
                    'retained_directories': sorted(directories), 'unowned': unowned,
                    'inventory_checked': paths.rel(paths.inventory), 'same_xdg_flags_required': True,
                    'running_controller_checked': paths.runtime_dir is not None,
                    'screensaver_cleanup': cleanup,
                    **({'warnings': [cleanup['note']]} if cleanup.get('note') else {}), **_retirement_notice()}


def _read_recovery(filesystem, relative):
    parent, name = filesystem.parent(relative + '/recovery.json')
    try:
        fd = os.open(name, _READ, dir_fd=parent)
        with os.fdopen(fd, 'rb') as stream:
            info = _file_info(stream.fileno(), MAX_RECOVERY_JSON)
            payload = stream.read(MAX_RECOVERY_JSON + 1)
            if len(payload) != info.st_size or _identity(info) != _identity(os.fstat(stream.fileno())):
                raise ValueError('Recovery journal changed while reading')
            receipt = {'sha256': hashlib.sha256(payload).hexdigest(), 'size': info.st_size,
                       'mode': stat.S_IMODE(info.st_mode), 'identity': _file_identity(info)}
    finally:
        os.close(parent)
    value = json.loads(payload)
    if (not isinstance(value, dict) or set(value) != {'schema', 'transaction', 'phase', 'groups', 'entries', 'previous', 'created'}
            or value['schema'] != 2 or value['transaction'] != relative or value['phase'] not in ('staging', 'publishing')
            or not isinstance(value['groups'], dict) or len(value['groups']) > MAX_FILES
            or not isinstance(value['entries'], dict) or len(value['entries']) > MAX_FILES
            or not isinstance(value['created'], dict) or len(value['created']) > MAX_FILES * 2 or receipt['mode'] != 0o600):
        raise ValueError('Invalid recovery journal')
    if value['previous'] is not None:
        value['previous'] = _validate_inventory(filesystem, value['previous'])
    external = bool(value['previous'] and value['previous']['checkout_mode'] == 'external-read-only')
    for key, group in value['groups'].items():
        _relative(key)
        if (not isinstance(group, dict) or set(group) != {'relative', 'identity', 'mode'}
                or not isinstance(group['relative'], str) or group['relative'].rsplit('/', 1)[0] != key
                or re.fullmatch(r'\.install-[0-9a-f]{24}', group['relative'].rsplit('/', 1)[-1]) is None
                or not _directory_record(group)):
            raise ValueError('Invalid recovery staging directory')
        _relative(group['relative'])
        parent = filesystem.paths.home / key
        if (not _managed(filesystem.paths, key + '/.probe', external)
                and parent not in (filesystem.paths.data, filesystem.paths.config_home / 'systemd/user',
                                   filesystem.paths.data_home / 'applications')):
            raise ValueError('Recovery staging parent is outside installer-managed locations')
    names = set()
    for relative, entry in value['entries'].items():
        if (not _managed(filesystem.paths, relative, external) or not isinstance(entry, dict)
                or entry.get('phase') not in ('writing', 'ready', 'reuse', 'delete')
                or set(entry) - {'sha256', 'size', 'mode', 'identity', 'phase', 'stage', 'group', 'ino', 'chunks', 'old', 'backup'}):
            raise ValueError('Invalid recovery target')
        if entry['phase'] == 'reuse':
            _validate_file_record({key: entry.get(key) for key in ('sha256', 'size', 'mode', 'identity')})
            if entry.get('stage') is not None:
                raise ValueError('Invalid recovery reused file')
            continue
        if value['phase'] == 'staging' and ('old' in entry or 'backup' in entry or entry['phase'] == 'delete'):
            raise ValueError('Staging journal cannot contain publication records')
        if value['phase'] == 'publishing' and entry['phase'] == 'writing':
            raise ValueError('Publishing journal cannot contain an incomplete staged file')
        if entry.get('group') != relative.rsplit('/', 1)[0] or entry['group'] not in value['groups']:
            raise ValueError('Invalid recovery target parent')
        if entry['phase'] in ('ready', 'writing'):
            _validate_file_record({key: entry.get(key) for key in ('sha256', 'size', 'mode')}, identity=False)
            if (not isinstance(entry.get('stage'), str) or re.fullmatch(r'new-[0-9]{1,4}', entry['stage']) is None
                    or type(entry.get('ino')) is not int or entry['ino'] < 0
                    or not isinstance(entry.get('chunks'), list) or len(entry['chunks']) != (entry.get('size', -1) + 65535) // 65536
                    or any(not _digest(chunk) for chunk in entry['chunks'])):
                raise ValueError('Invalid recovery staged file')
            if entry['phase'] == 'ready':
                _validate_file_record({key: entry.get(key) for key in ('sha256', 'size', 'mode', 'identity')})
                if entry['identity'][0] != entry['ino']:
                    raise ValueError('Invalid recovery staged identity')
        elif entry.get('stage') is not None:
            raise ValueError('Invalid recovery deletion')
        if 'old' in entry and entry['old'] is not None:
            _validate_file_record(entry['old'])
            if value['previous'] is None or entry['old'] != value['previous']['files'].get(relative):
                raise ValueError('Recovery prior ownership differs from inventory')
        if entry.get('backup') is not None and (entry.get('old') is None or not isinstance(entry['backup'], str)
                                               or re.fullmatch(r'old-[0-9]{1,4}', entry['backup']) is None):
            raise ValueError('Invalid recovery backup')
        for name in (entry.get('stage'), entry.get('backup')):
            if name is not None:
                pair = (entry['group'], name)
                if pair in names:
                    raise ValueError('Ambiguous recovery staging or backup name')
                names.add(pair)
    allowed = [filesystem.paths.home / key for key in value['entries']]
    allowed.append(filesystem.paths.inventory)
    for relative, record in value['created'].items():
        target = filesystem.paths.home / _relative(relative)
        if not _directory_record(record) or not any(target in item.parents for item in allowed):
            raise ValueError('Invalid recovery created directory')
    return value, receipt


def _digest(value):
    return isinstance(value, str) and re.fullmatch(r'[0-9a-f]{64}', value) is not None


def _directory_record(value):
    return (isinstance(value, dict) and isinstance(value.get('identity'), list) and len(value['identity']) == 1
            and type(value['identity'][0]) is int and value['identity'][0] >= 0 and value.get('mode') == 0o700)


def _names(fd, limit):
    names = []
    with os.scandir(fd) as scan:
        for entry in scan:
            if len(names) >= limit:
                raise ValueError('Recovery directory exceeds its inspection bound')
            names.append(entry.name)
    return names


def _validate_file_record(record, identity=True):
    if (not isinstance(record, dict) or set(record) != ({'sha256', 'size', 'mode', 'identity'} if identity else {'sha256', 'size', 'mode'})
            or not _digest(record.get('sha256')) or type(record.get('size')) is not int or not 0 <= record['size'] <= MAX_FILE_BYTES
            or type(record.get('mode')) is not int or record['mode'] not in (0o600, 0o644, 0o755)
            or (identity and (not isinstance(record.get('identity'), list) or len(record['identity']) != 2
                             or any(type(item) is not int or item < 0 for item in record['identity'])))):
        raise ValueError('Invalid recovery file record')


def _recovery_record(filesystem, relative):
    try:
        parent, name = filesystem.parent(relative)
    except FileNotFoundError:
        return None
    try:
        try:
            return _record_at(parent, name, allow_pair=True)[0]
        except FileNotFoundError:
            return None
    finally:
        os.close(parent)


def _partial_stage(filesystem, relative, entry, record):
    if entry['phase'] != 'writing':
        return record == {key: entry[key] for key in ('sha256', 'size', 'mode', 'identity')}
    if record['identity'][0] != entry['ino'] or record['size'] > entry['size']:
        return False
    if record['size'] == entry['size'] and record['sha256'] == entry['sha256'] and record['mode'] in (0o600, entry['mode']):
        return True
    if record['mode'] != 0o600 or record['size'] % 65536:
        return False  # An interrupted partial chunk cannot be authenticated.
    parent, name = filesystem.parent(relative)
    try:
        fd = os.open(name, _READ, dir_fd=parent)
        with os.fdopen(fd, 'rb') as stream:
            for chunk in entry['chunks'][:record['size'] // 65536]:
                if hashlib.sha256(stream.read(65536)).hexdigest() != chunk:
                    return False
            return not stream.read(1)
    finally:
        os.close(parent)


def _recovery_plan(filesystem):
    paths, previous = filesystem.paths, _inventory(filesystem)
    transactions, actions, conflicts, touched, referenced = [], [], [], set(), set()
    changed_targets = set()
    def committed_record(target):
        try:
            return _recovery_record(filesystem, target)
        except (OSError, ValueError):
            # Committed recovery never writes this target. A user replacement
            # is preserved; only exact-owned recovery files are cleaned.
            return None
    def same_contents(first, second):
        return (first is not None and second is not None
                and all(first[key] == second[key] for key in ('sha256', 'size', 'mode')))
    for relative in _transactions(filesystem):
        value, receipt = _read_recovery(filesystem, relative)
        coordinator = _directory(paths.home / relative)
        try:
            info = os.fstat(coordinator)
            if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
                raise ValueError('Recovery coordinator must remain private')
            if set(_names(coordinator, MAX_FILES * 2)) != {'recovery.json'}:
                conflicts.append(relative + ': unexpected coordinator contents')
            coordinator_record = {'identity': [info.st_ino], 'mode': 0o700}
        finally:
            os.close(coordinator)
        committed = bool(previous and previous.get('transaction') == relative and not previous.get('pending'))
        committed_targets = {}
        if committed:
            for target, expected in previous['files'].items():
                current = committed_record(target)
                committed_targets[target] = current
                if current != expected:
                    changed_targets.add(target)
        pending = bool(previous and isinstance(previous.get('pending'), dict) and previous['pending'].get('transaction') == relative)
        if not committed and not pending and previous != value['previous']:
            conflicts.append(relative + ': inventory changed outside the transaction')
        cleanup = []
        expected_names = {key: set() for key in value['groups']}
        for target, entry in value['entries'].items():
            if target in touched:
                raise ValueError('Recovery transactions overlap')
            touched.add(target)
            if entry['phase'] == 'reuse':
                continue
            new = {key: entry[key] for key in ('sha256', 'size', 'mode', 'identity')} if entry['phase'] == 'ready' else None
            old = entry.get('old')
            current = (committed_targets.get(target) if target in committed_targets
                       else committed_record(target) if committed else _recovery_record(filesystem, target))
            group = value['groups'][entry['group']]
            stage = group['relative'] + '/' + entry['stage'] if entry.get('stage') else None
            backup = group['relative'] + '/' + entry['backup'] if entry.get('backup') else None
            staged = _recovery_record(filesystem, stage) if stage else None
            saved = _recovery_record(filesystem, backup) if backup else None
            if staged is not None:
                expected_names[entry['group']].add(entry['stage'])
                if not _partial_stage(filesystem, stage, entry, staged):
                    conflicts.append(stage + ': staged bytes or identity changed')
                else:
                    cleanup.append({'action': 'remove-owned-staging-file', 'path': stage, 'expected': staged})
            if saved is not None:
                expected_names[entry['group']].add(entry['backup'])
                if saved != old:
                    conflicts.append(backup + ': backup bytes or identity changed')
                else:
                    cleanup.append({'action': 'remove-owned-backup', 'path': backup, 'expected': saved})
            if committed:
                if (new is not None and previous['files'].get(target) != new) or (entry['phase'] == 'delete' and target in previous['files']):
                    conflicts.append(target + ': committed inventory differs from journal')
                if same_contents(current, old) and not same_contents(new, old):
                    conflicts.append(target + ': previous contents found under committed inventory')
                elif (entry['phase'] == 'delete' and current is not None):
                    changed_targets.add(target)
                continue
            if value['phase'] == 'staging':
                continue  # No target publication can occur before the publishing journal.
            if old is not None:
                if current == old:
                    continue
                if current is None or current == new:
                    if saved == old:
                        actions.append({'action': 'restore-owned-backup', 'path': target, 'from': backup,
                                        'expected': current, 'backup_expected': saved})
                    else:
                        conflicts.append(target + ': prior backup is unavailable')
                else:
                    conflicts.append(target + ': preserve external edit and prior backup')
            elif current is not None:
                if new is not None and current == new:
                    actions.append({'action': 'remove-interrupted-new-file', 'path': target, 'expected': current})
                else:
                    conflicts.append(target + ': preserve unexpected target')
        for key, group in value['groups'].items():
            referenced.add(group['relative'])
            try:
                fd = _directory(paths.home / group['relative'])
            except FileNotFoundError:
                continue
            try:
                info = os.fstat(fd)
                if info.st_uid != os.getuid() or {'identity': [info.st_ino], 'mode': stat.S_IMODE(info.st_mode)} != {key: group[key] for key in ('identity', 'mode')}:
                    conflicts.append(group['relative'] + ': staging directory replaced')
                if set(_names(fd, MAX_FILES * 2)) != expected_names[key]:
                    conflicts.append(group['relative'] + ': unexpected staging contents')
            finally:
                os.close(fd)
        transactions.append({'path': relative, 'journal': receipt, 'directory': coordinator_record,
                             'committed': committed, 'pending': pending, 'value': value, 'cleanup': cleanup})
    if previous and previous.get('pending') and previous['pending']['transaction'] not in {tx['path'] for tx in transactions}:
        conflicts.append('Pending inventory has no recoverable journal')
    # Never adopt an unjournaled staging directory, including an interrupted first journal.
    try:
        fd = _directory(paths.data)
    except FileNotFoundError:
        fd = -1
    if fd >= 0:
        try:
            for name in _names(fd, 4096):
                if re.fullmatch(r'\.install-[0-9a-f]{24}', name):
                    relative = paths.rel(paths.data / name)
                    if relative not in referenced and relative not in {tx['path'] for tx in transactions}:
                        conflicts.append(relative + ': unjournaled recovery directory requires manual inspection')
        finally:
            os.close(fd)
    plan = {'schema': 1, 'operation': 'recover', 'actions': actions, 'transactions': transactions,
            'conflicts': sorted(set(conflicts)), 'changed_targets': sorted(changed_targets),
            'service_commands_performed': []}
    plan['fingerprint'] = _fingerprint(plan)
    return plan


def _recovery_unlink(filesystem, relative, expected):
    parent, name = filesystem.parent(relative)
    try:
        actual, instant = _record_at(parent, name, allow_pair=True)
        if actual != expected or _identity(os.stat(name, dir_fd=parent, follow_symlinks=False)) != instant:
            raise ValueError('Preserving changed recovery file: ' + relative)
        os.unlink(name, dir_fd=parent)
        os.fsync(parent)
    finally:
        os.close(parent)


def recover(paths, *, dry_run=False, expected_plan=None):
    """Explicitly undo interrupted publication, or clean an already committed journal.

    Conflicts cause no writes. Incomplete last chunks and unjournaled directories
    require manual inspection; recovery never guesses at their ownership.
    """
    if not isinstance(paths, InstallationPaths):
        raise TypeError('Explicit InstallationPaths are required')
    with _Home(paths) as filesystem:
        plan = _recovery_plan(filesystem)
        if dry_run:
            return plan
        if expected_plan is not None and expected_plan != plan['fingerprint']:
            raise ValueError('No changes were made. The recovery plan changed after confirmation')
        if plan['conflicts']:
            raise ValueError('No changes were made. ' + '; '.join(plan['conflicts']))
        if not plan['transactions']:
            return {'recovered': [], 'recovery_remaining': False, 'service_commands_performed': []}
        with _lock(filesystem, create=False):
            if _recovery_plan(filesystem) != plan:
                raise ValueError('No changes were made. The recovery plan changed before locking')
            for action in plan['actions']:
                if action['action'] == 'remove-interrupted-new-file':
                    _recovery_unlink(filesystem, action['path'], action['expected'])
                    continue
                source, source_name = filesystem.parent(action['from'])
                target, target_name = filesystem.parent(action['path'])
                try:
                    saved, instant = _record_at(source, source_name, allow_pair=True)
                    if (saved != action['backup_expected'] or _recovery_record(filesystem, action['path']) != action['expected']
                            or _identity(os.stat(source_name, dir_fd=source, follow_symlinks=False)) != instant):
                        raise ValueError('Preserving changed target or backup during recovery')
                    os.replace(source_name, target_name, src_dir_fd=source, dst_dir_fd=target)
                    os.fsync(source)
                    os.fsync(target)
                finally:
                    os.close(source)
                    os.close(target)
            for transaction in plan['transactions']:
                if transaction['pending']:
                    current = filesystem.record(paths.rel(paths.inventory))
                    if transaction['value']['previous'] is None:
                        filesystem.unlink_exact(paths.rel(paths.inventory), current)
                    else:
                        _write_inventory(filesystem, transaction['value']['previous'], current)
                for action in transaction['cleanup']:
                    current = _recovery_record(filesystem, action['path'])
                    if current is not None:
                        _recovery_unlink(filesystem, action['path'], action['expected'])
                directories = {group['relative']: {key: group[key] for key in ('identity', 'mode')}
                               for group in transaction['value']['groups'].values()}
                if filesystem.remove_empty(directories):
                    raise RuntimeError('Recovery retained changed or nonempty staging directories')
                _recovery_unlink(filesystem, transaction['path'] + '/recovery.json', transaction['journal'])
                if filesystem.remove_empty({transaction['path']: transaction['directory']}):
                    raise RuntimeError('Recovery retained a changed coordinator directory')
        # Removing the lock directory while holding its flock would permit a
        # second process to lock a new inode at the same path.
        for transaction in plan['transactions']:
            filesystem.remove_empty(transaction['value']['created'])
        return {'recovered': [transaction['path'] for transaction in plan['transactions']],
                'recovery_remaining': False, 'changed_targets': plan['changed_targets'], 'service_commands_performed': []}


def _print_plan(plan):
    print('Live Quake ' + plan['operation'] + ' plan (paths relative to the supplied HOME):')
    for relative in plan.get('create_directories', []):
        print('  create directory 0700 ' + relative)
    for entry in plan.get('entries', plan.get('actions', [])):
        print('  ' + entry['action'] + ' ' + entry['path'])
    if plan['operation'] == 'recover':
        for target in plan['changed_targets']:
            print('  preserve changed or missing target ' + target)
        for transaction in plan['transactions']:
            for entry in transaction['cleanup']:
                print('  ' + entry['action'] + ' ' + entry['path'])
            print('  remove owned recovery directories ' + transaction['path'])
    if plan['operation'] == 'install':
        for relative in plan['remove_empty_directories']:
            print('  remove obsolete owned directory if empty ' + relative)
        print('  renderer: ' + plan['renderer'])
        print('  write inventory 0600 ' + plan['inventory_path'])
        print('  create private staging in destination parents, then remove verified staging')
        print('  write the unit; do not enable or start the service; its state remains unverified')
        if plan['unit_changed']:
            print('  unit changed: run systemctl --user daemon-reload')
        print('  the desktop entry appears in the launcher immediately')
        print('  checkout ownership: ' + plan['checkout_mode'])
    elif plan['operation'] == 'uninstall':
        if plan.get('screensaver_preservation_note'):
            print('  ' + plan['screensaver_preservation_note'])
        for relative in plan['cleanup_empty_owned_directories']:
            print('  remove directory only if unchanged and empty ' + relative)
        print('  remove inventory if cleanup is complete; otherwise retain partial inventory ' + plan['inventory_path'])
        print('  prerequisite: /usr/bin/systemctl --user disable --now live-quake.service')
        print('  disable the Omarchy plugin, then remove its checkout only after uninstall')
        print('  plugin prerequisite: omarchy plugin disable ' + PLUGIN_ID)
        print('  service retirement is unverified; this installer runs no service commands')
        if not plan['running_controller_checked']:
            print('  running controller check skipped: no --runtime-dir supplied')
    for conflict in plan.get('conflicts', []) + plan.get('blockers', []):
        print('  BLOCKED: ' + conflict)
    print('Plan SHA-256: ' + plan['fingerprint'])


def _shell_query(timeout):
    result = subprocess.run(['/usr/bin/omarchy-shell', 'shell', 'call', PLUGIN_ID, 'loadedVersion', ''],
                            capture_output=True, text=True, timeout=timeout, check=False)
    return result.stdout.strip() if result.returncode == 0 else 'unavailable'


def _menu_reload_hint(paths, plan, query=_shell_query, session_home=None, wait=5.0):
    """After a real install that moved the menu, ask the session's shell which menu it runs.

    Read-only and only for the account's own HOME (never a private test HOME); the shell is never
    restarted. Returns one hint line, or None when the new menu is loaded or nothing was checked.
    """
    plugin = paths.rel(paths.plugin) + '/'
    created = [entry['path'] for entry in plan['entries'] if entry['action'] == 'create'
               and re.fullmatch(re.escape(plugin) + r'impl-[0-9a-f]{64}/Menu\.qml', entry['path'])]
    moved = any(entry['action'] == 'remove-obsolete-owned-file' and entry['path'].startswith(plugin)
                and entry['path'].endswith('/Menu.qml') for entry in plan['entries'])
    home = session_home if session_home is not None else Path(pwd.getpwuid(os.getuid()).pw_dir)
    if not created or not moved or paths.home != home:
        return None
    expected, deadline, reply = str((paths.home / created[0]).parent), time.monotonic() + wait, 'unknown'
    while True:  # Omarchy reloads the plugin about 150 ms after the last file change.
        try:
            reply = query(2)
        except (OSError, subprocess.SubprocessError):
            reply = 'unavailable'
        if reply.rstrip('/') == expected:
            return None
        if time.monotonic() >= deadline:
            if reply == 'unavailable':  # Not running, plugin disabled, or an old menu without loadedVersion.
                return ('Could not confirm the shell loaded the new Live Quake menu; if the menu looks old, '
                        'restart the shell: omarchy restart shell')
            return ('The running shell still has an older Live Quake menu (' + reply[:200]
                    + '); if the plugin is enabled, restart the shell: omarchy restart shell')
        time.sleep(0.25)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--home', required=True, type=Path)
    for name in ('config', 'data', 'state', 'cache'):
        parser.add_argument('--xdg-' + name + '-home', type=Path)
    parser.add_argument('--runtime-dir', type=Path, help='Explicit private runtime directory to check for a running controller')
    parser.add_argument('--dry-run', action='store_true', help='Print the exact plan without creating files or directories')
    parser.add_argument('--yes', action='store_true', help='Confirm the printed file mutations; does not authorize service commands')
    commands = parser.add_subparsers(dest='command', required=True)
    add = commands.add_parser('install', help='Install verified existing artifacts; do not start the service')
    add.add_argument('--source', required=True, type=Path)
    add.add_argument('--build', required=True, type=Path)
    add.add_argument('--archive', required=True, type=Path)
    add.add_argument('--private-copy', action='store_true', help='Copy a development source into an absent, installer-owned private checkout')
    remove = commands.add_parser('uninstall', help='Run before removing the plugin checkout; preserve changed files')
    repair = commands.add_parser('recover', help='Explicitly recover interrupted installer publication; preserve unexpected edits')
    for command in (add, remove, repair):
        command.add_argument('--dry-run', action='store_true', default=argparse.SUPPRESS)
        command.add_argument('--yes', action='store_true', default=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        paths = InstallationPaths(args.home, args.xdg_config_home, args.xdg_data_home,
                                  args.xdg_state_home, args.xdg_cache_home, args.runtime_dir)
        if args.command == 'install':
            operation = lambda **kwargs: install(paths, args.source, args.build, args.archive,
                                                  private_copy=args.private_copy, **kwargs)
        else:
            operation = lambda **kwargs: (uninstall if args.command == 'uninstall' else recover)(paths, **kwargs)
        plan = operation(dry_run=True)
        _print_plan(plan)
        if args.dry_run:
            return 0
        if plan.get('conflicts') or plan.get('blockers'):
            raise ValueError('No changes were made. Resolve the blockers in the printed plan first')
        if not args.yes:
            if not sys.stdin.isatty():
                raise ValueError('No changes were made. Use --yes or confirm interactively at a terminal')
            if input('Apply exactly this file plan? [y/N] ').strip().casefold() not in ('y', 'yes'):
                print('No changes were made.')
                return 1
        result = operation(expected_plan=plan['fingerprint'])
        print(json.dumps(result, allow_nan=False))
        for warning in result.get('warnings', []):
            print(warning, file=sys.stderr)
        hint = _menu_reload_hint(paths, plan) if args.command == 'install' else None
        if hint:
            print(hint, file=sys.stderr)
        if args.command == 'uninstall' and not result['removed'] and not result['installation_remaining']:
            print('No inventory-owned artifacts found at ' + str(paths.inventory)
                  + '; use the same --xdg-*-home flags as installation.', file=sys.stderr)
        if result.get('retained_directories'):
            print('Live Quake kept changed or unowned contents; see retained_directories, preserved and unowned in the result.', file=sys.stderr)
        return 0
    except (OSError, ValueError, RuntimeError, zipfile.BadZipFile) as error:
        print('Live Quake installation failed: ' + str(error), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
