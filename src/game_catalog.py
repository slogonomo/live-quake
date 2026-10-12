# SPDX-License-Identifier: 0BSD
"""Explicit-HOME, on-demand catalog; no launches, writes or data copies.

The daemon supplies HOME and its known free package. Only scan() discovers
Steam. A single read-only daemon worker keeps discovery off the controller's
policy loop; timeout/close request cooperative cancellation and discard its
result. Cancellation cannot interrupt a blocked kernel filesystem call or an
injected noncooperative discoverer. Retries never overlap that worker.
Public metadata contains no
installation paths or detector diagnostics. Selection is separately validated
and belongs inside the controller's paused switch transaction.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import re
import threading
import time
import unicodedata

import steam_games as games

MAX_PACKAGES = 16
MAX_PAKS = 3
_MAP = re.compile(r'[A-Za-z0-9_]{1,48}\Z', re.ASCII)
_KNOWN = {
    'steam-quake': ('Quake (Original)', 'id1', (2310,)),
    'steam-hipnotic': ('Scourge of Armagon', 'hipnotic', (2310, 9040)),
    'steam-rogue': ('Dissolution of Eternity', 'rogue', (2310, 9030)),
    'steam-quake-rerelease': ('Quake (Enhanced)', None, (2310,)),
    **{'steam-app-' + str(appid): (title, None, (appid,))
       for appid, title in games.APP_TITLES.items() if appid not in (2310, 9040, 9030)},
}


class CatalogError(ValueError):
    """Path-free diagnostic safe for a controller ERR reply."""


def known_package_id(identifier):
    """Recognize a closed catalog ID without scanning or testing playability."""
    return (type(identifier) is str and len(identifier) <= 64
            and (identifier == 'librequake' or identifier in _KNOWN))


def _absolute(value):
    try:
        raw = os.fspath(value)
    except TypeError:
        raise CatalogError('invalid catalog path') from None
    if (not isinstance(raw, str) or not raw.startswith('/') or raw.startswith('//')
            or len(raw) > 4096 or '\\' in raw or raw == '/'
            or any(ord(char) < 32 or ord(char) == 127 for char in raw)
            or any(part in ('', '.', '..') for part in raw.split('/')[1:])):
        raise CatalogError('invalid catalog path')
    return Path(raw)


def _text(value, limit=128):
    if (not isinstance(value, str) or not value.strip() or len(value) > limit
            or any(unicodedata.category(char) in ('Cc', 'Cf', 'Cs', 'Zl', 'Zp') for char in value)
            or any(char in value for char in '/\\<>')):
        raise CatalogError('invalid catalog text')
    return value


@dataclass(frozen=True)
class _Record:
    id: str
    title: str
    source: str
    edition: str
    playable: bool
    reason: str
    store: str | None
    basedir: Path | None = None
    gamedir: str | None = None
    paks: tuple[Path, ...] = ()
    start_map: str | None = None
    fingerprint: tuple = ()
    backend: str = 'ironwail'
    kpf: Path | None = None

    def public(self):
        return {key: getattr(self, key) for key in
                ('id', 'title', 'source', 'edition', 'playable', 'reason', 'store')}


@dataclass(frozen=True)
class Selection:
    """Private validated binding. Profile names contain no source paths.

Profiles namespace origin/package and hash canonical paths, inode, size,
timestamps and bounded PAK directory metadata. st_dev is deliberately omitted
from the profile hash so btrfs device-number changes do not move saves. The
fingerprint is a point-in-time change check, not a full payload hash or proof
of authenticity; source owners can still edit archives after validation.
"""
    id: str
    title: str
    source: str
    edition: str
    basedir: Path
    gamedir: str
    paks: tuple[Path, ...]
    start_map: str
    profile_id: str
    backend: str = 'ironwail'
    kpf: Path | None = None
    dm_maps: tuple[str, ...] = ()

    @property
    def data(self):
        result = {'id': self.id, 'title': self.title, 'basedir': str(self.basedir),
                'gamedir': self.gamedir, 'paks': [str(path) for path in self.paks],
                'game_args': [] if self.gamedir == 'id1' else ['-' + self.gamedir],
                'start_map': self.start_map, 'compatible': True, 'runtime_tested': False}
        result.update(backend=self.backend, edition=self.edition,
                      kpf=None if self.kpf is None else str(self.kpf), dm_maps=list(self.dm_maps))
        return result


def _fingerprint(record):
    """Reopen every component nofollow; read only bounded archive metadata."""
    directory = games._open_absolute_directory(record.basedir)
    try:
        result = []
        for path in (*record.paks, *((record.kpf,) if record.kpf is not None else ())):
            relative = path.relative_to(record.basedir)
            auxiliary = path == record.kpf
            if len(relative.parts) != (1 if auxiliary else 2):
                raise CatalogError('invalid package archives')
            fd = games._open_relative(directory, relative.parts, casefold=False)
            try:
                before = os.fstat(fd)
                pak = games._kpf(fd, path) if auxiliary else games._pak(fd, path)
                after = os.fstat(fd)
                identity = lambda info: (info.st_dev, info.st_ino, info.st_size,
                                         info.st_mtime_ns, info.st_ctime_ns)
                if identity(before) != identity(after) or pak['path'] != str(path):
                    raise CatalogError('package changed during validation; scan again')
                metadata = (pak['directory_sha256'] if auxiliary else
                            hashlib.sha256(json.dumps(pak['members'], sort_keys=True, separators=(',', ':')).encode()).hexdigest())
                result.append((str(path), *identity(after), metadata))
            finally:
                os.close(fd)
        return tuple(result)
    finally:
        os.close(directory)


def _paths(row, gamedir):
    basedir = _absolute(row.get('basedir'))
    values = row.get('paks')
    if (not isinstance(values, (list, tuple)) or not 1 <= len(values) <= MAX_PAKS):
        raise CatalogError('invalid package archives')
    paks = tuple(_absolute(value) for value in values)
    if len(set(paks)) != len(paks):
        raise CatalogError('duplicate package archives')
    for path in paks:
        relative = path.relative_to(basedir)
        if (len(relative.parts) != 2 or relative.parts[0].casefold() not in ('id1', gamedir)
                or relative.parts[1].casefold() not in ('pak0.pak', 'pak1.pak')):
            raise CatalogError('invalid package archives')
    return basedir, paks


def _kpf_path(row, basedir, edition):
    value = row.get('kpf')
    if edition != 'rerelease':
        if value is not None:
            raise CatalogError('unexpected auxiliary package archive')
        return None
    path = _absolute(value)
    relative = path.relative_to(basedir)
    if len(relative.parts) != 1 or relative.name.casefold() != 'quakeex.kpf':
        raise CatalogError('invalid enhanced package archive')
    return path


def _free(row, backend='ironwail'):
    if not isinstance(row, dict) or row.get('id') != 'librequake' or row.get('gamedir') != 'id1':
        raise CatalogError('invalid free package')
    title = _text(row.get('title'))
    start_map = row.get('start_map', 'lq_e0m1')
    if not isinstance(start_map, str) or not _MAP.fullmatch(start_map):
        raise CatalogError('invalid free start map')
    try:
        basedir, paks = _paths(row, 'id1')
    except (ValueError, TypeError):
        raise CatalogError('invalid free package') from None
    return _Record('librequake', title, 'free', 'original', True,
                   'Free fallback; data is validated when selected.', None,
                   basedir, 'id1', paks, start_map, backend=backend)


def _steam(row, backend='ironwail'):
    if (not isinstance(row, dict) or not isinstance(row.get('id'), str)
            or len(row['id']) > 64 or row['id'] not in _KNOWN):
        raise CatalogError('invalid Steam catalog response')
    identifier = row['id']
    title, gamedir, appids = _KNOWN[identifier]
    if identifier == 'steam-quake-rerelease':
        gamedir = 'id1'
    if type(row.get('appid')) is not int or row['appid'] not in appids:
        raise CatalogError('invalid Steam catalog response')
    if type(row.get('compatible')) is not bool:
        raise CatalogError('invalid Steam catalog response')
    edition = 'rerelease' if identifier == 'steam-quake-rerelease' else 'original'
    if row.get('edition') != edition:
        raise CatalogError('invalid Steam catalog response')
    playable = gamedir is not None and row['compatible']
    if gamedir is None:
        reason = 'Not Quake 1 data; this engine runs Quake 1 only.'
    else:
        reason = ('Early maps tested for Ironwail; campaign support is unproven.' if playable
                  else 'Ironwail package data is unavailable or invalid.')
        if edition == 'rerelease' and row.get('availability') == 'missing-kpf':
            reason = 'Enhanced data requires QuakeEX.kpf.'
    if row.get('advisories'):
        reason += ' Steam install state may be incomplete or updating.'
    store_id = 2310 if row['appid'] in (2310, 9040, 9030) else row['appid']
    record = _Record(identifier, title, 'steam', edition, playable, reason,
                     f'https://store.steampowered.com/app/{store_id}/', backend=backend)
    if not playable:
        return record
    if row.get('gamedir') != gamedir or row.get('start_map') != 'start':
        raise CatalogError('invalid Steam catalog response')
    basedir, paks = _paths(row, gamedir)
    kpf = _kpf_path(row, basedir, edition)
    record = _Record(identifier, title, 'steam', edition, True, reason, record.store,
                     basedir, gamedir, paks, 'start', backend=backend, kpf=kpf)
    return _Record(**{**record.__dict__, 'fingerprint': _fingerprint(record)})


def _validate_record(record, expected_profile=None):
    """Validate one exact binding independently of discovery/catalog lifetime."""
    try:
        before = _fingerprint(record)
        if record.fingerprint and before != record.fingerprint:
            raise CatalogError('package changed since scan; scan Steam again')
        directory = games._open_absolute_directory(record.basedir)
        try:
            data = games._game_data(directory, record.basedir, record.gamedir,
                                    record.source == 'steam', original_names=record.source == 'steam',
                                    backend=record.backend, edition=record.edition, start_map=record.start_map)
        finally:
            os.close(directory)
        if tuple(Path(value) for value in data['paks']) != record.paks:
            raise CatalogError('package archives changed; choose the package again')
        if (None if data.get('kpf') is None else Path(data['kpf'])) != record.kpf:
            raise CatalogError('package archives changed; choose the package again')
        if _fingerprint(record) != before:
            raise CatalogError('package changed during validation; choose the package again')
        # Device numbers are used in the live change check, not save identity.
        metadata = [tuple(value for index, value in enumerate(row) if index != 1) for row in before]
        namespace = games.backend_capabilities(record.backend).profile_namespace
        payload = json.dumps(['live-quake-profile-v1' + namespace, record.source, record.id,
                              str(record.basedir), record.gamedir, metadata],
                             separators=(',', ':')).encode()
        profile = namespace + record.id + '-' + hashlib.sha256(payload).hexdigest()
        if expected_profile is not None and profile != expected_profile:
            raise CatalogError('game data changed; choose the package again')
        return Selection(record.id, record.title, record.source, record.edition,
                         record.basedir, record.gamedir, record.paks, record.start_map, profile, record.backend, record.kpf,
                         dm_maps=tuple(data.get('dm_maps', ())))
    except CatalogError:
        raise
    except (OSError, ValueError, TypeError, KeyError):
        raise CatalogError('game data is unavailable or invalid; choose the package again') from None


def revalidate_selection(selection):
    """Validate a retained exact Selection and require its original save profile.

No HOME lookup, scan, catalog ID lookup, mutation, or launch occurs. A removed
catalog row does not invalidate usable retained data, and a refreshed same-ID
row cannot replace its installation. Used before cold activation and rollback.
Like resolve(), filesystem reads are count/size bounded, not kernel-time bounded.
"""
    if (not isinstance(selection, Selection) or not isinstance(selection.id, str)
            or len(selection.id) > 64 or not isinstance(selection.source, str)):
        raise CatalogError('invalid retained game selection')
    identifier = selection.id
    try:
        namespace = games.backend_capabilities(selection.backend).profile_namespace
    except games.DataError:
        raise CatalogError('invalid retained game selection') from None
    if selection.source == 'free':
        valid = identifier == 'librequake' and selection.gamedir == 'id1' and selection.edition == 'original'
    elif selection.source == 'steam' and identifier in _KNOWN:
        title, gamedir, _appids = _KNOWN[identifier]
        expected_edition = 'rerelease' if identifier == 'steam-quake-rerelease' else 'original'
        if expected_edition == 'rerelease':
            gamedir = 'id1'
        valid = gamedir is not None and selection.gamedir == gamedir and selection.edition == expected_edition and selection.title == title
    else:
        valid = False
    if (not valid or not isinstance(selection.profile_id, str)
            or len(selection.profile_id) != len(namespace) + len(identifier) + 65
            or re.fullmatch(re.escape(namespace + identifier) + r'-[0-9a-f]{64}', selection.profile_id, re.ASCII) is None
            or not isinstance(selection.start_map, str) or not _MAP.fullmatch(selection.start_map)
            or selection.source == 'steam' and selection.start_map != 'start'
            or not isinstance(selection.paks, tuple)):
        raise CatalogError('invalid retained game selection')
    try:
        title = _text(selection.title)
        basedir, paks = _paths(selection.data, selection.gamedir)
        kpf = _kpf_path(selection.data, basedir, selection.edition)
    except (ValueError, TypeError):
        raise CatalogError('invalid retained game selection') from None
    record = _Record(identifier, title, selection.source, selection.edition, True, '', None,
                     basedir, selection.gamedir, paks, selection.start_map, backend=selection.backend, kpf=kpf)
    return _validate_record(record, selection.profile_id)


class GameCatalog:
    def __init__(self, home, free_package, *, discoverer=None, clock=time.monotonic, scan_timeout=5, backend='ironwail'):
        try:
            self.backend = games.backend_capabilities(backend).name
        except games.DataError:
            raise CatalogError('invalid catalog backend') from None
        self.home = _absolute(home)
        self._free = _free(free_package, self.backend)
        self._discover = games.discover if discoverer is None else discoverer
        self._default_discovery = discoverer is None
        if not callable(self._discover) or not callable(clock):
            raise CatalogError('invalid catalog callable')
        if (type(scan_timeout) not in (int, float) or not math.isfinite(scan_timeout)
                or not .1 <= scan_timeout <= 30):
            raise CatalogError('invalid catalog deadline')
        self._clock, self._timeout = clock, scan_timeout
        self._lock = threading.Lock()
        self._records = {'librequake': self._free}
        self._thread = None
        self._cancelled = None
        self._generation = 0
        self._active = None
        self._completion = None
        self._deadline = 0
        self._closed = False
        self._error = None
        self._summary = 'Steam has not been scanned.'
        self._steam_found = False
        self._revision = 0

    def _work(self, generation, cancelled):
        records, found, error = None, False, None
        try:
            # Existing fixture/custom discoverers keep their one-argument API.
            # Only the production scanner accepts the cooperative flag.
            result = (self._discover(self.home, cancelled=cancelled.is_set,
                                     backend=self.backend)
                      if self._default_discovery else self._discover(self.home))
            games._check_cancelled(cancelled.is_set)
            if (not isinstance(result, dict) or type(result.get('steam_found')) is not bool
                    or not isinstance(result.get('packages'), list)
                    or len(result['packages']) > MAX_PACKAGES):
                raise CatalogError('invalid Steam catalog response')
            records = {'librequake': self._free}
            for row in result['packages']:
                games._check_cancelled(cancelled.is_set)
                record = _steam(row, self.backend)
                if record.id in records:
                    raise CatalogError('duplicate Steam package')
                records[record.id] = record
            found = result['steam_found']
        except Exception:
            error = 'Steam scan failed; kept the old list.'
        with self._lock:
            if not cancelled.is_set() and not self._closed and self._active == generation:
                self._completion = (generation, records, found, error, self._clock())

    def scan(self):
        """Return True when started; False coalesces a scan already running."""
        self.poll()
        with self._lock:
            if self._closed:
                raise CatalogError('catalog is closed')
            if self._active is not None or (self._thread and self._thread.is_alive()):
                return False
            self._generation += 1
            generation = self._generation
            self._active = generation
            self._deadline = self._clock() + self._timeout
            self._cancelled = threading.Event()
            self._error = None
            self._thread = threading.Thread(target=self._work, args=(generation, self._cancelled), daemon=True,
                                            name='live-quake-catalog')
            try:
                self._thread.start()
            except RuntimeError:
                self._cancelled.set()
                self._active = None
                self._error = 'Steam scan could not start.'
                raise CatalogError(self._error) from None
        return True

    def poll(self):
        with self._lock:
            completed_in_time = (self._completion is not None
                                 and self._completion[0] == self._active
                                 and self._completion[4] < self._deadline)
            if self._active is not None and self._clock() >= self._deadline and not completed_in_time:
                self._cancelled.set()
                self._active = None
                self._completion = None
                self._error = 'Steam scan timed out; kept the old list.'
            if self._completion is not None:
                generation, records, found, error, _completed_at = self._completion
                self._completion = None
                if generation == self._active:
                    self._active = None
                    self._error = error
                    if records is not None and error is None:
                        self._records, self._steam_found = records, found
                        self._revision += 1
                        count = sum(record.playable for key, record in records.items() if key != 'librequake')
                        self._summary = games._scan_summary(count, found)

    def settings(self):
        self.poll()
        with self._lock:
            rows = [self._records[key].public() for key in sorted(self._records,
                    key=lambda key: (key != 'librequake', key))]
            return {'catalog': rows, 'scanning': self._active is not None,
                    'scan_error': self._error, 'summary': self._summary,
                    'steam_found': self._steam_found, 'revision': self._revision}

    def resolve(self, identifier, *, revision=None):
        """Freshly validate a copied private binding; never launch or persist it."""
        self.poll()
        with self._lock:
            if self._closed:
                raise CatalogError('catalog is closed')
            if (not isinstance(identifier, str) or len(identifier) > 64 or identifier not in self._records
                    or identifier != 'librequake' and identifier not in _KNOWN):
                raise CatalogError('unknown game package; scan Steam first')
            if revision is not None and (type(revision) is not int or revision != self._revision):
                raise CatalogError('catalog changed; refresh the menu')
            record = self._records[identifier]
        if not record.playable:
            raise CatalogError('game package is not compatible with this engine')
        return _validate_record(record)

    def close(self):
        """Request cancellation without waiting for blocked read-only workers."""
        with self._lock:
            self._closed = True
            if self._cancelled is not None:
                self._cancelled.set()
            self._active = self._completion = None
