# Free game data and licensing

Live Quake ships **no game data**. It plays one of three things:

| Data | Licence | How it gets here | Status |
|---|---|---|---|
| **LibreQuake 0.09-beta** (default) | BSD-3-Clause art and sound, GPL-2.0-or-later QuakeC, `progs.dat` and `pop.lmp`, CC0 developer textures | You run one explicit fetch, pinned to a SHA-256. No terms to accept. | Plays, with the bot's game code in place of its own |
| **Your Quake from Steam** | Proprietary; you own it | Read in place from Steam's folder, never copied | Plays: the original game and the 2021 re-release ([steam-games.md](steam-games.md)) |
| **Quake shareware** (episode 1) | id's 1996 shareware licence, non-free | Not offered | The bot is a modified game, which the licence forbids (below) |

Research date: 2026-10-01. Every claim below was checked against a primary source at a pinned revision. A second,
independent pass then tried to refute the licensing claims, and its corrections are applied here. Unconfirmed items are
marked as such.

## LibreQuake: the zero-consent default

- **Pin:**
  - tag `v0.09-beta` is commit `d1515ca9bc01e590f2d54dc496f55c9a4ed72369`, released 2025-11-05 and still the latest
    release as of 2026-10-01;
  - asset `full.zip` is 115,678,161 bytes, SHA-256 `623e463b35811216244f9ba15e0c45abc1765288650b5e3d36a899b35e4bcf3d`
    (GitHub's asset digest).
  - The GitHub release is marked mutable, so the hash is always checked.
  - The project plans to move to Codeberg, and the archive.org item `libre-quake-0.09-beta` (same size, verified
    against the same SHA-256) is a fallback.
- **Layout:**
  - every member sits under `full/id1/`: `pak0.pak` (94,730,968 B), `pak1.pak` (100,635,264 B), loose deathmatch maps,
    `music/track02-11.ogg` and `docs/`;
  - the build extracts it with zip-slip and size checks into a private `0700` folder.
- **Licence** (there is no top-level licence file; GitHub reports `NOASSERTION`):
  - `docs/COPYING` is **BSD-3-Clause**, "Contributors to the LibreQuake project", and covers "all the models textures
    and sounds";
  - the QuakeC, `progs.dat`, `progs.lno` and `pop.lmp` are **GPL-2.0-or-later** (`docs/README-IMPORTANT-LICENCE-INFO`,
    `docs/misc-docs/`);
  - `lq_dev` textures are CC0;
  - some sounds come from **Freedoom** and **Blasphemer**, both BSD-3-Clause.
- **Known upstream gaps.** None of these matter to us, because we never redistribute the data:
  - the release omits Blasphemer's BSD notice;
  - `CREDITS` is incomplete;
  - one readme contains merge-conflict markers.
- **Maturity:**
  - it is a beta;
  - episode 0 has 8 complete maps (`lq_e0m1`–`m8`);
  - 10 of the 30 episode 1–4 maps are placeholders that only lead on to the next map;
  - 8 sounds (mostly liquid sounds) are silent;
  - map names are `lq_eXmY`, not `eXmY`;
  - `progs.dat` is compiled in CI with an unpinned `fteqcc`, so it is not byte-reproducible. The release's copy has
    MD5 `e9fe59e877d6d3fce9968bbd4dde4f4f`;
  - every map is BSP29 except **`lq_e2m4`**, which is `BSP2`. Ironwail accepts both map formats. Format validation alone does not establish bot progress on those maps.
- LibreQuake's `colormap.lmp` is 16,384 bytes. It is loaded from the selected game archives;
  the production engine does not load Ironwail's bundled art pak.

### Is any of it id's art? (the Live Doom lesson)

- **`palette.lmp`** is byte-identical to id's Quake palette.
  - LibreQuake's own asset list says "can use original", and the same bytes ship inside GPL engines such as
    Darkplaces.
  - It is functional data, not artwork. The public-domain statement attributed to John Carmack is second-hand and
    unconfirmed.
- **`pop.lmp`** is byte-identical to the `pop[]` array in id's GPL `WinQuake/common.c`. That makes it id's data,
  under the GPL.
- **The console font, menu plaques, title, console background and menu cursor are original art.** We inspected them
  visually, and they differ obviously from id's:
  - the font's second half is blue, not gold;
  - the title reads "LIBREQUAKE";
  - the plaque is blank;
  - the cursor is a bracket, not a spinning Q.
- **Some status-bar lumps have no documented author.** `SBAR` is credited to MissLav, but `SB_ARMOR1-3`,
  `SB_CELLS`, `SB_ROCKET`, `SCOREBAR`, `SBA*`, `INV2_*` and others are unattributed, with their git history squashed
  in 2023. They cannot be byte-checked against id's without id's data.
  - **Live Quake hides the status bar while the bot plays** (`viewsize 120`), and no screenshot here shows it. The engine
    still draws its level-end intermission and finale screens (`inter.lmp`, `complete.lmp`, the big number font)
    whatever the view size. With LibreQuake these are its own art.
- **`colormap.lmp`** is also marked "can use original" in LibreQuake's asset list. The current 16,384-byte file is
  structured differently from id's 16,385-byte one, which suggests it was regenerated from the palette. Its origin is
  unconfirmed. It is a lighting table, not art.
- **Ironwail’s bundled art is excluded.** Its engine pak is never built or loaded; game art comes from the selected PAKs.

Every screenshot shipped with Live Quake, and every illustration in these docs, uses LibreQuake only, captured on a
private test compositor with the status bar off and the crosshair off on the bot's view. The exception is the
development captures under `docs/runtime/`, in the development repository only: most show id's Quake art from the
developer's own copy, as private evidence of how the bots play, and they are never in the release export.

## Quake shareware: not offered

Live Quake's only bot, FrikBot, is QuakeC that replaces the game's `progs.dat`: a modified game. id's shareware
licence forbids modification and public display, so shareware data can't be offered with it. This section keeps the
research, for the record.

The pinned chain is the one Debian's game-data-packager uses:

| File | Bytes | SHA-256 |
|---|---|---|
| `quake106.zip` (the whole Software, with id's Agreement) | 9,094,045 | `ec6c9d34b1ae0252ac0066045b6611a7919c2a0d78a3a66d9387a8f597553239` |
| `resource.1` inside it (an LHA archive) | 9,086,574 | `c192c9c71bee41750dd7d14c99378766d61e077977b9d13d1a457b8d9eabe34a` |
| `id1/pak0.pak` 1.06 inside that | 18,689,235 | `35a9c55e5e5a284a159ad2a62e0e8def23d829561fe2f54eb402dbc0a9a946af` (MD5 `5906e5998fc3d896ddaf5e6a62e03abb`) |

Five HTTPS mirrors of id's `idstuff/quake/quake106.zip` serve the exact size: fu-berlin, gamers.org, gwdg,
youfailit and infania. Their content hashes are unverified until a fetch.

What id's terms require (`SLICNSE.TXT`, 1996-06-21, and `LICINFO.TXT`):
- **Distribution:** free, as a whole, with the Agreement.
- **Prohibited:**
  - modification and derivative works;
  - public display, including screenshots;
  - commercial use;
  - running user-made levels.

Any shareware mode would have to:
1. show `SLICNSE.TXT` and record acceptance before anything is fetched;
2. fetch the whole archive and verify all three hashes. **Python has no LHA decoder**, so this needs `bsdtar`
   (libarchive) or `lha` on the system, which is unconfirmed;
3. run the **unmodified** `id1` only: no LibreQuake maps, no mods and no custom QuakeC, so no FrikBot;
4. never ship anything derived from it (screenshots, navigation caches) and label the menu "shareware data".

§4 of the licence also forbids using its screen displays, sound and music "in any manner", and §3(d) forbids public
display, so even a bot-free live desktop of shareware data would be a grey area.

Owned Steam data is read the same way Live Doom reads your IWAD.

## The bot: FrikBot X

No autonomous single-player Quake campaign bot exists under a usable licence (search-based, so unconfirmed). The
nearest prior art is research: *Cortex* (arXiv 2607.22739, July 2026) is a behaviour-cloned policy that reaches E1M1
milestones and gets kills but does not complete the level.

| Bot | Licence | Verdict |
|---|---|---|
| FrikBot X 0.10.2 | "Public Domain", plus an indemnification clause and a notice-retention clause | **Used.** A deathmatch and co-op bot that also plays single-player maps with waypoints; it replaces `progs.dat` |
| Reaper Bot | Proprietary ("may not be used as a basis for other works") | Closed source, deathmatch only, registered data only |
| Omicron Bot | Binary only | Closed, deathmatch only |
| Frogbot (KTX) | GPL-2.0 | QuakeWorld, deathmatch only, needs per-map route files |
| Re-release bots | GPL-2.0 QuakeC hooks; the AI is in the proprietary KEX engine | The QuakeC hooks are empty |

Live Quake first wrote its own exploration and combat autopilot inside the engine. It couldn't walk e1m1's opening
corridor, and on 2026-10-11 FrikBot replaced it.
- **How it's built:** on your machine, by `scripts/build-frikbot-local.py`, into one local game pack, from FrikBot X
  0.10.2, id's GPL QuakeC v1.01 (`id-Software/Quake-Tools`, `qcc/v101qc`, at `c0d1b91c…`), our session QuakeC
  (`src/quakec/`) and the episode waypoints in `third_party/`, compiled with FTEQW's QuakeC compiler at
  `f937b9d8…`. A build receipt records the inputs' hashes.
- **Licences:** FrikBot X is public domain with the conditions above (its notice is in
  [THIRD-PARTY-NOTICES.md](../THIRD-PARTY-NOTICES.md)). id's QuakeC v1.01 is GPL-2.0-or-later. The episode waypoints
  come from the Quake Bot Archive, credited in the README.
- **In single player it doesn't finish levels by itself yet.** Every level change so far is a forced skip; see the
  README's "Single player".

## Code licences

| Component | Licence | Shipped? |
|---|---|---|
| Live Quake's own code and the menu, except the GPL files listed in [THIRD-PARTY-NOTICES.md](../THIRD-PARTY-NOTICES.md) | 0BSD | yes; the tests are 0BSD too (with the same GPL exceptions) but stay in the development repository |
| Live Quake's own art | CC0-1.0 | yes |
| Ironwail at `1eabd0df21130e55d94496c7af177e2dcbed75df`, plus `patches/ironwail-desktop.patch` and `src/ironwail/` | Our patch and adapter: GPL-2.0-or-later. Combined engine: GPL-2.0 | Patch and adapter shipped; pinned upstream fetched and built locally |
| `libretro.h`, the libretro ABI header (fetched at a pinned commit) | MIT | compiled into the frontend and the core |
| Our session QuakeC, `src/quakec/` | GPL-2.0-or-later | Source shipped, with its build script `scripts/build-frikbot-local.py`; compiled only into the local bot pack |
| FrikBot X 0.10.2 (`third_party/frikbot-x/`) and the episode waypoints (`third_party/quake-bot-archive/`) | FrikBot: public domain with conditions; waypoints: credited third-party data | Yes, unchanged |
| The bot pack: the above with id's GPL QuakeC v1.01 and ours | QuakeC v1.01: GPL-2.0-or-later | No: built on your machine |
| `src/waypoints/`: generated LibreQuake DM routes | 0BSD; underlying maps BSD-3-Clause | Yes: source and generated DM-maps pack, with the LibreQuake credits |
| MP3, Ogg Vorbis, FLAC and Opus music decoders; curl | n/a | Not compiled; WAV only. Networking is compiled but disabled for the local session at startup. |

The full notices are in [THIRD-PARTY-NOTICES.md](../THIRD-PARTY-NOTICES.md).
