# SPDX-License-Identifier: 0BSD
"""Bounded, read-only Steam discovery and Quake 1 PAK validation.

No game launches, copies, extraction, downloads or writes occur here. Supplied
Steam roots and libraries named by Steam metadata are resolved once through symlinks;
every entry below those roots is opened relative to a directory descriptor
with O_NOFOLLOW. Aliased library directories are deduplicated by device/inode.
``compatible`` describes a structurally plausible package for an explicit
Ironwail policy (four supported layouts),
not a runtime test, ownership determination or authenticity check. Game checks
read bounded program signature tables and four-byte map format prefixes.
Each discovery has an 8 MiB aggregate metadata budget and cooperatively
checks cancellation between files and bounded metadata chunks. A blocked
kernel filesystem call cannot be interrupted by that cancellation.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
import hashlib
from pathlib import Path
import stat
import struct

MAX_VDF_BYTES = 1024 * 1024
MAX_SCAN_METADATA_BYTES = 8 * 1024 * 1024
MAX_VDF_TOKENS = 65536
MAX_VDF_DEPTH = 16
MAX_LIBRARIES = 32
MAX_LIBRARY_PATHS = 64
MAX_DIRECTORY_ENTRIES = 4096
MAX_PAK_ENTRIES = 2048
MAX_PAK_BYTES = 2147483647
MAX_PROG_FUNCTIONS = 8192
MAX_PROG_STRINGS = 1024 * 1024
MAX_KPF_ENTRIES = 4096
MAX_KPF_DIRECTORY_BYTES = 8 * 1024 * 1024
MAX_KPF_BYTES = MAX_PAK_BYTES


@dataclass(frozen=True)
class BackendCapabilities:
    name: str
    bsp_formats: tuple[bytes, ...]
    profile_namespace: str


IRONWAIL = BackendCapabilities('ironwail', (struct.pack('<i', 29), b'2PSB', b'BSP2'), 'ironwail-v2-')


def backend_capabilities(backend='ironwail'):
    """Closed explicit policy; callers cannot supply arbitrary weaker checks."""
    if backend != 'ironwail':
        raise DataError('Unsupported engine backend')
    return IRONWAIL

APP_TITLES = {
    2310: 'Quake', 9040: 'Quake Mission Pack 1: Scourge of Armagon',
    9030: 'Quake Mission Pack 2: Dissolution of Eternity',
    2320: 'Quake II', 2200: 'Quake III Arena', 2210: 'Quake 4',
    282440: 'Quake Live', 611500: 'Quake Champions', 1089130: 'Quake II RTX',
}
GAME_TITLES = {'id1': 'Quake (Original)', 'hipnotic': 'Scourge of Armagon',
               'rogue': 'Dissolution of Eternity'}
MISSION_APPS = {9040: 'hipnotic', 9030: 'rogue'}


class VDFError(ValueError):
    pass


class DataError(ValueError):
    pass


class MissingKPF(DataError):
    pass


class ScanAborted(RuntimeError):
    """The whole scan was cancelled or exceeded its aggregate resource limit."""


def _check_cancelled(cancelled):
    if cancelled is not None and cancelled():
        raise ScanAborted('Steam scan cancelled')


def parse_vdf(text: str) -> dict:
    """Parse the bounded string/object subset of Valve KeyValues metadata."""
    if not isinstance(text, str) or len(text.encode('utf-8')) > MAX_VDF_BYTES:
        raise VDFError('VDF exceeds the size limit')
    tokens = []
    position = 0
    escapes = {'"': '"', '\\': '\\', 'n': '\n', 'r': '\r', 't': '\t'}
    while position < len(text):
        char = text[position]
        if char.isspace() or (position == 0 and char == '\ufeff'):
            position += 1
            continue
        if text.startswith('//', position):
            end = text.find('\n', position + 2)
            position = len(text) if end == -1 else end + 1
            continue
        if char in '{}':
            tokens.append((char, char))
            position += 1
        elif char == '"':
            position += 1
            value = []
            while position < len(text) and text[position] != '"':
                char = text[position]
                if char == '\\':
                    position += 1
                    if position >= len(text) or text[position] not in escapes:
                        raise VDFError('Invalid VDF escape')
                    char = escapes[text[position]]
                if char == '\0':
                    raise VDFError('NUL in VDF string')
                value.append(char)
                position += 1
            if position == len(text):
                raise VDFError('Unterminated VDF string')
            position += 1
            tokens.append(('string', ''.join(value)))
        else:
            start = position
            while position < len(text) and not text[position].isspace() and text[position] not in '{}"':
                if text.startswith('//', position):
                    break
                position += 1
            value = text[start:position]
            if not value or '\0' in value:
                raise VDFError('Invalid VDF token')
            tokens.append(('string', value))
        if len(tokens) > MAX_VDF_TOKENS:
            raise VDFError('VDF exceeds the token limit')
    cursor = 0

    def object_value(depth, nested):
        nonlocal cursor
        if depth > MAX_VDF_DEPTH:
            raise VDFError('VDF exceeds the nesting limit')
        result, names = {}, set()
        while cursor < len(tokens):
            kind, key = tokens[cursor]
            cursor += 1
            if kind == '}':
                if not nested:
                    raise VDFError('Unexpected VDF closing brace')
                return result
            if kind != 'string' or not key or key.casefold() in names:
                raise VDFError('Invalid or duplicate VDF key')
            names.add(key.casefold())
            if cursor == len(tokens):
                raise VDFError('Missing VDF value')
            kind, value = tokens[cursor]
            cursor += 1
            if kind == '{':
                result[key] = object_value(depth + 1, True)
            elif kind == 'string':
                result[key] = value
            else:
                raise VDFError('Missing VDF value')
        if nested:
            raise VDFError('Unterminated VDF object')
        return result

    try:
        return object_value(0, False)
    finally:
        # The recursive function captures its own closure cell as well as the
        # token list. Break that cycle on success and failure, without waiting
        # for cyclic GC to release every parsed manifest's tokens.
        object_value = None


def _value(data, key, default=None):
    if not isinstance(data, dict):
        return default
    return next((value for name, value in data.items() if name.casefold() == key.casefold()), default)


def _components(path: Path):
    parts = path.parts
    if not path.is_absolute() or any(part in ('.', '..') for part in parts):
        raise DataError('Path must be absolute and contain no traversal')
    return parts[1:]


def _case_name(directory, wanted):
    """Resolve one finite name, rejecting ambiguous variants even if exact exists."""
    matches = []
    with os.scandir(directory) as entries:
        for index, entry in enumerate(entries):
            if index >= MAX_DIRECTORY_ENTRIES:
                raise DataError('Directory exceeds the case-lookup entry limit')
            if entry.name.casefold() == wanted.casefold():
                matches.append(entry.name)
    if len(matches) > 1:
        raise DataError('Ambiguous case variants for ' + wanted)
    return matches[0] if matches else wanted


def _fd_path(fd):
    """Obtain the actual letter case of the object already opened safely."""
    value = os.readlink(f'/proc/self/fd/{fd}')
    if value.endswith(' (deleted)') or not value.startswith('/'):
        raise DataError('Opened game object no longer has a usable absolute path')
    return Path(value)


def _open_relative(directory, components, *, is_directory=False, casefold=True):
    """Open all parent components, not just the leaf, without following links."""
    current = os.dup(directory)
    try:
        for index, part in enumerate(components):
            if not part or part in ('.', '..') or any(char in part for char in '/\\\0\n\r'):
                raise DataError('Invalid path component')
            flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK
            if index < len(components) - 1 or is_directory:
                flags |= os.O_DIRECTORY
            child = os.open(_case_name(current, part) if casefold else part, flags, dir_fd=current)
            os.close(current)
            current = child
        if is_directory and not stat.S_ISDIR(os.fstat(current).st_mode):
            raise DataError('Not a directory')
        result, current = current, -1
        return result
    finally:
        if current >= 0:
            os.close(current)


def _open_absolute_directory(path):
    root = os.open('/', os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        return _open_relative(root, _components(path), is_directory=True, casefold=False)
    finally:
        os.close(root)


def _read_metadata(directory, relative, *, budget=None, cancelled=None):
    _check_cancelled(cancelled)
    fd = _open_relative(directory, relative.split('/'))
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise DataError('Metadata is not a regular file')
        if info.st_size > MAX_VDF_BYTES:
            raise DataError('Metadata exceeds the size limit')
        if budget is not None and info.st_size > budget.metadata_remaining:
            raise ScanAborted('Steam scan exceeds the total metadata size limit')
        chunks, used = [], 0
        # Read the validated size only. Growth/shrink during this bounded read
        # is rejected rather than parsing uncharged appended metadata.
        while used < info.st_size:
            _check_cancelled(cancelled)
            chunk = os.read(fd, min(65536, info.st_size - used))
            if not chunk:
                break
            chunks.append(chunk)
            used += len(chunk)
            if budget is not None:
                budget.metadata_remaining -= len(chunk)
        _check_cancelled(cancelled)
        if used != info.st_size or os.fstat(fd).st_size != info.st_size:
            raise DataError('Metadata changed while reading')
        return parse_vdf(b''.join(chunks).decode('utf-8-sig'))
    finally:
        os.close(fd)


def _pak(fd, path):
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or not 12 <= info.st_size <= MAX_PAK_BYTES:
        raise DataError('PAK must be a bounded regular file with a complete header')
    header = os.pread(fd, 12, 0)
    if len(header) != 12:
        raise DataError('Truncated PAK header')
    magic, offset, length = struct.unpack('<4sii', header)
    if magic != b'PACK':
        raise DataError('Not a Quake PACK file')
    if (offset < 12 or length <= 0 or length % 64 or length > MAX_PAK_ENTRIES * 64
            or offset + length > info.st_size):
        raise DataError('Invalid PAK directory boundary or length')
    directory = os.pread(fd, length, offset)
    if len(directory) != length:
        raise DataError('Truncated PAK directory')
    files, seen, members = [], set(), {}
    for raw_name, position, size in struct.iter_unpack('<56sii', directory):
        if b'\0' not in raw_name:
            raise DataError('Unterminated PAK entry name')
        try:
            name = raw_name.split(b'\0', 1)[0].decode('ascii')
        except UnicodeError as error:
            raise DataError('Invalid PAK entry name') from error
        if (not name or name.startswith('/') or '\\' in name or any(ord(char) < 32 or ord(char) == 127 for char in name)
                or any(part in ('', '.', '..') for part in name.split('/')) or name.casefold() in seen):
            raise DataError('Unsafe or duplicate PAK entry name')
        if position < 12 or size < 0 or position + size > info.st_size:
            raise DataError('Invalid PAK payload boundary')
        if size and position < offset + length and position + size > offset:
            raise DataError('PAK payload overlaps its directory')
        seen.add(name.casefold())
        files.append(name)
        members[name.casefold()] = (position, size)
    return {'path': str(_fd_path(fd)), 'size': info.st_size, 'file_count': len(files), 'files': files, 'members': members}


def validate_pak(path: Path | str) -> dict:
    """Validate a PAK header, names and every payload boundary without extraction.

    All path components reject symlinks. No resource payload is read. A valid
    archive does not prove Quake 1 semantics or permission to redistribute it.
    """
    path = Path(path)
    root = os.open('/', os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        fd = _open_relative(root, _components(path), casefold=False)
        try:
            return _pak(fd, path)
        finally:
            os.close(fd)
    finally:
        os.close(root)


def _kpf(fd, path):
    """Bounded ordinary-ZIP directory/local-header checks, without extraction.

    This is archive framing metadata, not proof of payload authenticity,
    decompression success or complete Ironwail game/program compatibility.
    ZIP64 and encrypted entries are outside the four proven package layouts.
    """
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or not 22 <= info.st_size <= MAX_KPF_BYTES:
        raise DataError('KPF must be a bounded regular ZIP archive')
    tail_offset = max(0, info.st_size - 65557)
    tail = os.pread(fd, info.st_size - tail_offset, tail_offset)
    end = tail.rfind(b'PK\x05\x06')
    if end < 0 or end + 22 > len(tail):
        raise DataError('KPF ZIP directory is incomplete')
    signature, disk, directory_disk, disk_count, count, length, offset, comment = struct.unpack_from('<4s4H2IH', tail, end)
    if (disk or directory_disk or disk_count != count or not 1 <= count <= MAX_KPF_ENTRIES
            or not 46 <= length <= MAX_KPF_DIRECTORY_BYTES or offset + length != tail_offset + end
            or end + 22 + comment != len(tail)):
        raise DataError('KPF ZIP directory exceeds bounds or uses unsupported framing')
    directory = os.pread(fd, length, offset)
    if len(directory) != length:
        raise DataError('KPF ZIP directory is incomplete')
    position, seen, expanded_total = 0, set(), 0
    for unused in range(count):
        if position + 46 > length:
            raise DataError('KPF ZIP directory is incomplete')
        fields = struct.unpack_from('<4s6H3I5H2I', directory, position)
        sig, made, needed, flags, method, stamp, date, crc, compressed, expanded, name_length, extra, note, start_disk, attrs, external, local = fields
        if (sig != b'PK\x01\x02' or flags & 1 or method not in (0, 8) or start_disk
                or not 1 <= name_length <= 1024 or position + 46 + name_length + extra + note > length
                or compressed == 0xffffffff or expanded == 0xffffffff or local == 0xffffffff
                or expanded > MAX_KPF_BYTES or expanded_total + expanded > MAX_KPF_BYTES
                or stat.S_IFMT(external >> 16) == stat.S_IFLNK):
            raise DataError('KPF ZIP entry is unsupported or exceeds bounds')
        expanded_total += expanded
        raw_name = directory[position + 46:position + 46 + name_length]
        try:
            name = raw_name.decode('utf-8' if flags & 0x800 else 'cp437')
        except UnicodeError:
            raise DataError('Invalid KPF ZIP entry name') from None
        parts = name.removesuffix('/').split('/')
        if (not name or name.startswith('/') or '\\' in name or any(part in ('', '.', '..') for part in parts)
                or any(ord(char) < 32 or ord(char) == 127 for char in name) or name.casefold() in seen):
            raise DataError('Unsafe or duplicate KPF ZIP entry name')
        seen.add(name.casefold())
        if local + 30 > offset:
            raise DataError('KPF ZIP local header exceeds bounds')
        header = os.pread(fd, 30, local)
        if len(header) != 30:
            raise DataError('KPF ZIP local header is incomplete')
        local_fields = struct.unpack('<4s5H3I2H', header)
        if (local_fields[0] != b'PK\x03\x04' or local_fields[2] != flags or local_fields[3] != method
                or local_fields[9] != name_length or local + 30 + name_length + local_fields[10] + compressed > offset
                or os.pread(fd, name_length, local + 30) != raw_name):
            raise DataError('KPF ZIP local header differs or exceeds bounds')
        position += 46 + name_length + extra + note
    if position != length:
        raise DataError('KPF ZIP directory has unconsumed entries')
    return {'path': str(_fd_path(fd)), 'size': info.st_size, 'file_count': count,
            'directory_sha256': hashlib.sha256(directory).hexdigest()}


def validate_kpf(path):
    """Read only bounded KPF metadata; every source component is nofollow."""
    root = os.open('/', os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        fd = _open_relative(root, _components(Path(path)), casefold=False)
        try:
            return _kpf(fd, path)
        finally:
            os.close(fd)
    finally:
        os.close(root)


def _signature_check(fd, pak, *, strict_maps=True, backend='ironwail'):
    """Read bounded program tables and four-byte map prefixes; never execute."""
    capabilities = backend_capabilities(backend)
    members = pak['members']
    if 'progs.dat' in members:
        offset, size = members['progs.dat']
        if size < 60:
            raise DataError('Truncated Quake program header')
        raw = os.pread(fd, 60, offset)
        if len(raw) != 60:
            raise DataError('Truncated Quake program header')
        header = struct.unpack('<15i', raw)
        if header[0] != 6 or header[1] != 5927:
            raise DataError('Unsupported Quake program version or engine field-layout CRC')
        functions, count, strings, length = header[8:12]
        if (functions < 60 or count < 1 or count > MAX_PROG_FUNCTIONS or functions + count * 36 > size
                or strings < 60 or length < 1 or length > MAX_PROG_STRINGS or strings + length > size):
            raise DataError('Invalid or oversized Quake program signature tables')
        table = os.pread(fd, count * 36, offset + functions)
        names = os.pread(fd, length, offset + strings)
        if len(table) != count * 36 or len(names) != length:
            raise DataError('Truncated Quake program signature tables')
        last_terminator = names.rfind(b'\0')
        for index in range(count):
            name = struct.unpack_from('<i', table, index * 36 + 16)[0]
            if not 0 <= name < length:
                raise DataError('Quake program function name exceeds its string table')
            if name > last_terminator:
                raise DataError('Unterminated Quake program function name')
    unsupported = []
    for name, (offset, size) in members.items():
        if name.startswith('maps/') and name.endswith('.bsp'):
            if size < 4:
                raise DataError('Truncated BSP map header')
            magic = os.pread(fd, 4, offset)
            if magic not in capabilities.bsp_formats:
                if strict_maps:
                    raise DataError('Unsupported BSP map version')
                unsupported.append({'pak': pak['path'], 'member': name, 'format': magic.hex()})
    return unsupported


def _game_data(directory, basedir, gamedir, require_full, *, original_names=True, cancelled=None,
               backend='ironwail', edition='original', start_map=None):
    backend_capabilities(backend)
    if edition not in ('original', 'rerelease'):
        raise DataError('Unsupported game edition')
    enhanced = edition == 'rerelease'
    selected_map = 'start' if start_map is None else start_map
    if (not isinstance(selected_map, str) or not 1 <= len(selected_map) <= 48
            or not selected_map.isascii() or not all(char.isalnum() or char == '_' for char in selected_map)):
        raise DataError('Invalid selected start map')
    if enhanced and gamedir != 'id1':
        raise DataError('Enhanced mission packs have not been tested')
    if gamedir not in GAME_TITLES:
        raise DataError('Only id1, hipnotic and rogue are supported')
    paks, names, base_members, unsupported_maps = [], set(), {}, []
    registered = False
    for name in (('pak0.pak',) if enhanced else ('pak0.pak', 'pak1.pak')):
        _check_cancelled(cancelled)
        try:
            fd = _open_relative(directory, ['id1', name])
        except FileNotFoundError:
            if name == 'pak0.pak' or require_full:
                raise DataError('Original Quake requires id1/' + name)
            continue
        try:
            pak = _pak(fd, basedir / 'id1' / name)
            unsupported_maps += _signature_check(fd, pak, strict_maps=original_names, backend=backend)
        finally:
            os.close(fd)
        paks.append(pak['path'])
        names.update(value.casefold() for value in pak['files'])
        base_members[name] = set(pak['members'])
        registered |= name == 'pak1.pak'
    required = {'progs.dat', 'gfx.wad', 'gfx/palette.lmp'}
    if original_names:
        required |= {'maps/start.bsp', 'maps/e1m1.bsp'}
    missing = required - base_members['pak0.pak']
    if missing:
        raise DataError('Base pak0 lacks required Quake 1 members: ' + ', '.join(sorted(missing)))
    if not any(name.startswith('maps/') and name.endswith('.bsp') for name in names):
        raise DataError('Base PAKs lack a Quake map')
    if registered and original_names and 'maps/e2m1.bsp' not in base_members['pak1.pak']:
        raise DataError('Registered pak1 lacks required maps/e2m1.bsp')
    if gamedir != 'id1':
        _check_cancelled(cancelled)
        if not registered:
            raise DataError('Mission packs require full original Quake id1/pak1.pak')
        fd = _open_relative(directory, [gamedir, 'pak0.pak'])
        try:
            pak = _pak(fd, basedir / gamedir / 'pak0.pak')
            unsupported_maps += _signature_check(fd, pak, strict_maps=original_names, backend=backend)
        finally:
            os.close(fd)
        expansion = {name.casefold() for name in pak['files']}
        if ('progs.dat' not in expansion or not any(name.startswith('maps/') and name.endswith('.bsp') for name in expansion)
                or 'maps/' + selected_map + '.bsp' not in expansion):
            raise DataError('Mission PAK lacks Quake 1 program or maps')
        paks.append(pak['path'])
    reason = 'Original Quake 1 PAK layout validated; runtime not tested.' if original_names else 'Quake 1 PAK structure and program signatures validated; runtime not tested.'
    if unsupported_maps:
        reason += ' Some maps use unsupported formats; choose a separately tested map.'
    result = {'basedir': str(_fd_path(directory)), 'gamedir': gamedir, 'paks': paks,
            'game_args': [] if gamedir == 'id1' else ['-' + gamedir], 'start_map': 'start',
            'registered_data_present': registered, 'compatible': True,
            'reason': reason, 'runtime_tested': False, 'unsupported_maps': unsupported_maps}
    if gamedir == 'id1' and 'maps/' + selected_map + '.bsp' not in names:
        raise DataError('Selected package lacks its start map')
    if any(value['member'] == 'maps/' + selected_map + '.bsp' for value in unsupported_maps):
        raise DataError('Selected start map uses an unsupported format')
    result.update(backend=backend, edition=edition, start_map=selected_map,
                  reason='Early maps tested for Ironwail; campaign support is unproven.', kpf=None)
    # Step 1 starts private DM sessions on validated maps in the package. The
    # installed LibreQuake arena pack and configurable list arrive in step 4.
    unsupported = {value['member'] for value in unsupported_maps}
    candidates = [f'dm{i}' for i in range(1, 9)] + [f'lqdm{i}' for i in range(1, 14)]
    result['dm_maps'] = [name for name in candidates if f'maps/{name}.bsp' in names
                         and f'maps/{name}.bsp' not in unsupported]
    if enhanced:
        _check_cancelled(cancelled)
        try:
            fd = _open_relative(directory, ['QuakeEX.kpf'])
        except FileNotFoundError:
            raise MissingKPF('Enhanced data requires QuakeEX.kpf') from None
        try:
            result['kpf'] = _kpf(fd, basedir / 'QuakeEX.kpf')['path']
        finally:
            os.close(fd)
    return result


def validate_manual_data(basedir: Path | str, gamedir='id1', *, require_full=False,
                         backend='ironwail', edition='original', start_map=None) -> dict:
    """Inspect an explicitly chosen game root; reads only its known PAK paths.

    The explicit root can be a symlink. Entries inside it cannot. A pak0-only
    manual base is allowed; mission packs always require base pak0 and pak1.
    """
    basedir = Path(basedir).resolve(strict=True)
    fd = _open_absolute_directory(basedir)
    try:
        return _game_data(fd, basedir, gamedir, require_full, original_names=False,
                          backend=backend, edition=edition, start_map=start_map)
    finally:
        os.close(fd)


class _Scanner:
    def __init__(self, *, cancelled=None, backend='ironwail'):
        self.backend = backend_capabilities(backend).name
        if cancelled is not None and not callable(cancelled):
            raise TypeError('Steam scan cancellation must be callable')
        self.cancelled = cancelled
        self.metadata_remaining = MAX_SCAN_METADATA_BYTES
        self.warnings, self.scanned_paths, self.libraries = [], [], []
        self.steam_found = False
        self._scanned = set()

    def record(self, path):
        if str(path) not in self._scanned:
            self._scanned.add(str(path))
            self.scanned_paths.append(str(path))

    def warn(self, message):
        if message not in self.warnings:
            self.warnings.append(message)

    def metadata(self, root, fd, relative):
        _check_cancelled(self.cancelled)
        self.record(root / relative)
        try:
            metadata = _read_metadata(fd, relative, budget=self, cancelled=self.cancelled)
            _check_cancelled(self.cancelled)
            return metadata
        except FileNotFoundError:
            return None
        except (OSError, UnicodeError, ValueError) as error:
            self.warn(f'Unable to read Steam metadata {root / relative}: {error}')
            return None

    def roots(self, explicit):
        pending = [Path(path) for path in explicit[:MAX_LIBRARY_PATHS]]
        if len(explicit) > MAX_LIBRARY_PATHS:
            self.warn('Steam root list exceeds the path limit')
        seen, attempted = set(), set()
        while pending and len(self.libraries) < MAX_LIBRARIES and len(attempted) < MAX_LIBRARY_PATHS:
            _check_cancelled(self.cancelled)
            requested = pending.pop(0)
            if requested in attempted:
                continue
            attempted.add(requested)
            self.record(requested)
            try:
                # Metadata may name a library on a symlinked mount path.
                # Canonicalize this root only; all entries below remain nofollow.
                path = requested.resolve(strict=True)
                fd = _open_absolute_directory(path)
            except FileNotFoundError:
                continue
            except (OSError, RuntimeError, ValueError) as error:
                self.warn(f'Steam root unavailable {requested}: {error}')
                continue
            identity = os.fstat(fd)
            key = identity.st_dev, identity.st_ino
            if key in seen:
                os.close(fd)
                continue
            seen.add(key)
            self.libraries.append((path, fd))
            # A supplied empty directory is not evidence of a Steam install.
            try:
                apps = _open_relative(fd, ['steamapps'], is_directory=True)
                os.close(apps)
                self.steam_found = True
            except FileNotFoundError:
                pass
            except (OSError, ValueError) as error:
                self.warn(f'Steam apps unavailable {path}: {error}')
            for relative in ('steamapps/libraryfolders.vdf', 'config/libraryfolders.vdf'):
                metadata = self.metadata(path, fd, relative)
                folders = _value(metadata, 'libraryfolders', {})
                if not isinstance(folders, dict):
                    self.warn('Invalid libraryfolders object: ' + str(path / relative))
                    continue
                for index, (key, entry) in enumerate(folders.items()):
                    if index >= MAX_LIBRARIES:
                        self.warn('Steam library list exceeds the root limit')
                        break
                    if not key.isdecimal():
                        continue
                    value = _value(entry, 'path') if isinstance(entry, dict) else entry
                    if not isinstance(value, str) or not value or any(char in value for char in '\0\n\r'):
                        self.warn('Invalid Steam library path in ' + str(path / relative))
                        continue
                    candidate = Path(value)
                    try:
                        _components(candidate)
                    except ValueError:
                        self.warn('Invalid Steam library path in ' + str(path / relative))
                        continue
                    pending.append(candidate)
        if pending:
            self.warn('Steam library scan reached its root/path limit')

    @staticmethod
    def package(appid, app_title, identifier, title, basedir, gamedir='id1', edition='original'):
        return {'id': identifier, 'title': title, 'appid': appid,
                'source': {'title': app_title, 'appid': appid, 'artwork': {'logo': None, 'cover': None}},
                'edition': edition, 'basedir': str(basedir), 'gamedir': gamedir,
                'paks': [], 'game_args': [] if gamedir == 'id1' else ['-' + gamedir],
                'start_map': 'start', 'compatible': False, 'reason': '',
                'store': f'https://store.steampowered.com/app/{2310 if appid in MISSION_APPS else appid}/', 'runtime_tested': False}

    @staticmethod
    def install_advisory(app):
        raw = _value(app, 'StateFlags')
        if raw is None:
            return None, []
        if not isinstance(raw, str) or not raw.isdecimal() or len(raw) > 10 or int(raw) > 2147483647:
            return None, ['Steam install state is unknown; data may be incomplete.']
        flags = int(raw)
        pending = 2 | 256 | 512 | 1024 | 2048 | 65536 | 131072 | 262144 | 524288 | 1048576 | 2097152 | 4194304 | 8388608
        if flags & pending:
            return flags, ['Steam reports an update or download pending/in progress; data may be incomplete.']
        if not flags & 4:
            return flags, ['Steam does not report this app as fully installed; data may be incomplete.']
        return flags, []

    def packages(self):
        result, seen_games = {}, set()
        for root, fd in self.libraries:
            _check_cancelled(self.cancelled)
            for appid, fallback in APP_TITLES.items():
                app = _value(self.metadata(root, fd, f'steamapps/appmanifest_{appid}.acf'), 'AppState')
                if app is None:
                    continue
                folder = _value(app, 'installdir')
                if (_value(app, 'appid') != str(appid) or not isinstance(folder, str) or not folder
                        or folder in ('.', '..') or any(char in folder for char in '/\\\0\n\r')):
                    self.warn(f'Invalid Steam app manifest for {appid} in {root}')
                    continue
                basedir = root / 'steamapps/common' / folder
                self.record(basedir)
                app_title = _value(app, 'name', fallback)
                if (not isinstance(app_title, str) or not app_title.strip() or len(app_title) > 128
                        or any(char in app_title for char in '\0\n\r')):
                    app_title = fallback
                try:
                    install = _open_relative(fd, ['steamapps', 'common', folder], is_directory=True)
                except (OSError, ValueError) as error:
                    self.warn(f'Steam install unavailable {basedir}: {error}')
                    continue
                try:
                    basedir = _fd_path(install)
                    flags, advisories = self.install_advisory(app)
                    for advisory in advisories:
                        self.warn(f'{app_title}: {advisory}')
                    if appid not in (2310, *MISSION_APPS):
                        row = self.package(appid, app_title, 'steam-app-' + str(appid), fallback, basedir)
                        row.update(gamedir=None, game_args=[], start_map=None,
                                   reason='Not Quake 1 data; this prototype runs Quake 1 only.')
                        self.select(result, row, flags, advisories)
                        continue
                    games = GAME_TITLES.items() if appid == 2310 else [(MISSION_APPS[appid], GAME_TITLES[MISSION_APPS[appid]])]
                    for gamedir, title in games:
                        _check_cancelled(self.cancelled)
                        if gamedir != 'id1' and not self.directory_exists(install, basedir, gamedir):
                            continue
                        try:
                            game = _open_relative(install, [gamedir], is_directory=True)
                        except (OSError, ValueError):
                            key = (str(basedir), gamedir)
                        else:
                            try:
                                info = os.fstat(game)
                                key = (info.st_dev, info.st_ino)
                            finally:
                                os.close(game)
                        if key in seen_games:
                            continue
                        seen_games.add(key)
                        row = self.package(appid, app_title, 'steam-quake' if gamedir == 'id1' else 'steam-' + gamedir,
                                           title, basedir, gamedir)
                        for relative in ('id1/pak0.pak', 'id1/pak1.pak'):
                            self.record(basedir / relative)
                        if gamedir != 'id1':
                            self.record(basedir / gamedir / 'pak0.pak')
                        try:
                            row.update(_game_data(install, basedir, gamedir, True, cancelled=self.cancelled, backend=self.backend))
                        except (OSError, ValueError) as error:
                            row['reason'] = 'Original data unavailable or invalid: ' + str(error)
                        self.select(result, row, flags, advisories)
                    if appid == 2310 and self.directory_exists(install, basedir, 'rerelease'):
                        rerelease = _open_relative(install, ['rerelease'], is_directory=True)
                        try:
                            release_path = _fd_path(rerelease)
                            row = self.package(appid, app_title, 'steam-quake-rerelease', 'Quake (Enhanced)',
                                               release_path, edition='rerelease')
                            try:
                                row.update(_game_data(rerelease, release_path, 'id1', False, cancelled=self.cancelled,
                                                      backend=self.backend, edition='rerelease'))
                            except (OSError, ValueError) as error:
                                row['reason'] = 'Enhanced data is unavailable or invalid.'
                                if isinstance(error, MissingKPF):
                                    row.update(reason='Enhanced data requires QuakeEX.kpf.', availability='missing-kpf')
                        finally:
                            os.close(rerelease)
                        self.select(result, row, flags, advisories)
                finally:
                    os.close(install)
        return list(result.values())

    def directory_exists(self, directory, path, name):
        self.record(path / name)
        try:
            fd = _open_relative(directory, [name], is_directory=True)
        except FileNotFoundError:
            return False
        except (OSError, ValueError) as error:
            self.warn(f'Game directory unavailable {path / name}: {error}')
            return False
        os.close(fd)
        return True

    def select(self, result, row, flags=None, advisories=()):
        row['state_flags'] = flags
        row['advisories'] = list(advisories)
        if advisories:
            row['reason'] += ' ' + ' '.join(advisories)
        old = result.get(row['id'])
        if old is None or (row['compatible'] and not old['compatible']):
            result[row['id']] = row

    def close(self):
        for _, fd in self.libraries:
            os.close(fd)


def _scan_summary(count, steam_found):
    if count:
        noun = 'package' if count == 1 else 'packages'
        return f'Found {count} Quake 1 {noun}; untested here.'
    return 'No compatible Quake 1 packages found.' if steam_found else 'Steam was not found.'


def discover_roots(steam_roots: list[Path], *, cancelled=None, backend='ironwail') -> dict:
    """Explicit roots; abort wholly on cancellation or aggregate metadata limit.

    No partial result is returned on ScanAborted. All opened descriptors are
    closed while unwinding. Existing direct callers need no cancellation hook.
    """
    scanner = _Scanner(cancelled=cancelled, backend=backend)
    try:
        scanner.roots(steam_roots)
        packages = scanner.packages()
        count = sum(package['compatible'] for package in packages)
        return {'packages': packages, 'warnings': scanner.warnings, 'scanned_paths': scanner.scanned_paths,
                'steam_found': scanner.steam_found,
                'summary': _scan_summary(count, scanner.steam_found)}
    finally:
        scanner.close()


def discover(home: Path | str | None = None, *, cancelled=None, backend='ironwail') -> dict:
    """Inspect finite conventional Steam roots under the selected home, read-only.

    cancelled is an optional callable used by the catalog worker. It does not
    change the explicit-HOME direct-call interface or cancel blocked kernel I/O.
    """
    home = Path.home() if home is None else Path(home)
    names = ('.local/share/Steam', '.steam/steam', '.steam/root', '.steam/debian-installation',
             '.var/app/com.valvesoftware.Steam/.local/share/Steam',
             '.var/app/com.valvesoftware.Steam/data/Steam')
    return discover_roots([home / name for name in names], cancelled=cancelled, backend=backend)
