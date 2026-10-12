# SPDX-License-Identifier: 0BSD
"""The LibreQuake deathmatch-maps pack: a standard Quake PACK built from pinned data.

The installer (and `scripts/bootstrap.py --fetch-data` for development trees)
builds `<build>/ironwail/frikbot/dm-maps.pak` from the loose lqdm maps in the
pinned LibreQuake v0.09-beta `full.zip`. The core adds it next to the bot pack
for every non-expansion package. It is generated data with its own pinned
member list, plus Live Quake-generated waypoint scripts from `src/waypoints/`.
Neither is part of the native build receipt.

Built from the pinned archive the pack is 24,381,658 bytes with SHA-256
`633b9e140ba1192fdc599b0fd4c54e4f3fbaa18d2a72ad79eb6e783383902462`.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
import stat
import struct

PACK_RELATIVE = 'ironwail/frikbot/dm-maps.pak'
ARCHIVE_PREFIX = 'full/id1/'
# Pack member name: (bytes, SHA-256), as found under full/id1/ in full.zip.
MEMBERS = {
    'maps/lqdm1.bsp': (1101760, 'eb1f9a74e489e32d82718e6366c29f92a65553c2c809b7b90f9862376b015708'),
    'maps/lqdm2.bsp': (848628, 'a2327d080cc62e4307cbdde8ed0ae376dc74b66d88b3fad8fb1b0f4cf7b8714a'),
    'maps/lqdm2.lit': (323648, '432b35650a0088ad29cdbc7ce689305af6883f173d457cff61435754a018e216'),
    'maps/lqdm3.bsp': (2623936, 'aeda1799bcf5166bf13beef2b354ba4aa4ac424a235a36c14fe4f5612fc36553'),
    'maps/lqdm3.lit': (1172708, '112e78e83d0561237c8f440a51d02de401901c691e1fc19ac45c2ef832487018'),
    'maps/lqdm4.bsp': (1446100, '7c9b4420a8a1b70a6743ceb9a7b6c87d13bc7967afaec1b7b80ce42375f35311'),
    'maps/lqdm4.lit': (1182032, '3db9879a425620c72ddf71e7161645c5fd46e80e40f5f90911e78124c0cadf74'),
    'maps/lqdm5.bsp': (2228932, '528a761134dfe057ec566bed9bcf97f379d74cb837823364c8fedea79619dec4'),
    'maps/lqdm5.lit': (716696, '00c82d3a73a6fb7e43dff7552a73f365937a38b05582f40893e54c49e9b0cf36'),
    'maps/lqdm6.bsp': (1855940, '4b43640199bd7bf53c6d7ffbc00a843673feba36cd77c78473f5a7bae7d21039'),
    'maps/lqdm6.lit': (636140, 'b55e180cbcb774f91be8c00c6336684970111cd56867541a3c4888b89c82a0ea'),
    'maps/lqdm7.bsp': (1603740, 'ef2dcf601eaaeab064224654e25bec5d97619cb2493a648ce4997f18f583a30d'),
    'maps/lqdm7.lit': (580952, '8a71a24fecd76118e4d114015f3963445e676b5b1b42a50fc6fc1a0d304d6cb2'),
    'maps/lqdm8.bsp': (1410832, '844f8eef6c7af366c2280d7eebf1d7766f6b2a271d6a886dee188d02f945f196'),
    'maps/lqdm8.lit': (499976, '41079a9d85da770a4a582fbeb69c6ce8caa5c5d4ebd484056426b02154b13096'),
    'maps/lqdm9.bsp': (2229944, 'a0897b5bf8b6917a978d89784b2a9c4c7f635d2c9faabbf1fd77f5a79a848dce'),
    'maps/lqdm10.bsp': (1344360, '42ed4a96a228b76a7ff14827e09671b9f2577f2c68c2bba43126e0eb18c305e6'),
    'maps/lqdm10.lit': (538976, '9ebb1e6491d3ef79e8fb57da440c610a5b25fd0cb9226c8d590ec7cfccca8818'),
    'maps/lqdm11.bsp': (376380, 'fefeee9549a780734467d187fe75e7290ad597d55043a4478562f2da971a6f8b'),
    'maps/lqdm12.bsp': (431576, 'bfdbe9ff8ff13838b14456254aa69b151ab030fd1736981b64cd36911fadddc0'),
    'maps/lqdm13.bsp': (1136132, '1619a212a21f31d483dc4ad5ea12a8e837923eee113e9a2d123cf7145306683f'),
}
WAYPOINT_DIRECTORY = Path(__file__).with_name('waypoints')
# Live Quake's stock-learning output, checked in paired fresh-match clips.
WAYPOINT_MEMBERS = {
    'maps/lqdm1.way': (6003, 'bf9d4c49e171e4a76c792e510db2b1e337d2ddd87972f175ae7d98e4f511c19f'),
    'maps/lqdm11.wa1': (3847, 'e60561c1017eb828eefd8bacb519512ee605240f4f906b57faf934428970603e'),
    'maps/lqdm11.way': (6784, 'fac958454bb912cc53a253c723564249ac61308bfd45ae08c2bcbbcfaa76c211'),
    'maps/lqdm12.wa1': (981, 'fb31c9a905af727cf0060ff48a4fe33ffd956a16295dcefff9566cfa8ad51527'),
    'maps/lqdm12.way': (6744, 'd7d4315d8a740fea407e0ec68a936bc87946c0622c034fbefb665aeb19d559df'),
    'maps/lqdm13.wa1': (792, '42a9be3ec607308a9c617523013db0827df148a4015724ccf0369d2eb4b548c9'),
    'maps/lqdm13.way': (6785, '65438ba82b2c88df5abf681efd1c8de59c877be126dab3b6b3e8108240de2e32'),
    'maps/lqdm2.way': (6444, '3882376a162d795840567b796e0cc0c5bab6e171ccb68ede95555939d9a52dbb'),
    'maps/lqdm3.wa1': (1385, 'e7baa3237569d13a1c9fad32448591a2f50eaf4747d026a300b0eb7b899bcc63'),
    'maps/lqdm3.way': (6767, 'e5a4ee467e61d62d66c6211753ea625396940a88255b50bff771bf2451316de9'),
    'maps/lqdm4.wa1': (981, '43660d8bd70071bfcb0cb58be513d4353a496a0b318eaa9b6ec06e76530ea0ae'),
    'maps/lqdm4.way': (6815, 'a93565b664f9b2010e41dab7608734d2754f5f16f22fdf7902bbd5bb4c4425f6'),
    'maps/lqdm5.wa1': (2508, '621d3ae83448288e0bd64a837169a9119e3fc8256b569dc404ed674d4f8c10b4'),
    'maps/lqdm5.way': (6706, '18c961bb059c7ea2644921ec971313f4a34fae40f787082dd6538d4dbba29fe9'),
    'maps/lqdm7.wa1': (4137, '40ed26168fc551c3105aff598eaeab1a2704886062ce98640504ff406f6cbf2f'),
    'maps/lqdm7.way': (6802, '159965466216a450a834ae17416fe68d1abc965ac7e43e2f7ed1ae0efdd9bf7a'),
    'maps/lqdm8.way': (6395, 'd917c47b9e677913608529c88b742a4cccd6978cdc273f17af79196b6e39389b'),
    'maps/lqdm9.wa1': (2046, '44e22badf24477505eb0a79f27b9fcb80d45426669bfa785f7731788c949b91c'),
    'maps/lqdm9.way': (6776, '89d42f8f4525f7f5c2d08cda40c96c4368457d9c6d6b2767486fdf33018f2b67'),
}
_HEADER = struct.Struct('<4sii')
_ENTRY = struct.Struct('<56sii')
_MAP = re.compile(r'maps/(lqdm[0-9]{1,2})\.bsp')
_MAX_ENTRIES = 1024
_MAX_PACK = 2 ** 31 - 1


def build_pack(members: dict[str, bytes]) -> bytes:
    """Return a deterministic PACK: header, payloads and directory in sorted name order."""
    offset, directory, payloads = _HEADER.size, [], []
    for name in sorted(members):
        encoded = name.encode('ascii')
        if not 0 < len(encoded) < _ENTRY.size - 8 or b'\0' in encoded:
            raise ValueError('Invalid PACK member name: ' + name)
        payload = bytes(members[name])
        directory.append(_ENTRY.pack(encoded, offset, len(payload)))
        payloads.append(payload)
        offset += len(payload)
    if not directory or offset + len(directory) * _ENTRY.size > _MAX_PACK:
        raise ValueError('PACK must hold 1 to 2 GiB of members')
    return _HEADER.pack(b'PACK', offset, len(directory) * _ENTRY.size) + b''.join(payloads) + b''.join(directory)


def pack_from_archive(package) -> bytes:
    """Read the pinned members from an open LibreQuake ZipFile, verify them and build the pack."""
    members = {}
    for name, (size, digest) in MEMBERS.items():
        try:
            info = package.getinfo(ARCHIVE_PREFIX + name)
        except KeyError:
            raise ValueError('LibreQuake archive lacks DM map ' + name) from None
        if info.is_dir() or info.file_size != size:
            raise ValueError('LibreQuake DM map differs from its pinned size: ' + name)
        with package.open(info) as stream:
            payload = stream.read(size + 1)
        if len(payload) != size or hashlib.sha256(payload).hexdigest() != digest:
            raise ValueError('LibreQuake DM map differs from its pinned SHA-256: ' + name)
        members[name] = payload
    members.update(waypoint_members())
    return build_pack(members)


def waypoint_members() -> dict[str, bytes]:
    """Read our generated scripts, with the same size/hash pins as the map data."""
    members = {}
    for name, (size, digest) in WAYPOINT_MEMBERS.items():
        path = WAYPOINT_DIRECTORY / Path(name).name
        with path.open('rb') as stream:
            payload = stream.read(size + 1)
        if len(payload) != size or hashlib.sha256(payload).hexdigest() != digest:
            raise ValueError('Live Quake waypoint differs from its pinned size/SHA-256: ' + name)
        members[name] = payload
    return members


def installed_lqdm_maps(build: Path) -> list[str]:
    """Pinned lqdm map names whose BSP is in a valid installed pack; reads only header and directory."""
    pinned = {match.group(1) for match in map(_MAP.fullmatch, MEMBERS) if match}
    try:
        fd = os.open(Path(build) / PACK_RELATIVE, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK)
    except (OSError, ValueError):
        return []
    try:
        info = os.fstat(fd)
        size = info.st_size
        if not stat.S_ISREG(info.st_mode):
            return []
        header = os.pread(fd, _HEADER.size, 0)
        if len(header) != _HEADER.size:
            return []
        magic, offset, length = _HEADER.unpack(header)
        if (magic != b'PACK' or length <= 0 or length % _ENTRY.size or length > _MAX_ENTRIES * _ENTRY.size
                or offset < _HEADER.size or offset + length > size):
            return []
        directory = os.pread(fd, length, offset)
        if len(directory) != length:
            return []
        found = set()
        for raw, position, member_size in _ENTRY.iter_unpack(directory):
            if position < _HEADER.size or member_size < 0 or position + member_size > size:
                return []
            match = _MAP.fullmatch(raw.split(b'\0', 1)[0].decode('latin-1'))
            if match and match.group(1) in pinned:
                found.add(match.group(1))
    except OSError:
        return []
    finally:
        os.close(fd)
    return sorted(found, key=lambda name: int(name[4:]))


def main(argv=None):
    """Development helper: `python3 -I src/dm_pack.py <full.zip> <out.pak>` (refuses an existing output)."""
    import argparse
    import json
    import zipfile
    parser = argparse.ArgumentParser(description='Build the pinned LibreQuake DM-maps pack from full.zip.')
    parser.add_argument('archive', type=Path)
    parser.add_argument('output', type=Path)
    args = parser.parse_args(argv)
    with zipfile.ZipFile(args.archive) as package:
        payload = pack_from_archive(package)
    with open(args.output, 'xb') as output:
        output.write(payload)
    print(json.dumps({'path': str(args.output), 'bytes': len(payload),
                      'sha256': hashlib.sha256(payload).hexdigest()}), flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
