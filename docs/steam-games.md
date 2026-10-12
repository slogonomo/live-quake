# Quake from your Steam library

Live Quake can play the Quake you own on Steam, straight from Steam's own folder. Nothing is copied, extracted or
uploaded. This page describes what the detector looks for, how it decides what is playable, and what has and has not
been verified.

Original and re-release `e1m1` sessions have been checked privately with the local FrikBot pack,
including takeover, handback and transitions to `e1m2`/`e1m3`. Full campaigns remain unproven.
Mission packs can be detected but have no FrikBot pack on this engine.

## What Steam ships (app 2310, as of September 2026)

Quake on Steam is a single app, **2310**, with install folder `steamapps/common/Quake`. The 2021 Nightdive
re-release replaced the old store entry, but **the original 1996 files still ship beside it**.

```
steamapps/common/Quake/
  Id1/PAK0.PAK  Id1/PAK1.PAK       original v1.06 registered data (note the capital I and upper-case names)
  hipnotic/PAK0.PAK                original Mission Pack 1, Scourge of Armagon
  rogue/PAK0.PAK                   original Mission Pack 2, Dissolution of Eternity
  glquake.exe, …                   the "(Original)" launch options
  rerelease/                       the KEX re-release, which is Steam's default launch
    id1/pak0.pak                   re-release base (no pak1)
    hipnotic/ rogue/               re-release mission packs
    dopa/                          Dimension of the Past (2016)
    mg1/                           Dimension of the Machine (2021)
    mg3/                           Dawn of the Machine (30th-anniversary update, 2026-08-06; folder name inferred)
    ctf/                           Threewave CTF (multiplayer)
```

Sources:
- Steam PICS for 2310: `installdir "Quake"`; launch options `rerelease/Quake_x64_steam.exe`, plus `glquake.exe`,
  `-hipnotic` and `-rogue` marked "(Original)"; Steam Cloud saves under `Id1`.
- Ironwail `Quake/steam.c:827-828` and `common.c:3206-3210`.
- The Steam community thread `3180111663689514309` ("the ones outside the rerelease folder are the old files").
- Debian game-data-packager `data/quake.yaml`.
- The Steam and Bethesda 30th-anniversary announcements.

Other facts that shape the design:

- **There is no native Linux build.** Steam installs the Windows depot through Proton, but every file is on disk
  under `steamapps/common/Quake` once the install finishes, even if the game was never launched.
- **The mission-pack apps 9040 (hipnotic) and 9030 (rogue) are retired** but still installed for people who bought
  them. They install into the same `Quake` folder, so the detector deduplicates by directory.
- **The re-release's in-game add-ons** (Quake 64, Arcane Dimensions, …) download into the Proton prefix, not the
  install folder. They are out of scope.

## Detection

The detector reads, never writes, and never follows a symlink inside a library:
- Steam roots and the library folders named in `libraryfolders.vdf` are each resolved once, so a library on a
  symlinked mount is found. Aliases of the same folder are counted once (by device and inode).
- Below those roots, every path component is opened without following symlinks. A symlinked manifest,
  `libraryfolders.vdf` or PAK file is refused.

1. **Steam roots:** `~/.local/share/Steam`, `~/.steam/{steam,root}` and the Flatpak locations.
2. **Libraries:** each root's `steamapps/libraryfolders.vdf`, with a bounded VDF parser (≤ 1 MiB, ≤ 65,536 tokens,
   depth ≤ 16). At most 32 libraries are read.
3. **Apps:** `appmanifest_<appid>.acf` for 2310 and the retired 9040 and 9030.
   - The `appid` must match, and `installdir` must be a single plain path component.
   - Installs that resolve to the same folder are counted once.
   - `StateFlags` is advisory: an app still updating or downloading gets a note, not a refusal.
4. **Letter case:** files are looked up case-insensitively, through one bounded directory scan per folder. The
   real on-disk names are reported (`Id1/PAK0.PAK`), and an ambiguous pair such as `id1` plus `Id1` is refused.
5. **PAK structure:** the 12-byte `PACK` header, a 64-byte-aligned directory with at most 2,048 entries, and every
   member's offset and size inside the file.
6. **Signatures:** small, bounded reads, nothing executed.
   - `progs.dat` must have version 6 and field-layout CRC 5927, with bounded function and string tables.
     Ironwail supports named extension builtins; their presence is not a rejection.
   - BSP29, `2PSB` and `BSP2` prefixes are accepted. The selected start map must exist and use a supported format.

Required members:
- `pak0` must contain `progs.dat`, `gfx.wad`, `gfx/palette.lmp`, `maps/start.bsp` and `maps/e1m1.bsp`.
- `pak1` must contain `maps/e2m1.bsp`.
- A mission pack must contain its own `progs.dat` and maps, and needs that full base.

| Package | Where | Runtime evidence |
|---|---|---|
| `steam-quake` | `Id1/PAK0.PAK` + `PAK1.PAK` | Checked original `e1m1` bot sessions and native persistence |
| `steam-quake-rerelease` | `rerelease/id1/pak0.pak` plus `QuakeEX.kpf` | Checked 90/240 s sessions with takeover, handback and map transitions |
| `steam-hipnotic` | `hipnotic/PAK0.PAK` plus full original base | Detected; FrikBot support unimplemented |
| `steam-rogue` | `rogue/PAK0.PAK` plus full original base | One native `r1m1` save reload proved; FrikBot support unimplemented |
| Quake II, III, 4, Live, Champions, II RTX | Their own apps | Refused as non-Quake-1 data |

The re-release is read in place with its localization archive. Later maps, finale behavior and the
re-release's own bots are unproven; early-map evidence is not a whole-campaign claim.

### Known hashes: shown as a badge, never a gate

Patches change these, so a mismatch is reported but never refused:

| File | Bytes | MD5 |
|---|---|---|
| `id1/pak0.pak` v1.06 (identical to the shareware file) | 18,689,235 | `5906e5998fc3d896ddaf5e6a62e03abb` |
| `id1/pak1.pak` | 34,257,856 | `d76b3e5678f0b64ac74ce5e340e6a685` |
| `hipnotic/pak0.pak` | 35,527,205 | `0ab83681aaf841c4320269e02941a14a` |
| `rogue/pak0.pak` (three known pressings) | 37,875,279 | `f71428763229bdb373f00a547296efe3`, `c38a4e04219c317cd1b02f386bdfe11f`, `f8898a483b131de21581656c94f3c1a4` |

There is no official re-release hash, and the re-release PAKs change with every update.

## Playing it

The menu's Find Steam games action lists validated packages. Selecting one builds a private lower-case
PAK-only link farm under the controller's `0700` state directory, preserving Steam's actual letter case
at the source. Loose config files and saves are excluded. A package switch rolls back if startup fails;
the selected package is remembered in Live Quake's configuration.

Native saves and config live in a separate game profile. The frontend supplies that directory explicitly;
the engine never writes into Steam's folder. Profiles remain under the `ironwail-v2-` namespace.
Native saves have a v2 footer recording the client-slot count and mode. A save with a different
layout or mode is refused before restoring the world. Deathmatch never writes or loads checkpoints.

In deathmatch, the package's own deathmatch maps join LibreQuake's arenas in the rotation, and registered data adds the
episode maps as opt-in arenas. Mission packs play single player only.

GOG and Epic copies under Heroic or Lutris are not discovered automatically. Runtime evidence covers one owned copy's
original and re-release archives; other editions and full campaigns need their own review.
