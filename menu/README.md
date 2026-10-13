# Live Quake settings menu

An Omarchy shell overlay plugin (Quickshell QML) that looks like the game's own options screen. Right-clicking the
live background opens it over the paused game; opening Live Quake from Apps shows it as a floating window. It is built
to be read on a real desktop (checked at 1280×720, 1920×1080 and 3440×1440): a near-black panel over a flat dim of the
game, with

- the OPTIONS plaque and the console's section rules;
- labels, values and headings in the console font's capitals, recoloured to solid inks: bone labels and headings,
  amber values (and only values), a dim live status line, a fixed faint ink for rows that cannot be changed, and a
  brighter label on the selected row's bar;
- the menu cursor, held steady, and the ten-cell slider;
- a message box with the selected row's help, the last real status and a key legend in a proportional UI font
  (Adwaita Sans).

The game art comes from the user's own game files at runtime. The plugin ships no game font, picture or sound.

Private test captures of the menu are kept with the development evidence, not in the release tree.

## Files

| file | role |
|---|---|
| `Menu.qml` | Overlay entry point (`manifest.json` → `entryPoints.overlay`). Holds all state and behaviour: the pause lease, the `quakectl` job queue, rows, keys and art loading. |
| `MenuWindow.qml` | Full-output layer-shell surface on the chosen output (Overlay layer, exclusive keyboard). Mapped only while showing. |
| `MenuFloat.qml` | A fixed-size floating window (`windowed` card, no lease) for opens from Apps (`launcher`) and the first-run guide (`onboarding`). |
| `MenuCard.qml` | Pure view: the options screen laid out on a 400-pixel game canvas, scaled by a whole number, on a near-black panel. |
| `QuakeText.qml` | A line of text in CONCHARS, with the font's two halves: `alt` is the engine's `M_Print`, plain is `M_PrintWhite`. `ink` (`bone`, `hi`, `amber`, `dim`, `faint`) takes the text from that recoloured sheet; `caps` draws capitals without changing the text. |
| `QuakeSlider.qml` | The engine's `M_DrawSlider`: CONCHARS 128 (left cap), 129 ×10 (track), 130 (right cap) and 131 (thumb). |
| `TextBox.qml` | The message box: help, status and key legend in the UI font, sized from the menu scale. `fits()` measures a help text with the box's own layout. |
| `MenuSounds.qml` | The three menu cues, each played by a short-lived `pw-play` process (PipeWire), never inside the shell. |
| `pakart.py` | Reads the art and sounds from the PAK files and caches them as PNG/WAV. |

## Talking to the game

Only through `<plugin>/scripts/quakectl`:

- `menu opened`, `menu renew` every 2 s, and `menu closed`. A failed renew closes the menu (fail closed).
- `settings` and `set <key> <json>`.
- `scan-steam`, `use-game <id>`, `hint-bot`, `retry-renderer`, `use-live-wallpaper` and `remove-live-wallpaper`.
- The first-run guide (contract v4): `onboarding step|opened|finish|skip`. A window takes no lease.

In deathmatch the Game section adds Round time, Frag limit, Bots and Maps (`set game.dm_timelimit`, `game.dm_fraglimit`
and `game.dm_bots`). Maps opens a second page, Deathmatch maps, with one row per group (id deathmatch, LibreQuake
arenas, Episode 1 to 4 maps); Enter opens or closes a group, each map's switch writes `game.dm_disabled_maps` (or, for
episode maps, which start off, `game.dm_enabled_sp_maps`), and Back or Esc returns to the settings. The backend refuses
a change that would leave no map on.

Lease calls run on their own serial queue, and other calls on a second one. Every process has:

- a deadline: 10 s for the lease, 30 s for jobs (40 s for `use-game` and for a mode change, `set game.mode`), 20 s for
  art;
- a closed environment: a fixed `PATH` plus the session variables it needs;
- an absolute executable.

No executable path ever comes from the environment. A test harness replaces `quakectl`, `osdCloseCommand`,
`themeCommand`, `cacheBase`, `hosted`, `sounds`, `testLimit`, `autoOpen` and `firstRunDelay` as component properties
when it loads `Menu.qml`.

## Art from the user's own game files

`pakart.py export <cache-root> <pak>…` takes the PAKs listed in `settings.game.data.paks`, in engine search order
(a later PAK overrides an earlier one, as in the engine). It reads only what the menu needs:

- `gfx.wad` → `CONCHARS`: the 128×128 console font. Index 0 is transparent. It is cached as is (`conchars.png`) and
  once per menu ink (`INKS`: `conchars_bone.png`, `_hi`, `_amber`, `_dim` and `_faint`), where the text glyphs
  (codes 32–126 and the centred dot 28, in both halves) are recoloured to that one solid ink, with the drop shadow in
  black. The game's own graphics in the font (rules, cursor, brackets, slider) keep their colours. The inks are part
  of the cache key.
- `gfx/palette.lmp`
- `gfx/qplaque.lmp`, `gfx/p_option.lmp`, `gfx/ttl_main.lmp`, `gfx/menudot1..6.lmp` and `gfx/box_*.lmp`: qpic
  pictures, where index 255 is transparent.
- `sound/misc/menu1.wav`, `menu2.wav` and `menu3.wav`: the move, enter and adjust cues.

Every read is bounded:
- PAKs are read with `pread` on an open descriptor, not mapped, so a file that shrinks or fails underneath gives a
  clean error;
- the PAK directory is capped at 4096 entries;
- each member's offset and size are checked against the file;
- per-file caps are 320×200 for pictures (the engine's menu canvas), 1 MiB for sounds and 4 MiB for `gfx.wad`.

The cache directory is named after a length-framed SHA-256 of exactly the bytes it holds. Both the cache root and
the entry are opened without following symlinks, must be ours, and are kept at `0700`. Every file goes through an
unguessable temp name, `fsync` and `rename`, relative to that directory's descriptor. `index.json` is written last,
so a half-written cache is never trusted. A cached index is used only when every file it names is still there, as a
non-empty regular file of ours; otherwise the art is exported again.

Different data sets look different, and the menu follows them:

| | id Software's data | LibreQuake 0.09-beta |
|---|---|---|
| CONCHARS second half (`alt`) | brown-gold (text drawn in the inks) | light blue (text drawn in the inks) |
| CONCHARS 12 / 13 (cursor) | the engine blinks between them | 12 is blank (the menu draws 13, steady) |
| CONCHARS `(` `)` | full height | short marks low in the cell, so capitals print `[` `]` |
| `qplaque.lmp` | the Quake plaque | blank (fully transparent) |
| `menudot*.lmp` | spinning Quake logo | wide bracket highlight |
| `p_option.lmp` | OPTIONS plaque | OPTIONS plaque (original art) |

`pakart.py` marks a fully transparent picture `"blank": true`, and the menu leaves it out. Only `p_option.lmp` and
the font are required. Without them the menu still works, in a monospace fallback in the same inks.

## Layout

On the 400-pixel canvas (game pixels, 8-pixel cells):

- labels are right-aligned to x = 248, the cursor sits at 256 and values start at 272 (the slider cap at 264);
- the plaque is at (16, 4), the title is centred at y = 4, the live status line is at y = 37 and rows start at 50;
- rows are 10 pixels high, a section adds 6 pixels of air; the panel reaches 8 pixels past the canvas on each side.

Sections are ruled with the console's own rule glyphs (29/30/31). The selected row sits on a bar of fixed width
(the whole canvas) with its label in the brighter ink and the cursor beside it, steady: a blinking mark read as a
prompt waiting for typing. When the list is longer than the panel, its cut-off edges fade into the panel.

The message box shows the selected row's help in Adwaita Sans at `max(13, 7.5 × scale)` px, wrapped at whole words by
the text layout to at most two lines; anything longer ends in "…". Below it, the last status shows only when there
is one, exactly as the menu wrote it (`saved`, or a reason in red with a warning sign); there is no resting "ready"
line. The key legend (bold key, dim verb) is `max(13, 6 × scale)` px and steps aside for a long status.

The integer scale is `floor(min(width / 440, height / 330))`, clamped to 1–6: 2 at 1280×720, 3 at 1080p, 4 at
1440p and 6 at 2160p.

## Keys and pointer

- Keys: ↑/↓ (or k/j, Tab) select, ←/→ (h/l) adjust, Enter or Space activates, and Esc or F12 closes. On the
  Deathmatch maps page, Esc returns to the settings instead.
- Pointer: hover selects; a click activates or toggles. A slider takes clicks, drags and the wheel. A click on the
  dimmed backdrop closes the menu.
