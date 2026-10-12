# Assets

| File | What it is | Licence |
|---|---|---|
| `live-quake.webp` | The live-background marker, 2560×1440. Adding it to a theme's backgrounds (as `1-live-quake.webp`) and selecting it turns the live background on, as Live Doom's `1-live-doom.webp` does. It is also what you see while the game is not running. | CC0-1.0 |

The marker is procedural art generated for this project. It contains no game art and no Quake logo or other trademark
imagery: a dark stone-textured sky, a warm horizon glow, a blocky battlement silhouette with a faintly lit gate, the
"live" dot shared with Live Doom, and the title. The text is set in JetBrains Mono (SIL OFL-1.1). The font is not
embedded; only rendered pixels are.

Regenerate it with ImageMagick 7. Fixed seeds make the result reproducible; grain and encoder details can vary
slightly between ImageMagick versions.

```sh
F=/usr/share/fonts/TTF/JetBrainsMonoNerdFont-Bold.ttf; FR=/usr/share/fonts/TTF/JetBrainsMonoNerdFont-Regular.ttf; W=2560; H=1440
magick -size ${W}x${H} gradient:'#060504'-'#130d09' base.png
magick -size 512x288 -seed 1996 plasma:fractal -colorspace gray -auto-level -resize ${W}x${H}! -blur 0x6 stone.png
magick base.png \( stone.png -level 0%,100% -evaluate multiply 0.5 -evaluate add 25% \) -compose softlight -composite sky.png
magick -size ${W}x${H} xc:black -fill '#a2481a' -draw "ellipse 1280,1300 1500,520 0,360" -blur 0x180 glow.png
magick -size ${W}x${H} xc:none -fill '#0d0907' -draw "polygon 0,1440 0,1120 90,1112 170,1128 260,1101 330,1109 410,1086 520,1118 610,1096 700,1104 780,1072 870,1093 960,1081 1030,1102 1320,1100 1390,1078 1470,1090 1560,1061 1650,1083 1720,1074 1800,1099 1890,1086 1980,1067 2060,1092 2150,1080 2240,1103 2330,1085 2420,1096 2500,1079 2560,1088 2560,1440" -blur 0x2 ridge.png
magick -size ${W}x${H} xc:none -fill '#040302' -draw "polygon 0,1440 0,1210 150,1210 150,1180 205,1180 205,1210 380,1210 380,1165 425,1165 425,1210 640,1210 640,1190 690,1190 690,1210 1060,1210 1060,1060 1090,1025 1130,1003 1170,996 1210,1003 1250,1025 1280,1060 1280,1210 1560,1210 1560,1180 1610,1180 1610,1210 1880,1210 1880,1160 1925,1160 1925,1210 2140,1210 2140,1185 2190,1185 2190,1210 2560,1210 2560,1440" near.png
magick -size ${W}x${H} xc:black -fill '#2c7d86' -draw "polygon 1092,1210 1092,1070 1115,1043 1145,1027 1170,1023 1195,1027 1225,1043 1248,1070 1248,1210" \( -size ${W}x${H} gradient:black-white -level 55%,100% \) -compose multiply -composite -blur 0x10 gatehaze.png
magick -size ${W}x${H} xc:black -fill '#2c7d86' -draw "ellipse 1170,1170 190,140 0,360" -blur 0x80 -evaluate multiply 0.45 gateglow.png
magick -size ${W}x${H} xc:black -fill '#ff5a12' -draw "circle 772,648 772,678" dotcore.png
magick dotcore.png \( +clone -blur 0x22 -evaluate multiply 2.2 \) -compose screen -composite \( dotcore.png -blur 0x70 -evaluate multiply 2.5 \) -compose screen -composite dot.png
magick sky.png glow.png -compose screen -composite gateglow.png -compose screen -composite ridge.png -compose over -composite near.png -compose over -composite gatehaze.png -compose screen -composite dot.png -compose screen -composite \
  -font $F -pointsize 168 -fill '#e6d6bc' -kerning 10 -gravity northwest -annotate +850+566 'LIVE QUAKE' \
  -font $FR -pointsize 44 -fill '#c96a2c' -kerning 2 -annotate +646+772 'a bot plays on your desktop  ·  click in to take over' \
  -seed 7 -attenuate 0.18 +noise gaussian marker.png
magick marker.png -strip -define webp:method=6 -quality 88 live-quake.webp
```
