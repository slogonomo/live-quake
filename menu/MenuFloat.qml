// SPDX-License-Identifier: 0BSD
import QtQuick
import Quickshell
import Quickshell.Hyprland

// Window host for MenuCard, used when the menu is opened from Apps or by the first-run guide: a real
// toplevel, so it behaves like any app (Super+W closes it, it ends Omarchy's "Launching…" toast, it
// never sits unseen over other windows) and it takes no lease. Hyprland floats and centres a window
// whose minimum and maximum sizes are equal, so the size is fixed before each map and the card
// scrolls inside. The size follows the card's whole-number scale: 3 on a screen of at least
// 1480x1250 logical pixels, else 2. A right-click keeps the overlay (MenuWindow.qml).
FloatingWindow {
  id: win
  property var root: null

  readonly property bool shown: !!root && root.surfaceShown
  property bool mapped: false
  property int px: 2
  property int fixedH: 620
  function pickSize() {
    var m = Hyprland.focusedMonitor, w = 0, h = 0
    for (var i = 0; i < Quickshell.screens.length; i++)
      if (m && Quickshell.screens[i].name === String(m.name || "")) { w = Quickshell.screens[i].width; h = Quickshell.screens[i].height }
    win.px = w >= 1480 && h >= 1250 ? 3 : 2
    win.fixedH = Math.max(300 * win.px, Math.min(330 * win.px + 150, (h || 720) - 100))
  }
  onShownChanged: {
    if (shown) {
      pickSize()      // the size decides floating, so it is set before the window maps
      mapped = true
    } else {
      mapped = false
    }
  }
  // Closed by the compositor (Super+W, or the window's close) while the menu wanted it up:
  // that is the user closing the menu. An unmap the menu asked for is not.
  onVisibleChanged: if (!visible && root && root.opened && root.surfaceShown) root.dismiss()
  onClosed: if (root && root.opened && root.surfaceShown) root.dismiss()

  title: "Live Quake"
  visible: mapped
  color: "#0b0907"
  implicitWidth: 440 * px
  implicitHeight: fixedH
  minimumSize: Qt.size(440 * px, fixedH)
  maximumSize: Qt.size(440 * px, fixedH)

  MenuCard {
    m: win.root
    windowed: true
    shown: win.shown
    anchors.fill: parent
  }
}
