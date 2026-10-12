// SPDX-License-Identifier: 0BSD
import QtQuick
import Quickshell
import Quickshell.Io

// Live Quake settings menu (Omarchy shell overlay plugin "io.github.slogonomo.live-quake",
// overlay entry point of the plugin's manifest; installs copy menu/ into <plugin>/impl-<sha256>/).
//
// Summoned by the Live Quake controller on right-click:
//   omarchy-shell shell summon io.github.slogonomo.live-quake '{"output":"DP-2"}'
// Talks to the backend only through `quakectl` (docs/design/MENU-CONTRACT.md, frozen v1):
//   menu opened (once, from an explicit open), menu renew (every 2 s while open; any renew
//   failure closes the menu, fail closed), menu closed, settings, set <key> <json>.
// Everything applies immediately. The look is the game's own options screen: the OPTIONS
// plaque, the console font (recoloured for legibility), the menu cursor and the slider are read
// at runtime from the user's PAK files by pakart.py; nothing from the game is redistributed.
// Opened from Apps or by the first-run guide (origins `launcher`, `onboarding`) the menu is a
// floating window (MenuFloat.qml) that takes no lease; a right-click keeps the leased overlay.
Item {
  id: root

  // Omarchy plugin host contract
  property var shell: null
  property var manifest: null
  property bool opened: false

  // ------------------------------------------------------------------ state
  property bool surfaceShown: false
  property string targetOutput: ""
  property var settings: null
  property var art: null               // pakart index, or null: monospace fallback
  property int cursor: -1
  property string statusLine: ""
  property bool statusError: false

  readonly property string home: Quickshell.env("HOME") || ""
  readonly property string pluginDir: decodeURIComponent(String(Qt.resolvedUrl(".")).replace(/^file:\/\//, ""))
  readonly property string pluginRoot: root.pluginDir.replace(/[^\/]+\/$/, "")
  // The installer copies this folder to <plugin>/impl-<sha256>/ (Qt caches compiled QML by URL), so
  // pluginRoot stays <plugin>/ and this tells which menu build the running shell actually loaded.
  function loadedVersion() { return root.pluginDir }
  readonly property string python: "/usr/bin/python3"
  property Item cardItem: null         // set by MenuCard when it is created

  // The backend ships in the same plugin: <plugin>/scripts/quakectl, one level above this menu's folder.
  // No executable is ever taken from the environment. A private test harness replaces these by
  // setting the properties when it loads the component (setSource), never through env.
  property string quakectl: root.pluginRoot + "scripts/quakectl"
  property var osdCloseCommand: ["/usr/bin/omarchy-shell", "osd", "close"]
  property var themeCommand: null      // harness only: replaces /usr/bin/omarchy-theme-{set,install}
  property string cacheBase: (Quickshell.env("XDG_CACHE_HOME") || (root.home + "/.cache")) + "/live-quake/menu-art"
  property bool hosted: true           // false: no layer-shell window (a harness hosts MenuCard itself)
  property bool sounds: true           // false: never load the menu sounds (private harness: no audio server)
  property int testLimit: 0            // harness only: one deadline for every process, in ms
  property string openedUrl: ""        // records the store link when not hosted, instead of opening it

  readonly property string mono: "monospace"
  readonly property var rows: buildRows(settings)
  readonly property bool guiding: !!(root.settings && root.settings.onboarding && root.settings.onboarding.show)

  // ------------------------------------------------------- plugin lifecycle
  property int session: 0               // bumped per open; tags lease ops
  property bool awaitingLease: false    // first `menu opened` of this session not yet answered
  property bool leased: false           // this session holds the pause lease (overlay host only)
  property string host: "overlay"       // "overlay" (right-click) or "window" (Apps, first-run guide)
  readonly property bool windowed: root.host === "window"

  function open(payloadJson) {
    var payload = ({})
    try { payload = JSON.parse(payloadJson || "{}") } catch (e) { payload = ({}) }
    root.targetOutput = String(payload.output || "")
    root.resetLaunchToast()
    root.capturePolls = 0
    if (root.opened) {
      if (payload.origin === "launcher") root.armLaunchToast()
      if (root.windowed) {             // summoned again: map the window afresh, on this workspace
        root.surfaceShown = false
        Qt.callLater(root.showSurface)
      } else if (!root.awaitingLease) root.showSurface()
      root.refresh()
      return
    }
    root.session += 1
    root.opened = true
    root.host = payload.origin === "launcher" || payload.origin === "onboarding" ? "window" : "overlay"
    windowLoader.use(root.host)
    if (payload.origin === "launcher") root.armLaunchToast()
    root.statusError = false
    root.statusLine = "connecting to the game"     // the first settings read replaces it with any backend problem
    if (root.windowed) {
      // A window takes no lease: like any window it makes its workspace busy, and it can sit on
      // any workspace without pausing the others.
      root.showSurface()
    } else {
      root.leased = true
      root.awaitingLease = true
      root.lifecycle("opened")
      showFallback.restart()
      heartbeat.start()
    }
    root.refresh()
  }

  // Opened from the app launcher: Omarchy shows "Launching …" until a new toplevel appears,
  // which this layer-shell overlay never is. Close that toast with Omarchy's own IPC.
  property int launchToastTries: 0
  property int launchToastSession: -1
  function resetLaunchToast() {
    launchToast.stop()
    launchToast.interval = 2200
    root.launchToastTries = 0
    root.launchToastSession = -1
  }
  function armLaunchToast() {
    root.resetLaunchToast()
    root.launchToastSession = root.session
    launchToast.start()
  }
  Timer {
    id: launchToast
    interval: 2200
    onTriggered: {
      if (!root.opened || root.launchToastSession !== root.session) return root.resetLaunchToast()
      if (!osdProc.running) {
        osdProc.command = root.osdCloseCommand
        root.freshStreams(osdProc, root.errCap, null)
        osdDeadline.arm()
        osdProc.running = true
      }
      if (++root.launchToastTries < 2) { launchToast.interval = 800; launchToast.start() }
      else root.resetLaunchToast()
    }
  }
  Process {
    id: osdProc
    environment: root.sessionEnv
    clearEnvironment: true
    onExited: osdDeadline.stop()
  }
  Deadline { id: osdDeadline; proc: osdProc; interval: 5000 }

  function close() {
    if (!root.opened) return
    root.resetLaunchToast()
    root.opened = false
    root.awaitingLease = false
    root.surfaceShown = false
    heartbeat.stop()
    showFallback.stop()
    root.flushPending()        // a quick Esc must not drop a debounced change
    if (root.leased) root.lifecycle("closed")
    root.leased = false
    root.wizStep = ""          // the guide's place is saved; the next open resumes it from settings
    root.page = ""             // every open starts on the settings
    root.mapGroupsOpen = ({})
  }

  function dismiss() {
    root.close()
    if (root.shell && typeof root.shell.hide === "function")
      root.shell.hide((root.manifest && root.manifest.id) || "io.github.slogonomo.live-quake")
  }

  function toggle() {
    if (root.opened) root.dismiss()
    else root.open("{}")
  }

  function showSurface() {
    if (!root.opened) return
    var was = root.surfaceShown
    root.surfaceShown = true
    if (!was) root.cue("enter")
    Qt.callLater(function() { if (root.cardItem) root.cardItem.focusKeys() })
  }

  // ---------------------------------------------------- lease lifecycle queue
  // `menu opened` / `menu closed` run on their own serial queue (never behind slow settings
  // jobs), because the controller acts on arrival order: a renewal landing after `closed` would
  // pause the game for the whole 6 s lease. So renewals coalesce and a close drops renewals that
  // have not started. A failed renew always closes the menu: never fall back to `opened`.
  property var lifeQueue: []
  property var lifeCurrent: null

  function lifecycle(op) {
    var q = root.lifeQueue.slice()
    if (op === "opened" || op === "renew") {
      var busy = function(item) { return item && (item.op === "opened" || item.op === "renew") && item.session === root.session }
      if (op === "renew" && busy(root.lifeCurrent)) return
      for (var i = 0; i < q.length; i++) if (busy(q[i])) return
    } else {
      q = q.filter(function(item) { return item.op !== "opened" && item.op !== "renew" })
    }
    q.push({ op: op, session: root.session })
    root.lifeQueue = q
    Qt.callLater(root.lifePump)
  }

  function lifePump() {
    if (root.lifeCurrent || lifeProc.running || root.lifeQueue.length === 0) return
    var q = root.lifeQueue.slice()
    var next = q.shift()
    root.lifeQueue = q
    root.lifeCurrent = next
    lifeProc.command = [root.quakectl, "menu", next.op]
    root.freshStreams(lifeProc, root.errCap, null)     // lease verbs print nothing; drained and capped
    lifeProc.didStart = false
    lifeProc.launching = true
    lifeDeadline.arm()
    lifeProc.running = true
  }

  function lifeFinished(exitCode, errText) {
    var done = root.lifeCurrent
    if (!done) return
    root.lifeCurrent = null
    if (done.op === "opened" && done.session === root.session && root.awaitingLease && root.opened) {
      root.awaitingLease = false
      showFallback.stop()
      if (exitCode !== 0) root.say((errText || "the game did not answer").trim(), true)
      root.showSurface()
    } else if (done.op === "opened" && done.session === root.session && exitCode === 0 && root.opened
               && root.statusLine === "the game is not answering") {
      root.say("", false)                          // the fallback showed the menu early; the game did answer
    } else if (done.op === "renew" && exitCode !== 0 && done.session === root.session && root.opened) {
      // Fail closed: the lease is gone (the screensaver took over), or quakectl is unavailable.
      console.warn("live-quake menu: lease renewal failed, closing:", (errText || "").trim().slice(0, 300))
      root.dismiss()
    }
    Qt.callLater(root.lifePump)
  }

  // Every process the menu starts has a deadline: past it the process is stopped and the job
  // fails like any other error, so a hung command can never stall the queues.
  // Past the deadline the process gets SIGTERM; one that ignores it gets SIGKILL 2 s later.
  component Deadline: Timer {
    id: dl
    property var proc: null
    property bool hit: false
    function arm() { hit = false; restart() }
    onTriggered: if (proc && proc.running) { hit = true; proc.running = false; killLater.restart() }
    property Timer killLater: Timer { interval: 2000; onTriggered: if (dl.proc && dl.proc.running) dl.proc.signal(9) }
  }
  // Every output stream is capped as it arrives: past `cap` characters the process is stopped
  // and the call fails ("output too large"), so a misbehaving command can never fill memory.
  component Capped: StdioCollector {
    property var proc: null
    property int cap: 65536
    property bool hit: false
    waitForEnd: false
    onDataChanged: if (!hit && text.length > cap) { hit = true; if (proc && proc.running) proc.running = false }
  }
  // Each run gets fresh collectors, so no run can ever read the previous run's output.
  Component { id: cappedComponent; Capped {} }
  function freshStreams(proc, outCap, onOutFinished) {
    var oldOut = proc.stdout, oldErr = proc.stderr
    var out = cappedComponent.createObject(proc, { proc: proc, cap: outCap })
    if (onOutFinished) out.streamFinished.connect(onOutFinished)
    proc.stdout = out
    proc.stderr = cappedComponent.createObject(proc, { proc: proc, cap: root.errCap })
    if (oldOut) oldOut.destroy()
    if (oldErr) oldErr.destroy()
  }
  readonly property int outCap: 1048576     // settings JSON and the art index
  readonly property int errCap: 65536
  Deadline { id: lifeDeadline; proc: lifeProc; interval: root.testLimit || 10000 }
  Deadline { id: jobDeadline; proc: jobProc; interval: root.testLimit || 30000 }
  Deadline { id: artDeadline; proc: artProc; interval: root.testLimit || 20000 }

  // Quickshell 0.3.1: a process that fails to start (quakectl missing or not executable)
  // clears `running` without `started` or `exited`. Those launches complete here.
  Process {
    id: lifeProc
    environment: root.sessionEnv
    clearEnvironment: true
    property bool didStart: false
    property bool launching: false
    onStarted: { lifeProc.didStart = true; lifeProc.launching = false }
    onRunningChanged: if (!running && lifeProc.launching && !lifeProc.didStart) {
      lifeProc.launching = false
      lifeDeadline.stop()
      root.lifeFinished(-1, "could not run " + root.quakectl)
    }
    onExited: function(exitCode) {
      lifeDeadline.stop()
      var capped = lifeProc.stdout.hit || lifeProc.stderr.hit
      root.lifeFinished(capped ? -1 : exitCode,
                        lifeDeadline.hit ? "the game did not answer in time"
                        : (capped ? "quakectl output too large" : (lifeProc.stderr.text || "")))
    }
  }
  Timer {
    id: heartbeat      // the controller's lease expires 6 s after the last renewal
    interval: 2000
    repeat: true
    onTriggered: if (root.opened && root.leased && !root.awaitingLease) root.lifecycle("renew")
  }
  Timer {
    id: showFallback   // backend unresponsive: show anyway rather than strand the user
    interval: 1500
    onTriggered: {
      if (!root.opened || !root.awaitingLease) return
      root.awaitingLease = false
      root.say("the game is not answering", true)
      root.showSurface()
    }
  }

  // ------------------------------------------------------------ quakectl jobs
  // One serial queue: the backend applies each `set` as its own transaction, and ordering
  // keeps optimistic UI state and the backend from crossing.
  property var jobs: []
  property var currentJob: null

  function run(args, done) {
    root.jobs.push({ args: args, done: done })
    Qt.callLater(root.pump)
  }

  // Run fn once every edit made so far has reached the backend: flush settling values, then wait
  // behind the queued jobs. Finish uses it so its theme command never overtakes a queued save.
  function afterSettings(fn) {
    root.flushPending()
    root.jobs.push({ barrier: fn })
    Qt.callLater(root.pump)
  }

  function pump() {
    if (root.currentJob || jobProc.running) return
    while (root.jobs.length > 0 && root.jobs[0].barrier) {
      var b = root.jobs.shift()
      try { b.barrier() } catch (e) { console.warn("live-quake menu barrier:", e) }
    }
    if (root.jobs.length === 0) return
    root.currentJob = root.jobs.shift()
    jobProc.outDone = false
    jobProc.exitDone = false
    jobProc.failedStart = false
    jobProc.didStart = false
    jobProc.launching = true
    jobProc.command = [root.quakectl].concat(root.currentJob.args)
    root.freshStreams(jobProc, root.outCap, function() { jobProc.outDone = true; root.finishJob() })
    var args = root.currentJob.args     // package and mode switches restart the engine
    jobDeadline.interval = root.testLimit || (args[0] === "use-game" || (args[0] === "set" && args[1] === "game.mode") ? 40000 : 30000)
    jobDeadline.arm()
    jobProc.running = true
  }

  function finishJob() {
    if (!jobProc.outDone || !jobProc.exitDone || !root.currentJob) return
    var job = root.currentJob
    root.currentJob = null
    jobDeadline.stop()
    var capped = jobProc.stdout.hit || jobProc.stderr.hit
    var out = jobProc.failedStart || jobDeadline.hit || capped ? "" : (jobProc.stdout.text || "")
    var err = jobProc.failedStart ? "could not run " + root.quakectl
            : (jobDeadline.hit ? "the game did not answer in time"
            : (capped ? "quakectl output too large" : (jobProc.stderr.text || "")))
    try { if (job.done) job.done(capped ? -1 : jobProc.code, out, err) } catch (e) { console.warn("live-quake menu job callback:", e) }
    Qt.callLater(root.pump)
  }

  Process {
    id: jobProc
    environment: root.sessionEnv
    clearEnvironment: true
    property bool outDone: false
    property bool exitDone: false
    property bool failedStart: false
    property bool didStart: false
    property bool launching: false
    property int code: 0
    onStarted: { jobProc.didStart = true; jobProc.launching = false }
    onRunningChanged: if (!running && jobProc.launching && !jobProc.didStart) {   // failed to start
      jobProc.launching = false
      jobProc.failedStart = true
      jobProc.code = -1
      jobProc.outDone = true
      jobProc.exitDone = true
      root.finishJob()
    }
    onExited: function(exitCode) {
      jobProc.code = jobDeadline.hit ? -1 : exitCode
      jobProc.exitDone = true
      root.finishJob()
    }
  }

  // ------------------------------------------------------------- game art
  // The menu's look comes from the game data the controller reports (game.data.paks), in engine
  // search order. One export per distinct PAK list; a failure keeps the monospace fallback.
  property string artTried: ""
  Process {
    id: artProc
    environment: root.sessionEnv
    clearEnvironment: true
    onExited: {
      artDeadline.stop()
      var want = root.wantedPaks()
      if (want && want !== root.artTried) Qt.callLater(root.loadArt)
    }
  }
  function artOutput() {
    var out = artProc.stdout
    if (!out || out.hit) return
    try {
      var idx = JSON.parse(out.text)
      root.art = idx && idx.conchars && idx.dir ? idx : null
    } catch (e) { /* keep the fallback */ }
    var err = artProc.stderr ? String(artProc.stderr.text || "").trim() : ""
    if (err) console.warn("live-quake menu: art: " + err.split("\n").pop().slice(0, 300))
  }
  function wantedPaks() {
    var d = root.settings && root.settings.game && root.settings.game.data
    var paks = d && Array.isArray(d.paks) ? d.paks.filter(function(p) { return typeof p === "string" && p.charAt(0) === "/" }) : []
    return paks.length ? JSON.stringify(paks.slice(0, 8)) : ""
  }
  function loadArt() {
    var want = root.wantedPaks()
    if (!want || want === root.artTried || artProc.running) return
    root.artTried = want
    artProc.command = [root.python, "-E", "-s", "-B", root.pluginDir + "pakart.py", "export", root.cacheBase].concat(JSON.parse(want))
    root.freshStreams(artProc, root.outCap, root.artOutput)
    artDeadline.arm()
    artProc.running = true
  }
  function pic(name) {
    var p = root.art && root.art.pictures ? root.art.pictures[name] : null
    return p && !p.blank ? { url: "file://" + root.art.dir + "/" + p.file, w: p.w, h: p.h } : null
  }

  // Menu cues, as the engine plays them: move (menu1), enter (menu2), adjust (menu3). Each plays
  // in a short-lived pw-play process (MenuSounds.qml), so no audio context lives in the shell.
  // Silent when every game sound is switched off.
  Loader {
    id: soundLoader
    active: root.sounds && root.hosted && !!(root.art && root.art.sounds)
    source: active ? "MenuSounds.qml" : ""
    onLoaded: {
      item.env = root.sessionEnv
      item.dir = Qt.binding(function() { return root.art ? root.art.dir : "" })
    }
  }
  function cue(name) {
    var s = root.settings && root.settings.sound
    if (!s || !(s.playing || s.idle || s.screensaver)) return
    if (soundLoader.item && root.art && root.art.sounds && root.art.sounds[name]) soundLoader.item.play(root.art.sounds[name])
  }

  // --------------------------------------------------------------- settings
  function refresh(then) {
    root.run(["settings"], function(code, out, err) {
      if (code === 0) {
        var s = root.applySettings(out)
        var problem = s && s.runtime && s.runtime.last_error ? String(s.runtime.last_error) : ""
        if (root.statusLine === "connecting to the game") root.say(problem, !!problem)   // nothing to say when all is well
        if (root.captureStarting(s)) { if (root.opened && !capturePoll.running && root.capturePolls++ < 12) capturePoll.restart() }
        else root.capturePolls = 0
        if (then) then()
      } else {
        root.say((err || out || "the game is not running").trim(), true)
      }
    })
  }

  // OpenGL freeze-frame: "Show on" and blur become available shortly after the
  // game session reports that frozen copies work. While that report is pending, re-read settings every
  // 500 ms, for at most 6 s, so the rows unlock without reopening the menu.
  property int capturePolls: 0
  function captureStarting(s) {
    var rr = s && s.runtime && s.runtime.renderer || ({}), wp = s && s.wallpaper || ({})
    return rr.state === "ready" && wp.mode_available === false
           && /starting/i.test(String(wp.mode_reason || ""))
  }
  Timer { id: capturePoll; interval: 500; onTriggered: if (root.opened) root.refresh() }

  // Returns the parsed document (or null) so callers can read derived state such as runtime.last_error.
  function applySettings(json) {
    try {
      var s = JSON.parse(json)
      if (!s || typeof s !== "object" || !s.sound) return null
      // A snapshot can predate edits still queued or settling; keep those.
      for (var k in root.inflight) root.assign(s, k, root.inflight[k].value)
      for (var q in root.pending) root.assign(s, q, root.pending[q])
      root.settings = s
      Qt.callLater(root.loadArt)
      var ob = s.onboarding && s.onboarding.show ? s.onboarding : null
      if (ob && root.wizStep === "" && root.opened) {   // first settings of this open: resume the guide
        if (ob.fresh && !ob.opened)                     // seen, however it opened: no auto-open at the next login
          root.run(["onboarding", "opened"], function(c, o, err) {
            if (c !== 0) console.warn("live-quake menu: could not record the guide's first open: " + String(err || o).trim().slice(0, 200))
          })
        root.wizWentBack = false
        root.wizSync(s)
        root.wizCursor()
        root.wizAutoScan(s)
      } else if (ob && root.wizStep === "game" && root.wizSteps(s).indexOf("game") === -1) {
        // A scan found Quake, so the game step no longer applies: on to the next step, or back to the one before when
        // Back led here. The scan's own status says what it found.
        root.wizStep = root.wizWentBack ? "background" : "screensaver"
        root.wizSave()
        root.wizCursor()
      }
      root.clampCursor()
      return s
    } catch (e) { root.say("unreadable settings from quakectl", true) }
    return null
  }

  function getv(obj, key) {
    var parts = key.split(".")
    var v = obj
    for (var i = 0; i < parts.length; i++) { if (v === null || v === undefined) return undefined; v = v[parts[i]] }
    return v
  }
  function assign(obj, key, value) {
    var parts = key.split(".")
    var o = obj
    for (var i = 0; i < parts.length - 1; i++) { if (!o[parts[i]]) o[parts[i]] = ({}); o = o[parts[i]] }
    o[parts[parts.length - 1]] = value
  }
  function setLocal(key, value) {
    var copy = JSON.parse(JSON.stringify(root.settings || {}))
    root.assign(copy, key, value)
    root.settings = copy
  }

  // Toggles and choices go straight out; sliders settle first.
  property var pending: ({})
  function setValue(key, value, settle) {
    root.setLocal(key, value)
    if (settle) {
      var p = root.pending
      p[key] = value
      root.pending = p
      settleTimer.restart()
      return
    }
    root.send(key, value)
  }
  property var inflight: ({})      // key -> {value, count}: sets queued or running
  function send(key, value) {
    var f = root.inflight
    var entry = f[key] || { value: value, count: 0 }
    entry.value = value
    entry.count += 1
    f[key] = entry
    root.inflight = f
    root.run(["set", key, JSON.stringify(value)], function(code, out, err) {
      var g = root.inflight
      if (g[key] && --g[key].count <= 0) delete g[key]
      root.inflight = g
      if (code === 0) {
        // Saved; the backend may still report that applying it failed (it then applies at the next start).
        var before = root.settings && root.settings.runtime ? root.settings.runtime.last_error : null
        var s = root.applySettings(out)
        var problem = s && s.runtime && s.runtime.last_error ? String(s.runtime.last_error) : ""
        if (!problem) root.say("saved", false)
        else if (problem !== String(before || "")) root.say("saved; " + problem, true)     // a new problem
        else root.say("saved; still: " + problem, false)                                 // a persistent one, kept visible
      } else {
        root.say((err || out || "rejected").trim(), true)
        root.refresh()
      }
    })
  }
  function flushPending() {
    settleTimer.stop()
    var p = root.pending
    root.pending = ({})
    for (var k in p) root.send(k, p[k])
  }
  Timer {
    id: settleTimer
    interval: 220
    onTriggered: root.flushPending()
  }

  function say(text, isError) {
    // one line: a traceback or multi-line stderr shows its last meaningful line
    var lines = String(text).split(/\r?\n/).filter(function(l) { return l.trim() !== "" })
    root.statusLine = (lines.length ? lines[lines.length - 1].trim() : "").toLowerCase()
    root.statusError = !!isError
    if (isError && lines.length) console.warn("live-quake menu: " + lines[lines.length - 1].trim().slice(0, 300))
  }

  // ------------------------------------------------------------------- rows
  // Each row carries its own help text, shown in the message box at the bottom while the row is
  // selected (the engine's options screen has no inline notes; this keeps the list as compact).
  readonly property var skills: [[0, "Easy"], [1, "Normal"], [2, "Hard"], [3, "Nightmare"]]
  readonly property var modes: [["deathmatch", "Deathmatch"], ["single", "Single player"]]
  readonly property var roundTimes: [[3, "3 min"], [6, "6 min"], [10, "10 min"], [15, "15 min"], [20, "20 min"]]
  readonly property var fragLimits: [[0, "Off"], [15, "15"], [25, "25"], [50, "50"]]
  readonly property var botCounts: [[1, "1"], [2, "2"], [3, "3"], [4, "4"], [5, "5"], [6, "6"], [7, "7"]]
  property string page: ""               // "" the settings; "maps" the Deathmatch maps page
  property var mapGroupsOpen: ({})       // the maps page's expanded groups, by group id
  readonly property string storeUrl: "https://store.steampowered.com/app/2310/"

  // screensaver.state and screensaver.reason_code (additive contract): why Live Quake's screensaver is or isn't the
  // one that shows. Sticky codes stay until the user turns it on again; transient ones clear by themselves, and
  // Omarchy's own screensaver covers idle meanwhile. Backends without the keys get the plain description.
  function saverHelp(ss) {
    var code = typeof ss.reason_code === "string" ? ss.reason_code : ""
    var sticky = {
      "claim-revoked": "Turned off because Omarchy's screensaver setting changed. Turn on to use Live Quake's again.",
      "repeated-presentation-failure": "Turned off after it failed to appear three times in a row. Turn on to try again.",
      "external-suppression": "Omarchy's screensaver was switched off elsewhere, so Live Quake stood down. Turn off and on to retry.",
      "recovery-unavailable": "Its recovery record couldn't be read or saved, so Omarchy's screensaver covers idle. Turn off and on to reset it."
    }
    if (sticky[code]) return sticky[code]
    if (!ss.enabled) return "When the screen idles, the game becomes your screensaver instead of Omarchy's. Any input dismisses it."
    var why = {
      "paused": "while the game is paused, the menu is open or you're playing",
      "loading": "while a game loads",
      "runtime-unavailable": "until the game is running",
      "desktop-unavailable": "while the screen is locked or asleep",
      "idle-unavailable": "while Omarchy's idle status can't be read",
      "ownership-unavailable": "while Omarchy's screensaver setting can't be checked",
      "failure-backoff": "for the next idle, because the last screensaver didn't appear"
    }
    if (why[code]) return "Omarchy's screensaver covers idle " + why[code] + "."
    if (ss.state === "presenting" || ss.state === "active") return "Showing now. Any input dismisses it."
    if (ss.state === "warming") return "Getting the game ready; Omarchy's screensaver covers idle until then."
    return "On: when the screen idles, the game becomes your screensaver instead of Omarchy's. Any input dismisses it."
  }
  // A foreign "screensaver-off" entry (Omarchy's own toggle or another plugin) blocks turning ours on: "taken". The toggle
  // stays when ours is on (to turn it off) or when the entry is our own one, changed by Omarchy's toggle
  // (screensaver.adoption_available), so turning ours on again can take it back. Older backends: "unavailable".
  function saverOffer(ss) {
    if (ss.owner === "other" && !(ss.claim_enabled !== false && (ss.enabled || ss.adoption_available === true))) return "taken"
    return ss.claim_enabled === false ? "unavailable" : "ok"
  }
  readonly property string saverTaken: "Omarchy's screensaver is switched off elsewhere (its own toggle or another plugin). Live Quake leaves that alone."
  readonly property string saverUnavailable: "Live Quake doesn't take over Omarchy's screensaver yet; that needs your approval."

  function buildRows(s) {
    var r = []
    if (!s) return r
    if (s.onboarding && s.onboarding.show) { root.wizardRows(r, s, s.onboarding); return r }
    if (root.page === "maps") { root.mapRows(r, s); return r }
    var dm = (s.game || ({})).effective_mode === "deathmatch"     // game.effective_mode (additive contract)
    function section(t) { r.push({ kind: "section", label: t }) }
    function toggle(key, label, help) { r.push({ kind: "toggle", key: key, label: label, help: help }) }

    section("Sound")
    // runtime.audio (additive contract): where game sound goes now, and why it is silent when it is.
    var audio = s.runtime && s.runtime.audio && typeof s.runtime.audio === "object" ? s.runtime.audio : null
    var muted = ""
    if (audio) {
      var reason = typeof audio.reason === "string" ? audio.reason : ""
      var output = ({
        playing: ["System audio", "Game sound goes to your system audio output."],
        ready: ["System audio", "Sound is on. It plays when the game resumes."],
        muted: ["Muted", "Sound is off for what is showing now. Turn it on below."],
        unavailable: ["Unavailable", reason || "Could not open the system audio output."],
        off: ["Off", reason || "Sound output is turned off."]
      })[String(audio.state)] || ["Unknown", reason]
      r.push({ kind: "info", id: "audio", label: "Output", value: output[0], help: output[1] })
      if (audio.state === "unavailable") muted = " Applies once audio is available."
    } else if (s.runtime && typeof s.runtime.sound_output === "string" && /discard/i.test(s.runtime.sound_output)) {
      muted = " Sound output is not connected in this prototype yet."      // older backends
    }
    toggle("sound.playing", "While you play", "Game sound while you are playing." + muted)
    toggle("sound.idle", "Live background", "Game sound while Quake plays on your desktop." + muted)
    toggle("sound.screensaver", "Screensaver", "Game sound while the screensaver runs." + muted)

    section("Live background")
    var wp = s.wallpaper || ({})
    var markerMode = typeof wp.live_choice_present === "boolean"   // contract v2: the selected background decides
    if (typeof wp.enabled === "boolean") {
      if (markerMode)
        r.push({ kind: "info", label: "Enabled", value: wp.enabled ? "on" : "off",
                 help: "Choose Live Quake in this theme's backgrounds to turn it on, or any other to turn it off." })
      else
        toggle("wallpaper.enabled", "Enabled", (dm ? "Bots play deathmatch" : "A bot plays Quake") + " on your desktop. Opening a window pauses it.")
    }
    // wallpaper.mode_available (additive contract): with OpenGL it is false until the running game session
    // reports that frozen copies work; a saved "All desktops" comes back by itself once it does.
    if (wp.mode_available === false)
      r.push({ kind: "off", id: "showon", label: "Show on", value: "Empty desktops",
               help: typeof wp.mode_reason === "string" && wp.mode_reason ? wp.mode_reason
                     : "Available once the game is running." })
    else
      r.push({ kind: "choice", key: "wallpaper.mode", label: "Show on",
               options: [["empty", "Empty desktops"], ["all", "All desktops"]],
               help: "Empty desktops: plays where no windows are open, pauses behind them. All desktops: keeps playing everywhere." })
    // Live-background marker (contract v2): shown only once the backend reports it.
    if (markerMode) {
      if (!wp.active)
        r.push({ kind: "action", id: "uselive", label: "Use with this theme", value: "Go",
                 help: wp.live_choice_present
                   ? "Selects this theme's Live Quake background. Pick another background to turn it off."
                   : "Adds a Live Quake background to this theme and selects it. Your theme stays as it is." })
      if (wp.can_remove)
        r.push({ kind: "action", id: "removelive", label: "Remove from this theme", value: "Remove",
                 help: "Archives the Live Quake background Live Quake added to this theme. Nothing is deleted." })
    }
    var offer = root.themeOffer(s.onboarding)
    if (offer)
      r.push({ kind: "action", id: "theme", label: "Slipgate theme", value: offer === "install" ? "Install" : "Use",
               help: (offer === "install" ? "Installs" : "Switches to") + " our Quake-inspired theme, with the live background selected." })

    section("Live screensaver")
    var ss = s.screensaver || ({})
    var saver = root.saverOffer(ss)
    if (saver === "taken")
      r.push({ kind: "off", label: "Enabled", value: "Taken", help: root.saverTaken })
    else if (saver === "unavailable")              // older backends: read-only on Omarchy's toggle
      r.push({ kind: "off", label: "Enabled", value: ss.enabled ? "on" : "off", help: root.saverUnavailable })
    else
      toggle("screensaver.enabled", "Enabled", root.saverHelp(ss))

    section("View and controls")
    // runtime.renderer and blur.available (additive contract): which renderer draws the game, and whether
    // the paused-game blur can work with it.
    var renderer = s.runtime && s.runtime.renderer && typeof s.runtime.renderer === "object" ? s.runtime.renderer : null
    if (renderer) {
      // runtime.renderer.state and retry_available (additive contract): after repeated start failures OpenGL
      // stops retrying until the user asks.
      var rstate = typeof renderer.state === "string" ? renderer.state : ""
      var rwhy = typeof renderer.reason === "string" && renderer.reason ? " " + String(renderer.reason).replace(/\.+$/, "") + "." : ""
      r.push({ kind: "info", id: "renderer", label: "Renderer",
               value: rstate === "unavailable" ? "Unavailable" : rstate === "starting" ? "Starting" : "OpenGL",
               help: rstate === "unavailable" ? "OpenGL stopped repeatedly, so the game is off for now." + rwhy
                     : rstate === "starting" ? (s.runtime.pause_reason === "checkpoint-loading"
                                                ? "OpenGL is restoring the saved game."
                                                : s.runtime.pause_reason === "package-loading"
                                                ? "Checking your chosen game before starting." : "OpenGL is starting.")
                     : rstate === "idle" ? "OpenGL draws the game when there is something to show."
                     : "Drawn on your GPU and handed to the desktop without copying frames." })
      if (renderer.retry_available === true)
        r.push({ kind: "action", id: "retryrenderer", label: "Retry OpenGL", value: "Retry",
                 help: "Tries to start OpenGL again. If it keeps failing, the daemon log says why." })
    }
    var blur = s.blur || ({})
    if (blur.available === false)
      r.push({ kind: "off", label: "Blur when paused", value: "N/A",
               help: typeof blur.reason === "string" && blur.reason ? blur.reason : "Available once the game is running." })
    else
      r.push({ kind: "slider", key: "blur.percent", label: "Blur when paused", min: 0, max: 100, step: 5, digits: 0, suffix: "%",
               help: "How much the paused game blurs behind your windows." })
    r.push({ kind: "slider", key: "mouse.sensitivity", label: "Mouse speed", min: 0.1, max: 4, step: 0.1, digits: 1, suffix: "",
             help: "Mouse speed when you take over the game." })
    toggle("mouse.invert", "Invert mouse", "Mouse up looks down when you take over.")
    // wallpaper.hide_chatter (additive contract): notify lines only, while the bot is shown.
    if (typeof (s.wallpaper || ({})).hide_chatter === "boolean")
      toggle("wallpaper.hide_chatter", "Hide chatter", "Hides chat, kills and pickups while the bot plays. Map messages and scores stay.")

    section("Game")
    var g = s.game || ({}), d = g.data || ({})
    if (g.mode_available !== true)
      r.push({ kind: "off", label: "Mode", value: "Single player", help: String(g.mode_reason || "Deathmatch is unavailable for this game.") })
    else
      r.push({ kind: "choice", key: "game.mode", label: "Mode", options: root.modes,
               help: "Deathmatch: bots fight each other. Changing mode restarts the game." })
    r.push({ kind: "choice", key: "game.skill", label: "Skill", options: root.skills,
             help: g.effective_mode === "deathmatch" ? "How well the bots fight, from the next round. Nightmare bots fight as Hard."
                                                     : "The bot's and your difficulty. Applies from the next level." })
    if (dm) {      // game.dm_timelimit, dm_fraglimit and dm_bots (additive contract): from the next round
      r.push({ kind: "choice", key: "game.dm_timelimit", label: "Round time", options: root.roundTimes,
               help: "How long each round lasts. Applies from the next round." })
      r.push({ kind: "choice", key: "game.dm_fraglimit", label: "Frag limit", options: root.fragLimits,
               help: "A round also ends when someone reaches this many frags." })
      r.push({ kind: "choice", key: "game.dm_bots", label: "Bots", options: root.botCounts,
               help: "How many bots fight. Applies from the next round." })
      var counts = root.mapCounts(g)   // game.dm_available_maps and dm_available_sp_maps (additive contract)
      if (counts[1])
        r.push({ kind: "action", id: "maps", label: "Maps", value: counts[0] + " on",
                 help: "Choose the maps in the random rotation, in groups. Single-player maps start off." })
    }
    var cat = Array.isArray(g.catalog) ? g.catalog : []
    // Never show paths: they can name the user's home folder and would land in screenshots.
    var where = "Played in place from your own files; nothing is copied."
    var listed = cat.some(function(p) { return p && p.id && p.id === g.selected_package })
    if (d.title && !listed)       // the catalog row says the same when the selected package is in it
      r.push({ kind: "info", label: "Game data", value: root.fit(d.title, 16),
               help: d.title + ", played in place from your own files; nothing is copied." })
    var steamQuake = false
    var switching = typeof g.scanning === "boolean"     // contract v3: the backend can switch games and scan
    var note = typeof g.selection_note === "string" && g.selection_note ? " " + g.selection_note : ""
    for (var i = 0; i < cat.length; i++) {
      var p = cat[i] || ({})
      if (p.source === "steam" && p.playable && /quake/i.test(String(p.title || ""))) steamQuake = true
      var selected = p.id && p.id === g.selected_package
      var title = root.shortTitle(p.title || p.id || "?")
      if (selected)
        // The message box holds two lines; keep each help short enough for the backend's note to fit.
        r.push({ kind: "info", label: title, value: "Playing",
                 help: p.id === "librequake" ? (note ? "The bot is playing this." + note : "The bot is playing this. " + where)
                       : "Playing in place from Steam's own folder." + note })
      else if (p.playable && switching)
        r.push({ kind: "action", id: "game", pkg: String(p.id), label: title, value: "Play",
                 help: note ? (dm ? "Plays this instead; the current match ends." : "Plays this instead, saving the current game.") + note
                            : p.source === "steam" ? (dm ? "Plays this instead, in place from Steam; the current match ends."
                                                         : "Plays this instead, in place from Steam; the current game is saved.")
                            : dm ? "Plays this instead; the current match ends." : "Plays this instead, saving the current game." })
      else if (p.playable)
        r.push({ kind: "info", label: title, value: "Found", help: "Switching games is not available in this prototype yet." })
      else
        r.push({ kind: "off", label: title, value: "N/A", help: "Not playable here" + (p.reason ? ": " + String(p.reason).replace(/\.+$/, "") : "") + "." })
    }
    if (switching)
      r.push({ kind: "action", id: "scan", label: "Find Steam games", value: g.scanning ? "Scanning" : "Scan",
               help: g.scan_error ? String(g.scan_error)
                     : "Looks for Quake in your Steam libraries without changing them. Games play from there." })
    var rt = s.runtime || ({})
    if (rt.reroute_available === true && rt.loaded !== false && rt.owner === "bot")
      r.push({ kind: "action", id: "reroute", label: "Reroute bot", value: "Go",
               help: dm ? "The bot you're watching looks stuck? This gives it a nudge now."
                        : "The bot looks like it's stuck? This makes it plan a new route now." })
    if (!steamQuake)
      r.push({ kind: "action", id: "store", label: "Get Quake on Steam", value: "Store",
               help: "Opens Quake's Steam store page. Once it's installed, use Find Steam games." })
    return r
  }
  // Labels hold 24 cells and values 16 (menu canvas columns); longer text ends in "…".
  function fit(t, n) {
    t = String(t || "")
    if (t.length <= n) return t
    var cut = t.lastIndexOf(" ", n)             // whole words where possible: "LibreQuake 0.09-beta" → "LibreQuake"
    return cut >= n / 2 ? t.slice(0, cut) : t.slice(0, n - 2) + ".."
  }
  function shortTitle(t) { return root.fit(t, 24) }

  // ------------------------------------------------------------ first-run guide
  // Live Background, Choose your Quake (only while no official Steam game is playable), Live Screensaver, then a
  // summary (contract v4, ported from Live Doom). The background and screensaver choices are inert until Finish (the
  // backend saves them with the step, `onboarding step`), so Back never undoes anything; the game step acts at once.
  property string wizStep: ""            // "" until this open has read the saved step
  property string wizBackground: "current"
  property bool wizScreensaver: true
  property bool wizScanned: false        // the game step's one automatic Steam scan
  property bool wizWentBack: false       // the last step change was Back

  // The official game, once a scan has found it playable in Steam (null until then).
  function wizOfficial(s) {
    var g = s && s.game || ({})
    if (!(g.revision > 0)) return null
    return (Array.isArray(g.catalog) ? g.catalog : []).filter(function(p) {
      return !!p && (p.id === "steam-quake" || p.id === "steam-quake-rerelease") && p.playable === true
    })[0] || null
  }
  function wizSteps(s) {
    return root.wizOfficial(s) ? ["background", "screensaver", "summary"] : ["background", "game", "screensaver", "summary"]
  }
  function wizSync(s) {
    var ob = s.onboarding, c = ob.choices || ({}), steps = root.wizSteps(s)
    root.wizBackground = ["current", "slipgate", "skip"].indexOf(c.background) !== -1 ? c.background : "current"
    root.wizScreensaver = c.screensaver === true
    root.wizStep = steps.indexOf(ob.step) !== -1 ? ob.step : ob.step === "game" ? "screensaver" : steps[0]
  }
  // Slipgate can only be chosen while it can be offered; otherwise the choice reads as the current theme.
  function wizBackgroundChoice(ob) {
    return root.wizBackground === "slipgate" && !root.themeOffer(ob) ? "current" : root.wizBackground
  }
  function wizCursor() { root.cursorTo(function(r) { return r.id === "wizNext" }) }   // Enter takes the easy path
  function wizSave() {                   // the step and the inert choices, for a resume after a close or restart
    if (root.wizStep === "") return
    var ob = root.settings && root.settings.onboarding
    root.run(["onboarding", "step", root.wizStep, "background=" + root.wizBackgroundChoice(ob),
              "screensaver=" + (root.wizScreensaver ? "on" : "off")], function(code) {
      if (code !== 0) root.say("could not save the guide's place", true)
    })
  }
  function wizAutoScan(s) {              // the game step looks in Steam once, unless a scan has already run
    if (root.wizStep !== "game" || root.wizScanned || !s.game || s.game.revision !== 0) return
    root.wizScanned = true
    root.scanSteam()
  }
  function wizGo(dir) {
    var s = root.settings, ob = s && s.onboarding
    if (!ob || !ob.show || root.wizStep === "") return
    if (root.switching) return root.say("still applying the previous change", false)
    var steps = root.wizSteps(s), i = Math.max(0, steps.indexOf(root.wizStep))
    if (dir > 0 && steps[i] === "summary") return root.wizFinish()
    var j = Math.max(0, Math.min(steps.length - 1, i + dir))
    if (j === i) return
    root.wizStep = steps[j]
    root.wizWentBack = dir < 0
    root.say("", false)
    root.wizSave()
    root.wizCursor()
    root.wizAutoScan(s)
  }
  function wizSkip() {
    root.switchJob(["onboarding", "skip"], "", "could not skip the guide", function() {
      root.cursor = -1
      root.clampCursor()
      return ["setup skipped. every setting is here", false]
    })
  }
  // Display name of an Omarchy theme directory ("osaka-jade" -> "Osaka Jade").
  function themeTitle(s) {
    var n = String(s && s.wallpaper && s.wallpaper.theme || "")
    if (!n) return "your current theme"
    return n.split(/[-_]+/).map(function(w) { return w ? w.charAt(0).toUpperCase() + w.slice(1) : w }).join(" ")
  }
  function gameTitle(s) {
    var g = s.game || ({}), cat = Array.isArray(g.catalog) ? g.catalog : []
    for (var i = 0; i < cat.length; i++)
      if (cat[i] && cat[i].id && cat[i].id === g.selected_package) return root.fit(cat[i].title || cat[i].id, 16)
    return root.fit(g.data && g.data.title || "LibreQuake", 16)
  }
  // The subtitle while the guide shows ("" otherwise).
  function guideHead(s) {
    if (!s || !s.onboarding || !s.onboarding.show) return ""
    var steps = root.wizSteps(s), i = steps.indexOf(root.wizStep)
    return i < 0 ? "WELCOME" : "WELCOME  ·  STEP " + (i + 1) + " OF " + steps.length
  }

  // Quake-native rows: choices are actions whose value column marks the chosen one, Next, Back and Skip are actions,
  // and each step has one `note` (wrapped UI-font text) under its heading.
  function wizardRows(r, s, ob) {
    if (root.wizStep === "") {           // reopened: wait for this open's settings before offering choices
      r.push({ kind: "section", label: "Welcome to Live Quake" })
      r.push({ kind: "note", text: "One moment…" })
      return
    }
    var steps = root.wizSteps(s), step = steps.indexOf(root.wizStep) !== -1 ? root.wizStep : steps[0]
    var bg = root.wizBackgroundChoice(ob), wp = s.wallpaper || ({}), saver = root.saverOffer(s.screensaver || ({}))
    function option(choice, label, help) {
      r.push({ kind: "action", id: "wizBg", choice: choice, label: label, value: bg === choice ? "[x]" : "[ ]", help: help })
    }
    if (step === "background") {
      r.push({ kind: "section", label: "Live Background" })
      r.push({ kind: "note", text: "A bot plays Quake on your empty desktops. Left-click to jump in any time; F12 hands it back." })
      option("current", wp.active ? "Keep on current theme" : "Add to current theme",
             (wp.active ? "Keeps the live background on " : "Adds and selects the live background on ") + root.themeTitle(s)
             + ". SUPER CTRL + SPACE switches backgrounds and turns it off.")
      var offer = root.themeOffer(ob)
      if (offer)
        option("slipgate", offer === "install" ? "Install Slipgate theme" : "Use Slipgate theme",
               (offer === "install" ? "Installs" : "Switches to") + " our Quake-inspired theme, with the live background already selected.")
      option("skip", "Skip live background", wp.active ? "Leaves the live background as it is."
             : "Add it later by opening Live Quake from Apps. Until then there is no game on your desktop to jump into.")
    } else if (step === "game") {
      var g = s.game || ({})
      r.push({ kind: "section", label: "Choose your Quake" })
      r.push({ kind: "note", text: "The bot needs game data. LibreQuake is free and already installed. Quake from Steam plays from Steam's own folder." })
      if (g.selected_package === "librequake")
        r.push({ kind: "info", label: "LibreQuake", value: "Playing", help: "The bot is playing LibreQuake, the free game data that came with Live Quake." })
      else
        r.push({ kind: "action", id: "game", pkg: "librequake", label: "LibreQuake", value: "Play",
                 help: "Plays LibreQuake, the free game data that came with Live Quake, saving the current game." })
      r.push({ kind: "action", id: "scan", label: "Find Steam games", value: g.scanning ? "Scanning" : "Scan",
               help: g.scan_error ? String(g.scan_error) : "Looks for Quake in your Steam libraries without changing them. Games play from there." })
      r.push({ kind: "action", id: "store", label: "Get Quake on Steam", value: "Store",
               help: "Opens Quake's Steam store page. Once it's installed, use Find Steam games." })
    } else if (step === "screensaver") {
      r.push({ kind: "section", label: "Live Screensaver" })
      r.push({ kind: "note", text: "When your screen idles, Quake becomes your screensaver and the bot keeps playing. Any input dismisses it." })
      if (saver === "ok")
        r.push({ kind: "toggle", id: "wizSaver", label: "Enable", on: root.wizScreensaver, help: "Applied when you press Finish." })
      else
        r.push({ kind: "off", label: "Enable", value: saver === "taken" ? "Taken" : "Unavailable",
                 help: saver === "taken" ? root.saverTaken : root.saverUnavailable })
    } else {
      var live = bg !== "skip" || !!wp.active    // Finish never removes a live background that is already on
      r.push({ kind: "section", label: "Your setup" })
      r.push({ kind: "info", label: "Live background", value: live ? "Enabled" : "Disabled",
               help: "Chosen in the first step. Back goes there to change it." })
      r.push({ kind: "info", label: "Quake", value: root.gameTitle(s),
               help: "The game the bot plays. The Game section of the menu changes it any time." })
      r.push({ kind: "info", label: "Mode", value: (s.game || ({})).effective_mode === "single" ? "Single player" : "Deathmatch",
               help: "Deathmatch: bots fight each other. The Game section's Mode row changes it." })
      var official = root.wizOfficial(s)
      if (official && (s.game || ({})).selected_package === "librequake")   // offered here; Finish never switches
        r.push({ kind: "action", id: "game", pkg: official.id, label: "Quake from Steam", value: "Play",
                 help: "Plays your own Quake now, from Steam's folder, instead of LibreQuake." })
      r.push({ kind: "info", label: "Screensaver",
               value: saver === "taken" ? "Taken" : saver !== "ok" ? "Unavailable" : root.wizScreensaver ? "Enabled" : "Disabled",
               help: saver === "taken" ? root.saverTaken : saver !== "ok" ? root.saverUnavailable
                     : "Chosen in the previous step. Applied when you press Finish." })
      r.push({ kind: "note", text: "Open Live Quake from Apps to change settings at any time." })
    }
    r.push({ kind: "note", text: "" })     // a blank line before the guide's own controls
    r.push({ kind: "action", id: "wizNext", label: step === "summary" ? "Finish" : "Next", value: "",
             help: step === "summary" ? "Applies these choices, closes the guide and shows every setting."
                                      : "On to the next step. Nothing is applied until you press Finish." })
    if (steps.indexOf(step) > 0)
      r.push({ kind: "action", id: "wizBack", label: "Back", value: "", help: "Goes back a step. Nothing is undone." })
    if (step !== "summary")
      r.push({ kind: "action", id: "wizSkip", label: "Skip setup", value: "All settings",
               help: "Closes this guide for good and shows every setting. Nothing else changes." })
  }

  // Finish applies the saved choices with the ordinary verbs, in order, then closes the guide. Any failure stops
  // there and stays on the summary with the reason. No game switch: the game step already acted.
  function wizFinish() {
    var s = root.settings || ({}), ob = s.onboarding
    if (!ob || !ob.show) return
    if (root.switching || themeProc.running) return root.say("still applying the previous change", false)
    var bg = root.wizBackgroundChoice(ob), offer = root.themeOffer(ob), wp = s.wallpaper || ({}), ss = s.screensaver || ({})
    var saver = root.wizScreensaver, saverChange = root.saverOffer(ss) === "ok" && !!ss.enabled !== saver
    if (saverChange && saver && ss.owner === "unknown") {   // the backend would refuse it last, after the background
      root.refresh()
      return root.say("screensaver ownership unclear; try finish again", true)
    }
    root.switching = true
    function fail(text) {
      root.switching = false
      root.say(String(text || "could not finish setup").trim(), true)
      root.refresh()
    }
    var chain = []
    if (bg === "slipgate") chain.push(function(next) {
      root.say(offer === "install" ? "installing slipgate" : "switching to slipgate", false)
      root.runTheme(offer, ob.theme, function(code, error) { if (code === 0) next(); else fail(error || "could not switch to slipgate") })
    })
    if (bg === "slipgate" || (bg === "current" && !wp.active)) chain.push(function(next) {
      root.say("turning on the live background", false)
      root.run(["use-live-wallpaper"], function(code, out, err) { if (code === 0) next(); else fail(err || out || "could not turn on the live background") })
    })
    if (saverChange) chain.push(function(next) {
      root.run(["set", "screensaver.enabled", JSON.stringify(saver)], function(code, out, err) {
        if (code === 0) next(); else fail(err || out || "could not change the screensaver")
      })
    })
    chain.push(function() {
      root.run(["onboarding", "finish"], function(code, out, err) {
        if (code !== 0) return fail(err || out || "could not finish setup")
        root.refresh(function() {
          root.cursor = -1
          root.clampCursor()
          var w = root.settings && root.settings.wallpaper || ({})
          if (w.active) root.say("live quake is on", false)
          else if (bg === "skip") root.say("all set. open live quake from apps any time", false)
          else root.say("selected; waiting for the background to switch", true)
        })
        root.settled()
      })
    })
    var i = 0
    var next = function() { if (i < chain.length) chain[i++](next) }
    root.say("finishing setup", false)
    root.afterSettings(next)
  }

  // What the Slipgate offer can do: "set" (installed, not active), "install" (a GitHub https URL from the backend), or
  // "" (nothing to offer; with the URL still null, nothing until the theme is installed by hand).
  function themeOffer(ob) {
    var th = ob && ob.theme
    if (!th || th.active === true || !/^[a-z0-9][a-z0-9._+-]*$/.test(String(th.name || ""))) return ""
    if (th.installed === true) return "set"
    return /^https:\/\/github\.com\/[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+(\.git)?$/.test(String(th.url || "")) ? "install" : ""
  }
  // Install or switch to Slipgate with Omarchy's own theme commands, from this session process (they need the
  // desktop session; the game service never runs them); done(code, error).
  function runTheme(offer, th, done) {
    if (!offer || !th || themeProc.running) return done(-1, "the slipgate theme is not available")
    var fake = root.themeCommand
    themeProc.command = offer === "set"
      ? (fake ? fake.concat(["set", th.name]) : ["/usr/bin/omarchy-theme-set", th.name])
      : (fake ? fake.concat(["install", th.url]) : ["/usr/bin/omarchy-theme-install", th.url])
    themeProc.done = done
    themeProc.didStart = false
    themeProc.launching = true
    root.freshStreams(themeProc, root.outCap, null)
    themeDeadline.arm()
    themeProc.running = true
  }
  function themeDone(code, error) {
    themeDeadline.stop()
    var f = themeProc.done
    themeProc.done = null
    if (f) f(code, error)
  }
  Process {
    id: themeProc
    environment: root.sessionEnv
    clearEnvironment: true
    property var done: null
    property bool didStart: false
    property bool launching: false
    onStarted: { themeProc.didStart = true; themeProc.launching = false }
    onRunningChanged: if (!running && themeProc.launching && !themeProc.didStart) {   // failed to start
      themeProc.launching = false
      root.themeDone(-1, "could not run " + String(themeProc.command[0]))
    }
    onExited: function(exitCode) {
      var capped = themeProc.stdout.hit || themeProc.stderr.hit
      root.themeDone(themeDeadline.hit || capped ? -1 : exitCode,
                     themeDeadline.hit ? "the theme change did not finish in time"
                     : (capped ? "theme output too large" : String(themeProc.stderr.text || "")))
    }
  }
  Deadline { id: themeDeadline; proc: themeProc; interval: root.testLimit || 190000 }
  // From the full menu: install (or switch to) Slipgate now, then select its live background.
  function applyTheme() {
    var ob = root.settings && root.settings.onboarding, offer = root.themeOffer(ob)
    if (!offer) return
    if (root.switching || themeProc.running) return root.say("still applying the previous change", false)
    root.switching = true
    root.say(offer === "install" ? "installing slipgate" : "switching to slipgate", false)
    root.runTheme(offer, ob.theme, function(code, error) {
      root.switching = false
      if (code !== 0) return root.say(String(error || "could not switch to slipgate").trim(), true)
      root.liveChoice("theme", "use-live-wallpaper", "turning on the live background", function(w) {
        return w.active ? ["slipgate is on, with live quake", false] : ["slipgate is on; pick live quake in the background switcher", true]
      })
    })
  }

  // First run: once the real shell has loaded the plugin, read settings until the controller answers: every 10 s for a
  // minute, then once a minute, since the plugin can load long before the service first starts (the README's install
  // adds the plugin first). A fresh install whose guide nobody has seen yet opens it, once, in the window; any open
  // that shows the guide marks it seen (`onboarding opened`, in applySettings). Never on upgrades.
  property bool autoOpen: !!root.shell && root.hosted   // a harness sets it to test this; tests never get it otherwise
  property int firstRunDelay: 10000      // harness only: the quick retry delay, in ms (the slow one is 6 times it)
  property int firstRunTries: -1         // -1: not started
  onAutoOpenChanged: root.firstRun()
  Component.onCompleted: root.firstRun()
  function firstRun() {
    if (!root.autoOpen || root.firstRunTries >= 0) return
    root.firstRunTries = 0
    root.firstRunCheck()
  }
  function firstRunCheck() {
    root.firstRunTries += 1
    root.run(["settings"], function(code, out) {
      var s = null
      try { s = code === 0 ? JSON.parse(out) : null } catch (e) { s = null }
      if (!s || typeof s !== "object") {
        firstRunTimer.interval = root.firstRunDelay * (root.firstRunTries < 6 ? 1 : 6)
        return firstRunTimer.restart()
      }
      var ob = s.onboarding
      if (ob && ob.show === true && ob.fresh === true && ob.opened === false && !root.opened)
        root.open(JSON.stringify({ origin: "onboarding" }))
    })
  }
  Timer { id: firstRunTimer; onTriggered: root.firstRunCheck() }

  function selectable(i) {
    var k = root.rows[i] ? root.rows[i].kind : ""
    return k === "toggle" || k === "choice" || k === "slider" || k === "action" || k === "info" || k === "off"
  }
  function clampCursor() {
    if (root.cursor >= 0 && root.cursor < root.rows.length && root.selectable(root.cursor)) return
    for (var i = 0; i < root.rows.length; i++) if (root.selectable(i)) { root.cursor = i; return }
    root.cursor = -1
  }
  function move(dir) {
    var n = root.rows.length
    if (n === 0) return
    var i = root.cursor
    for (var step = 0; step < n; step++) {
      i = (i + dir + n) % n
      if (root.selectable(i)) { root.cursor = i; root.cue("move"); return }
    }
  }

  function adjust(row, dir) {
    if (!row || !root.settings) return
    if (row.kind === "toggle" && row.map) {
      root.toggleMap(row)
    } else if (row.kind === "toggle" && !row.key) {     // the guide's inert screensaver choice
      root.wizScreensaver = !root.wizScreensaver
      root.wizSave()
    } else if (row.kind === "toggle") {
      root.setValue(row.key, !root.getv(root.settings, row.key), false)
    } else if (row.kind === "choice") {
      var cur = root.getv(root.settings, row.key)
      var idx = 0
      for (var i = 0; i < row.options.length; i++) if (row.options[i][0] === cur) idx = i
      idx = (idx + dir + row.options.length) % row.options.length
      if (row.key === "game.mode") root.changeMode(row.options[idx])
      else root.setValue(row.key, row.options[idx][0], false)
    } else if (row.kind === "slider") {
      var v = Number(root.getv(root.settings, row.key))
      if (!isFinite(v)) v = row.min
      v = Math.max(row.min, Math.min(row.max, v + dir * row.step))
      root.setValue(row.key, Number(v.toFixed(row.digits)), true)
    } else {
      return
    }
    root.cue("adjust")
  }

  function sliderPick(row, fraction) {
    var v = row.min + fraction * (row.max - row.min)
    v = Math.round(v / row.step) * row.step
    root.setValue(row.key, Number(Math.max(row.min, Math.min(row.max, v)).toFixed(row.digits)), true)
  }

  function activate(row) {
    if (!row) return
    if (row.kind === "toggle" || row.kind === "choice") return root.adjust(row, 1)
    if (row.kind !== "action") return
    root.cue("enter")
    if (row.id === "store") root.openStore()
    else if (row.id === "reroute") {
      root.say("rerouting the bot", false)
      root.run(["hint-bot"], function(code, out, err) {
        root.say(code === 0 ? "bot rerouted" : (err || out || "could not reroute the bot").trim(), code !== 0)
      })
    }
    else if (row.id === "retryrenderer") root.retryRenderer()
    else if (row.id === "game") root.useGame(row.pkg, row.label)
    else if (row.id === "scan") root.scanSteam()
    else if (row.id === "uselive")
      root.liveChoice(row.id, "use-live-wallpaper", "adding live quake to this theme", function(w) {
        return w.active ? ["live background on", false] : ["selected; waiting for the background to switch", true]
      })
    else if (row.id === "removelive")
      root.liveChoice(row.id, "remove-live-wallpaper", "removing live quake from this theme", function(w) {
        return !w.can_remove ? ["removed from this theme", false] : ["still listed in this theme", true]
      })
    else if (row.id === "theme") root.applyTheme()
    else if (row.id === "maps") { root.page = "maps"; root.cursorTo(function(r) { return r.id === "mapGroup" }) }
    else if (row.id === "mapsBack") root.leaveMaps()
    else if (row.id === "mapGroup") {
      var open = JSON.parse(JSON.stringify(root.mapGroupsOpen))
      open[row.group] = !open[row.group]
      root.mapGroupsOpen = open
      root.cursorTo(function(r) { return r.id === "mapGroup" && r.group === row.group })
    }
    else if (row.id === "wizBg") { root.wizBackground = row.choice; root.wizSave() }
    else if (row.id === "wizNext") root.wizGo(1)
    else if (row.id === "wizBack") root.wizGo(-1)
    else if (row.id === "wizSkip") root.wizSkip()
  }

  // A map row edits one list: DM maps are on unless in game.dm_disabled_maps; single-player maps are off unless in
  // game.dm_enabled_sp_maps. The backend refuses a change that would leave no map; the reply then restores the row.
  // The Deathmatch maps page: one header per group (Enter opens or closes it) with how many of its maps are on.
  function mapGroups() {
    return [
    { id: "id", label: "id deathmatch", sp: false, prefix: /^dm[0-9]+$/, help: "id Software's deathmatch arenas, from your own Quake data." },
    { id: "lq", label: "LibreQuake arenas", sp: false, prefix: /^lqdm[0-9]+$/, help: "Free deathmatch maps from LibreQuake, installed with Live Quake." },
    { id: "e1", label: "Episode 1 maps", sp: true, prefix: /^e1m/, help: "Single-player maps as arenas, without monsters and with closed exits." },
    { id: "e2", label: "Episode 2 maps", sp: true, prefix: /^e2m/, help: "Single-player maps as arenas, without monsters and with closed exits." },
    { id: "e3", label: "Episode 3 maps", sp: true, prefix: /^e3m/, help: "Single-player maps as arenas, without monsters and with closed exits." },
    { id: "e4", label: "Episode 4 maps", sp: true, prefix: /^e4m/, help: "Single-player maps as arenas, without monsters and with closed exits." }]
  }
  function mapIsOn(g, name, sp) {
    var list = g[sp ? "dm_enabled_sp_maps" : "dm_disabled_maps"]
    var listed = Array.isArray(list) && list.indexOf(name) !== -1
    return sp ? listed : !listed
  }
  function mapCounts(g) {          // [maps on, maps offered] across every group
    var on = 0, all = 0
    var dmMaps = Array.isArray(g.dm_available_maps) ? g.dm_available_maps : []
    var spMaps = Array.isArray(g.dm_available_sp_maps) ? g.dm_available_sp_maps : []
    for (var i = 0; i < dmMaps.length; i++) { all++; if (root.mapIsOn(g, dmMaps[i], false)) on++ }
    for (var j = 0; j < spMaps.length; j++) { all++; if (root.mapIsOn(g, spMaps[j], true)) on++ }
    return [on, all]
  }
  function mapRows(r, s) {
    var g = s.game || ({})
    r.push({ kind: "section", label: "Deathmatch maps" })
    r.push({ kind: "action", id: "mapsBack", label: "Back", value: "Esc",
             help: "Back to the settings. Map changes apply from the next round." })
    var groups = root.mapGroups()
    for (var i = 0; i < groups.length; i++) {
      var group = groups[i]
      var source = g[group.sp ? "dm_available_sp_maps" : "dm_available_maps"]
      var members = (Array.isArray(source) ? source : []).filter(function(name) { return group.prefix.test(String(name)) })
      if (!members.length) continue
      var on = members.filter(function(name) { return root.mapIsOn(g, name, group.sp) }).length
      var open = root.mapGroupsOpen[group.id] === true
      r.push({ kind: "action", id: "mapGroup", group: group.id, label: (open ? "- " : "+ ") + group.label,
               value: on + " of " + members.length + " on", help: group.help + (open ? " Enter closes the list." : " Enter lists them.") })
      if (open)
        for (var k = 0; k < members.length; k++)
          r.push({ kind: "toggle", map: String(members[k]), sp: group.sp, label: "   " + members[k],
                   on: root.mapIsOn(g, members[k], group.sp),
                   help: group.sp ? "On: this map joins the rotation, with no monsters and its exits closed."
                                  : "On: this map is in the random rotation. One map always stays on." })
    }
  }
  function leaveMaps() {
    root.page = ""
    root.cursorTo(function(r) { return r.id === "maps" })
  }
  function toggleMap(row) {
    var g = root.settings && root.settings.game || ({})
    var name = row.sp ? "dm_enabled_sp_maps" : "dm_disabled_maps"
    var list = (Array.isArray(g[name]) ? g[name] : []).filter(function(m) { return m !== row.map })
    if (row.sp ? !row.on : row.on) list.push(row.map)
    root.setValue("game." + name, list.sort(), false)
  }
  // A mode change restarts the game like a package switch: one at a time, with progress and the outcome shown.
  function changeMode(option) {
    var name = String(option[1]).toLowerCase()
    root.switchJob(["set", "game.mode", JSON.stringify(option[0])], "switching to " + name, "could not switch to " + name, function(s) {
      var g = s.game || ({})
      if (g.effective_mode === option[0]) return ["playing " + name, false]
      return ["saved; " + String(g.mode_reason || "the game stays in its current mode"), true]
    })
  }
  // Select a catalogue package (contract v3): one synchronous switch with rollback in the backend.
  function useGame(pkg, title) {
    if (!pkg) return
    var name = String(title || pkg).toLowerCase()
    root.switchJob(["use-game", pkg], "loading " + name, "could not start " + name, function(s) {
      if (!root.cursorTo(function(r) { return r.id === "game" && r.pkg === pkg })) root.wizCursor()   // the guide's offer goes
      var ok = s.game && s.game.selected_package === pkg
      return ok ? ["playing " + name, false] : ["selected " + name + "; settings still show another game", true]
    })
  }
  // Re-arm OpenGL start attempts (additive contract). OK only means the retry was scheduled, so follow
  // runtime.renderer.state every 500 ms, for at most 30 s, until it settles.
  property int rendererPolls: 0
  function retryRenderer() {
    if (root.switching) return root.say("still applying the previous change", false)
    root.say("starting opengl", false)
    root.run(["retry-renderer"], function(code, out, err) {
      if (code !== 0) return root.say((err || out || "could not retry opengl").trim(), true)
      root.rendererPolls = 0
      rendererPoll.restart()
    })
  }
  Timer {
    id: rendererPoll
    interval: 500
    onTriggered: root.refresh(function() {
      var rr = root.settings && root.settings.runtime && root.settings.runtime.renderer || ({})
      if (rr.state === "starting" && root.opened && ++root.rendererPolls < 60) return rendererPoll.restart()
      root.cursorTo(function(r) { return r.id === "retryrenderer" || r.id === "renderer" })
      if (rr.state === "ready") return root.say("opengl is running", false)
      if (rr.state === "starting") return root.say("opengl is still starting", false)
      if (rr.state === "idle") return root.say("opengl is off; nothing to show right now", false)
      root.say("opengl stopped again; see the daemon log", true)
    })
  }
  // Start one background scan (contract v3) and re-read settings every 500 ms while it runs, for at most 90 s.
  property int scanPolls: 0
  function scanSteam() {
    if (root.switching) return root.say("still applying the previous change", false)
    root.say("looking for steam games", false)
    root.run(["scan-steam"], function(code, out, err) {
      // A scan already running (for example, started before the menu was reopened): follow it instead.
      if (code !== 0 && !/still running/.test(String(err || "")))
        return root.say((err || "the steam scan did not start").trim(), true)
      root.scanPolls = 0
      scanPoll.restart()
    })
  }
  Timer {
    id: scanPoll
    interval: 500
    onTriggered: root.refresh(function() {
      var g = root.settings && root.settings.game || ({})
      if (g.scanning && root.opened && ++root.scanPolls < 180) return scanPoll.restart()
      if (g.scanning) return root.say("the steam scan is still running", false)
      if (g.scan_error) return root.say(String(g.scan_error).replace(/\.$/, "").toLowerCase(), true)
      var cat = Array.isArray(g.catalog) ? g.catalog : []
      var n = cat.filter(function(p) { return p && p.source === "steam" && p.playable }).length
      // The backend's path-free summary ("Steam was not found.") when it is short (44 characters at most).
      var sum = typeof g.summary === "string" ? g.summary.replace(/\.$/, "").toLowerCase() : ""
      root.say(sum && sum.length <= 44 ? sum
               : n ? "found " + n + " playable steam package" + (n > 1 ? "s" : "") : "no playable quake found in steam", false)
    })
  }

  function cursorTo(pred) {
    for (var i = 0; i < root.rows.length; i++) if (root.selectable(i) && pred(root.rows[i])) { root.cursor = i; return true }
    return false
  }

  // A synchronous backend switch (ported from Live Doom): one at a time, after queued edits, then a fresh settings
  // read; `done(settings)` returns [status, isError] once the rows have been rebuilt.
  property bool switching: false
  function switchJob(args, working, failure, done) {
    if (root.switching) return root.say("still applying the previous change", false)
    root.switching = true
    root.say(working, false)
    root.flushPending()
    root.run(args, function(code, out, err) {
      if (code !== 0) {
        root.switching = false
        return root.say((err || failure).trim(), true)
      }
      root.refresh(function() {
        var o = done(root.settings || ({}))
        root.say(o[0], o[1])
      })
      root.settled()
    })
  }
  // Ends a switch only once the settings read queued after it has landed (or failed): a press before that would act
  // on the old rows, and start the same switch again.
  function settled() { root.afterSettings(function() { root.switching = false }) }
  // Add or remove this theme's live background; the rows come and go with the state, so the cursor stays on the
  // action or falls back to the section's "Show on" row.
  function liveChoice(id, verb, working, outcome) {
    var w0 = root.settings && root.settings.wallpaper || ({})
    root.switchJob([verb], working, w0.remove_reason || "could not change the live background", function(s) {
      var w = s.wallpaper || ({})
      if (!root.cursorTo(function(r) { return r.id === id }))
        root.cursorTo(function(r) { return r.key === "wallpaper.mode" || r.id === "showon" })
      return outcome(w)
    })
  }

  // Quake's store page, only on an explicit click/Enter. The overlay closes first so the browser
  // gets the keyboard (a window stays, under it); only Steam store links are opened.
  function openStore() {
    if (!root.windowed) root.dismiss()
    if (!root.hosted) root.openedUrl = root.storeUrl
    else Qt.openUrlExternally(root.storeUrl)
  }

  function modeText(s) {
    var rt = s && s.runtime || ({})
    var words = rt.paused ? "paused" : ({ bot: "bot playing", human: "you are playing", screensaver: "screensaver" })[rt.owner] || ""
    if (rt.loaded === false) words = "not running"
    var map = rt.map ? String(rt.map) : ""
    return [words, map].filter(function(x) { return x }).join("  ·  ")
  }

  // Every process the menu starts gets this closed session environment: a fixed PATH and only
  // the variables the session needs.
  readonly property var sessionEnv: {
    var keep = ["HOME", "USER", "LOGNAME", "LANG", "XDG_RUNTIME_DIR", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME",
                "XDG_CACHE_HOME", "WAYLAND_DISPLAY", "HYPRLAND_INSTANCE_SIGNATURE", "DBUS_SESSION_BUS_ADDRESS"]
    var e = { "PATH": "/usr/bin:/bin", "OMARCHY_PATH": "/usr/share/omarchy", "GIT_TERMINAL_PROMPT": "0" }
    for (var i = 0; i < keep.length; i++) { var v = Quickshell.env(keep[i]); if (v) e[keep[i]] = String(v) }
    return e
  }

  // ------------------------------------------------------------------ view
  // The host only changes while the menu is closed (open() picks it), so one card is ever live.
  Loader {
    id: windowLoader
    active: root.hosted
    property string host: ""
    function use(h) {
      if (!root.hosted || windowLoader.host === h) return
      windowLoader.host = h
      windowLoader.setSource(h === "window" ? "MenuFloat.qml" : "MenuWindow.qml", { root: root })
    }
    Component.onCompleted: windowLoader.use("overlay")
  }
}
