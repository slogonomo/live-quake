# SPDX-License-Identifier: 0BSD
"""Explicit-HOME saver ownership and cached first-party idle observations.

Ownership tokens and paths are internal: settings must use public_status().
No constructor launches a process. Tests inject the idle command runner; the
production runner uses one fixed read-only verb with a closed environment.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, replace
import fcntl
import json
import math
import os
from pathlib import Path
import secrets
import stat
import threading
import time

from desktop import bounded_command, session_environment

IDLE_STATUS = ('/usr/bin/omarchy-shell', 'idle', 'status')
MAX_IDLE_BYTES = 16384
MAX_JOURNAL_BYTES = 4096
MAX_TOKEN_BYTES = 64
_READ = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK


def _absolute(value):
    raw = os.fspath(value)
    if (not isinstance(raw, str) or not raw.startswith('/') or raw.startswith('//')
            or len(raw) > 4096 or any(ord(c) < 32 or ord(c) == 127 for c in raw)
            or any(part in ('', '.', '..') for part in raw.split('/')[1:])):
        raise ValueError('An explicit absolute path without traversal is required')
    return Path(raw)


def _directory(path, *, create=False, private=False):
    current = os.open('/', _READ | os.O_DIRECTORY)
    try:
        for part in _absolute(path).parts[1:]:
            try:
                child = os.open(part, _READ | os.O_DIRECTORY, dir_fd=current)
            except FileNotFoundError:
                if not create:
                    raise
                os.mkdir(part, 0o700, dir_fd=current)
                child = os.open(part, _READ | os.O_DIRECTORY, dir_fd=current)
            os.close(current); current = child
        info = os.fstat(current)
        if (info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o022
                or (private and stat.S_IMODE(info.st_mode) != 0o700)):
            raise ValueError('An owned non-writable-to-others directory is required')
        result, current = current, -1
        return result
    finally:
        if current >= 0:
            os.close(current)


def _identity(info):
    # Anonymous filesystem device numbers may change across a reboot. They are
    # not persistent claim identity; the random token and inode remain required.
    return [info.st_ino, info.st_mtime_ns, info.st_ctime_ns,
            info.st_size, stat.S_IMODE(info.st_mode), info.st_nlink]


def _read(path, maximum):
    parent = _directory(path.parent)
    fd = -1
    try:
        fd = os.open(path.name, _READ, dir_fd=parent)
        before = os.fstat(fd)
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid()
                or before.st_nlink != 1 or stat.S_IMODE(before.st_mode) != 0o600
                or before.st_size > maximum):
            raise ValueError('A bounded independent private regular file is required')
        data = bytearray()
        while len(data) <= maximum:
            block = os.read(fd, maximum + 1 - len(data))
            if not block:
                break
            data.extend(block)
        if len(data) > maximum or _identity(before) != _identity(os.fstat(fd)):
            raise ValueError('Ownership file changed or exceeded its bound')
        return bytes(data), before
    finally:
        if fd >= 0:
            os.close(fd)
        os.close(parent)


def _inspect(path, maximum):
    """Bounded read-only recognition; unsafe aliases never become authority."""
    parent = _directory(path.parent)
    fd = -1
    try:
        fd = os.open(path.name, _READ, dir_fd=parent)
        before = os.fstat(fd)
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid()
                or before.st_size > maximum):
            raise ValueError('A bounded owned regular file is required for inspection')
        data = bytearray()
        while len(data) <= maximum:
            block = os.read(fd, maximum + 1 - len(data))
            if not block:
                break
            data.extend(block)
        if len(data) > maximum or _identity(before) != _identity(os.fstat(fd)):
            raise ValueError('Ownership file changed during inspection')
        # Pin the name too: a held inode does not recognize its replacement.
        if _identity(os.stat(path.name, dir_fd=parent, follow_symlinks=False)) != _identity(before):
            raise ValueError('Ownership entry changed during inspection')
        return bytes(data), before
    finally:
        if fd >= 0:
            os.close(fd)
        os.close(parent)


def _toggle_safe(info):
    return (stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid()
            and info.st_nlink == 1 and stat.S_IMODE(info.st_mode) == 0o600
            and info.st_size <= MAX_TOKEN_BYTES)


def _publish(path, payload):
    """Complete bytes enter a new name atomically, never replacing an entry."""
    parent = _directory(path.parent, create=True)
    temporary = '.live-quake-idle-' + secrets.token_hex(16)
    fd, created = -1, False
    try:
        fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
                     0o600, dir_fd=parent)
        created = True
        os.fchmod(fd, 0o600)
        view = memoryview(payload)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError('Short ownership file write')
            view = view[written:]
        os.fsync(fd)
        os.link(temporary, path.name, src_dir_fd=parent, dst_dir_fd=parent, follow_symlinks=False)
        os.unlink(temporary, dir_fd=parent)
        created = False
        os.fsync(parent)
        # This is the identity of OUR still-open inode, not a reread of a name
        # that another writer may have replaced with identical token bytes.
        return os.fstat(fd)
    finally:
        try:
            if created:
                try:
                    temporary_info = os.stat(temporary, dir_fd=parent, follow_symlinks=False)
                    ours = os.fstat(fd)
                    if (temporary_info.st_dev, temporary_info.st_ino) == (ours.st_dev, ours.st_ino):
                        os.unlink(temporary, dir_fd=parent)
                except FileNotFoundError:
                    pass
        finally:
            if fd >= 0:
                os.close(fd)
            os.close(parent)


def _unlink_exact(path, payload, expected):
    try:
        current, info = _read(path, max(len(payload), MAX_TOKEN_BYTES))
        if current != payload or _identity(info) != _identity(expected):
            return False
        parent = _directory(path.parent)
        try:
            if _identity(os.stat(path.name, dir_fd=parent, follow_symlinks=False)) != _identity(info):
                return False
            os.unlink(path.name, dir_fd=parent)
            os.fsync(parent)
            return True
        finally:
            os.close(parent)
    except (OSError, ValueError):
        return False


def _token(value):
    return isinstance(value, str) and len(value) == 64 and all(c in '0123456789abcdef' for c in value)


def _json(payload):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('Duplicate JSON key')
            result[key] = value
        return result
    return json.loads(payload, object_pairs_hook=unique)


def _replace_private(path, payload, *, expected=None, before_replace=None):
    """Atomically update an absent or validated private regular recovery record."""
    try:
        previous, info = _read(path, MAX_JOURNAL_BYTES)
    except FileNotFoundError:
        previous, info = None, None
    if expected is not None and (previous != expected[0] or info is None
                                or _identity(info) != expected[1]):
        raise ValueError('Pinned private record changed')
    parent = _directory(path.parent, create=True, private=True)
    temporary = '.live-quake-recovery-' + secrets.token_hex(16)
    fd, created = -1, False
    try:
        fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
                     0o600, dir_fd=parent)
        created = True
        os.fchmod(fd, 0o600)
        view = memoryview(payload)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError('Short recovery write')
            view = view[written:]
        os.fsync(fd)
        if info is None:
            os.link(temporary, path.name, src_dir_fd=parent, dst_dir_fd=parent, follow_symlinks=False)
            os.unlink(temporary, dir_fd=parent)
        else:
            again, again_info = _read(path, MAX_JOURNAL_BYTES)
            if again != previous or _identity(again_info) != _identity(info):
                raise ValueError('Recovery record changed')
            if before_replace is not None:
                before_replace()
            if _identity(os.stat(path.name, dir_fd=parent, follow_symlinks=False)) != _identity(info):
                raise ValueError('Private record changed before replacement')
            os.replace(temporary, path.name, src_dir_fd=parent, dst_dir_fd=parent)
        created = False
        os.fsync(parent)
    finally:
        try:
            if created:
                try:
                    temporary_info = os.stat(temporary, dir_fd=parent, follow_symlinks=False)
                    ours = os.fstat(fd)
                    if (temporary_info.st_dev, temporary_info.st_ino) == (ours.st_dev, ours.st_ino):
                        os.unlink(temporary, dir_fd=parent)
                except FileNotFoundError:
                    pass
        finally:
            if fd >= 0:
                os.close(fd)
            os.close(parent)


def _archive_oversized_private(path):
    """Linux metadata-only explicit recovery; never reads an oversized body."""
    import ctypes
    import errno

    parent = _directory(path.parent, private=True)
    fd = -1
    try:
        fd = os.open(path.name, _READ, dir_fd=parent)
        before = os.fstat(fd)
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid()
                or before.st_nlink != 1 or stat.S_IMODE(before.st_mode) != 0o600
                or before.st_size <= MAX_JOURNAL_BYTES):
            raise ValueError('Oversized recovery record is unsafe')
        archive = 'idle-recovery-rejected-' + secrets.token_hex(16) + '.json'
        libc = ctypes.CDLL(None, use_errno=True)
        rename = getattr(libc, 'renameat2', None)
        if rename is None:
            raise OSError(errno.ENOSYS, 'Recovery archive capability unavailable')
        rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        rename.restype = ctypes.c_int
        current = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
        if _identity(current) != _identity(before) or _identity(os.fstat(fd)) != _identity(before):
            raise ValueError('Recovery record changed before archive')
        # RENAME_NOREPLACE: neither a preexisting archive nor a raced collision
        # can be overwritten. No pathname or content escapes this private dir.
        if rename(parent, os.fsencode(path.name), parent, os.fsencode(archive), 1) != 0:
            raise OSError(ctypes.get_errno(), 'Recovery record could not be archived')
        archived = os.stat(archive, dir_fd=parent, follow_symlinks=False)
        held = os.fstat(fd)
        if (_identity(archived) != _identity(held) or held.st_ino != before.st_ino
                or held.st_uid != before.st_uid or held.st_size != before.st_size
                or held.st_mtime_ns != before.st_mtime_ns or held.st_nlink != 1
                or stat.S_IMODE(held.st_mode) != 0o600):
            raise ValueError('Recovery archive identity changed')
        os.fsync(parent)
    finally:
        if fd >= 0:
            os.close(fd)
        os.close(parent)


class SaverRecovery:
    """Private bounded state; contains no ownership token or filesystem path."""
    @staticmethod
    def initial():
        return {'schema': 1, 'failures': 0, 'skip': 'none', 'consumed': False,
                'acknowledged': False, 'cycle_open': False, 'attempt': False,
                'disable_reason': None, 'cycle_id': 0, 'engine_generation': 0}

    @classmethod
    def validate(cls, value):
        if (type(value) is not dict or set(value) != set(cls.initial())
                or type(value['schema']) is not int or value['schema'] != 1
                or type(value['failures']) is not int or not 0 <= value['failures'] <= 3
                or value['skip'] not in ('none', 'current', 'next', 'skipping')
                or any(type(value[key]) is not bool for key in ('consumed', 'acknowledged', 'cycle_open', 'attempt'))
                or value['disable_reason'] not in (None, 'claim-revoked', 'repeated-presentation-failure', 'external-suppression')
                or any(type(value[key]) is not int or not 0 <= value[key] <= 2**64 - 1
                       for key in ('cycle_id', 'engine_generation'))):
            raise ValueError('Invalid saver recovery state')
        return value

    def __init__(self, ownership):
        self.ownership = ownership
        self.path = ownership.state / 'idle-recovery.json'

    def load(self):
        try:
            raw, _ = _read(self.path, MAX_JOURNAL_BYTES)
        except FileNotFoundError:
            return self.initial()
        return self.validate(_json(raw))

    def save(self, value):
        self.validate(value)
        payload = (json.dumps(value, separators=(',', ':'), sort_keys=True) + '\n').encode()
        with self.ownership._lock():
            # A malformed existing record is never replaced by a clean one.
            self.load()
            _replace_private(self.path, payload)

    def reset(self):
        """Explicit re-enable resets valid state or archives safe malformed bytes.

        An unsafe entry is never replaced. The archive is a complete exclusive
        private copy; the original must still match before it can be retired.
        """
        value = self.initial()
        payload = (json.dumps(value, separators=(',', ':'), sort_keys=True) + '\n').encode()
        with self.ownership._lock():
            try:
                raw, info = _read(self.path, MAX_JOURNAL_BYTES)
            except FileNotFoundError:
                _publish(self.path, payload)
                return value
            except ValueError:
                # Explicit reset may archive an oversized private inode using
                # only metadata. Normal load/save retain the 4096-byte bound.
                _archive_oversized_private(self.path)
                _publish(self.path, payload)
                return value
            try:
                self.validate(_json(raw))
                valid = True
            except (ValueError, RecursionError, UnicodeError):
                valid = False
            if not valid:
                archive = self.path.with_name('idle-recovery-rejected-' + secrets.token_hex(16) + '.json')
                _publish(archive, raw)
                if not _unlink_exact(self.path, raw, info):
                    raise ValueError('Recovery record changed during reset')
                _publish(self.path, payload)
            else:
                again, again_info = _read(self.path, MAX_JOURNAL_BYTES)
                if again != raw or _identity(again_info) != _identity(info):
                    raise ValueError('Recovery record changed during reset')
                _replace_private(self.path, payload)
        return value


class SaverOwnership:
    def __init__(self, home, state_dir=None):
        self.home = _absolute(home)
        fd = _directory(self.home); os.close(fd)
        self.state = _absolute(state_dir) if state_dir is not None else self.home / '.local/state/live-quake'
        if self.home not in self.state.parents:
            raise ValueError('Ownership journal must remain inside the explicit HOME')
        self.toggle = self.home / '.local/state/omarchy/toggles/screensaver-off'
        self.journal = self.state / 'idle-ownership.json'
        self.pending = self.state / 'idle-claim-pending.json'

    @contextmanager
    def _lock(self):
        parent = _directory(self.state, create=True, private=True)
        fd = -1
        try:
            fd = os.open('.idle-ownership.lock', os.O_CREAT | os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK,
                         0o600, dir_fd=parent)
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1
                    or info.st_size != 0 or stat.S_IMODE(info.st_mode) != 0o600):
                raise ValueError('Unsafe ownership lock')
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            yield
        finally:
            if fd >= 0:
                os.close(fd)
            os.close(parent)

    def _journal(self):
        try:
            raw, info = _read(self.journal, MAX_JOURNAL_BYTES)
        except FileNotFoundError:
            return None, None, None
        value = _json(raw)
        identity = value.get('toggle_identity') if isinstance(value, dict) else None
        if (not isinstance(value, dict) or set(value) != {'schema', 'token', 'toggle_identity'}
                or type(value.get('schema')) is not int or value['schema'] != 1
                or not _token(value.get('token')) or not isinstance(identity, list) or len(identity) not in (6, 7)
                or any(type(v) is not int or not 0 <= v <= 2**64 - 1 for v in identity)
                or identity[-3:] != [64, 0o600, 1]):
            raise ValueError('Invalid ownership journal')
        if len(identity) == 7:
            # Read old journals without using their reboot-unstable st_dev.
            value['toggle_identity'] = identity[1:]
        return value, raw, info

    def _pending(self):
        try:
            raw, info = _read(self.pending, MAX_JOURNAL_BYTES)
        except FileNotFoundError:
            return None, None, None
        value = _json(raw)
        if (type(value) is not dict or set(value) != {'schema', 'token'}
                or type(value['schema']) is not int or value['schema'] != 1
                or not _token(value['token'])):
            raise ValueError('Invalid pending ownership claim')
        return value, raw, info

    def _pending_context(self, pending):
        record, raw, info = self._journal()
        if record is not None and record['token'] != pending['token']:
            raise ValueError('Pending claim conflicts with ownership journal')
        toggle, toggle_info = self._toggle_entry()
        return record, raw, info, toggle, toggle_info

    def _toggle_entry(self):
        """Inspect a pinned entry without following or opening nonregular files.

        A nonregular or non-64-byte entry cannot be our exact regular token.
        Unreadable 64-byte regular data never establishes a foreign replacement.
        """
        try:
            parent = _directory(self.toggle.parent)
            try:
                info = os.stat(self.toggle.name, dir_fd=parent, follow_symlinks=False)
                if not stat.S_ISREG(info.st_mode) or info.st_size != MAX_TOKEN_BYTES:
                    again = os.stat(self.toggle.name, dir_fd=parent, follow_symlinks=False)
                    if _identity(again) != _identity(info):
                        raise ValueError('Foreign ownership entry changed during inspection')
                    return None, info
            finally:
                os.close(parent)
        except FileNotFoundError:
            return None, None
        payload, captured = _inspect(self.toggle, MAX_TOKEN_BYTES)
        if _identity(captured) != _identity(info):
            raise ValueError('Ownership entry changed before inspection')
        return payload, captured

    @staticmethod
    def _cleanup_entry_safe(record, toggle, info):
        if info is None:
            return True
        if toggle != record['token'].encode():
            # A foreign entry cannot use this token's journal. Never unlink it.
            return True
        return _toggle_safe(info) and info.st_ino == record['toggle_identity'][0]

    def _resolve_pending_locked(self):
        """One bounded reconciliation attempt with the ownership lock held."""
        released = False
        try:
            pending, raw, info = self._pending()
            if pending is None:
                return {'safe': self.cleanup_status()['safe'], 'recovered': False, 'released': False}
            record, journal, journal_info, toggle, toggle_info = self._pending_context(pending)
            # Recheck both authority records immediately before mutation.
            again, again_info = _read(self.pending, MAX_JOURNAL_BYTES)
            if again != raw or _identity(again_info) != _identity(info):
                return {'safe': False, 'recovered': False, 'released': False}
            if record is not None:
                again, again_info = _read(self.journal, MAX_JOURNAL_BYTES)
                if again != journal or _identity(again_info) != _identity(journal_info):
                    return {'safe': False, 'recovered': False, 'released': False}
                if not self._cleanup_entry_safe(record, toggle, toggle_info):
                    return {'safe': False, 'recovered': False, 'released': False}
                # Journal publication completed the claim. Pending removal never
                # touches that claim or a later foreign replacement.
                recovered = _unlink_exact(self.pending, raw, info)
                return {'safe': recovered and self.cleanup_status()['safe'],
                        'recovered': recovered, 'released': False}
            if toggle == pending['token'].encode():
                released = _unlink_exact(self.toggle, toggle, toggle_info)
                if not released:
                    return {'safe': False, 'recovered': False, 'released': False}
            recovered = _unlink_exact(self.pending, raw, info)
            return {'safe': recovered, 'recovered': recovered, 'released': released}
        except (OSError, ValueError, RecursionError):
            return {'safe': False, 'recovered': False, 'released': released}

    def resolve_pending(self):
        """Resolve an interrupted claim while the caller holds its runtime lock.

        Only a complete private pending record is authority. A matching active
        journal remains intact; malformed, conflicting or unreadable data does
        not authorize cleanup. The ownership lock excludes another claimant.
        """
        released = False
        try:
            pending, _, _ = self._pending()
            if pending is None:
                return {'safe': self.cleanup_status()['safe'], 'recovered': False, 'released': False}
            with self._lock():
                result = self._resolve_pending_locked()
                released = result['released']
                return result
        except (OSError, ValueError, RecursionError):
            return {'safe': False, 'recovered': False, 'released': released}

    def status(self):
        """Read only. A failed read never establishes that a claim was revoked."""
        result = {'owner': 'unknown', 'owned': False, 'blocked': False, 'revoked': False,
                  'token': None, 'toggle': str(self.toggle), 'journal': str(self.journal),
                  'preserved_changed': False, 'journal_retained': False,
                  'toggle_retained': False, 'toggle_safe': False,
                  'reason': 'Screensaver ownership is unavailable',
                  'reason_code': 'ownership-unavailable'}
        try:
            try:
                parent = _directory(self.toggle.parent)
                try:
                    toggle_info = os.stat(self.toggle.name, dir_fd=parent, follow_symlinks=False)
                    present = True
                finally:
                    os.close(parent)
            except FileNotFoundError:
                present = False
            result['toggle_retained'] = present
            result['toggle_safe'] = present and _toggle_safe(toggle_info)
            try:
                parent = _directory(self.journal.parent)
                try:
                    os.stat(self.journal.name, dir_fd=parent, follow_symlinks=False)
                    result['journal_retained'] = True
                finally:
                    os.close(parent)
            except FileNotFoundError:
                pass
            try:
                record, _, _ = self._journal()
                result['journal_retained'] = result['journal_retained'] or record is not None
                try:
                    _read(self.pending, MAX_JOURNAL_BYTES)
                    pending = True
                except FileNotFoundError:
                    pending = False
            except (OSError, ValueError, RecursionError):
                return result
            if pending:
                return result
            if record is None:
                if present:
                    result.update(owner='other', blocked=True, reason_code='external-suppression',
                                  reason='Screensaver suppression is controlled externally')
                else:
                    result.update(owner=None, reason_code=None, reason='Screensaver suppression is unclaimed')
                return result
            result['token'] = record['token']
            if not present:
                result.update(owner=None, revoked=True, reason_code='claim-revoked',
                              reason='The recorded screensaver claim was removed')
                return result
            if _identity(toggle_info) != record['toggle_identity']:
                result.update(owner='other', blocked=True, revoked=True, reason_code='claim-revoked',
                              reason='The recorded screensaver claim was replaced')
                try:
                    payload, info = _inspect(self.toggle, MAX_TOKEN_BYTES)
                    if _identity(info) == _identity(toggle_info):
                        result['preserved_changed'] = (payload == record['token'].encode()
                                                       and info.st_ino == record['toggle_identity'][0])
                except (OSError, ValueError):
                    pass
                return result
            try:
                payload, info = _read(self.toggle, MAX_TOKEN_BYTES)
            except (OSError, ValueError):
                return result
            if payload == record['token'].encode() and _identity(info) == record['toggle_identity']:
                result.update(owner='live-quake', owned=True, blocked=False, reason_code=None,
                              reason='Live Quake owns screensaver suppression')
            else:
                result.update(owner='other', blocked=True, revoked=True, reason_code='claim-revoked',
                              reason='The recorded screensaver claim was replaced')
                result['preserved_changed'] = (payload == record['token'].encode()
                                               and info.st_ino == record['toggle_identity'][0])
                result['toggle_safe'] = _toggle_safe(info)
            return result
        except (OSError, ValueError):
            return result

    def public_status(self):
        value = self.status()
        return {key: value[key] for key in ('owner', 'reason')}

    def cleanup_status(self):
        """Read-only cleanup plan; resolvable pending claims still require locks."""
        try:
            pending, _, _ = self._pending()
            if pending is not None:
                record, _, _, toggle, info = self._pending_context(pending)
                safe = (info is None or toggle != pending['token'].encode()
                        or (record is None and _toggle_safe(info))
                        or (record is not None and self._cleanup_entry_safe(record, toggle, info)))
                changed = (record is not None and info is not None
                           and toggle == record['token'].encode()
                           and info.st_ino == record['toggle_identity'][0]
                           and _identity(info) != record['toggle_identity'])
                return {'safe': safe, 'recorded': True, 'owned': False,
                        'reason': 'Interrupted ownership claim',
                        'preserved_changed': changed, 'journal_retained': record is not None,
                        'toggle_retained': info is not None,
                        'toggle_safe': info is not None and _toggle_safe(info)}
            record, _, _ = self._journal()
            status = self.status()
            foreign = False
            if record is not None:
                toggle, info = self._toggle_entry()
                foreign = info is not None and toggle != record['token'].encode()
            safe = ((record is None and status['owner'] != 'unknown') or status['owned']
                    or (status['revoked'] and (not status['toggle_retained']
                        or foreign or (status['preserved_changed'] and status['toggle_safe']))))
            return {'safe': safe, 'recorded': record is not None,
                    'owned': status['owned'], 'reason': status['reason'],
                    **{key: status[key] for key in ('preserved_changed', 'journal_retained',
                                                    'toggle_retained', 'toggle_safe')}}
        except (OSError, ValueError, RecursionError):
            status = self.status()
            return {'safe': False, 'recorded': False, 'owned': False, 'reason': 'Ownership record unavailable',
                    **{key: status[key] for key in ('preserved_changed', 'journal_retained',
                                                    'toggle_retained', 'toggle_safe')}}

    def _adoption_candidate(self, expected_identity=None):
        if expected_identity is not None and (
                type(expected_identity) is not tuple or len(expected_identity) != 6
                or any(type(value) is not int or not 0 <= value <= 2**64 - 1
                       for value in expected_identity)
                or expected_identity[-3:] != (64, 0o600, 1)):
            raise ValueError('Invalid expected adoption identity')
        pending, _, _ = self._pending()
        if pending is not None:
            raise ValueError('Pending ownership cannot be adopted')
        record, raw, journal_info = self._journal()
        if record is None:
            raise ValueError('Adoption requires a valid ownership journal')
        payload, toggle_info = _read(self.toggle, MAX_TOKEN_BYTES)
        identity = tuple(_identity(toggle_info))
        if (payload != record['token'].encode()
                or toggle_info.st_ino != record['toggle_identity'][0]
                or (expected_identity is not None and identity != expected_identity)):
            raise ValueError('Adoption does not match the recorded inode and token')
        # Recognition is read-only. Both names must still hold the captured
        # bytes and metadata; a replacement never becomes fresh authority.
        again, again_info = _read(self.journal, MAX_JOURNAL_BYTES)
        if again != raw or _identity(again_info) != _identity(journal_info):
            raise ValueError('Adoption journal changed')
        again, again_info = _read(self.toggle, MAX_TOKEN_BYTES)
        if again != payload or tuple(_identity(again_info)) != identity:
            raise ValueError('Adoption toggle changed')
        pending, _, _ = self._pending()
        if pending is not None:
            raise ValueError('Ownership became pending during adoption')
        return record, raw, journal_info, payload, toggle_info

    def adoption_identity(self):
        """Read-only safe token+inode recognition; never changes ownership."""
        try:
            return tuple(_identity(self._adoption_candidate()[4]))
        except (OSError, ValueError, RecursionError):
            return None

    def can_adopt(self, expected_identity=None):
        try:
            self._adoption_candidate(expected_identity)
            return True
        except (OSError, ValueError, RecursionError):
            return False

    def adopt(self, expected_identity=None):
        """Explicit metadata-only adoption of the same safe token and inode.

        The caller may pin a recognition made before warmup. A second touch or
        replacement invalidates that expectation. No toggle is written,
        removed, renamed, or linked; only its private journal is updated.
        """
        adopted = False
        try:
            with self._lock():
                record, raw, journal_info, token, toggle_info = self._adoption_candidate(expected_identity)
                current = tuple(_identity(toggle_info))
                if list(current) == record['toggle_identity']:
                    return self.status() | {'adopted': False}
                replacement = {'schema': 1, 'token': record['token'],
                               'toggle_identity': list(current)}
                payload = (json.dumps(replacement, separators=(',', ':')) + '\n').encode()

                def pinned():
                    _, checked, info, _, _ = self._adoption_candidate(current)
                    if checked != raw or _identity(info) != _identity(journal_info):
                        raise ValueError('Pinned adoption journal changed')

                _replace_private(self.journal, payload,
                                 expected=(raw, _identity(journal_info)), before_replace=pinned)
                final, _, _ = self._journal()
                actual, info = _read(self.toggle, MAX_TOKEN_BYTES)
                pending, _, _ = self._pending()
                adopted = (final == replacement and actual == token
                           and tuple(_identity(info)) == current and pending is None)
        except (OSError, ValueError, RecursionError):
            pass
        status = self.status()
        return status | {'adopted': adopted and status['owned']}

    def claim(self, enabled):
        if type(enabled) is not bool:
            raise ValueError('Saver enabled must be a boolean')
        if not enabled:
            return self.status()
        try:
            with self._lock():
                pending_record, _, _ = self._pending()
                if pending_record is not None:
                    committed, _, _ = self._journal()
                    if committed is not None and committed['token'] == pending_record['token']:
                        self._resolve_pending_locked()
                before = self.status()
                if before['owner'] is not None or before['revoked']:
                    return before
                token = secrets.token_hex(32)
                pending = (json.dumps({'schema': 1, 'token': token}, separators=(',', ':')) + '\n').encode()
                pending_info = _publish(self.pending, pending)
                toggle_info = None
                try:
                    toggle_info = _publish(self.toggle, token.encode())
                    if toggle_info.st_nlink != 1:
                        raise ValueError('Created toggle changed before journaling')
                    record = {'schema': 1, 'token': token, 'toggle_identity': _identity(toggle_info)}
                    _publish(self.journal, (json.dumps(record, separators=(',', ':')) + '\n').encode())
                except (OSError, ValueError):
                    # Publication can install the complete journal before its
                    # directory fsync fails. That completed claim must survive
                    # with its pending marker for resolve_pending().
                    try:
                        committed, _, _ = self._journal()
                    except (OSError, ValueError, RecursionError):
                        committed = None
                    if committed is not None and committed['token'] == token:
                        self._resolve_pending_locked()
                        return self.status()
                    if toggle_info is not None:
                        _unlink_exact(self.toggle, token.encode(), toggle_info)
                    _unlink_exact(self.pending, pending, pending_info)
                    return self.status()
                _unlink_exact(self.pending, pending, pending_info)
                return self.status()
        except (OSError, ValueError):
            return self.status()

    def release(self, *, recognize=False):
        """Retire only the exact recorded identity, for every caller.

        The legacy recognize keyword is accepted but grants no extra authority.
        OUR token bytes with changed metadata retain their journal for explicit
        adoption or manual handling. A foreign entry is untouched while its
        obsolete journal is retired; unsafe token aliases are never unlinked.
        """
        if type(recognize) is not bool:
            raise ValueError('Claim recognition must be a boolean')
        released = False
        try:
            with self._lock():
                pending, _, _ = self._pending()
                if pending is not None:
                    committed, _, _ = self._journal()
                    if committed is not None and committed['token'] == pending['token']:
                        self._resolve_pending_locked()
                    pending, _, _ = self._pending()
                    if pending is not None:
                        return self.status() | {'released': False}
                if not self.cleanup_status()['safe']:
                    return self.status() | {'released': False}
                record, raw, journal_info = self._journal()
                if record is None:
                    return self.status() | {'released': False}
                try:
                    payload, toggle_info = self._toggle_entry()
                    if toggle_info is not None and payload != record['token'].encode():
                        # Repin the foreign name and journal. Retiring metadata
                        # never authorizes changing the replacement entry.
                        again, again_info = self._toggle_entry()
                        if (again_info is not None and again == payload
                                and _identity(again_info) == _identity(toggle_info)):
                            _unlink_exact(self.journal, raw, journal_info)
                        return self.status() | {'released': False}
                    if toggle_info is None:
                        raise FileNotFoundError
                    payload, toggle_info = _read(self.toggle, MAX_TOKEN_BYTES)
                    identity = _identity(toggle_info)
                    matches = identity == record['toggle_identity']
                    if payload == record['token'].encode() and matches:
                        # Check the journal too immediately before removing its toggle.
                        again, again_info = _read(self.journal, MAX_JOURNAL_BYTES)
                        if again == raw and _identity(again_info) == _identity(journal_info):
                            released = _unlink_exact(self.toggle, payload, toggle_info)
                except FileNotFoundError:
                    # No remaining toggle uses this journal. Retiring our
                    # unchanged record is safe and does not create a new claim.
                    _unlink_exact(self.journal, raw, journal_info)
                except (OSError, ValueError):
                    pass
                if released:
                    _unlink_exact(self.journal, raw, journal_info)
        except (OSError, ValueError, RecursionError):
            pass
        return self.status() | {'released': released}


@dataclass(frozen=True)
class IdleState:
    available: bool = False
    enabled: bool = False
    stay_awake: bool = False
    in_idle_cycle: bool = False
    screensaver_started: bool = False
    screensaver_seconds: int | None = None
    command_started_at: float | None = None
    reply_received_at: float | None = None

    def __post_init__(self):
        if any(type(getattr(self, key)) is not bool for key in
               ('available', 'enabled', 'stay_awake', 'in_idle_cycle', 'screensaver_started')):
            raise ValueError('Idle state booleans must be explicit')
        if (self.screensaver_seconds is not None
                and (type(self.screensaver_seconds) is not int or not 1 <= self.screensaver_seconds <= 86400)):
            raise ValueError('Invalid idle screensaver timeout')
        if self.available and self.screensaver_seconds is None:
            raise ValueError('Readable idle state requires a screensaver timeout')
        for value in (self.command_started_at, self.reply_received_at):
            if (value is not None and (type(value) not in (int, float)
                    or not math.isfinite(value) or value < 0)):
                raise ValueError('Invalid idle observation time')
        if (self.reply_received_at is not None and (self.command_started_at is None
                or self.reply_received_at < self.command_started_at)):
            raise ValueError('Idle reply must follow its command start')

    @property
    def should_start(self):
        return self.available and self.enabled and not self.stay_awake and self.in_idle_cycle and self.screensaver_started


def parse_idle(payload):
    if not isinstance(payload, bytes) or len(payload) > MAX_IDLE_BYTES:
        raise ValueError('Invalid idle status size/type')
    value = _json(payload)
    if not isinstance(value, dict):
        raise ValueError('Idle status must be an object')
    keys = ('enabled', 'stayAwake', 'inIdleCycle', 'screensaverStarted')
    if any(type(value.get(key)) is not bool for key in keys):
        raise ValueError('Idle status booleans are unavailable')
    seconds = value.get('screensaver')
    if type(seconds) is not int or not 1 <= seconds <= 86400:
        raise ValueError('Idle screensaver timeout is unavailable')
    return IdleState(True, value['enabled'], value['stayAwake'], value['inIdleCycle'], value['screensaverStarted'], seconds)


@dataclass(frozen=True)
class IdleDecision:
    should_start: bool
    should_stop: bool
    reason: str


def decision(state, ownership, *, enabled, locked):
    if type(enabled) is not bool:
        raise ValueError('Saver enabled must be a boolean')
    if not enabled:
        reason = 'Screensaver disabled'
    elif locked is not False:
        reason = 'Session locked or lock state unavailable'
    elif not isinstance(ownership, dict) or ownership.get('owner') != 'live-quake' or ownership.get('owned') is not True:
        reason = 'Screensaver suppression is not owned'
    elif not isinstance(state, IdleState) or not state.available:
        reason = 'First-party idle status unavailable'
    elif not state.should_start:
        reason = 'First-party screensaver step inactive'
    else:
        return IdleDecision(True, False, 'First-party screensaver step active')
    return IdleDecision(False, True, reason)


class IdleObserver:
    """One dormant worker; enabled queries share a start-to-start budget.

    Successful observations expire after two seconds, allowing the one-second
    period plus the bounded command and cleanup. Error grace runs through the
    next scheduled poll plus one second, capped at that same healthy expiry;
    repeated failures never renew the last successful command-start timestamp.
    Claims separately require a validated reply received within half a second.
    """
    def __init__(self, environment, *, runner=None, poll_interval=1, stale_after=2,
                 command_timeout=.4, clock=time.monotonic):
        for value in (poll_interval, stale_after, command_timeout):
            if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
                raise ValueError('Invalid idle observer timing')
        if stale_after <= poll_interval or stale_after > 2:
            raise ValueError('Idle expiry must exceed polling interval')
        self.environment = session_environment(environment)
        if 'HOME' not in self.environment:
            raise ValueError('Idle observer requires explicit HOME')
        _absolute(self.environment['HOME'])
        self._runner = bounded_command if runner is None else runner
        if not callable(self._runner) or not callable(clock):
            raise ValueError('Idle observer runner and clock must be callable')
        self._interval, self._stale, self._timeout, self._clock = poll_interval, stale_after, command_timeout, clock
        self._stop, self._wake, self._lock = threading.Event(), threading.Event(), threading.Lock()
        self._state, self._observed_at, self._error = IdleState(), None, 'Idle observer has not started'
        self._thread, self._closed = None, False
        self._enabled, self._epoch, self._next_query_at = False, 0, 0.0
        self._query_stop = None

    def start(self):
        with self._lock:
            if self._closed:
                raise RuntimeError('Idle observer closed')
            if self._thread is None:
                self._thread = threading.Thread(target=self._work, name='quake-idle-observer', daemon=True)
                self._thread.start()
        return self

    def set_enabled(self, enabled):
        if type(enabled) is not bool:
            raise ValueError('Idle observation enabled must be a boolean')
        with self._lock:
            if self._closed or enabled == self._enabled:
                return
            self._enabled = enabled
            self._epoch += 1
            self._state, self._observed_at = IdleState(), None
            self._error = 'Idle observer awaiting status' if enabled else 'Idle observer disabled'
            if self._query_stop is not None:
                self._query_stop.set()
            # Keep the global deadline across toggles; enabling is not authority
            # to create another shell connection before its period has elapsed.
        self._wake.set()

    def request_fresh(self):
        """Wake the existing worker without bypassing its launch budget."""
        with self._lock:
            if self._closed or not self._enabled:
                return False
        self._wake.set()
        return True

    def _poll_once(self):
        # Both worker and fake-clock fixtures enter this exact launch boundary.
        with self._lock:
            began = self._clock()
            if (self._closed or self._stop.is_set() or not self._enabled
                    or self._query_stop is not None or began < self._next_query_at):
                return False
            epoch = self._epoch
            self._next_query_at = began + self._interval
            query_stop = self._query_stop = threading.Event()
        state = None
        try:
            payload = self._runner(IDLE_STATUS, environment=dict(self.environment), timeout=self._timeout,
                                   limit=MAX_IDLE_BYTES, stop=query_stop)
            state = replace(parse_idle(payload), command_started_at=began, reply_received_at=self._clock())
        except Exception:
            pass  # Private tool payloads and exceptions never enter diagnostics.
        with self._lock:
            self._query_stop = None
            if self._closed or self._stop.is_set() or not self._enabled or epoch != self._epoch:
                return True  # Disabled/cancelled generations cannot publish late.
            if state is not None:
                self._state, self._observed_at, self._error = state, began, None
            else:
                self._error = 'First-party idle status unavailable'
        return True

    def _work(self):
        while not self._stop.is_set():
            self._wake.clear()
            self._poll_once()
            with self._lock:
                if self._closed or self._stop.is_set():
                    return
                delay = max(0, self._next_query_at - self._clock()) if self._enabled else None
            self._wake.wait(delay)

    def __call__(self):
        with self._lock:
            expiry = min(self._stale, self._interval + 1) if self._error else self._stale
            if (self._closed or not self._enabled or self._observed_at is None
                    or not 0 <= self._clock() - self._observed_at <= expiry):
                return IdleState()
            return self._state

    def diagnostics(self):
        with self._lock:
            age = None if self._observed_at is None else max(0, self._clock() - self._observed_at)
            expiry = min(self._stale, self._interval + 1) if self._error else self._stale
            return {'running': bool(self._thread and self._thread.is_alive() and not self._closed),
                    'age_seconds': age, 'stale': age is None or age > expiry, 'error': self._error}

    def close(self):
        with self._lock:
            self._closed = True
            self._enabled = False
            self._state, self._observed_at = IdleState(), None
            if self._query_stop is not None:
                self._query_stop.set()
            thread = self._thread
        self._stop.set()
        self._wake.set()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=1.5)
            if thread.is_alive():
                with self._lock:
                    self._error = 'Idle observer runner did not stop'
