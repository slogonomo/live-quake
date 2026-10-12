// SPDX-License-Identifier: 0BSD
import QtQuick
import Quickshell
import Quickshell.Wayland
import Quickshell.Hyprland

// Layer-shell host for MenuCard: a fullscreen overlay on the right-clicked output, mapped only
// while the menu is showing. Behind it the controller holds the game paused (the menu's lease).
PanelWindow {
  id: panel
  property var root: null

  function screenNamed(name) {
    for (var i = 0; i < Quickshell.screens.length; i++)
      if (Quickshell.screens[i].name === name) return Quickshell.screens[i]
    return null
  }
  function chosenScreen() {
    var s = root ? panel.screenNamed(root.targetOutput) : null
    if (s) return s
    var m = Hyprland.focusedMonitor
    return m ? panel.screenNamed(String(m.name || "")) : null
  }

  // Mapped fresh on each open and unmapped on close (see Live Doom's MenuWindow for why a
  // parked surface is not used); the card fades in from transparent, hiding a first frame.
  property bool shown: !!root && root.surfaceShown
  property var target: null
  property bool mapped: false
  onShownChanged: {
    if (shown) {
      target = chosenScreen() || target
      mapped = true
    } else {
      mapped = false
    }
  }
  Connections {
    target: panel.root
    ignoreUnknownSignals: true
    function onTargetOutputChanged() { if (panel.shown) panel.target = panel.chosenScreen() || panel.target }
  }

  visible: mapped
  screen: target
  color: "transparent"
  anchors { top: true; left: true; bottom: true; right: true }
  exclusionMode: ExclusionMode.Ignore
  WlrLayershell.namespace: "live-quake-menu"
  WlrLayershell.layer: WlrLayer.Overlay
  WlrLayershell.keyboardFocus: WlrKeyboardFocus.Exclusive

  MenuCard {
    m: panel.root
    shown: panel.shown
    anchors.fill: parent
  }
}
