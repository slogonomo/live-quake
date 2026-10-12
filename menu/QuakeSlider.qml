// SPDX-License-Identifier: 0BSD
import QtQuick

// The engine's options slider (menu.c M_DrawSlider), drawn from CONCHARS: 128 is the left cap,
// 129 the track (SLIDER_RANGE = 10 cells), 130 the right cap and 131 the thumb, which moves
// continuously over the first nine cells. Click or drag picks a value; the wheel steps it.
Item {
  id: s
  property var art: null
  property int pixel: 2
  property real value: 0           // 0..1
  property bool wheelEnabled: true
  signal picked(real fraction)
  signal stepped(int dir)
  readonly property int cell: 8 * pixel
  readonly property bool hasArt: !!(art && art.conchars)
  implicitWidth: 12 * cell
  implicitHeight: cell
  width: implicitWidth
  height: implicitHeight

  component Glyph: Image {
    property int c: 129
    source: s.hasArt ? "file://" + s.art.dir + "/" + s.art.conchars.file : ""
    sourceClipRect: Qt.rect((c % 16) * 8, Math.floor(c / 16) * 8, 8, 8)
    width: s.cell
    height: s.cell
    smooth: false
  }

  Row {
    visible: s.hasArt
    Glyph { c: 128 }
    Repeater { model: 10; Glyph { c: 129 } }
    Glyph { c: 130 }
  }
  Glyph {
    visible: s.hasArt
    c: 131
    // whole game pixels, like the 320x200 original: 72 px of travel over the track
    x: s.cell + Math.round(72 * Math.max(0, Math.min(1, s.value))) * s.pixel
  }

  // Without game art: a plain track and thumb in the same geometry.
  Rectangle {
    visible: !s.hasArt
    x: s.cell; y: s.cell / 2 - s.pixel; width: 10 * s.cell; height: 2 * s.pixel
    color: "#7a6a58"
  }
  Rectangle {
    visible: !s.hasArt
    x: s.cell + Math.round(72 * Math.max(0, Math.min(1, s.value))) * s.pixel
    width: s.cell; height: s.cell
    color: "#d8c8a8"
  }

  MouseArea {
    anchors.fill: parent
    cursorShape: Qt.PointingHandCursor
    function pick(mx) {
      // the thumb's left edge travels from one cell in to nine cells further along
      var f = (mx - s.cell - s.cell / 2) / (72 * s.pixel)
      s.picked(Math.max(0, Math.min(1, f)))
    }
    onPressed: function(mouse) { pick(mouse.x) }
    onPositionChanged: function(mouse) { if (pressed) pick(mouse.x) }
    property real acc: 0
    onWheel: function(wheel) {
      if (!s.wheelEnabled || Math.abs(wheel.angleDelta.y) <= Math.abs(wheel.angleDelta.x)) { acc = 0; wheel.accepted = false; return }
      if (acc !== 0 && (acc > 0) !== (wheel.angleDelta.y > 0)) acc = 0
      acc += wheel.angleDelta.y
      while (Math.abs(acc) >= 120) { s.stepped(acc > 0 ? 1 : -1); acc -= acc > 0 ? 120 : -120 }
    }
  }
}
