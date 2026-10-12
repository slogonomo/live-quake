// SPDX-License-Identifier: 0BSD
import QtQuick

// The menu's message box, pinned to the bottom of the panel: the selected row's help in a clean
// proportional UI font (Adwaita Sans), then one quiet line that holds the last real status on the
// left (only when there is one, exactly as the menu wrote it) and the key legend on the right
// (bold key, dim verb). Sizes follow the menu's integer scale with floors, so the box reads the
// same at 720p, 1080p and 1440p: help max(13, 7.5 x scale) px, legend max(13, 6 x scale) px.
// The help wraps at whole words to `lines` lines; anything longer ends in "…". fits() answers,
// with the very same text layout, whether a help text shows whole (the private probe and
// tests/menu_fit_check.py check every help with it).
Item {
  id: box
  property int pixel: 2
  property string help: ""
  property int lines: 2
  property string status: ""
  property bool error: false
  property var hints: []           // [[key, verb], ...]

  readonly property string family: "Adwaita Sans"
  readonly property int helpSize: Math.max(13, Math.round(7.5 * pixel))
  readonly property int lineHeight: Math.max(helpSize + 4, 10 * pixel)
  readonly property int hintSize: Math.max(13, Math.round(6 * pixel))
  readonly property int padX: 16 * pixel
  readonly property int padY: 5 * pixel
  readonly property color helpColor: "#cbbea8"
  readonly property color statusColor: "#e6d6bc"
  readonly property color errorColor: "#f28c6e"
  readonly property color keyColor: "#c9bba3"
  readonly property color verbColor: "#8c8170"
  height: pixel + padY + lines * lineHeight + 3 * pixel + bottomLine.height + padY

  // True when `text` shows whole in the help area at this size: no "…", at most `lines` lines.
  function fits(text) {
    measure.text = String(text || "")
    return !measure.truncated && measure.lineCount <= box.lines
  }

  Rectangle { anchors.fill: parent; color: "#14100c" }
  Rectangle { width: parent.width; height: box.pixel; color: "#3a2c1f" }

  Text {
    id: helpText
    x: box.padX
    y: box.pixel + box.padY
    width: box.width - 2 * box.padX
    textFormat: Text.PlainText     // help can carry backend reasons: data, never markup
    renderType: Text.NativeRendering
    text: box.help
    wrapMode: Text.WordWrap
    maximumLineCount: box.lines
    elide: Text.ElideRight
    color: box.helpColor
    font.family: box.family
    font.pixelSize: box.helpSize
    lineHeightMode: Text.FixedHeight
    lineHeight: box.lineHeight
  }
  Text {                           // the same layout as helpText, never shown: measures any help text
    id: measure
    visible: false
    width: helpText.width
    textFormat: Text.PlainText
    renderType: helpText.renderType
    wrapMode: helpText.wrapMode
    maximumLineCount: helpText.maximumLineCount
    elide: helpText.elide
    font: helpText.font
    lineHeightMode: helpText.lineHeightMode
    lineHeight: helpText.lineHeight
  }

  Item {
    id: bottomLine
    x: box.padX
    width: box.width - 2 * box.padX
    height: box.lineHeight
    y: box.pixel + box.padY + box.lines * box.lineHeight + 3 * box.pixel
    readonly property real statusNeed: (warn.visible ? warn.implicitWidth + statusRow.spacing : 0) + statusText.implicitWidth
    Row {
      id: statusRow
      visible: box.status !== ""
      anchors.verticalCenter: parent.verticalCenter
      spacing: Math.round(box.helpSize * 0.4)
      Text {
        id: warn
        visible: box.error
        anchors.verticalCenter: parent.verticalCenter
        text: "!"            // plain, so the marker never depends on a Nerd Font being installed
        renderType: Text.NativeRendering
        color: box.errorColor
        font.family: "Adwaita Sans"
        font.bold: true
        font.pixelSize: box.helpSize
      }
      Text {
        id: statusText
        anchors.verticalCenter: parent.verticalCenter
        width: Math.min(implicitWidth, bottomLine.width - (warn.visible ? warn.implicitWidth + statusRow.spacing : 0))
        textFormat: Text.PlainText
        renderType: Text.NativeRendering
        text: box.status           // as the menu wrote it: never re-cased here
        elide: Text.ElideRight
        color: box.error ? box.errorColor : box.statusColor
        font.family: box.family
        font.pixelSize: box.helpSize
        font.weight: box.error ? Font.DemiBold : Font.Normal
      }
    }
    Row {
      id: legend
      anchors.right: parent.right
      anchors.verticalCenter: parent.verticalCenter
      spacing: Math.round(box.hintSize * 1.2)
      // a long status keeps the whole line; the legend steps aside rather than overlapping it
      // (by opacity: hiding the Row would also drop its width, which this test reads)
      opacity: !statusRow.visible || bottomLine.statusNeed + 3 * box.helpSize + implicitWidth <= bottomLine.width ? 1 : 0
      Repeater {
        model: box.hints
        Row {
          required property var modelData
          spacing: Math.round(box.hintSize * 0.35)
          Text {
            text: modelData[0]
            textFormat: Text.PlainText
            renderType: Text.NativeRendering
            color: box.keyColor
            font.family: box.family
            font.pixelSize: box.hintSize
            font.weight: Font.Bold
          }
          Text {
            text: modelData[1]
            textFormat: Text.PlainText
            renderType: Text.NativeRendering
            color: box.verbColor
            font.family: box.family
            font.pixelSize: box.hintSize
          }
        }
      }
    }
  }
}
