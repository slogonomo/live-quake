# SPDX-License-Identifier: 0BSD
"""Private lowercase PAK links without copying original or enhanced game data.

Only the declared archives are exposed. Steam's loose config files, scripts,
saves and other loose game files never enter this view. Explicit Iron enhanced
bindings also expose only the validated root QuakeEX.kpf archive link.
This is a point-in-time archive check;
the owner of the source installation can still replace its files afterward.
The frontend must select its game explicitly rather than infer it from a path.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
import re
import stat
import tempfile

from steam_games import DataError, backend_capabilities, validate_kpf, validate_pak

MAX_PAKS = 15
MAX_DIRECTORY_ENTRIES = 4096
_GAMES = {'id1', 'hipnotic', 'rogue'}
_PAK = re.compile(r'pak([0-9]{1,2})\.pak', re.IGNORECASE)
_PROJECT = Path(__file__).resolve().parents[1]
_OPEN = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK


def _absolute(value, label):
    try:
        raw = os.fspath(value)
    except TypeError as error:
        raise DataError(label + ' must be an absolute path without traversal') from error
    if (not isinstance(raw, str) or not raw.startswith('/') or raw.startswith('//')
            or any(ord(char) < 32 or ord(char) == 127 for char in raw)
            or '\\' in raw or any(part in ('.', '..', '') for part in raw.split('/')[1:])):
        raise DataError(label + ' must be an absolute path without traversal')
    return Path(raw)


def _open_directory(path):
    current = os.open('/', _OPEN | os.O_DIRECTORY)
    try:
        for part in path.parts[1:]:
            child = os.open(part, _OPEN | os.O_DIRECTORY, dir_fd=current)
            os.close(current)
            current = child
        result, current = current, -1
        return result
    finally:
        if current >= 0:
            os.close(current)


def _identity(info):
    return info.st_dev, info.st_ino


def _private_directory(fd, label):
    info = os.fstat(fd)
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o700):
        raise DataError(label + ' must be an owned 0700 directory')
    return _identity(info)


def _case_name(fd, requested):
    matches = []
    with os.scandir(fd) as entries:
        for count, entry in enumerate(entries, 1):
            if count > MAX_DIRECTORY_ENTRIES:
                raise DataError('Source directory exceeds the entry limit')
            if entry.name.casefold() == requested.casefold():
                matches.append(entry.name)
    if len(matches) != 1:
        raise DataError('Missing or ambiguous source component: ' + requested)
    return matches[0]


def _source_pak(base_fd, base, components, validator=validate_pak):
    current, names = os.dup(base_fd), []
    try:
        for index, requested in enumerate(components):
            name = _case_name(current, requested)
            flags = _OPEN | (os.O_DIRECTORY if index < len(components) - 1 else 0)
            child = os.open(name, flags, dir_fd=current)
            os.close(current)
            current = child
            names.append(name)
        before = os.fstat(current)
        if not stat.S_ISREG(before.st_mode):
            raise DataError('Source PAK is not a regular file')
        actual = base.joinpath(*names)
        validator(actual)
        # Reject replacement during validation, including a newly inserted link.
        parent_fd = _open_parent(base_fd, names[0]) if len(names) == 2 else os.dup(base_fd)
        try:
            check = os.open(names[-1], _OPEN, dir_fd=parent_fd)
        finally:
            os.close(parent_fd)
        try:
            after = os.fstat(check)
            if (_identity(before), before.st_size, before.st_mtime_ns) != (
                    _identity(after), after.st_size, after.st_mtime_ns):
                raise DataError('Source PAK changed during validation')
        finally:
            os.close(check)
        return actual
    finally:
        os.close(current)


def _open_parent(base_fd, name):
    """Return a short-lived parent descriptor; caller must close it."""
    return os.open(name, _OPEN | os.O_DIRECTORY, dir_fd=base_fd)


def _farm_names(fd):
    names = set()
    with os.scandir(fd) as entries:
        for count, entry in enumerate(entries, 1):
            if count > MAX_PAKS:
                raise DataError('Farm has unexpected entries; refusing cleanup')
            names.add(entry.name)
    return names


@dataclass
class DataFarm:
    root: Path
    pak: Path
    gamedir: str
    _parent: Path = field(repr=False)
    _parent_identity: tuple = field(repr=False)
    _root_identity: tuple = field(repr=False)
    _directories: dict = field(repr=False)
    _links: dict = field(repr=False)
    _removed: bool = field(default=False, repr=False)
    _root_links: dict = field(default_factory=dict, repr=False)
    kpf: Path | None = None
    backend: str = 'ironwail'

    def as_dict(self):
        result = {'root': str(self.root), 'pak': str(self.pak), 'gamedir': self.gamedir}
        if self.kpf is not None:
            result['kpf'] = str(self.kpf)
        result['backend'] = self.backend
        return result

    def cleanup(self):
        """Remove only this exact, unchanged farm; refuse unexpected contents."""
        if self._removed:
            return
        parent_fd = _open_directory(self._parent)
        root_fd, directories = -1, {}
        try:
            if _private_directory(parent_fd, 'Farm parent') != self._parent_identity:
                raise DataError('Farm parent was replaced')
            root_fd = os.open(self.root.name, _OPEN | os.O_DIRECTORY, dir_fd=parent_fd)
            if _private_directory(root_fd, 'Farm') != self._root_identity:
                raise DataError('Farm was replaced')
            if _farm_names(root_fd) != set(self._directories) | set(self._root_links):
                raise DataError('Farm has unexpected entries; refusing cleanup')
            for leaf, (identity, target) in self._root_links.items():
                info = os.stat(leaf, dir_fd=root_fd, follow_symlinks=False)
                if (not stat.S_ISLNK(info.st_mode) or _identity(info) != identity
                        or os.readlink(leaf, dir_fd=root_fd) != target):
                    raise DataError('Farm auxiliary link was replaced')
            for name, identity in self._directories.items():
                fd = os.open(name, _OPEN | os.O_DIRECTORY, dir_fd=root_fd)
                directories[name] = fd
                if _private_directory(fd, 'Farm game directory') != identity:
                    raise DataError('Farm game directory was replaced')
                expected = {leaf for game, leaf in self._links if game == name}
                if _farm_names(fd) != expected:
                    raise DataError('Farm has unexpected entries; refusing cleanup')
                for leaf in expected:
                    identity, target = self._links[name, leaf]
                    info = os.stat(leaf, dir_fd=fd, follow_symlinks=False)
                    if (not stat.S_ISLNK(info.st_mode) or _identity(info) != identity
                            or os.readlink(leaf, dir_fd=fd) != target):
                        raise DataError('Farm PAK link was replaced')
            # Preflight checks above ensure cleanup cannot follow a substituted
            # directory or silently remove somebody else's extra file.
            for leaf, (identity, target) in self._root_links.items():
                info = os.stat(leaf, dir_fd=root_fd, follow_symlinks=False)
                if _identity(info) != identity or os.readlink(leaf, dir_fd=root_fd) != target:
                    raise DataError('Farm auxiliary link changed during cleanup')
                os.unlink(leaf, dir_fd=root_fd)
            for (name, leaf), (identity, target) in self._links.items():
                fd = directories[name]
                info = os.stat(leaf, dir_fd=fd, follow_symlinks=False)
                if _identity(info) != identity or os.readlink(leaf, dir_fd=fd) != target:
                    raise DataError('Farm PAK link changed during cleanup')
                os.unlink(leaf, dir_fd=fd)
            for name, fd in directories.items():
                if _identity(os.stat(name, dir_fd=root_fd, follow_symlinks=False)) != self._directories[name]:
                    raise DataError('Farm game directory changed during cleanup')
                os.close(fd)
                directories[name] = -1
                os.rmdir(name, dir_fd=root_fd)
            if _identity(os.stat(self.root.name, dir_fd=parent_fd,
                                 follow_symlinks=False)) != self._root_identity:
                raise DataError('Farm changed during cleanup')
            os.close(root_fd)
            root_fd = -1
            os.rmdir(self.root.name, dir_fd=parent_fd)
            self._removed = True
        finally:
            for fd in directories.values():
                if fd >= 0:
                    os.close(fd)
            if root_fd >= 0:
                os.close(root_fd)
            os.close(parent_fd)

    def __enter__(self):
        return self

    def __exit__(self, *unused):
        self.cleanup()


def create_farm(package, destination_parent, *, backend='ironwail'):
    """Create a random private view and return a lifetime/cleanup handle.

    The package contains an absolute ``basedir``, a selected ``gamedir`` and
    fewer than 16 ``paks``. Sources must be immediate id1/selected-game PAKs
    below that base. Every path component and archive rejects symbolic links.
    A caller must keep this handle until the engine stops, then call cleanup.
    """
    backend_capabilities(backend)
    if not isinstance(package, dict) or package.get('compatible') is False:
        raise DataError('A compatible package is required')
    edition = package.get('edition', 'original')
    if edition not in ('original', 'rerelease'):
        raise DataError('Unsupported game edition')
    gamedir = package.get('gamedir')
    if not isinstance(gamedir, str) or gamedir not in _GAMES:
        raise DataError('Only id1, hipnotic and rogue are supported')
    if edition == 'rerelease' and gamedir != 'id1':
        raise DataError('Enhanced mission packs have not been tested')
    base = _absolute(package.get('basedir', ''), 'Package base')
    parent = _absolute(destination_parent, 'Farm parent')
    if parent == _PROJECT or _PROJECT in parent.parents:
        raise DataError('Farm parent must be outside the project checkout')
    if parent == base or base in parent.parents:
        raise DataError('Farm parent must be outside the source installation')
    paks = package.get('paks')
    if not isinstance(paks, (list, tuple)) or not 1 <= len(paks) <= MAX_PAKS:
        raise DataError('Package must declare between 1 and 15 PAKs')
    parent_fd, base_fd = _open_directory(parent), -1
    try:
        parent_identity = _private_directory(parent_fd, 'Farm parent')
        base_fd = _open_directory(base)
        sources = {}
        for value in paks:
            path = _absolute(value, 'Source PAK')
            try:
                relative = path.relative_to(base)
            except ValueError as error:
                raise DataError('Source PAK lies outside the package base') from error
            if len(relative.parts) != 2:
                raise DataError('Source PAK must be directly inside a game directory')
            game, name = relative.parts
            game = game.casefold()
            match = _PAK.fullmatch(name)
            if (game not in {'id1', gamedir} or not match or int(match[1]) > 15
                    or name.casefold() != 'pak' + str(int(match[1])) + '.pak'):
                raise DataError('Unexpected source game directory or PAK filename')
            leaf = 'pak' + str(int(match[1])) + '.pak'
            key = game, leaf
            if key in sources:
                raise DataError('Ambiguous or duplicate package PAK')
            sources[key] = _source_pak(base_fd, base, relative.parts)
        kpf_source = None
        if edition == 'rerelease':
            kpf = _absolute(package.get('kpf', ''), 'Enhanced KPF')
            try:
                relative = kpf.relative_to(base)
            except ValueError:
                raise DataError('Enhanced KPF lies outside the package base') from None
            if len(relative.parts) != 1 or relative.name.casefold() != 'quakeex.kpf':
                raise DataError('Enhanced KPF must be directly inside the package base')
            kpf_source = _source_pak(base_fd, base, relative.parts, validate_kpf)
        elif package.get('kpf') is not None:
            raise DataError('Unexpected auxiliary archive for original data')
        required = {('id1', 'pak0.pak'), (gamedir, 'pak0.pak')}
        if gamedir != 'id1':
            required.add(('id1', 'pak1.pak'))
        if not required <= sources.keys():
            raise DataError('Package is missing its base or selected game PAK')
        for game in {key[0] for key in sources}:
            indexes = {int(name[3:-4]) for folder, name in sources if folder == game}
            if indexes != set(range(max(indexes) + 1)):
                raise DataError('PAK numbering must be contiguous from pak0')
        # The neutral basename is deliberately unrelated to a game name.
        root = Path(tempfile.mkdtemp(prefix='lq-data-', dir=parent))
        root_fd = os.open(root.name, _OPEN | os.O_DIRECTORY, dir_fd=parent_fd)
        directories, links = {}, {}
        farm = DataFarm(root, root / gamedir / 'pak0.pak', gamedir, parent,
                        parent_identity, _private_directory(root_fd, 'Farm'), directories, links, backend=backend)
        try:
            if kpf_source is not None:
                leaf = 'QuakeEX.kpf'
                os.symlink(str(kpf_source), leaf, dir_fd=root_fd)
                farm._root_links[leaf] = (_identity(os.stat(leaf, dir_fd=root_fd, follow_symlinks=False)), str(kpf_source))
                farm.kpf = root / leaf
            for game in sorted({key[0] for key in sources}):
                os.mkdir(game, 0o700, dir_fd=root_fd)
                fd = os.open(game, _OPEN | os.O_DIRECTORY, dir_fd=root_fd)
                try:
                    directories[game] = _private_directory(fd, 'Farm game directory')
                    for (folder, leaf), source in sorted(sources.items()):
                        if folder == game:
                            os.symlink(str(source), leaf, dir_fd=fd)
                            links[game, leaf] = (_identity(os.stat(leaf, dir_fd=fd,
                                                                 follow_symlinks=False)), str(source))
                finally:
                    os.close(fd)
        except BaseException:
            os.close(root_fd)
            root_fd = -1
            farm.cleanup()
            raise
        finally:
            if root_fd >= 0:
                os.close(root_fd)
        return farm
    finally:
        if base_fd >= 0:
            os.close(base_fd)
        os.close(parent_fd)
