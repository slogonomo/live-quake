// SPDX-License-Identifier: 0BSD
import QtQuick
import Quickshell.Io

// The menu's three cues, played from the cached copies of the user's own menu1/2/3.wav.
// Each cue runs in its own short-lived pw-play process, never inside the shell. Qt's own
// PipeWire audio context crashed the whole shell when the default sink disappeared (a
// DisplayPort monitor going to sleep); a vanished sink can now only end one pw-play.
// At most one process per cue runs at a time (a busy cue is skipped, never queued), timeout
// bounds its life, and a one-byte core limit keeps a pw-play crash from raising a notification.
Item {
  id: sounds
  property string dir: ""
  property var env: ({})
  function play(file) {
    var p = file === "move.wav" ? move : file === "enter.wav" ? enter : file === "adjust.wav" ? adjust : null
    if (!p || !sounds.dir || p.running) return
    p.command = ["/usr/bin/prlimit", "--core=1:1", "--", "/usr/bin/timeout", "-k", "1", "3",
                 "/usr/bin/pw-play", "--media-role", "Event", "--volume", "0.5", sounds.dir + "/" + file]
    p.running = true
  }
  Process { id: move; environment: sounds.env; clearEnvironment: true }
  Process { id: enter; environment: sounds.env; clearEnvironment: true }
  Process { id: adjust; environment: sounds.env; clearEnvironment: true }
}
