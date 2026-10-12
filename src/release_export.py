# SPDX-License-Identifier: 0BSD
"""Validate and export a quiet source candidate to a new local directory.

The destination is an explicit existing parent, never a replacement target.
Preflight snapshots every allowed file before mutation. No network, subprocess,
Git operations, environment-derived paths, builds or game-data copies occur.
The caller supplies the full source commit; this module checks its syntax, not
whether a working tree matches it. Export from a clean committed clone.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat
import struct
import sys

PLUGIN_ID = 'io.github.slogonomo.live-quake'
INVENTORY = 'source-inventory.json'
MAX_FILES = 256
MAX_DIRECTORY_ENTRIES = 512
MAX_TEXT_BYTES = 2 * 1024 * 1024
MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_TOTAL_BYTES = 32 * 1024 * 1024
_READ = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK
LICENSE_EXPRESSION = '0BSD AND CC0-1.0 AND GPL-2.0-or-later AND BSD-3-Clause AND MIT AND HPND-sell-variant AND LicenseRef-FrikBot-X'
SOURCE_FILES = (
    'manifest.json', 'README.md', 'LICENSE', 'THIRD-PARTY-NOTICES.md', 'preview.png',
    'LICENSES/README.md', 'LICENSES/0BSD.txt', 'LICENSES/CC0-1.0.txt', 'LICENSES/GPL-2.0-or-later.txt',
    'LICENSES/BSD-3-Clause-LibreQuake.txt', 'LICENSES/LicenseRef-FrikBot-X.txt', 'assets/README.md', 'assets/live-quake.webp',
    'docs/architecture.md', 'docs/engine-choice.md', 'docs/free-data.md', 'docs/steam-games.md',
    'menu/Menu.qml', 'menu/MenuCard.qml', 'menu/MenuFloat.qml', 'menu/MenuSounds.qml', 'menu/MenuWindow.qml',
    'menu/QuakeSlider.qml', 'menu/QuakeText.qml', 'menu/TextBox.qml', 'menu/pakart.py', 'menu/README.md',
    'scripts/quakectl', 'scripts/install.py', 'scripts/bootstrap.py', 'scripts/build.py', 'scripts/build-frikbot-local.py',
    'scripts/build-viewer.sh', 'scripts/export-release.py',
    'src/controller.py', 'src/onboarding.py', 'src/daemon.py', 'src/data_farm.py', 'src/desktop.py', 'src/frame.py',
    'src/policy.py', 'src/policy_log.py', 'src/game_catalog.py', 'src/bot_watchdog.py', 'src/steam_games.py', 'src/background.py', 'src/background_controller.py',
    'src/install.py', 'src/menu_launcher.py', 'src/idle.py', 'src/idle_controller.py', 'src/idle_cleanup.py', 'src/runtime_lock.py',
    'src/process_helper.py', 'src/audio.c', 'src/audio.h', 'src/release_export.py', 'src/dm_pack.py',
    'src/engine.c', 'src/bridge.h', 'src/viewer.c',
    'src/level_bypass.h', 'src/quakec/live_bot.qc', 'src/quakec/live_session.qc', 'src/quakec/e1m1_assist.qc',
    'src/protocols/pointer-constraints-unstable-v1.xml', 'src/protocols/relative-pointer-unstable-v1.xml',
    'src/protocols/wlr-layer-shell-unstable-v1.xml', 'src/protocols/xdg-output-unstable-v1.xml',
    'src/protocols/xdg-shell.xml',
    'scripts/bootstrap-ironwail.py', 'scripts/build-ironwail.py', 'patches/ironwail-desktop.patch',
    'src/ironwail/live_quake.c', 'src/ironwail/lq_egl_gbm.c',
    'src/ironwail/lq_egl_offscreen.h', 'src/ironwail/lq_libretro.c', 'src/ironwail/lq_libretro.h',
    'src/ironwail/lq_session.h', 'src/ironwail/dm_rotation.h', 'src/ironwail/dm_director.h',
    'src/ironwail/lq_sound.c', 'src/ironwail/lq_steam_disabled.c',
    'src/gpu_gbm.c', 'src/gpu_gbm.h', 'src/gpu_transport.c', 'src/gpu_transport.h',
    'src/gpu_wayland.c', 'src/gpu_wayland.h',
    'src/protocols/linux-dmabuf-v1.xml', 'src/protocols/viewporter.xml',
) + (
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
) + (
    # FrikBot X 0.10.2 and the Quake Bot Archive's episode waypoints, unchanged (third_party/README.md).
    'third_party/README.md', 'third_party/frikbot-x/readme.html',
) + tuple('third_party/frikbot-x/src/frikbot/%s.qc' % name for name in (
    'bot', 'bot_ai', 'bot_ed', 'bot_fight', 'bot_misc', 'bot_move', 'bot_phys', 'bot_qw', 'bot_way',
)) + tuple('third_party/frikbot-x/src/waypoints/map_dm%d.qc' % number for number in range(1, 7)) + (
    'third_party/quake-bot-archive/waypoints/e1m1.wa1',
    'third_party/quake-bot-archive/waypoints/e1m1.way',
    'third_party/quake-bot-archive/waypoints/e1m2.wa1',
    'third_party/quake-bot-archive/waypoints/e1m2.way',
    'third_party/quake-bot-archive/waypoints/e1m3.wa1',
    'third_party/quake-bot-archive/waypoints/e1m3.way',
    'third_party/quake-bot-archive/waypoints/e1m4.wa1',
    'third_party/quake-bot-archive/waypoints/e1m4.way',
    'third_party/quake-bot-archive/waypoints/e1m5.wa1',
    'third_party/quake-bot-archive/waypoints/e1m5.way',
    'third_party/quake-bot-archive/waypoints/e1m6.way',
    'third_party/quake-bot-archive/waypoints/e1m7.way',
    'third_party/quake-bot-archive/waypoints/e2m1.wa1',
    'third_party/quake-bot-archive/waypoints/e2m1.way',
    'third_party/quake-bot-archive/waypoints/e2m2.wa1',
    'third_party/quake-bot-archive/waypoints/e2m2.way',
    'third_party/quake-bot-archive/waypoints/e2m3.wa1',
    'third_party/quake-bot-archive/waypoints/e2m3.way',
    'third_party/quake-bot-archive/waypoints/e2m4.wa1',
    'third_party/quake-bot-archive/waypoints/e2m4.wa2',
    'third_party/quake-bot-archive/waypoints/e2m4.way',
    'third_party/quake-bot-archive/waypoints/e2m5.wa1',
    'third_party/quake-bot-archive/waypoints/e2m5.wa2',
    'third_party/quake-bot-archive/waypoints/e2m5.way',
    'third_party/quake-bot-archive/waypoints/e2m6.wa1',
    'third_party/quake-bot-archive/waypoints/e2m6.way',
    'third_party/quake-bot-archive/waypoints/e2m7.wa1',
    'third_party/quake-bot-archive/waypoints/e2m7.way',
    'third_party/quake-bot-archive/waypoints/e3m1.wa1',
    'third_party/quake-bot-archive/waypoints/e3m1.way',
    'third_party/quake-bot-archive/waypoints/e3m2.wa1',
    'third_party/quake-bot-archive/waypoints/e3m2.way',
    'third_party/quake-bot-archive/waypoints/e3m3.way',
    'third_party/quake-bot-archive/waypoints/e3m4.wa1',
    'third_party/quake-bot-archive/waypoints/e3m4.way',
    'third_party/quake-bot-archive/waypoints/e3m5.wa1',
    'third_party/quake-bot-archive/waypoints/e3m5.way',
    'third_party/quake-bot-archive/waypoints/e3m6.wa1',
    'third_party/quake-bot-archive/waypoints/e3m6.way',
    'third_party/quake-bot-archive/waypoints/e3m7.wa1',
    'third_party/quake-bot-archive/waypoints/e3m7.way',
    'third_party/quake-bot-archive/waypoints/e4m1.wa1',
    'third_party/quake-bot-archive/waypoints/e4m1.way',
    'third_party/quake-bot-archive/waypoints/e4m2.wa1',
    'third_party/quake-bot-archive/waypoints/e4m2.way',
    'third_party/quake-bot-archive/waypoints/e4m3.wa1',
    'third_party/quake-bot-archive/waypoints/e4m3.way',
    'third_party/quake-bot-archive/waypoints/e4m4.wa1',
    'third_party/quake-bot-archive/waypoints/e4m4.way',
    'third_party/quake-bot-archive/waypoints/e4m5.wa1',
    'third_party/quake-bot-archive/waypoints/e4m5.way',
    'third_party/quake-bot-archive/waypoints/e4m6.wa1',
    'third_party/quake-bot-archive/waypoints/e4m6.wa2',
    'third_party/quake-bot-archive/waypoints/e4m6.way',
    'third_party/quake-bot-archive/waypoints/e4m7.wa1',
    'third_party/quake-bot-archive/waypoints/e4m7.way',
    'third_party/quake-bot-archive/waypoints/e4m8.wa1',
    'third_party/quake-bot-archive/waypoints/e4m8.wa2',
    'third_party/quake-bot-archive/waypoints/e4m8.way',
    'third_party/quake-bot-archive/waypoints/end.way',
    'third_party/quake-bot-archive/waypoints/start.way',
)
_IMAGES = frozenset(('assets/live-quake.webp', 'preview.png'))
# FrikBot waypoint scripts keep their authors' lines in Quake's console font (control and high-bit bytes) verbatim.
_WAYPOINT = re.compile(r'third_party/quake-bot-archive/waypoints/(?:e[1-4]m[1-8]|start|end)\.wa(?:y|[1-9])')
# Reject personal home paths and scratch paths without embedding a developer's
# identifiers. The documented mktemp build-directory example is public.
_PRIVATE_PATH = re.compile(rb'/(?:home|Users)/[A-Za-z0-9_.-]+|/tmp/(?!live-quake-(?:build|dev)\.X{6}\b)[A-Za-z0-9_.-]+')


def _absolute(value):
    raw = os.fspath(value)
    if (not isinstance(raw, str) or not raw.startswith('/') or raw.startswith('//')
            or len(raw) > 4096 or '\\' in raw
            or any(ord(char) < 32 or ord(char) == 127 for char in raw)
            or any(part in ('', '.', '..') for part in raw.split('/')[1:])):
        raise ValueError('An explicit canonical absolute path without traversal is required')
    return Path(raw)


def _relative(value):
    if (not isinstance(value, str) or not value or len(value) > 512 or value.startswith('/')
            or '\\' in value or any(ord(char) < 32 or ord(char) == 127 for char in value)
            or any(part in ('', '.', '..') for part in value.split('/'))):
        raise ValueError('Invalid export-relative path')
    return value


def _directory(path):
    current = os.open('/', _READ | os.O_DIRECTORY)
    try:
        for name in _absolute(path).parts[1:]:
            child = os.open(name, _READ | os.O_DIRECTORY, dir_fd=current)
            os.close(current); current = child
        info = os.fstat(current)
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o022:
            raise ValueError('Source and destination parents must be owned and not writable to others')
        result, current = current, -1
        return result
    finally:
        if current >= 0:
            os.close(current)


def _parent(root, relative):
    parts = _relative(relative).split('/')
    current = os.dup(root)
    try:
        for part in parts[:-1]:
            child = os.open(part, _READ | os.O_DIRECTORY, dir_fd=current)
            info = os.fstat(child)
            if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o022:
                os.close(child)
                raise ValueError('Unsafe source directory')
            os.close(current); current = child
        result, current = current, -1
        return result, parts[-1]
    finally:
        if current >= 0:
            os.close(current)


def _identity(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns,
            stat.S_IMODE(info.st_mode), info.st_nlink)


def _read(root, relative, maximum, expected=None):
    parent, leaf = _parent(root, relative)
    fd = -1
    try:
        fd = os.open(leaf, _READ, dir_fd=parent)
        before = os.fstat(fd)
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid()
                or before.st_nlink != 1 or stat.S_IMODE(before.st_mode) & 0o7022
                or not 0 < before.st_size <= maximum):
            raise ValueError('Source must be an owned single-link bounded regular file: ' + relative)
        if expected is not None and _identity(before) != expected:
            raise ValueError('New output was replaced or changed before completion: ' + relative)
        payload = bytearray()
        while len(payload) <= maximum:
            block = os.read(fd, min(65536, maximum + 1 - len(payload)))
            if not block:
                break
            payload.extend(block)
        if len(payload) != before.st_size or _identity(before) != _identity(os.fstat(fd)):
            raise ValueError('Source changed or exceeded its bound: ' + relative)
        return bytes(payload)
    finally:
        if fd >= 0:
            os.close(fd)
        os.close(parent)


def _unique_json(payload):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('Duplicate manifest JSON key')
            result[key] = value
        return result
    def invalid_constant(value):
        raise ValueError('Nonstandard manifest JSON constant')
    return json.loads(payload, object_pairs_hook=unique, parse_constant=invalid_constant)


def _validate_payload(relative, payload):
    if _PRIVATE_PATH.search(payload):
        raise ValueError('Private machine path in public source: ' + relative)
    if payload.startswith((b'\x7fELF', b'PACK', b'PK\x03\x04', b'MZ')):
        raise ValueError('Compiled/game/archive artifact in source allowlist: ' + relative)
    if relative in _IMAGES:
        if relative.endswith('.png'):
            if (len(payload) < 36 or payload[:8] != b'\x89PNG\r\n\x1a\n'
                    or payload[12:16] != b'IHDR' or payload[-12:] != b'\0\0\0\0IEND\xaeB`\x82'):
                raise ValueError('Invalid source preview image')
            width, height = struct.unpack('>II', payload[16:24])
            if not 1 <= width <= 16384 or not 1 <= height <= 16384:
                raise ValueError('Unbounded source preview dimensions')
        elif (len(payload) < 20 or payload[:4] != b'RIFF' or payload[8:12] != b'WEBP'
              or struct.unpack('<I', payload[4:8])[0] + 8 != len(payload)):
            raise ValueError('Invalid own marker image')
    elif _WAYPOINT.fullmatch(relative):
        if (len(payload) > 65536 or b'\0' in payload
                or any(not line.startswith(b'//') and re.fullmatch(rb'[ -~]*', line) is None
                       for line in payload.replace(b'\r\n', b'\n').split(b'\n'))):
            raise ValueError('Invalid FrikBot waypoint script: ' + relative)
    else:
        text = payload.decode('utf-8')
        if any(ord(char) < 32 and char not in '\t\n\r' for char in text):
            raise ValueError('Binary content in text source: ' + relative)


def _validate_set(entries):
    if not all(isinstance(entry, ExportFile) for entry in entries):
        raise ValueError('Invalid preflight file type')
    names = [entry.relative for entry in entries]
    if (not 1 <= len(entries) <= MAX_FILES or len(names) != len(set(names))
            or len(SOURCE_FILES) != len(set(SOURCE_FILES)) or set(names) != set(SOURCE_FILES)):
        raise ValueError('Export must contain the complete unique source allowlist')
    total = 0
    files = {}
    for entry in entries:
        _relative(entry.relative)
        maximum = MAX_IMAGE_BYTES if entry.relative in _IMAGES else MAX_TEXT_BYTES
        if (not isinstance(entry.payload, bytes) or not 0 < len(entry.payload) <= maximum
                or entry.sha256 != hashlib.sha256(entry.payload).hexdigest()
                or entry.mode != (0o755 if entry.relative.startswith('scripts/') else 0o644)):
            raise ValueError('Invalid preflight file snapshot')
        total += len(entry.payload)
        if total > MAX_TOTAL_BYTES:
            raise ValueError('Export exceeds its total byte bound')
        _validate_payload(entry.relative, entry.payload)
        files[entry.relative] = entry.payload
    manifest = _unique_json(files['manifest.json'])
    if (not isinstance(manifest, dict) or manifest.get('id') != PLUGIN_ID
            or type(manifest.get('schemaVersion')) is not int or manifest['schemaVersion'] != 1
            or manifest.get('license') != LICENSE_EXPRESSION
            or not isinstance(manifest.get('entryPoints'), dict)
            or manifest['entryPoints'].get('overlay') != 'menu/Menu.qml'
            or not isinstance(manifest.get('version'), str)
            or len(manifest['version']) > 80
            or re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9.-]+)?', manifest['version']) is None):
        raise ValueError('Unexpected or incomplete plugin manifest')
    if files['LICENSE'] != files['LICENSES/0BSD.txt']:
        raise ValueError('Root licence and 0BSD notice differ')
    anchors = {
        'LICENSES/0BSD.txt': (400, (b'Permission to use, copy, modify,', b'THE SOFTWARE IS PROVIDED')),
        'LICENSES/CC0-1.0.txt': (6000, (b'CC0 1.0 Universal', b'Limitations and Disclaimers')),
        'LICENSES/GPL-2.0-or-later.txt': (14000, (b'GNU GENERAL PUBLIC LICENSE', b'Version 2, June 1991', b'NO WARRANTY')),
        'LICENSES/BSD-3-Clause-LibreQuake.txt': (1200, (b'Contributors to the LibreQuake project', b'Redistribution and use', b'THIS SOFTWARE IS PROVIDED')),
    }
    for name, (minimum, phrases) in anchors.items():
        if len(files[name]) < minimum or any(phrase not in files[name] for phrase in phrases):
            raise ValueError('Missing or truncated licence notice: ' + name)
    notices = files['THIRD-PARTY-NOTICES.md']
    if len(notices) < 1024 or any(phrase not in notices for phrase in
                                (b'Ironwail', b'LibreQuake', b'CC0-1.0', b'GPL-2.0', b'MIT')):
        raise ValueError('Third-party notices are incomplete')
    for name in SOURCE_FILES:
        if name.startswith('src/protocols/'):
            payload = files[name]
            if b'<!DOCTYPE' in payload or b'<!ENTITY' in payload:
                raise ValueError('Unexpected protocol entity declaration')
            opening, closing = payload.find(b'<copyright>'), payload.find(b'</copyright>')
            if opening < 0 or closing - opening < 400:
                raise ValueError('Protocol copyright notice is unavailable: ' + name)
    return manifest, total


@dataclass(frozen=True)
class ExportFile:
    relative: str
    payload: bytes = field(repr=False)
    sha256: str
    mode: int


@dataclass(frozen=True)
class ExportPlan:
    source: Path = field(repr=False)
    destination: Path = field(repr=False)
    destination_identity: tuple = field(repr=False)
    source_commit: str
    files: tuple[ExportFile, ...] = field(repr=False)

    def inventory(self):
        manifest, total = _validate_set(self.files)
        _commit(self.source_commit)
        return {'schema': 1, 'plugin_id': PLUGIN_ID, 'version': manifest['version'],
                'source_commit': self.source_commit, 'kind': 'source-only', 'total_bytes': total,
                'files': {entry.relative: {'sha256': entry.sha256, 'size': len(entry.payload), 'mode': entry.mode}
                          for entry in self.files}}


def _commit(value):
    if not isinstance(value, str) or re.fullmatch(r'[0-9a-f]{40}', value) is None or value == '0' * 40:
        raise ValueError('A full lowercase nonzero 40-character source commit is required')
    return value


def preflight(source, destination, source_commit):
    source, destination = _absolute(source), _absolute(destination)
    _commit(source_commit)
    if (source == destination or source in destination.parents or destination in source.parents):
        raise ValueError('Source and destination parent must be separate and nonoverlapping')
    root, target = _directory(source), -1
    try:
        target = _directory(destination)
        if not 1 <= len(SOURCE_FILES) <= MAX_FILES or len(set(SOURCE_FILES)) != len(SOURCE_FILES):
            raise ValueError('Invalid source allowlist size or duplicate paths')
        # Inspect only the directories that can contribute export files, never
        # vendor/build/tests, private reports, hidden metadata or game folders.
        parents = {str(Path(name).parent) for name in SOURCE_FILES}
        for relative in sorted(parents):
            directory = os.dup(root) if relative == '.' else _parent(root, relative + '/.probe')[0]
            try:
                with os.scandir(directory) as listing:
                    for index, entry in enumerate(listing):
                        if index >= MAX_DIRECTORY_ENTRIES:
                            raise ValueError('Source directory inventory exceeded its bound')
                        if entry.name.casefold() == 'manifest.json' and (relative != '.' or entry.name != 'manifest.json'):
                            raise ValueError('Duplicate or aliased plugin manifest')
            finally:
                os.close(directory)
        entries, total = [], 0
        for relative in sorted(SOURCE_FILES):
            maximum = MAX_IMAGE_BYTES if relative in _IMAGES else MAX_TEXT_BYTES
            payload = _read(root, relative, min(maximum, MAX_TOTAL_BYTES - total))
            total += len(payload)
            entries.append(ExportFile(relative, payload, hashlib.sha256(payload).hexdigest(),
                                      0o755 if relative.startswith('scripts/') else 0o644))
        _validate_set(tuple(entries))
        # Paths themselves must not be embedded in shipped text/images.
        for entry in entries:
            if str(source).encode() in entry.payload or str(destination).encode() in entry.payload:
                raise ValueError('Private source/destination path in export input: ' + entry.relative)
        info = os.fstat(target)
        return ExportPlan(source, destination, (info.st_dev, info.st_ino, info.st_uid, stat.S_IMODE(info.st_mode)),
                          source_commit, tuple(entries))
    finally:
        os.close(root)
        if target >= 0:
            os.close(target)


def export(plan):
    if not isinstance(plan, ExportPlan):
        raise TypeError('A validated ExportPlan is required')
    inventory = plan.inventory()
    parent = _directory(plan.destination)
    root, name = -1, None
    try:
        info = os.fstat(parent)
        if (info.st_dev, info.st_ino, info.st_uid, stat.S_IMODE(info.st_mode)) != plan.destination_identity:
            raise ValueError('Destination parent changed since preflight')
        for _ in range(8):
            candidate = 'live-quake-source-' + secrets.token_hex(16)
            try:
                os.mkdir(candidate, 0o700, dir_fd=parent)
                name = candidate
                break
            except FileExistsError:
                continue
        if name is None:
            raise ValueError('Could not create a new unique export directory')
        root = os.open(name, _READ | os.O_DIRECTORY, dir_fd=parent)
        os.fchmod(root, 0o700)
        directories = {'.': os.dup(root)}
        try:
            created_files = {}
            outputs = [(entry.relative, entry.payload, entry.mode) for entry in plan.files]
            outputs.append((INVENTORY, (json.dumps(inventory, indent=2, allow_nan=False) + '\n').encode(), 0o644))
            for relative, payload, mode in outputs:
                parts = relative.split('/')
                prefix = '.'
                for part in parts[:-1]:
                    child_relative = part if prefix == '.' else prefix + '/' + part
                    if child_relative not in directories:
                        os.mkdir(part, 0o700, dir_fd=directories[prefix])
                        directory = os.open(part, _READ | os.O_DIRECTORY, dir_fd=directories[prefix])
                        try:
                            os.fchmod(directory, 0o700)
                        except BaseException:
                            os.close(directory)
                            raise
                        directories[child_relative] = directory
                    prefix = child_relative
                fd = os.open(parts[-1], os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
                             mode, dir_fd=directories[prefix])
                try:
                    os.fchmod(fd, mode)
                    remaining = memoryview(payload)
                    while remaining:
                        count = os.write(fd, remaining)
                        if count <= 0:
                            raise OSError('Short source export write')
                        remaining = remaining[count:]
                    os.fsync(fd)
                    created_files[relative] = _identity(os.fstat(fd))
                finally:
                    os.close(fd)
            for directory in sorted(directories, key=lambda value: value.count('/'), reverse=True):
                os.fsync(directories[directory])
            os.fsync(parent)
            # Verify the visible result, including the final inventory, before
            # reporting success. A late replacement is never adopted.
            for relative, payload, mode in outputs:
                copied = _read(root, relative, len(payload), expected=created_files[relative])
                if copied != payload:
                    raise ValueError('Exported file changed before completion: ' + relative)
            named = os.stat(name, dir_fd=parent, follow_symlinks=False)
            held = os.fstat(root)
            if (not stat.S_ISDIR(named.st_mode) or named.st_uid != os.getuid()
                    or stat.S_IMODE(named.st_mode) != 0o700
                    or (named.st_dev, named.st_ino) != (held.st_dev, held.st_ino)):
                raise ValueError('New export directory changed before completion')
            check = _directory(plan.destination)
            try:
                current = os.fstat(check)
                if (current.st_dev, current.st_ino, current.st_uid, stat.S_IMODE(current.st_mode)) != plan.destination_identity:
                    raise ValueError('Destination parent changed during export')
            finally:
                os.close(check)
        finally:
            for directory in directories.values():
                os.close(directory)
        return plan.destination / name
    except (OSError, ValueError):
        if name is not None:
            raise RuntimeError('Export failed; new partial directory retained as ' + name) from None
        raise
    finally:
        if root >= 0:
            os.close(root)
        os.close(parent)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True)
    parser.add_argument('--destination', required=True,
                        help='Existing owned canonical parent for a new random source directory')
    parser.add_argument('--source-commit', required=True)
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--yes', action='store_true', help='Confirm creation of one new local source directory')
    args = parser.parse_args(argv)
    try:
        plan = preflight(args.source, args.destination, args.source_commit)
        inventory = plan.inventory()
        if args.dry_run:
            print(json.dumps({'result': 'DRY_RUN', 'creates_files': False, 'plan': inventory}, allow_nan=False))
            return 0
        if not args.yes:
            if not sys.stdin.isatty() or not sys.stdout.isatty():
                raise ValueError('Actual local export requires --yes or a TTY confirmation')
            print(json.dumps({'result': 'PLAN', 'plan': inventory}, allow_nan=False))
            if input('Create this new local source directory? [y/N] ').strip().lower() != 'y':
                raise ValueError('Local export was not confirmed')
        output = export(plan)
        print(json.dumps({'result': 'EXPORTED', 'directory': str(output),
                          'inventory': INVENTORY, 'source_commit': plan.source_commit}, allow_nan=False))
        return 0
    except (OSError, ValueError, RuntimeError, UnicodeError, RecursionError) as error:
        print('Live Quake source export failed: ' + str(error), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
