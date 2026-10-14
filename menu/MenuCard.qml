// SPDX-License-Identifier: 0BSD
import QtQuick

// The visual side of the Live Quake menu: the game's own options screen, laid out like the
// engine's (menu.c M_Options_Draw) on a 400 x N "game pixel" canvas of 8 px cells, scaled by a
// whole number so the font stays crisp. All state and behaviour live in Menu.qml (`m`).
//
// Built to be read on a real desktop: a near-black panel over a flat dim of the game, the console
// font in solid capitals from pakart's ink sheets (bone labels and headings, amber values and only
// values, a dim live status line, a fixed faint ink for rows that cannot be changed, a brighter
// label on the selected row's bar), and the help, status and key legend in a proportional UI font
// (TextBox.qml). The plaque, the section rules, the steady cursor and the slider stay the game's
// own art.
//
//   x:  16 plaque · labels right-aligned to 248 · cursor at 256 · values from 272 (slider cap 264)
//   y:   4 OPTIONS plaque · 37 live status · 50 rows (10 px each) · message box pinned to the bottom
//
// `windowed` (MenuFloat.qml): the window is the panel, so there is no dim, no click-outside close and
// no border, and the scale fills its height (300 game pixels instead of 330). While the first-run
// guide shows, "LIVE QUAKE" replaces the OPTIONS plaque and `note` rows carry each step's text.
Item {
  id: card
  property var m: null
  property bool shown: true
  property bool windowed: false
  function focusKeys() { keys.forceActiveFocus() }
  function register() {
    if (!m) return
    m.cardItem = card
    if (m.surfaceShown) Qt.callLater(card.focusKeys)
  }
  onMChanged: register()
  Component.onCompleted: register()
  onShownChanged: if (shown) Qt.callLater(card.focusKeys)

  // ------------------------------------------------------------- geometry
  readonly property int canvasW: 400
  readonly property int labelEnd: 248
  readonly property int cursorX: 256
  readonly property int valueX: 272
  readonly property int helpLines: 2
  readonly property int px: Math.max(1, Math.min(6, Math.floor(Math.min(width / (canvasW + 40), height / (windowed ? 300 : 330)))))
  readonly property var art: m ? m.art : null
  readonly property bool guiding: !!(m && m.guiding)
  readonly property var plaque: m && m.art ? m.pic("qplaque") : null
  readonly property var title: m && m.art && !guiding ? m.pic("p_option") : null

  opacity: card.shown ? 1 : 0
  Behavior on opacity { NumberAnimation { duration: 120 } }

  // A flat dim over the paused game: the game stays visible around the panel, never through it.
  Rectangle {
    visible: !card.windowed
    anchors.fill: parent
    color: "#000000"
    opacity: 0.6
  }
  // a click outside the menu closes it, like Esc
  MouseArea {
    enabled: !card.windowed
    anchors.fill: parent
    acceptedButtons: Qt.LeftButton | Qt.RightButton
    onClicked: if (card.m) card.m.dismiss()
  }

  // Pointer-driven selection never scrolls the list, and a row only takes the cursor when the
  // pointer really moved, not when content scrolled underneath a resting pointer.
  property bool pointerSelect: false
  property point lastPointer: Qt.point(-1, -1)
  property bool scrolling: false
  Timer { id: scrollQuiet; interval: 250; onTriggered: card.scrolling = false }
  function pointerMoved(area) {
    var p = area.mapToItem(null, area.mouseX, area.mouseY)
    var same = p.x === card.lastPointer.x && p.y === card.lastPointer.y
    card.lastPointer = p
    return !same && !card.scrolling
  }
  function pointTo(index) {
    if (m.cursor === index) return
    card.pointerSelect = true
    m.cursor = index
    card.pointerSelect = false
  }

  // True when a help text shows whole in the message box at the current size: no "…", at most
  // helpLines lines. Measured by the message box's own text layout (TextBox.fits), so it is
  // exactly what the player sees; the private probe and tests/menu_fit_check.py rely on it.
  function helpFits(text) { return box.fits(text) }
  readonly property int helpSize: box.helpSize     // px, for the probe's report
  // A guide `note`: wrapped UI-font text across the middle of the canvas, at most noteLines lines (menu_fit_check).
  readonly property int noteSize: Math.max(13, Math.round(7 * px))
  readonly property int noteLines: 3
  function noteFits(text) {
    noteMeasure.text = String(text || "")
    return noteMeasure.lineCount <= card.noteLines
  }
  Text {
    id: noteMeasure
    visible: false
    width: (card.canvasW - 80) * card.px
    textFormat: Text.PlainText
    renderType: Text.NativeRendering
    wrapMode: Text.WordWrap
    font.family: "Adwaita Sans"
    font.pixelSize: card.noteSize
  }
  // Colours of the panel (bone on near-black); the text itself comes from pakart's ink sheets.
  readonly property color panelColor: Qt.rgba(11 / 255, 9 / 255, 7 / 255, 0.96)
  readonly property color fadeColor: "#0b0907"     // the panel, opaque: the list's cut edges fade into it
  readonly property color edgeColor: "#3a2c1f"
  readonly property color barColor: "#3d2d1c"      // the selected row: clearly lifted off the near-black panel
  // The selected row's bar spans the whole canvas, whatever the row holds and however the list
  // scrolls, so it never changes width with the menu's state.
  readonly property int barW: canvasW

  // ---------------------------------------------------------------- canvas
  Item {
    id: canvas
    readonly property int listGameH: list.implicitHeight / card.px
    width: card.canvasW * card.px
    // a window is the panel: the canvas fills it, so the message box sits at its foot
    height: card.windowed ? card.height : Math.min(card.height - 2 * card.px * 8, (50 + listGameH + 4 + 1) * card.px + box.height)
    anchors.centerIn: parent
    // the panel: 8 game pixels wider than the canvas on each side, so values never touch its edge
    Rectangle {
      x: -8 * card.px
      width: parent.width + 16 * card.px
      height: parent.height
      color: card.panelColor
      border.width: card.windowed ? 0 : card.px
      border.color: card.edgeColor
    }
    MouseArea {      // swallow clicks on the menu itself so only the dimmed backdrop closes it
      x: -8 * card.px
      width: parent.width + 16 * card.px
      height: parent.height
      acceptedButtons: Qt.AllButtons
      onClicked: function(mouse) { mouse.accepted = true }
      onWheel: function(wheel) { wheel.accepted = false }
    }

    Image {          // the plaque beside the title, where the data set has one
      visible: !!card.plaque
      source: card.plaque ? card.plaque.url : ""
      sourceSize: card.plaque ? Qt.size(card.plaque.w, card.plaque.h) : undefined   // decode no larger than the index says
      x: 16 * card.px; y: 4 * card.px
      width: card.plaque ? card.plaque.w * card.px : 0
      height: card.plaque ? card.plaque.h * card.px : 0
      smooth: false
    }
    Image {
      id: titleImage
      visible: !!card.title
      source: card.title ? card.title.url : ""
      sourceSize: card.title ? Qt.size(card.title.w, card.title.h) : undefined
      width: card.title ? card.title.w * card.px : 0
      height: card.title ? card.title.h * card.px : 0
      x: Math.round((card.canvasW - (card.title ? card.title.w : 0)) / 2) * card.px
      y: 4 * card.px
      smooth: false
    }
    QuakeText {
      visible: !card.title
      art: card.art
      ink: "bone"
      pixel: card.px * 2
      text: card.guiding ? "LIVE QUAKE" : "OPTIONS"
      x: Math.round((canvas.width - width) / 2)
      y: 8 * card.px
    }
    QuakeText {
      id: subtitle
      art: card.art
      pixel: card.px
      ink: "dim"                     // amber is for values only; the live status line stays quiet
      caps: true
      text: !card.m ? "" : card.guiding ? card.m.guideHead(card.m.settings)
            : ["LIVE QUAKE", card.m.modeText(card.m.settings)].filter(function(x) { return x }).join("  ·  ")
      x: Math.round((canvas.width - width) / 2 / card.px) * card.px
      y: 37 * card.px
    }

    Flickable {
      id: flick
      x: 0
      y: 50 * card.px
      width: canvas.width
      height: box.y - 4 * card.px - y
      contentWidth: width
      contentHeight: list.implicitHeight
      clip: true
      boundsBehavior: Flickable.StopAtBounds
      interactive: contentHeight > height
      onContentYChanged: { card.scrolling = true; scrollQuiet.restart() }
      function reveal(item) {
        if (!item) return
        var top = item.mapToItem(list, 0, 0).y
        var bottom = top + item.height
        // The edge fades cover 10 game pixels at each cut-off edge, so a selected row must clear them, not
        // just the list's bounds.
        var edge = 10 * card.px, pad = 16 * card.px
        if (top < contentY + edge) contentY = Math.max(0, top - pad)
        else if (bottom > contentY + height - edge) contentY = Math.min(contentHeight - height, bottom - height + pad)
      }

      Column {
        id: list
        width: flick.width
        // a window shows the guide's few rows a third of the way down its free space, not crowded under the title
        y: card.windowed && card.guiding ? Math.max(0, Math.floor((flick.height - implicitHeight) / 3 / card.px) * card.px) : 0
        Repeater {
          model: m ? m.rows : []
          delegate: Item {
            id: rowItem
            required property var modelData
            required property int index
            readonly property var row: modelData
            readonly property bool selected: m.cursor === index
            readonly property var value: m.settings && row.key ? m.getv(m.settings, row.key) : undefined
            readonly property int cell: 8 * card.px
            readonly property bool off: row.kind === "off"      // shown, but cannot be changed here
            width: list.width
            // a section is a little air and a ruled heading; a note is its wrapped text with air
            // around it; every other row is one 8 px line with a game pixel above and below, so
            // pointer targets are not razor thin
            height: row.kind === "section" ? (index === 0 ? 0 : 6 * card.px) + cell + 2 * card.px
                    : row.kind === "note" ? noteText.implicitHeight + 12 * card.px : cell + 2 * card.px
            onSelectedChanged: if (selected && !card.pointerSelect) flick.reveal(rowItem)

            // the selected row sits on a lighter bar of fixed width across the canvas
            Rectangle {
              visible: rowItem.selected
              x: 0
              width: card.barW * card.px
              height: parent.height
              color: card.barColor
            }

            // ruled section heading: CONCHARS 29/30/31 are the console's own rule pieces
            Item {
              visible: rowItem.row.kind === "section"
              anchors.left: parent.left
              anchors.right: parent.right
              anchors.bottom: parent.bottom
              anchors.bottomMargin: card.px
              height: rowItem.cell
              readonly property string label: String(rowItem.row.label || "").toUpperCase()
              // With a visible plaque (id's data: 32 px wide at x = 16) the rule starts right of it.
              readonly property int ruleLeft: card.plaque ? 56 : 16
              readonly property int side: Math.max(2, Math.floor(((card.canvasW - 16 - ruleLeft) / 8 - label.length - 2) / 2))
              QuakeText {
                art: card.art
                ink: "bone"
                pixel: card.px
                text: "\x1d" + "\x1e".repeat(Math.max(0, parent.side - 2)) + "\x1f " + parent.label + " \x1d"
                      + "\x1e".repeat(Math.max(0, parent.side - 2)) + "\x1f"
                x: Math.round(((parent.ruleLeft + card.canvasW - 16) * card.px - width) / 2 / card.px) * card.px   // width: the fallback font too
              }
            }

            // a guide step's text, in the help box's UI font, centred under its heading
            Text {
              id: noteText
              visible: rowItem.row.kind === "note"
              x: 40 * card.px
              width: (card.canvasW - 80) * card.px
              anchors.verticalCenter: parent.verticalCenter
              text: rowItem.row.kind === "note" ? String(rowItem.row.text || "") : ""
              textFormat: Text.PlainText      // data, never markup
              renderType: Text.NativeRendering
              wrapMode: Text.WordWrap
              horizontalAlignment: Text.AlignHCenter
              color: "#cbbea8"
              font.family: "Adwaita Sans"
              font.pixelSize: card.noteSize
            }

            // label, right-aligned to the cursor column like the engine's padded M_Print labels
            QuakeText {
              visible: rowItem.row.kind !== "section" && rowItem.row.kind !== "note"
              art: card.art
              ink: rowItem.off ? (rowItem.selected ? "dim" : "faint") : rowItem.selected ? "hi" : "bone"
              caps: true
              pixel: card.px
              text: rowItem.row.label || ""
              x: card.labelEnd * card.px - width
              anchors.verticalCenter: parent.verticalCenter
            }
            // the engine's menu cursor, CONCHARS 13, held steady: the engine alternates it with 12,
            // which LibreQuake leaves blank, and a blinking mark reads as a prompt waiting for typing
            QuakeText {
              visible: rowItem.selected
              art: card.art
              pixel: card.px
              text: card.art ? "\x0d" : ">"
              x: card.cursorX * card.px
              anchors.verticalCenter: parent.verticalCenter
            }

            // value column
            QuakeText {
              visible: rowItem.row.kind === "toggle"
              art: card.art; pixel: card.px; ink: "amber"; caps: true
              text: (rowItem.row.key ? rowItem.value : rowItem.row.on) ? "on" : "off"   // the guide's choice has no key
              x: card.valueX * card.px
              anchors.verticalCenter: parent.verticalCenter
            }
            QuakeText {
              visible: rowItem.row.kind === "choice"
              art: card.art; pixel: card.px; ink: "amber"; caps: true
              text: {
                var opts = rowItem.row.options || []
                for (var i = 0; i < opts.length; i++) if (opts[i][0] === rowItem.value) return String(opts[i][1])
                return rowItem.value === undefined ? "" : String(rowItem.value)
              }
              x: card.valueX * card.px
              anchors.verticalCenter: parent.verticalCenter
            }
            QuakeText {
              visible: ["info", "off", "action"].indexOf(rowItem.row.kind) !== -1
              art: card.art; pixel: card.px; ink: rowItem.off ? (rowItem.selected ? "dim" : "faint") : "amber"; caps: true
              text: rowItem.row.value || ""
              x: card.valueX * card.px
              anchors.verticalCenter: parent.verticalCenter
            }
            QuakeSlider {
              z: 2
              visible: rowItem.row.kind === "slider"
              art: card.art
              pixel: card.px
              x: (card.valueX - 8) * card.px
              anchors.verticalCenter: parent.verticalCenter
              value: rowItem.row.kind === "slider" ? (Number(rowItem.value) - rowItem.row.min) / (rowItem.row.max - rowItem.row.min) : 0
              wheelEnabled: !card.scrolling && (m.cursor === rowItem.index || !flick.interactive)
              onPicked: function(f) { card.pointTo(rowItem.index); m.sliderPick(rowItem.row, f) }
              onStepped: function(dir) { card.pointTo(rowItem.index); m.adjust(rowItem.row, dir) }
            }
            QuakeText {
              visible: rowItem.row.kind === "slider"
              art: card.art; pixel: card.px; ink: "amber"
              text: rowItem.row.kind === "slider" && isFinite(Number(rowItem.value))
                    ? Number(rowItem.value).toFixed(rowItem.row.digits) + rowItem.row.suffix : ""
              x: (card.valueX + 96) * card.px
              anchors.verticalCenter: parent.verticalCenter
            }

            MouseArea {
              anchors.fill: parent
              visible: m.selectable(rowItem.index)
              hoverEnabled: true
              cursorShape: rowItem.row.kind === "info" || rowItem.row.kind === "off" ? Qt.ArrowCursor : Qt.PointingHandCursor
              onPositionChanged: if (card.pointerMoved(this)) card.pointTo(rowItem.index)
              onClicked: { card.pointTo(rowItem.index); if (rowItem.row.kind !== "slider") m.activate(rowItem.row) }
              onWheel: function(wheel) { wheel.accepted = false }
            }
          }
        }
      }
    }

    // When the list is longer than the panel, its cut-off edges fade into the panel instead of
    // slicing a row in half, so they read as "more this way".
    Rectangle {
      visible: flick.interactive && flick.contentY > 0
      x: flick.x; y: flick.y
      width: flick.width; height: 10 * card.px
      gradient: Gradient {
        GradientStop { position: 0; color: card.fadeColor }
        GradientStop { position: 1; color: Qt.rgba(card.fadeColor.r, card.fadeColor.g, card.fadeColor.b, 0) }
      }
    }
    Rectangle {
      visible: flick.interactive && flick.contentY + flick.height < flick.contentHeight - 1
      x: flick.x; y: flick.y + flick.height - height
      width: flick.width; height: 10 * card.px
      gradient: Gradient {
        GradientStop { position: 0; color: Qt.rgba(card.fadeColor.r, card.fadeColor.g, card.fadeColor.b, 0) }
        GradientStop { position: 1; color: card.fadeColor }
      }
    }

    // The message box, pinned to the bottom of the panel: the selected row's help, the last real
    // status (never a resting "ready") and the key legend.
    TextBox {
      id: box
      pixel: card.px
      lines: card.helpLines
      x: -7 * card.px                // inside the panel's one-pixel edge
      width: canvas.width + 14 * card.px
      y: canvas.height - height - card.px
      readonly property var current: m && m.cursor >= 0 ? m.rows[m.cursor] : null
      help: current && current.help ? current.help : ""
      status: m ? m.statusLine : ""
      error: !!(m && m.statusError)
      hints: card.guiding ? [["\u2191\u2193", "move"], ["Enter", "select"], ["Esc", "close"]]
                          : [["\u2191\u2193", "move"], ["\u2190\u2192", "adjust"], ["Enter", "select"], ["Esc", "close"]]
    }
  }

  // keyboard (exclusive while shown)
  Item {
    id: keys
    focus: true
    Keys.priority: Keys.BeforeItem
    Keys.onPressed: function(event) { if (card.handleKey(event.key, event.modifiers)) event.accepted = true }
  }

  // Returns true when the key was handled (kept separate so tests can drive it).
  function handleKey(key, modifiers) {
    var row = m.rows[m.cursor]
    var shift = modifiers & Qt.ShiftModifier
    if (key === Qt.Key_Escape && m.page === "maps") {
      m.leaveMaps()                    // the maps page steps back to the settings
    } else if (key === Qt.Key_Escape || key === Qt.Key_F12) {
      m.dismiss()
    } else if (key === Qt.Key_Backtab || (key === Qt.Key_Tab && shift)) {
      m.move(-1)
    } else if (key === Qt.Key_Tab || key === Qt.Key_Down || key === Qt.Key_J) {
      m.move(1)
    } else if (key === Qt.Key_Up || key === Qt.Key_K) {
      m.move(-1)
    } else if (key === Qt.Key_Left || key === Qt.Key_H) {
      m.adjust(row, -1)
    } else if (key === Qt.Key_Right || key === Qt.Key_L) {
      m.adjust(row, 1)
    } else if (key === Qt.Key_Return || key === Qt.Key_Enter || key === Qt.Key_Space) {
      m.activate(row)
    } else {
      return false
    }
    return true
  }
}
