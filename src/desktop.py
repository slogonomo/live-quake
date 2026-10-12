# SPDX-License-Identifier: 0BSD
"""Read-only desktop observations, polled away from controller requests.

No host queries were run while developing this adapter. Its schemas follow
Live Doom's observer and Hyprland's JSON batch API. Tests provide a callable
runner and sanitized fixtures.
The adapter never changes a desktop setting or dispatches an input action.
"""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import selectors
import signal
import subprocess
import threading
import time
from typing import Mapping

from policy import Output, Snapshot

COMMAND_CLEANUP_ALLOWANCE = .4

BATCH = ('/usr/bin/hyprctl', '-j', '--batch', 'monitors; clients')
BATCH_LIMIT = 1310724
LOCK = ('/usr/bin/omarchy-shell', 'lock', 'isLocked')
LOCK_FALLBACK_INTERVAL = 5.0
_SESSION_KEYS = frozenset({'HOME', 'XDG_RUNTIME_DIR', 'WAYLAND_DISPLAY',
    'HYPRLAND_INSTANCE_SIGNATURE', 'DBUS_SESSION_BUS_ADDRESS', 'XDG_CONFIG_HOME',
    'XDG_DATA_HOME', 'XDG_CACHE_HOME', 'XDG_STATE_HOME', 'XDG_CURRENT_DESKTOP',
    'XDG_SESSION_TYPE'})


def session_environment(values: Mapping[str, str]) -> dict[str, str]:
    """Keep session addresses, never command/interpreter/loader overrides."""
    result = {key: value for key, value in values.items()
              if key in _SESSION_KEYS and isinstance(value, str) and '\0' not in value
              and len(value) <= 4096}
    result.update(PATH='/usr/bin:/bin', LANG='C.UTF-8',
                  PYTHONDONTWRITEBYTECODE='1', OMARCHY_PATH='/usr/share/omarchy')
    return result


def bounded_command(command, *, environment, timeout, limit, stop):
    """Bound one transient command and its ancestry-owned descendants.

waitid(WNOWAIT) retains the leader's PID until group cleanup, even if a
grandchild closes its pipes or outlives the leader. The internal subreaper
cleans descendants that create new sessions/process groups. No threaded
preexec_fn is used, and each helper has its own deadline and parent-death guard.
Cleanup adds at most COMMAND_CLEANUP_ALLOWANCE to the caller's deadline.
"""
    if (not isinstance(command, (list, tuple)) or not command
            or not all(isinstance(part, str) and '\0' not in part for part in command)
            or not os.path.isabs(command[0])):
        raise ValueError('Desktop observer command must have an absolute executable')
    if (type(timeout) not in (float, int) or not math.isfinite(timeout)
            or timeout <= 0 or type(limit) is not int or limit <= 0):
        raise ValueError('Invalid desktop observer process bounds')
    if stop.is_set():
        raise RuntimeError('Desktop observer stopped')
    deadline = time.monotonic() + timeout
    argv = ['/usr/bin/prlimit', '--core=1:1', '--', '/usr/bin/python3', '-E', '-s', '-B',
            str(Path(__file__).with_name('process_helper.py').resolve()),
            '--parent', str(os.getpid()), '--deadline', repr(deadline), '--', *command]
    child = subprocess.Popen(argv, env=dict(environment), stdin=subprocess.DEVNULL,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             start_new_session=True, close_fds=True)
    output, error = bytearray(), bytearray()
    try:
        with selectors.DefaultSelector() as selector:
            for stream, buffer in ((child.stdout, output), (child.stderr, error)):
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, selectors.EVENT_READ, buffer)
            while True:
                if stop.is_set():
                    raise RuntimeError('Desktop observer stopped')
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError('Desktop observer command timed out')
                exited = os.waitid(os.P_PID, child.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
                if exited is not None and not selector.get_map():
                    break
                for key, _ in selector.select(min(.05, remaining)):
                    try:
                        block = os.read(key.fileobj.fileno(), 65536)
                    except BlockingIOError:
                        continue
                    if not block:
                        selector.unregister(key.fileobj)
                    elif len(output) + len(error) + len(block) > limit:
                        raise ValueError('Desktop observer output exceeded its byte limit')
                    else:
                        key.data.extend(block)
    finally:
        # Allow the subreaper to clean escaped descendants before stopping
        # its exact owned group. Its signal handler preserves cleanup work.
        try:
            os.killpg(child.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            child.wait(timeout=COMMAND_CLEANUP_ALLOWANCE - .05)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                child.wait(timeout=.05)
            except subprocess.TimeoutExpired:
                pass  # Never wait indefinitely for an unkillable kernel task.
        child.stdout.close()
        child.stderr.close()
    if child.returncode == 124:
        raise TimeoutError('Desktop observer command timed out')
    if child.returncode != 0:
        # Deliberately omit tool stderr: it can include private desktop data.
        raise RuntimeError('Desktop observer command failed')
    return bytes(output)


def _integer(value, label, minimum=-2147483648, maximum=2147483647):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError('Invalid desktop ' + label)
    return value


def _boolean(value, label):
    if type(value) is not bool:
        raise ValueError('Invalid desktop ' + label)
    return value


def _refresh_millihz(value):
    # Hyprland's refreshRate is the advertised mode in Hz, not callback cadence.
    # Optional diagnostic metadata must not invalidate desktop policy. Discard
    # absent/malformed values, including booleans and nonfinite JSON numbers.
    if type(value) not in (int, float) or not 0 < value <= 1000:
        return None
    rate = round(value * 1000)
    return rate if 1 <= rate <= 1000000 else None


def parse_batch(raw):
    """Read exactly the two JSON arrays in Hyprland's fixed batch response.

Hyprland efb5099 dispatchBatch joins each inline getReply with three newlines;
hyprctl -j applies the JSON flag to each batch command. No second compositor
request can run between this response's monitor and client observations.
Only bounded input is parsed; callers retain the projected Snapshot alone.
"""
    if not isinstance(raw, bytes) or len(raw) > BATCH_LIMIT:
        raise ValueError('Invalid desktop observer batch')
    try:
        text = raw.decode('utf-8', errors='surrogatepass')
        decoder = json.JSONDecoder()
        start = len(text) - len(text.lstrip(' \t\r\n'))
        monitors, middle = decoder.raw_decode(text, start)
        if not text.startswith('\n\n\n', middle):
            raise ValueError()
        client_start = middle + 3
        while client_start < len(text) and text[client_start] in ' \t\r\n':
            client_start += 1
        clients, end = decoder.raw_decode(text, client_start)
        if (text[end:].strip(' \t\r\n')
                or not isinstance(monitors, list) or len(monitors) > 128
                or not isinstance(clients, list) or len(clients) > 4096
                or len(text[start:middle].encode('utf-8', errors='surrogatepass')) > 256 * 1024
                or len(text[middle + 3:end].encode('utf-8', errors='surrogatepass')) > 1024 * 1024):
            raise ValueError()
        return monitors, clients
    except (ValueError, RecursionError):
        # JSON errors can include private strings from the compositor reply.
        raise ValueError('Invalid desktop observer batch') from None


def parse_snapshot(monitors, clients, locked) -> Snapshot:
    """Project policy and nominal refresh; discard titles and identity payloads."""
    if (not isinstance(monitors, list) or len(monitors) > 128
            or not isinstance(clients, list) or len(clients) > 4096):
        raise ValueError('Invalid desktop observation arrays')
    _boolean(locked, 'lock state')
    occupied = set()
    pinned = False
    for client in clients:
        if not isinstance(client, dict):
            raise ValueError('Invalid desktop client')
        mapped = _boolean(client.get('mapped'), 'client mapped state')
        hidden = _boolean(client.get('hidden'), 'client hidden state')
        if not mapped or hidden:
            continue
        workspace = client.get('workspace')
        if not isinstance(workspace, dict):
            raise ValueError('Invalid desktop client workspace')
        occupied.add(_integer(workspace.get('id'), 'client workspace', -2**63, 2**63 - 1))
        # Pinned windows are visible across workspaces. Without a reliable
        # per-output location, conservatively block all outputs.
        pinned |= _boolean(client.get('pinned', False), 'client pinned state')
    outputs, names = [], set()
    for monitor in monitors:
        if not isinstance(monitor, dict):
            raise ValueError('Invalid desktop monitor')
        if _boolean(monitor.get('disabled', False), 'monitor disabled state'):
            continue
        name = monitor.get('name')
        if (not isinstance(name, str) or not name or len(name) > 128
                or any(ord(char) < 33 or ord(char) > 126 for char in name) or name in names):
            raise ValueError('Invalid desktop output name')
        names.add(name)
        workspace = monitor.get('activeWorkspace')
        special = monitor.get('specialWorkspace', {'id': 0})
        if not isinstance(workspace, dict) or not isinstance(special, dict):
            raise ValueError('Invalid desktop monitor workspace')
        active = _integer(workspace.get('id'), 'active workspace', -2**63, 2**63 - 1)
        visible_special = _integer(special.get('id'), 'special workspace', maximum=0)
        # Named workspaces use negative int64 IDs below the special range.
        # Invalid/transient or mirrored workspaces only block their output.
        unknown_workspace = (-99 <= active <= 0 or visible_special < -99
                             or visible_special == -1 or monitor.get('mirrorOf', 'none') != 'none')
        focused = _boolean(monitor.get('focused'), 'monitor focus')
        awake = _boolean(monitor.get('dpmsStatus'), 'monitor DPMS state')
        width = _integer(monitor.get('width'), 'monitor width', 1, 32768)
        height = _integer(monitor.get('height'), 'monitor height', 1, 32768)
        transform = _integer(monitor.get('transform', 0), 'monitor transform', 0, 7)
        scale = monitor.get('scale', 1)
        if type(scale) not in (int, float) or not math.isfinite(scale) or not .25 <= scale <= 8:
            raise ValueError('Invalid desktop monitor scale')
        if transform in (1, 3, 5, 7):
            width, height = height, width
        width, height = max(1, round(width / scale)), max(1, round(height / scale))
        busy = (unknown_workspace or pinned or active in occupied
                or bool(visible_special and visible_special in occupied))
        fields = dict(name=name, workspace=active, focused=focused, busy=busy,
                      awake=awake, width=width, height=height,
                      refresh_millihz=_refresh_millihz(monitor.get('refreshRate')))
        if 'special_workspace' in Output.__dataclass_fields__:
            fields['special_workspace'] = visible_special
        outputs.append(Output(**fields))
    if sum(output.focused for output in outputs) > 1:
        raise ValueError('Invalid desktop focus count')
    return Snapshot(tuple(outputs), locked, bool(outputs), 'unknown')


def derived_lock(monitors):
    """Use monitor lock blockers; absent or unreadable metadata is unknown.

LOCK wins across outputs. Without it, a present null or a blocker list past
WORKSPACE determines unlocked; missing and all-WORKSPACE observations do not.
"""
    if not isinstance(monitors, list) or len(monitors) > 128:
        return None
    readable = False
    for monitor in monitors:
        if not isinstance(monitor, dict) or 'solitaryBlockedBy' not in monitor:
            continue
        blockers = monitor['solitaryBlockedBy']
        if blockers is None:
            readable = True
            continue
        if (not isinstance(blockers, list) or len(blockers) > 128
                or any(not isinstance(value, str) or not value or len(value) > 128
                       for value in blockers)):
            continue
        if 'LOCK' in blockers:
            return True
        readable |= 'WORKSPACE' not in blockers
    return False if readable else None


class DesktopObserver:
    """Callable cached Snapshot with explicit start/close lifecycle.

runner(command, *, environment, timeout, limit, stop) -> bytes is an in-process
test injection only. A runner must obey the deadline and cancellation event.
The production runner and executable paths cannot be changed by environment.
Staleness measures time since publication of a complete batch. It exceeds the
desktop batch and lock query plus polling/cleanup bounds; data within a
published batch can be up to that batch's duration older than publication.
An undetermined monitor lock uses a rate-limited shell fallback. That value
expires from its own command start; fresh batches cannot renew it.
"""
    def __init__(self, environment, *, runner=None, poll_interval=.25,
                 stale_after=3.5, command_timeout=.6, clock=time.monotonic):
        for value in (poll_interval, stale_after, command_timeout):
            if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
                raise ValueError('Invalid desktop observer timing')
        cycle_bound = 2 * (command_timeout + COMMAND_CLEANUP_ALLOWANCE) + poll_interval
        if stale_after <= cycle_bound:
            raise ValueError('Desktop observer stale expiry must exceed its bounded polling cycle')
        self.environment = session_environment(environment)
        self._runner = bounded_command if runner is None else runner
        if not callable(self._runner) or not callable(clock):
            raise ValueError('Desktop observer runner and clock must be callable')
        self._interval, self._stale, self._timeout = poll_interval, stale_after, command_timeout
        self._clock = clock
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._thread = None
        self._snapshot = Snapshot(saver_owner='unknown')
        self._observed_at = None
        self._snapshot_lock_started_at = self._observation_lock_started_at = None
        self._fallback_locked = self._fallback_started_at = None
        self._fallback_next_at = 0.0
        self._error = 'Desktop observer has not started'
        self._closed = False

    def start(self):
        with self._lock:
            if self._closed:
                raise RuntimeError('Desktop observer is closed')
            if self._thread is None:
                self._thread = threading.Thread(target=self._work, name='quake-desktop-observer', daemon=True)
                self._thread.start()
        return self

    def _fetch(self, command, limit):
        raw = self._runner(command, environment=dict(self.environment), timeout=self._timeout,
                           limit=limit, stop=self._stop)
        if not isinstance(raw, bytes) or len(raw) > limit:
            raise ValueError('Invalid desktop observer runner output')
        return raw

    def _observe(self):
        # Keep raw private payloads out of the long-lived worker's locals,
        # including after a later lock failure. Only projected policy escapes.
        monitors, clients = parse_batch(self._fetch(BATCH, BATCH_LIMIT))
        self._observation_lock_started_at = None
        locked = derived_lock(monitors)
        if locked is not None:
            # Do not resurrect a pre-lock fallback after a determinate batch.
            # The attempt cooldown remains in force through rapid transitions.
            self._fallback_locked = self._fallback_started_at = None
        else:
            began = self._clock()
            if began >= self._fallback_next_at:
                self._fallback_next_at = began + LOCK_FALLBACK_INTERVAL
                self._fallback_locked = self._fallback_started_at = None
                lock = self._fetch(LOCK, 16384).strip()
                if lock not in (b'true', b'false'):
                    raise ValueError('Unknown desktop lock state')
                self._fallback_locked, self._fallback_started_at = lock == b'true', began
            if (self._fallback_started_at is None
                    or not 0 <= self._clock() - self._fallback_started_at <= self._stale):
                raise ValueError('Unknown desktop lock state')
            locked = self._fallback_locked
            self._observation_lock_started_at = self._fallback_started_at
        return parse_snapshot(monitors, clients, locked)

    def _work(self):
        while not self._stop.is_set():
            try:
                snapshot = self._observe()
                with self._lock:
                    self._snapshot, self._observed_at, self._error = snapshot, self._clock(), None
                    self._snapshot_lock_started_at = self._observation_lock_started_at
            except Exception:
                # No raw exception, stdout or stderr is stored: tool failures
                # and malformed responses can contain private window data.
                with self._lock:
                    self._snapshot = Snapshot(saver_owner='unknown')
                    self._snapshot_lock_started_at = None
                    self._error = 'Desktop observation unavailable'
            self._stop.wait(self._interval)

    def __call__(self):
        with self._lock:
            if self._closed or self._observed_at is None:
                self.observation_state = 'unavailable'
                return Snapshot(saver_owner='unknown')
            now = self._clock()
            if (now - self._observed_at > self._stale
                    or (self._snapshot_lock_started_at is not None
                        and now - self._snapshot_lock_started_at > self._stale)):
                self.observation_state = 'stale'
                return Snapshot(saver_owner='unknown')
            self.observation_state = 'available' if self._snapshot.available else 'unavailable'
            return self._snapshot

    def diagnostics(self):
        with self._lock:
            now = self._clock()
            age = None if self._observed_at is None else max(0, now - self._observed_at)
            lock_stale = (self._snapshot_lock_started_at is not None
                          and now - self._snapshot_lock_started_at > self._stale)
            return {'running': bool(self._thread and self._thread.is_alive() and not self._closed),
                    'age_seconds': age, 'stale': age is None or age > self._stale or lock_stale,
                    'error': self._error}

    def close(self):
        with self._lock:
            self._closed = True
            self._snapshot = Snapshot(saver_owner='unknown')
            thread = self._thread
        self._stop.set()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=1.5)
            if thread.is_alive():
                with self._lock:
                    self._error = 'Desktop observer runner did not stop'
