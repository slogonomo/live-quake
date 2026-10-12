# Engine and bot

Live Quake uses [Ironwail](https://github.com/andrei-drexler/ironwail) at
`1eabd0df21130e55d94496c7af177e2dcbed75df`. Its native frontend loads the patched shared engine
through the libretro ABI, supplies input and audio, and controls exact suspension by stopping `retro_run`.
The engine renders through GBM/EGL into dmabufs consumed by the Wayland viewer. Paused and covered heads
use an engine capture and SHM blur; this is part of the GPU presentation path.

The [patch](../patches/ironwail-desktop.patch) and [adapter](../src/ironwail/) are GPL-2.0-or-later;
the combined engine is distributed under GPL-2.0. The frontend, controller and viewer are 0BSD.
See [third-party notices](../THIRD-PARTY-NOTICES.md) for source and data obligations.
Ironwail's bundled pak is neither built nor loaded. Local networking is disabled at startup.

## Bot and human ownership

The local FrikBot X pack supplies QuakeC and per-map waypoints. Live Quake's session code follows the bot
with a hidden, nonsolid human client. Takeover transfers the bot's position, eye angles, stats and inventory
to that human; F12 returns ownership to a bot with the same state. Co-op changelevel preserves camera ownership.
Ambient rendering flattens flickering lightstyles and softens powerup tint; takeover restores human values.

Deathmatch, the default mode, runs several bots on the same model. The hidden client follows whichever bot the
engine's camera director picks, and takeover takes that bot's place: its frags move with the body, and the
scoreboard shows the human's row as "You". Rounds end on the time or frag limit, and the next map is picked at
random from the enabled list. Deathmatch keeps no checkpoints.

The native Quake menu can save and start a new game in the local FrikBot session. Saves belong to that mod
and remain in private game profiles. Steam archives are read through a validated PAK-only link farm;
configuration and saves are never written beside the original data.

In single player, private runs show combat and exploration on original and re-release data, but do not reach an exit
naturally. A bounded ambient recovery ladder uses stock completion screens before advancing stuck maps.
LibreQuake lacks authored waypoints and plays less convincingly. Mission packs have no FrikBot pack and play single
player only. The experimental e1m1 objective assist is disabled by default.

## Presentation requirements

The production path requires GBM/EGL and Wayland dmabuf, viewporter and subcompositor support.
A failed GPU start stops the game and exposes Retry OpenGL. Private diagnostic captures may use llvmpipe;
they exercise the same engine and bot but do not establish that the production viewer transport works.
