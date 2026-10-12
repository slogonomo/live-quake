# SPDX-License-Identifier: 0BSD
"""Observe and manage Live Quake background choices using explicit HOME paths.

BackgroundSelection.poll() reports a changed stable selection as True/False;
None means unchanged or unavailable. Inspect .error to distinguish failures.
.live always describes the current readable selection and fails off on errors,
including when a cached marker observation exists. No wallpaper config is read.

The marker helpers expose filesystem operations and a caller-owned selection
callback. They never launch a command or inherit HOME/XDG environment values.
New images enter the personal per-theme folder; removal archives only an exact
recorded owned image. Palettes, current theme copies, shell config and idle
toggles are outside these operations. Tests use throwaway private HOME paths.
"""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat
import time

IMAGE_SUFFIXES = {'.webp', '.png', '.jpg', '.jpeg', '.bmp', '.gif'}
MAX_IMAGE_BYTES = 32 * 1024 * 1024
MAX_STATE_BYTES = 256 * 1024
MAX_RECORDS = 128
MAX_RECOVERY_HISTORY = 64
MAX_DIRECTORY_ENTRIES = 4096
_OPEN = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK
_CURRENT = '.local/state/omarchy/current'


def _absolute(value):
    raw = os.fspath(value)
    if (not isinstance(raw, str) or len(raw) > 4096 or not raw.startswith('/') or raw.startswith('//')
            or any(ord(char) < 32 or ord(char) == 127 for char in raw) or '\\' in raw
            or any(part in ('', '.', '..') for part in raw.split('/')[1:])):
        raise ValueError('An absolute path without traversal is required')
    return Path(raw)


def _open_directory(path, *, create=False, private=False):
    """Walk every component relative to descriptors, never following a link."""
    path = _absolute(path)
    current = os.open('/', _OPEN | os.O_DIRECTORY)
    try:
        for name in path.parts[1:]:
            try:
                child = os.open(name, _OPEN | os.O_DIRECTORY, dir_fd=current)
            except FileNotFoundError:
                if not create:
                    raise
                os.mkdir(name, 0o700, dir_fd=current)
                child = os.open(name, _OPEN | os.O_DIRECTORY, dir_fd=current)
            os.close(current)
            current = child
        info = os.fstat(current)
        if private and (info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700):
            raise ValueError('The directory must be owned and mode 0700')
        result, current = current, -1
        return result
    finally:
        if current >= 0:
            os.close(current)


def _home(value):
    home = _absolute(value)
    fd = _open_directory(home, private=True)
    os.close(fd)
    return home


def _state(home, state_dir=None):
    path = _absolute(state_dir) if state_dir is not None else home / '.local/state/live-quake'
    if path == home or home not in path.parents:
        raise ValueError('Live Quake state must remain inside the supplied HOME')
    return path


def _open_file(path):
    path = _absolute(path)
    parent = _open_directory(path.parent)
    try:
        return os.open(path.name, _OPEN, dir_fd=parent)
    finally:
        os.close(parent)


def _identity(info):
    return [info.st_dev, info.st_ino, info.st_mtime_ns, info.st_ctime_ns]


def _read_bytes(path, maximum):
    fd = _open_file(path)
    try:
        before = os.fstat(fd)
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid()
                or before.st_size > maximum):
            raise ValueError('A bounded owned regular file is required')
        payload = bytearray()
        while len(payload) <= maximum:
            chunk = os.read(fd, min(65536, maximum + 1 - len(payload)))
            if not chunk:
                break
            payload.extend(chunk)
        after = os.fstat(fd)
        if len(payload) > maximum or _identity(before) != _identity(after) or before.st_size != after.st_size:
            raise ValueError('The file changed or exceeded its size limit')
        return bytes(payload)
    finally:
        os.close(fd)


def _atomic_json(path, value, maximum=MAX_STATE_BYTES):
    payload = (json.dumps(value, indent=2, allow_nan=False) + '\n').encode()
    if len(payload) > maximum:
        raise ValueError('Live background state exceeds its size limit')
    parent = _open_directory(path.parent, create=True, private=True)
    temporary = '.background-' + secrets.token_hex(12)
    try:
        try:
            info = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
                raise ValueError('Background state cannot replace a link or nonregular file')
        except FileNotFoundError:
            pass
        fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
                     0o600, dir_fd=parent)
        try:
            view = memoryview(payload)
            while view:
                view = view[os.write(fd, view):]
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(temporary, path.name, src_dir_fd=parent, dst_dir_fd=parent)
        os.fsync(parent)
    finally:
        try:
            os.unlink(temporary, dir_fd=parent)
        except FileNotFoundError:
            pass
        os.close(parent)


def _selection(home):
    parent = _open_directory(home / _CURRENT)
    try:
        before = os.stat('background', dir_fd=parent, follow_symlinks=False)
        if not stat.S_ISLNK(before.st_mode) or before.st_uid != os.getuid():
            raise ValueError('The selected background must be an owned symlink')
        target = os.readlink('background', dir_fd=parent)
        if len(target) > 4096 or '\0' in target or '\\' in target:
            raise ValueError('The selected background target is invalid')
        chosen = _absolute(os.path.normpath(target if target.startswith('/') else str(home / _CURRENT / target)))
        fd = _open_file(chosen)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= 2 * 1024 * 1024 * 1024:
                raise ValueError('The selected background is not a bounded nonempty regular file')
        finally:
            os.close(fd)
        after = os.stat('background', dir_fd=parent, follow_symlinks=False)
        if _identity(before) != _identity(after):
            raise ValueError('The selected background changed during observation')
        return {'path': str(chosen), 'link': _identity(after)}
    finally:
        os.close(parent)


def _live(path):
    return 'live-quake' in path.name.casefold() and path.suffix.casefold() in IMAGE_SUFFIXES


def _static_image(path):
    """Static replacements must not activate this or another live wallpaper."""
    name = path.name.casefold()
    return (path.suffix.casefold() in IMAGE_SUFFIXES
            and 'live-doom' not in name and 'live-quake' not in name
            and re.search(r'(^|[^a-z0-9])live-[a-z0-9]+', name) is None)


class BackgroundSelection:
    def __init__(self, home, state_dir=None):
        self.home = _home(home)
        self.link = self.home / _CURRENT / 'background'
        self.state_path = _state(self.home, state_dir) / 'background-selection.json'
        self.previous = None
        self.error = None
        self._cache_error = None
        self.live = False
        self.selected_path = None
        try:
            saved = json.loads(_read_bytes(self.state_path, 16384))
            selection = saved.get('selection') if isinstance(saved, dict) else None
            if (type(saved.get('schema')) is int and saved['schema'] == 1
                    and isinstance(selection, dict) and isinstance(selection.get('path'), str)
                    and isinstance(selection.get('link'), list) and len(selection['link']) == 4
                    and all(type(value) is int for value in selection['link'])):
                _absolute(selection['path'])
                self.previous = selection
        except (OSError, ValueError, AttributeError):
            pass

    def _remember(self, selection):
        _atomic_json(self.state_path, {'schema': 1, 'selection': selection}, 16384)

    def poll(self):
        """A stable changed choice returns bool; unavailable/unchanged returns None."""
        try:
            selection = _selection(self.home)
        except (OSError, ValueError, RuntimeError) as error:
            self.live, self.selected_path = False, None
            self.error = str(error)[:256]
            return None
        self.selected_path = selection['path']
        self.live = _live(Path(self.selected_path))
        if selection == self.previous:
            self.error = self._cache_error
            return None
        self.previous = selection
        try:
            self._remember(selection)
            self.error = self._cache_error = None
        except (OSError, ValueError, RuntimeError) as error:
            self.error = self._cache_error = 'Background observation not cached: ' + str(error)[:220]
        return self.live


@contextmanager
def _theme_lock(runtime):
    parent = _open_directory(runtime, private=True)
    fd = -1
    try:
        fd = os.open('omarchy-theme-set.lock', os.O_CREAT | os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK,
                     0o600, dir_fd=parent)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
            raise ValueError('The theme lock must be an independent owned regular file')
        deadline = time.monotonic() + 2
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise RuntimeError('A theme change is running; try again when it finishes')
                time.sleep(.025)
        yield
    finally:
        if fd >= 0:
            os.close(fd)
        os.close(parent)


def _theme(home):
    name_path = home / _CURRENT / 'theme.name'
    name = _read_bytes(name_path, 256).decode().strip()
    if (not name or len(name) > 128 or name.startswith('.') or Path(name).name != name
            or '\\' in name or any(ord(char) < 32 or ord(char) == 127 for char in name)):
        raise ValueError('The current theme name is invalid')
    name_fd = theme_fd = -1
    try:
        name_fd = _open_file(name_path)
        theme_fd = _open_directory(home / _CURRENT / 'theme')
        return {'name': name, 'name_identity': _identity(os.fstat(name_fd)),
                'directory_identity': _identity(os.fstat(theme_fd))[:2]}
    finally:
        if name_fd >= 0:
            os.close(name_fd)
        if theme_fd >= 0:
            os.close(theme_fd)


def _assert_theme(home, expected):
    if _theme(home) != expected:
        raise RuntimeError('The current theme changed; no image was removed')


def _personal(home, theme):
    return home / '.config/omarchy/backgrounds' / theme


def _names(folder):
    try:
        fd = _open_directory(folder)
    except FileNotFoundError:
        return []
    try:
        names = []
        with os.scandir(fd) as entries:
            for count, entry in enumerate(entries, 1):
                if count > MAX_DIRECTORY_ENTRIES:
                    raise ValueError('The background folder exceeds its entry limit')
                names.append(entry.name)
        return sorted(names)
    finally:
        os.close(fd)


def _first_live(folder):
    for name in _names(folder):
        path = folder / name
        if not _live(path):
            continue
        try:
            fd = _open_file(path)
            try:
                info = os.fstat(fd)
                if stat.S_ISREG(info.st_mode) and 0 < info.st_size <= MAX_IMAGE_BYTES:
                    return path
            finally:
                os.close(fd)
        except (OSError, ValueError):
            pass
    return None


def _file_record(path):
    fd = _open_file(path)
    try:
        before = os.fstat(fd)
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid() or before.st_nlink != 1
                or not 0 < before.st_size <= MAX_IMAGE_BYTES):
            raise ValueError('The image must be an independent bounded owned regular file')
        digest, total = hashlib.sha256(), 0
        while total <= MAX_IMAGE_BYTES:
            chunk = os.read(fd, min(65536, MAX_IMAGE_BYTES + 1 - total))
            if not chunk:
                break
            digest.update(chunk)
            total += len(chunk)
        after = os.fstat(fd)
        if (total != before.st_size or _identity(before) != _identity(after) or before.st_mode != after.st_mode):
            raise ValueError('The image changed during observation')
        return {'path': str(path), 'sha256': digest.hexdigest(), 'mode': stat.S_IMODE(after.st_mode),
                'identity': _identity(after)[:3]}
    finally:
        os.close(fd)


def _matches(path, record):
    try:
        actual = _file_record(path)
        return all(actual[key] == record.get(key) for key in ('path', 'sha256', 'mode', 'identity'))
    except (OSError, ValueError):
        return False


def _same_content(path, record):
    """A prior selection may survive a theme recopy; it is never deleted."""
    try:
        actual = _file_record(path)
        return all(actual[key] == record.get(key) for key in ('path', 'sha256', 'mode'))
    except (OSError, ValueError):
        return False


def _unlink_recorded(path, record):
    """Remove only the exact still-unchanged file described by this record."""
    if not _matches(path, record):
        raise RuntimeError('The marker changed; no image was removed')
    parent = _open_directory(path.parent)
    try:
        info = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
        if (_identity(info)[:3] != record['identity'] or not stat.S_ISREG(info.st_mode)
                or stat.S_IMODE(info.st_mode) != record['mode'] or info.st_uid != os.getuid() or info.st_nlink != 1):
            raise RuntimeError('The marker was replaced; no image was removed')
        os.unlink(path.name, dir_fd=parent)
        os.fsync(parent)
    finally:
        os.close(parent)


def _ledger_path(home, state_dir):
    return _state(home, state_dir) / 'live-backgrounds/ownership.json'


def _ledger(home, state_dir):
    try:
        value = json.loads(_read_bytes(_ledger_path(home, state_dir), MAX_STATE_BYTES))
    except FileNotFoundError:
        return {'schema': 1, 'images': {}, 'recovered': []}
    if (not isinstance(value, dict) or value.get('schema') != 1 or not isinstance(value.get('images'), dict)
            or not isinstance(value.get('recovered'), list)
            or len(value['images']) > MAX_RECORDS
            or len(value['recovered']) > MAX_RECORDS + MAX_RECOVERY_HISTORY
            or any(not isinstance(record, dict) for record in value['recovered'])):
        raise ValueError('The live background ownership journal is invalid or too large')
    _compact_ledger(home, value, interrupted=True)
    return value


def _trim_history(ledger):
    pending = [entry for entry in ledger['recovered'] if entry.get('state') == 'pending-removal']
    history = [entry for entry in ledger['recovered'] if entry.get('state') != 'pending-removal'][-MAX_RECOVERY_HISTORY:]
    ledger['recovered'] = history + pending
    while history and len((json.dumps(ledger, indent=2, allow_nan=False) + '\n').encode()) > MAX_STATE_BYTES:
        history.pop(0)
        ledger['recovered'] = history + pending


def _compact_ledger(home, ledger, interrupted=False):
    # Prune only stale ownership metadata; user images and all recovery images
    # stay on disk. The recovered/<theme>/ directory remains the full archive.
    for text, record in list(ledger['images'].items()):
        try:
            path = _absolute(text)
            valid = (isinstance(record, dict) and isinstance(record.get('theme'), str)
                     and path.parent == _personal(home, record['theme']) and _live(path)
                     and _matches(path, record))
        except (TypeError, ValueError):
            valid = False
        if not valid:
            ledger['images'].pop(text)
    if interrupted:
        for entry in ledger['recovered']:
            if entry.get('state') == 'pending-removal':
                entry['state'] = 'interrupted'
    _trim_history(ledger)


def _has_room(ledger, extra_image=0, pending=None):
    prospective = {'schema': 1, 'images': dict(ledger['images']), 'recovered': list(ledger['recovered'])}
    if pending is not None:
        prospective['recovered'].append(pending)
    _trim_history(prospective)
    unfinished = [entry for entry in prospective['recovered'] if entry.get('state') == 'pending-removal'
                  and entry.get('identity') != prospective['images'].get(entry.get('path'), {}).get('identity')]
    return (len(prospective['images']) + len(unfinished) + extra_image <= MAX_RECORDS
            and len((json.dumps(prospective, indent=2, allow_nan=False) + '\n').encode()) <= MAX_STATE_BYTES)


def _pending_record(home, state_dir, theme, path, record):
    recovery_folder = _state(home, state_dir) / 'live-backgrounds/recovered' / theme['name']
    expected = recovery_folder / (path.stem + '.' + ('0' * 24) + path.suffix)
    return dict(record, recovery=str(expected), state='pending-removal')


def _save_ledger(home, state_dir, ledger):
    _compact_ledger(home, ledger)
    if not _has_room(ledger):
        raise ValueError('The live background recovery journal is full')
    _atomic_json(_ledger_path(home, state_dir), ledger)


def _owned_choice(home, theme, ledger, selected):
    personal = _personal(home, theme['name'])
    candidates = []
    for text, record in ledger['images'].items():
        try:
            path = _absolute(text)
        except (ValueError, TypeError):
            continue
        if (isinstance(record, dict) and record.get('theme') == theme['name']
                and path.parent == personal and _live(path) and _matches(path, record)):
            candidates.append((path, record))
    candidates.sort(key=lambda item: (str(item[0]) != selected['path'], str(item[0])))
    return candidates[0] if candidates else None


def _static_choice(home, theme, record):
    prior = record.get('prior_static')
    if isinstance(prior, dict) and prior.get('theme') == theme['name']:
        try:
            path = _absolute(prior.get('path'))
            if _static_image(path) and _same_content(path, prior):
                return path
        except (OSError, ValueError, TypeError):
            pass
    for folder in (home / _CURRENT / 'theme/backgrounds', _personal(home, theme['name'])):
        for name in _names(folder):
            path = folder / name
            if _static_image(path):
                try:
                    _file_record(path)
                    return path
                except (OSError, ValueError):
                    pass
    return None


def live_wallpaper_metadata(home, state_dir=None):
    """Read-only menu metadata; a filename never establishes ownership."""
    result = {'name': '', 'live_choice_present': False, 'can_remove_live': False,
              'remove_reason': '', 'selected_path': None, 'owned_path': None}
    try:
        home = _home(home)
        theme, selected = _theme(home), _selection(home)
        result.update(name=theme['name'], selected_path=selected['path'])
        result['live_choice_present'] = bool(_first_live(home / _CURRENT / 'theme/backgrounds')
                                             or _first_live(_personal(home, theme['name'])))
        ledger = _ledger(home, state_dir)
        owned = _owned_choice(home, theme, ledger, selected)
        if owned is None:
            result['remove_reason'] = 'No unchanged Live Quake-owned personal marker; user images and edits are preserved'
            return result
        path, record = owned
        result['owned_path'] = str(path)
        if not _has_room(ledger, pending=_pending_record(home, state_dir, theme, path, record)):
            result['remove_reason'] = 'The live background recovery journal is full'
            return result
        if selected['path'] == str(path) and _static_choice(home, theme, record) is None:
            result['remove_reason'] = 'Select or add a static background before removing the selected marker'
            return result
        _assert_theme(home, theme)
        result.update(can_remove_live=True, remove_reason='Owned marker can be archived recoverably')
    except (OSError, ValueError, RuntimeError) as error:
        result['can_remove_live'] = False
        result['remove_reason'] = str(error)[:256]
    return result


def _pick(home, theme, select, target, expected=None):
    _assert_theme(home, theme)
    if expected is not None and _selection(home) != expected:
        raise RuntimeError('The background selection changed')
    if select(target) is False:
        raise RuntimeError('Background selection was cancelled')
    _assert_theme(home, theme)
    selected = _selection(home)
    if selected['path'] != str(target):
        raise RuntimeError('The background was not selected')
    return selected


def _publish(folder, name, payload, mode):
    parent = _open_directory(folder, create=True)
    temporary = '.live-quake-' + secrets.token_hex(12)
    try:
        info = os.fstat(parent)
        if info.st_uid != os.getuid():
            raise ValueError('The image folder must be owned by this user')
        fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
                     0o600, dir_fd=parent)
        try:
            view = memoryview(payload)
            while view:
                view = view[os.write(fd, view):]
            os.fchmod(fd, mode)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.link(temporary, name, src_dir_fd=parent, dst_dir_fd=parent, follow_symlinks=False)
        os.unlink(temporary, dir_fd=parent)
        os.fsync(parent)
        return folder / name
    finally:
        try:
            os.unlink(temporary, dir_fd=parent)
        except FileNotFoundError:
            pass
        os.close(parent)


def use_live_wallpaper(home, fallback, select, runtime, state_dir=None):
    """Reuse a choice or publish a recorded marker, then invoke the caller's picker."""
    home, fallback = _home(home), _absolute(fallback)
    with _theme_lock(runtime):
        theme, selected = _theme(home), _selection(home)
        personal = _personal(home, theme['name'])
        target = _first_live(home / _CURRENT / 'theme/backgrounds') or _first_live(personal)
        ledger = _ledger(home, state_dir)
        prior = None
        chosen = Path(selected['path'])
        if _static_image(chosen):
            try:
                prior = dict(_file_record(chosen), theme=theme['name'])
            except (OSError, ValueError):
                pass
        if target is None:
            if not _has_room(ledger, extra_image=1):
                raise ValueError('The live background recovery journal is full')
            if fallback.suffix.casefold() not in IMAGE_SUFFIXES:
                raise ValueError('The bundled marker must be an image')
            _file_record(fallback)
            payload = _read_bytes(fallback, MAX_IMAGE_BYTES)
            suffix = fallback.suffix.casefold()
            name = '1-live-quake' + suffix
            # A directory/link collision never replaces or adopts user data.
            if name in _names(personal):
                name = '1-live-quake-' + hashlib.sha256(payload).hexdigest() + suffix
            _assert_theme(home, theme)
            target = _publish(personal, name, payload, 0o644)
            record = dict(_file_record(target), theme=theme['name'], prior_static=prior)
            ledger['images'][str(target)] = record
            try:
                _save_ledger(home, state_dir, ledger)
            except (OSError, ValueError, RuntimeError):
                # Do not leave an unrecorded product copy after failed
                # ownership persistence; a subsequent user edit is preserved.
                try:
                    _unlink_recorded(target, record)
                except (OSError, ValueError, RuntimeError):
                    pass
                raise
        elif isinstance(ledger['images'].get(str(target)), dict) and _matches(target, ledger['images'][str(target)]) and prior:
            ledger['images'][str(target)]['prior_static'] = prior
            _save_ledger(home, state_dir, ledger)
        _pick(home, theme, select, target)
        return target


def remove_live_wallpaper(home, select, runtime, state_dir=None):
    """Archive one unchanged owned marker, switching to a verified static choice first."""
    home = _home(home)
    with _theme_lock(runtime):
        theme, selected = _theme(home), _selection(home)
        ledger = _ledger(home, state_dir)
        owned = _owned_choice(home, theme, ledger, selected)
        if owned is None:
            raise RuntimeError('No unchanged Live Quake-owned personal marker can be removed')
        path, record = owned
        if not _has_room(ledger, pending=_pending_record(home, state_dir, theme, path, record)):
            raise ValueError('The live background recovery journal is full')
        switched = removed = False
        replacement = replacement_record = archived = None
        expected = selected
        try:
            if selected['path'] == str(path):
                replacement = _static_choice(home, theme, record)
                if replacement is None:
                    raise RuntimeError('No safe static background is available')
                replacement_record = _file_record(replacement)
                switched = True
                try:
                    expected = _pick(home, theme, select, replacement, selected)
                except Exception:
                    # The stock picker can change its link before IPC fails.
                    # Capture only that exact replacement for guarded rollback.
                    try:
                        observed = _selection(home)
                        if observed['path'] == str(replacement) and _matches(replacement, replacement_record):
                            expected = observed
                    except (OSError, ValueError, RuntimeError):
                        pass
                    raise
            _assert_theme(home, theme)
            if (_selection(home) != expected or expected['path'] == str(path) or not _matches(path, record)):
                raise RuntimeError('The marker or selection changed; no image was removed')
            payload = _read_bytes(path, MAX_IMAGE_BYTES)
            if hashlib.sha256(payload).hexdigest() != record['sha256']:
                raise RuntimeError('The marker changed; no image was removed')
            recovery_folder = _state(home, state_dir) / 'live-backgrounds/recovered' / theme['name']
            recovery = _publish(recovery_folder, path.stem + '.' + secrets.token_hex(12) + path.suffix, payload, 0o600)
            archived = dict(record, recovery=str(recovery), state='pending-removal')
            ledger['recovered'].append(archived)
            _save_ledger(home, state_dir, ledger)
            _assert_theme(home, theme)
            if _selection(home) != expected or not _matches(path, record):
                raise RuntimeError('The theme, marker or selection changed; no image was removed')
            _unlink_recorded(path, record)
            removed = True
            archived['state'] = 'archived'
            ledger['images'].pop(str(path))
            _save_ledger(home, state_dir, ledger)
            return {'theme': theme['name'], 'removed': str(path), 'recovery': str(recovery),
                    'selected': _selection(home)['path']}
        except Exception:
            if archived is not None and not removed:
                archived['state'] = 'aborted'
                try:
                    _save_ledger(home, state_dir, ledger)
                except (OSError, ValueError, RuntimeError):
                    pass
            if switched and not removed and replacement is not None:
                try:
                    _assert_theme(home, theme)
                    if (_selection(home) == expected and expected['path'] == str(replacement)
                            and _matches(replacement, replacement_record) and _matches(path, record)):
                        _pick(home, theme, select, path, expected)
                except (OSError, ValueError, RuntimeError):
                    pass
            raise
