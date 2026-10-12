#!/usr/bin/env python3
# SPDX-License-Identifier: 0BSD
"""Extract Quake menu art from the user's own PAK files for the Live Quake menu.

Nothing from id Software is redistributed: the menu ships no font, plaque or sound. At
runtime this reads the console font (CONCHARS in gfx.wad), a few menu pictures (gfx/*.lmp)
and the three menu sounds from whichever game data the controller reports in
`settings.game.data.paks`, and caches them as PNG/WAV files keyed by their content.
LibreQuake's equivalents are BSD-licensed, so the free data works the same way.

Usage:
    pakart.py export <cache-root> <pak>...   # prints the cache index as JSON
PAKs are given in engine search order: a later PAK overrides an earlier one, as in the engine.
Pure standard library (zlib PNG writer), so it runs anywhere python3 does.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import stat
import struct
import sys
import zlib
from pathlib import Path

CACHE_VERSION = 4
MAX_PAKS = 8
MAX_PAK_FILES = 4096              # the engine's own limit is 2048 (MAX_FILES_IN_PACK)
MAX_PIC_W, MAX_PIC_H = 320, 200   # menu pictures fit the engine's 320x200 menu canvas
MAX_PICTURE_BYTES = 8 + MAX_PIC_W * MAX_PIC_H
MAX_SOUND_BYTES = 1 << 20
MAX_WAD_BYTES = 4 << 20
MAX_WAD_LUMPS = 1024
MAX_INDEX_BYTES = 1 << 20
CACHED_NAME = re.compile(r"^[a-z0-9_]{1,24}\.(png|wav)$")
DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC

# Menu pictures: gfx/<name>.lmp (qpic: int32 width, int32 height, 8-bit indices; 255 is transparent)
PICTURES = ("qplaque", "p_option", "ttl_main", "menudot1", "menudot2", "menudot3", "menudot4",
            "menudot5", "menudot6", "box_tl", "box_tm", "box_tr", "box_ml", "box_mm", "box_mm2",
            "box_mr", "box_bl", "box_bm", "box_br")
REQUIRED = ("p_option",)            # the OPTIONS title plaque; everything else is optional
# The menu's three cues, as the engine uses them (menu.c): move, enter, adjust/back.
SOUNDS = {"move": "sound/misc/menu1.wav", "enter": "sound/misc/menu2.wav", "adjust": "sound/misc/menu3.wav"}
# The menu's legible inks: one copy of the console font per ink (conchars_<ink>.png), its text glyphs recoloured for
# the menu's near-black #0b0907 panel. MenuCard.qml names them, and QuakeText.qml repeats them for its no-art
# fallback. Part of the cache key, so a change here re-exports the art.
INKS = {"bone": (0xE6, 0xD6, 0xBC),     # labels and headings (13.9:1 on the panel)
        "hi": (0xFF, 0xF1, 0xD8),       # the selected row's label
        "amber": (0xE8, 0xB4, 0x65),    # values, and only values (10.6:1)
        "dim": (0xAB, 0x9D, 0x86),      # the live status line under the title (7.5:1)
        "faint": (0x85, 0x79, 0x65)}    # rows that cannot be changed (4.7:1)


class Pak:
    """One PACK file, read with bounded pread calls on an open descriptor. No mapping: a file
    that shrinks or fails underneath us gives a short read or EIO (a clean error), never SIGBUS."""

    def __init__(self, path: Path):
        self.fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
        try:
            st = os.fstat(self.fd)
            if not stat.S_ISREG(st.st_mode) or st.st_size < 12:
                raise ValueError(f"{path} is not a PAK")
            size = st.st_size
            magic, offset, length = struct.unpack("<4sii", self._pread(12, 0))
            if magic != b"PACK" or offset < 12 or length < 0 or length % 64 or offset + length > size:
                raise ValueError(f"{path} is not a valid PAK")
            if length // 64 > MAX_PAK_FILES:
                raise ValueError(f"{path} has too many files")
            directory = self._pread(length, offset)
            self.files: dict[str, tuple[int, int]] = {}
            for i in range(length // 64):
                raw, pos, n = struct.unpack_from("<56sii", directory, 64 * i)
                name = raw.split(b"\0", 1)[0].decode("ascii", "replace").lower()
                if pos < 0 or n < 0 or pos + n > size:
                    continue
                self.files.setdefault(name, (pos, n))     # within one PAK the first entry wins, as in the engine
        except BaseException:
            os.close(self.fd)
            raise

    def _pread(self, n: int, pos: int) -> bytes:
        data = os.pread(self.fd, n, pos)
        if len(data) != n:
            raise ValueError("PAK is truncated")
        return data

    def read(self, name: str, limit: int) -> bytes | None:
        entry = self.files.get(name)
        if not entry or entry[1] > limit:
            return None
        return self._pread(entry[1], entry[0])

    def close(self):
        os.close(self.fd)


class GameData:
    def __init__(self, paks: list[Path]):
        if not 1 <= len(paks) <= MAX_PAKS:
            raise ValueError("give between 1 and %d PAK files" % MAX_PAKS)
        self.paks = []
        try:
            for p in paks:
                self.paks.append(Pak(p))
        except BaseException:
            self.close()
            raise

    def read(self, name: str, limit: int) -> bytes | None:
        for pak in reversed(self.paks):                # a later PAK overrides an earlier one
            if name in pak.files:
                return pak.read(name, limit)
        return None

    def close(self):
        for pak in self.paks:
            pak.close()
        self.paks = []


def wad_lump(wad: bytes, wanted: str) -> bytes | None:
    """A lump from a WAD2 (gfx.wad), by name."""
    if len(wad) < 12:
        return None
    magic, count, table = struct.unpack_from("<4sii", wad, 0)
    if magic != b"WAD2" or not 0 <= count <= MAX_WAD_LUMPS or table < 12 or table + 32 * count > len(wad):
        return None
    for i in range(count):
        pos, disk, size, kind, compression, _, _, raw = struct.unpack_from("<iiibbbb16s", wad, table + 32 * i)
        name = raw.split(b"\0", 1)[0].decode("ascii", "replace").lower()
        if name == wanted and compression == 0 and 0 <= pos and 0 <= disk and pos + disk <= len(wad):
            return wad[pos:pos + disk]
    return None


def rgba(indices: bytes, palette: bytes, transparent: int) -> bytes:
    lut = [bytes((palette[3 * c], palette[3 * c + 1], palette[3 * c + 2], 255)) for c in range(256)]
    lut[transparent] = b"\0\0\0\0"
    return b"".join(lut[c] for c in indices)


def qpic(raw: bytes | None) -> tuple[int, int, bytes] | None:
    if not raw or len(raw) < 8:
        return None
    width, height = struct.unpack_from("<ii", raw, 0)
    if not (0 < width <= MAX_PIC_W and 0 < height <= MAX_PIC_H) or len(raw) < 8 + width * height:
        return None
    return width, height, raw[8:8 + width * height]


def face_colour(indices: bytes, palette: bytes, skip: int) -> int | None:
    """The most common colour among the brighter half of a glyph set's pixels: the face of the
    letters rather than their shadow or outline (LibreQuake's font has a dark drop shadow)."""
    def luma(c):
        return 2 * palette[3 * c] + 5 * palette[3 * c + 1] + palette[3 * c + 2]
    used = [c for c in indices if c != skip]
    if not used:
        return None
    top = max(luma(c) for c in used)
    counts: dict[int, int] = {}
    for c in used:
        if 2 * luma(c) >= top:
            counts[c] = counts.get(c, 0) + 1
    return max(counts, key=counts.get)


def conchars_glyphs(atlas: bytes, codes) -> bytes:
    """The 8x8 cells for the given character codes, concatenated (for colour sampling)."""
    out = bytearray()
    for code in codes:
        cx, cy = (code % 16) * 8, (code // 16) * 8
        for y in range(8):
            out += atlas[(cy + y) * 128 + cx:(cy + y) * 128 + cx + 8]
    return bytes(out)


def ink_atlas(atlas: bytes, palette: bytes, ink: tuple) -> bytes:
    """CONCHARS as RGBA with its text in one ink, for legibility. In both halves, the printable glyphs (codes 32..126)
    and the centred dot (28) become solid `ink`: a pixel as bright as the letters' face colour or brighter takes the
    ink, a dimmer one a proportionally darker ink (so id's shaded strokes keep their shape), and a drop-shadow pixel
    (under a third of the face's brightness) turns black. LibreQuake's striped two-tone face flattens to solid strokes.
    The game's own graphics (the other codes 0..31 and 127 of each half: rules, cursor, brackets, slider) keep their
    colours, so `alt` still picks them from the second half."""
    def luma(c):
        return 2 * palette[3 * c] + 5 * palette[3 * c + 1] + palette[3 * c + 2]
    out = bytearray(rgba(atlas, palette, 0))
    letters = range(ord("A"), ord("Z") + 1)
    for half in (0, 128):
        face = face_colour(conchars_glyphs(atlas, [half + c for c in letters]), palette, 0)
        top = max(1, luma(face)) if face is not None else 1
        for code in [half + 28] + list(range(half + 32, half + 127)):
            cx, cy = (code % 16) * 8, (code // 16) * 8
            for y in range(cy, cy + 8):
                for x in range(cx, cx + 8):
                    c = atlas[y * 128 + x]
                    if c == 0:
                        continue
                    f = min(1.0, luma(c) / top)
                    rgb = (0, 0, 0) if f < 1 / 3 else tuple(round(v * f) for v in ink)
                    out[4 * (y * 128 + x):4 * (y * 128 + x) + 4] = bytes(rgb + (255,))
    return bytes(out)


def png(width: int, height: int, pixels: bytes) -> bytes:
    def chunk(kind: bytes, payload: bytes) -> bytes:
        return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", zlib.crc32(kind + payload))
    rows = b"".join(b"\0" + pixels[y * width * 4:(y + 1) * width * 4] for y in range(height))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows, 9)) + chunk(b"IEND", b""))


def atomic_write(dir_fd: int, name: str, data: bytes) -> None:
    """A fresh, unguessable temp file in the already-validated directory (by descriptor, never by
    path), synced, then renamed over `name`: no truncated or redirected cache file is possible."""
    tmp = f".{name}.{secrets.token_hex(8)}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=dir_fd)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
    except BaseException:
        try:
            os.unlink(tmp, dir_fd=dir_fd)
        except OSError:
            pass
        raise


def private_dir(path: Path, create: bool) -> int | None:
    """An owned 0700 directory, opened without following a symlink at its last component; the
    returned descriptor is what every later read and write goes through. None when absent."""
    if create:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        fd = os.open(path, DIR_FLAGS)
    except FileNotFoundError:
        return None
    st = os.fstat(fd)
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid():
        os.close(fd)
        raise ValueError(f"{path} is not a private directory")
    if stat.S_IMODE(st.st_mode) & 0o077:
        os.fchmod(fd, 0o700)
    return fd


def read_index(out_fd: int, out: Path):
    """The cached index, read without following symlinks and at most MAX_INDEX_BYTES, accepted only
    if it belongs to `out`, every file it names is a plain cache file name, and each of those files
    is still a non-empty regular file of ours (otherwise the cache re-exports itself)."""
    try:
        fd = os.open("index.json", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=out_fd)
    except OSError:
        return None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or st.st_size > MAX_INDEX_BYTES:
            return None
        raw = os.read(fd, MAX_INDEX_BYTES + 1)
    finally:
        os.close(fd)
    try:
        cached = json.loads(raw)
    except (ValueError, RecursionError):
        return None
    if not (isinstance(cached, dict) and cached.get("dir") == str(out) and cached.get("version") == CACHE_VERSION
            and isinstance(cached.get("pictures"), dict) and isinstance(cached.get("sounds"), dict)
            and isinstance(cached.get("conchars"), dict)
            and isinstance(cached.get("inks"), dict) and sorted(cached["inks"]) == sorted(INKS)):
        return None
    names = [e.get("file") if isinstance(e, dict) else None
             for e in list(cached["pictures"].values()) + list(cached["inks"].values()) + [cached["conchars"]]]
    names += list(cached["sounds"].values())
    for name in names:
        if not CACHED_NAME.match(str(name)):
            return None
        try:
            st = os.stat(name, dir_fd=out_fd, follow_symlinks=False)
        except OSError:
            return None
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or st.st_size == 0:
            return None
    return cached


def content_key(palette: bytes, conchars: bytes, pictures: dict, sounds: dict) -> str:
    """SHA-256 over every byte we would write, each field length-framed so no two different data
    sets can produce the same key by moving bytes across a field boundary."""
    key = hashlib.sha256(b"live-quake-pakart\0%d\0" % CACHE_VERSION)
    def field(label: str, data: bytes | None):
        key.update(label.encode() + b"\0" + struct.pack("<q", -1 if data is None else len(data)) + (data or b""))
    field("palette", palette)
    field("conchars", conchars)
    field("inks", json.dumps(INKS, sort_keys=True).encode())
    for name in PICTURES:
        field("pic:" + name, pictures[name])
    for cue in SOUNDS:
        field("snd:" + cue, sounds[cue])
    return key.hexdigest()


def export(cache_root: Path, paks: list[Path]) -> dict:
    cache_root = Path(cache_root)
    if not cache_root.is_absolute():
        raise ValueError("cache root must be absolute")
    paks = [Path(p).resolve() for p in paks]
    game = GameData(paks)
    try:
        palette = game.read("gfx/palette.lmp", 768)
        wad = game.read("gfx.wad", MAX_WAD_BYTES)
        conchars = wad_lump(wad, "conchars") if wad else None
        if not palette or len(palette) != 768 or not conchars or len(conchars) < 128 * 128:
            raise ValueError("this game data has no Quake console font (gfx.wad, gfx/palette.lmp)")
        conchars = conchars[:128 * 128]
        pictures = {name: game.read(f"gfx/{name}.lmp", MAX_PICTURE_BYTES) for name in PICTURES}
        sounds = {cue: game.read(path, MAX_SOUND_BYTES) for cue, path in SOUNDS.items()}
    finally:
        game.close()

    out = cache_root / f"{content_key(palette, conchars, pictures, sounds)[:32]}-v{CACHE_VERSION}"
    root_fd = private_dir(cache_root, create=True)
    out_fd = None
    try:
        out_fd = private_dir(out, create=False)
        if out_fd is not None:
            cached = read_index(out_fd, out)          # trust a cached index only if it is whole and ours
            if cached is not None:
                cached["paks"] = [str(p) for p in paks]
                return cached
        else:
            os.mkdir(out.name, 0o700, dir_fd=root_fd)
            out_fd = private_dir(out, create=False)

        # Console font: index 0 is transparent (draw.c turns it into 255 at load).
        atomic_write(out_fd, "conchars.png", png(128, 128, rgba(conchars, palette, 0)))
        inks = {}
        for name, ink in INKS.items():
            atomic_write(out_fd, f"conchars_{name}.png", png(128, 128, ink_atlas(conchars, palette, ink)))
            inks[name] = {"file": f"conchars_{name}.png", "color": "#%02x%02x%02x" % ink}
        pics = {}
        for name in PICTURES:
            pic = qpic(pictures[name])
            if pic:
                w, h, px = pic
                atomic_write(out_fd, f"{name}.png", png(w, h, rgba(px, palette, 255)))
                # A data set may ship a picture as an empty placeholder (LibreQuake's qplaque is all
                # transparent): say so, and the menu leaves it out instead of reserving space for it.
                pics[name] = {"file": f"{name}.png", "w": w, "h": h, "blank": all(c == 255 for c in px)}
        missing = [name for name in REQUIRED if name not in pics]
        if missing:
            raise ValueError("this game data lacks menu pictures: " + ", ".join(missing))
        snd = {}
        for cue, data in sounds.items():
            if data and data[:4] == b"RIFF" and data[8:12] == b"WAVE":
                atomic_write(out_fd, f"{cue}.wav", data)
                snd[cue] = f"{cue}.wav"

        def hexcolor(index):
            return None if index is None else "#%02x%02x%02x" % tuple(palette[3 * index:3 * index + 3])
        letters = list(range(ord("A"), ord("Z") + 1))
        index = {
            "version": CACHE_VERSION,
            "dir": str(out),
            "paks": [str(p) for p in paks],
            "conchars": {"file": "conchars.png", "w": 128, "h": 128, "cell": 8},
            # The same font with its text in each of the menu's legible inks (see ink_atlas), laid out identically.
            "inks": inks,
            "pictures": pics,
            "sounds": snd,
            # "plain" is the font's first half (M_PrintWhite), "alt" its second half (M_Print, +128):
            # brown-gold in id's data, light blue in LibreQuake's.
            "colors": {
                "plain": hexcolor(face_colour(conchars_glyphs(conchars, letters), palette, 0)),
                "alt": hexcolor(face_colour(conchars_glyphs(conchars, [c + 128 for c in letters]), palette, 0)),
            },
        }
        atomic_write(out_fd, "index.json", json.dumps(index).encode())   # written last: marks the cache complete
        return index
    finally:
        if out_fd is not None:
            os.close(out_fd)
        os.close(root_fd)


def main(argv: list[str]) -> int:
    if len(argv) >= 3 and argv[0] == "export":
        try:
            print(json.dumps(export(Path(argv[1]), [Path(p) for p in argv[2:]])))
        except (OSError, ValueError) as error:
            print(str(error).splitlines()[-1] if str(error) else type(error).__name__, file=sys.stderr)
            return 1
        return 0
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
