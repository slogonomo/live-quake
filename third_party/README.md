# Third-party bot sources and waypoints

These files are included unchanged. `scripts/build-frikbot-local.py` uses them by default to build the bot pack.

## `frikbot-x/`: FrikBot X 0.10.2

The bot itself, by Ryan "FrikaC" Smith, with contributions noted in its source:

- `readme.html`;
- the QuakeC in `src/frikbot/`;
- its waypoints for id's dm1 to dm6 in `src/waypoints/`.

FrikBot X is in the public domain. Its notice is at the top of `src/frikbot/bot.qc` and is reproduced in
THIRD-PARTY-NOTICES.md.

## `quake-bot-archive/waypoints/`: FrikBot waypoints for id's single-player maps

They came in the FrikBot X package, whose notes point to a winxp76.net FrikBot X collection, from the Quake Bot
Archive, https://github.com/Jason2Brownlee/QuakeBotArchive. Thanks to Jason Brownlee for preserving Quake bot history
that would otherwise disappear.

They cover `start`, `end` and e1m1 to e4m8, except e1m8. Each map has a `.way` file and, on larger maps,
`.wa1`/`.wa2` continuation files. The authors are named in the files themselves: "[bta].monster" on most maps,
"monster.[bta]" on `end`, and `/\/\O/\/sT3rTU` on e1m7, written in Quake's console font. The files are kept
byte for byte as published, including those name lines.
