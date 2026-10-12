// SPDX-License-Identifier: 0BSD
import QtQuick

// A line of text in the game's own console font (CONCHARS: 16x16 cells of 8x8 in a 128x128
// sheet, read from the user's gfx.wad by pakart.py). `alt` draws the font's second half, which
// the engine's M_Print uses. With an `ink` ("bone", "hi", "amber", "dim" or "faint") the text
// glyphs come from pakart's recoloured copy of the font (conchars_<ink>.png: one solid colour,
// readable on the menu's near-black panel) while the game's own graphics in the font (rules,
// cursor, brackets) keep their colours. `caps` prints the text in capitals: LibreQuake's
// lowercase is a mix of true lowercase and small capitals (i, l, p, y), which reads oddly; its
// capitals are clean. It only changes what is drawn, never `text`. Without game art it falls
// back to a monospace font in the same inks, so the menu never depends on the art to work.
Item {
  id: t
  property string text: ""
  property var art: null           // pakart index
  property int pixel: 2            // integer scale: 8x8 cells become (8*pixel)^2, nearest-neighbour
  property bool alt: false
  property string ink: ""          // a key of pakart's INKS, or "" for the font's own colours
  property bool caps: false
  // pakart.INKS, repeated for the no-art fallback (and the font's own two halves without an ink)
  readonly property var inkColors: ({ bone: "#e6d6bc", hi: "#fff1d8", amber: "#e8b465", dim: "#ab9d86", faint: "#857965" })
  property color fallbackColor: t.ink && t.inkColors[t.ink] ? t.inkColors[t.ink] : (alt ? "#c8a060" : "#d8d8d8")
  property string fallbackFamily: "monospace"
  // In capitals the font's "(" and ")" are short marks low in the cell (LibreQuake draws them like
  // a "c" and its mirror), so "QUAKE (ENHANCED)" would read "QUAKE cENHANCEDↄ": they print as the
  // font's full-height "[" and "]" instead.
  readonly property string shown: caps ? text.toUpperCase().replace(/\(/g, "[").replace(/\)/g, "]") : text
  readonly property bool hasArt: !!(art && art.conchars)
  readonly property var sheet: !hasArt ? null : (t.ink && art.inks && art.inks[t.ink] ? art.inks[t.ink] : art.conchars)
  readonly property int cell: 8 * pixel
  implicitWidth: hasArt ? shown.length * cell : fallback.implicitWidth
  implicitHeight: cell
  width: implicitWidth
  height: implicitHeight

  // Characters outside the font's printable range read as the engine would print them; the
  // few non-ASCII ones the menu uses map to their CONCHARS equivalents.
  function code(ch) {
    var c = ch.charCodeAt(0)
    if (ch === "·" || ch === "•") c = 28           // centred dot
    else if (ch === "…") return 46                       // "…" → "."
    else if (c > 126) c = 63                                  // "?"
    else if (c === 0) c = 32
    // 1..31 print as the font's own glyphs, as the engine prints raw bytes: 12/13 are the
    // menu cursor, 16/17 brackets, 18..27 digits, 28 a dot and 29/30/31 the console rule.
    return t.alt && c < 128 ? c + 128 : c
  }

  Row {
    visible: t.hasArt
    Repeater {
      model: t.hasArt ? t.shown.length : 0
      Image {
        required property int index
        readonly property int c: t.code(t.shown.charAt(index))
        source: t.sheet ? "file://" + t.art.dir + "/" + t.sheet.file : ""
        sourceClipRect: Qt.rect((c % 16) * 8, Math.floor(c / 16) * 8, 8, 8)
        width: t.cell
        height: t.cell
        smooth: false
        cache: true
      }
    }
  }
  Text {
    id: fallback
    visible: !t.hasArt
    anchors.verticalCenter: parent.verticalCenter
    textFormat: Text.PlainText      // labels and reasons are data, never markup
    // CONCHARS 12/13 (cursor), 16/17 (brackets), 28 (dot) and 29-31 (rule) have no glyph in a system font.
    text: t.shown.replace(/[\x0c\x0d]/g, ">").replace(/\x10/g, "[").replace(/\x11/g, "]").replace(/\x1c/g, "·")
                 .replace(/[\x1d\x1e\x1f]/g, "-").replace(/[\x00-\x1f]/g, " ")
    color: t.fallbackColor
    font.family: t.fallbackFamily
    font.pixelSize: Math.round(t.cell * 0.9)
    font.bold: true
  }
}
