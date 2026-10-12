# LibreQuake deathmatch waypoint scripts

These scripts were generated for Live Quake by FrikBot X's own dynamic
waypointing during ten-minute, four-bot matches at skill 1. Its stock
`DumpWaypoints` output supplies every coordinate, link and flag. Continuation
files retain the stock `exec maps/<map>.wa1` chain. No coordinates or routes
were manually corrected, and no third-party waypoint files were copied.

The graph data is offered under [0BSD](../../LICENSE). The underlying
LibreQuake maps retain their [BSD-3-Clause notice](../../LICENSES/BSD-3-Clause-LibreQuake.txt).
The pinned map source is LibreQuake v0.09-beta `full.zip`, SHA-256
`623e463b35811216244f9ba15e0c45abc1765288650b5e3d36a899b35e4bcf3d`.
Learning used the current Live Quake FrikBot pack on id's official GPL v1.01
QuakeC, pinned to Quake-Tools `c0d1b91c74eb654365ac7755bc837e497caaca73`.

`src/dm_pack.py` verifies these files by size and SHA-256 and includes them in
the generated DM-maps pack. Only graphs accepted after paired fresh-match
clips are included. Other maps continue using FrikBot dynamic waypointing.
The generation records, private tools and real-engine comparisons live in
the development repository; they are not required to load these files.
