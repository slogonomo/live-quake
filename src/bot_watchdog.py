# SPDX-License-Identifier: 0BSD
"""Pure bot-stall policy using native simulation milliseconds and milliunits.

No clocks, commands, processes or files are used here. The controller supplies
eligibility and executes returned requests under its ownership/pause guards.
Only acted() and hint() count acknowledged actions. Failed actions use cancel()
to begin a fresh observation window without incrementing or escalating.
"""
from __future__ import annotations

from dataclasses import dataclass
import re

_I32_MIN, _I32_MAX = -(2 ** 31), 2 ** 31 - 1
_U32_MAX = 2 ** 32 - 1
_MAX_COUNT = 2 ** 53 - 1  # Exact integer representation in the QML/JSON consumer.
_MAP = re.compile(r'[A-Za-z0-9_]{1,15}\Z', re.ASCII)

# Release tuning: active bot play, in seconds, on the current map.
BOT_STUCK_SECONDS = 180
BOT_MAP_SECONDS = 900
BOT_REPLAN_SECONDS = 60
BOT_RECOVERY_COOLDOWN_SECONDS = 10
BOT_CELL_XY_UNITS = 128
BOT_CELL_Z_UNITS = 256
BOT_CELL_CAP = 8192
_CONFIRM_HORIZONTAL_DISTANCE_SQUARED = (48 * 1000) ** 2


@dataclass(frozen=True)
class _Sample:
    pid: int
    map: str
    leveltime: int
    x: int
    y: int
    kills: int
    health: int
    deaths: int
    bot: int
    paused: int
    gamestate: int

    @property
    def context(self):
        return self.pid, self.map

    @property
    def live_bot(self):
        return self.bot == 1 and self.health > 0 and self.gamestate == 0


def _sample(frame):
    if not isinstance(frame, dict):
        return None
    name = frame.get('map')
    if not isinstance(name, str) or not _MAP.fullmatch(name):
        return None
    bounds = {'pid': (1, _U32_MAX), 'leveltime': (0, _I32_MAX),
              'x': (_I32_MIN, _I32_MAX), 'y': (_I32_MIN, _I32_MAX),
              'kills': (0, _I32_MAX), 'health': (_I32_MIN, _I32_MAX),
              'deaths': (0, _U32_MAX), 'bot': (0, 1), 'paused': (0, 1),
              'gamestate': (0, _U32_MAX)}
    values = {}
    for key, (low, high) in bounds.items():
        value = frame.get(key)
        if type(value) is not int or not low <= value <= high:
            return None
        values[key] = value
    return _Sample(map=name, **values)


class BotWatchdog:
    def __init__(self, stall_seconds=60, progress_units=48):
        if type(stall_seconds) is not int or not 1 <= stall_seconds <= 3600:
            raise ValueError('invalid watchdog interval')
        if type(progress_units) is not int or not 1 <= progress_units <= 4096:
            raise ValueError('invalid watchdog progress threshold')
        self._stall_ms = stall_seconds * 1000
        self._distance_squared = (progress_units * 1000) ** 2
        self._progress_units = progress_units
        self._counts = {'replans': 0, 'restarts': 0, 'hints': 0}
        self._last_action = None
        self._baseline = self._last = self._request = None
        self._request_anchor = None
        self._request_progressed = False
        self._pending = None
        self._stage = 0
        self._eligible = False

    def _reset(self, sample=None, eligible=False):
        self._baseline = self._last = sample
        self._request = self._pending = None
        self._request_anchor = None
        self._request_progressed = False
        self._stage = 0
        self._eligible = eligible

    def observe(self, frame, eligible):
        """Request replan, then restart after another full stalled sim window.

Movement means displacement of at least 48 world units from the last progress
anchor, not accumulated jitter or vertical motion. A kill increase is progress.
Invalid/ineligible/dead frames invalidate pending requests and escalation.
"""
        sample = _sample(frame)
        if type(eligible) is not bool or not eligible or sample is None or not sample.live_bot or sample.paused:
            self._reset()
            return None
        if (self._last is None or sample.context != self._last.context
                or sample.leveltime < self._last.leveltime or sample.deaths != self._last.deaths
                or sample.kills < self._last.kills):
            self._reset(sample, True)
            return None
        self._last, self._eligible = sample, True
        anchor = self._baseline
        distance = (sample.x - anchor.x) ** 2 + (sample.y - anchor.y) ** 2
        if distance >= self._distance_squared or sample.kills > anchor.kills:
            if self._pending is not None:
                self._request_progressed = True
            self._baseline = sample
            self._stage = 0
            return None
        if self._pending is None and sample.leveltime - anchor.leveltime >= self._stall_ms:
            self._pending = 'restart' if self._stage else 'replan'
            self._request = sample
            self._request_anchor = anchor
            return self._pending
        return None

    def confirm(self, frame):
        """Revalidate a pending request after the controller pauses the core.

The intentional paused flag is allowed. Context/death/time changes, a dead or
nonbot world, kills, or threshold displacement from the progress baseline
invalidate the request before a destructive recovery command. Invalidation
starts a fresh first-stage window without counting; confirmation preserves
pending/escalation and updates only the latest sample.
"""
        if self._pending is None:
            return False
        sample = _sample(frame)
        anchor = self._request_anchor
        valid = (not self._request_progressed and sample is not None and sample.live_bot
                 and sample.context == self._request.context
                 and sample.deaths == self._request.deaths
                 and sample.leveltime >= self._last.leveltime
                 and sample.kills == anchor.kills
                 and (sample.x - anchor.x) ** 2 + (sample.y - anchor.y) ** 2
                 < self._distance_squared)
        if not valid:
            self.cancel(frame)
            return False
        self._last = sample
        return True

    def acted(self, action, frame):
        """Commit exactly one pending, successfully acknowledged native action.

The returned frame may be deliberately paused. A restart may rewind leveltime;
a replan may not. Changed PID/map/death state invalidates a stale acknowledgement.
Wrong/duplicate actions raise without counting. Invalid frames also reset.
"""
        if not isinstance(action, str) or action not in ('replan', 'restart') or action != self._pending:
            raise ValueError('watchdog action is not pending')
        sample = _sample(frame)
        if sample is None:
            self._reset()
            raise ValueError('invalid watchdog acknowledgement frame')
        if (not sample.live_bot or sample.context != self._request.context
                or sample.deaths != self._request.deaths
                or action == 'replan' and (sample.leveltime < self._last.leveltime
                                          or sample.kills < self._last.kills)):
            self._reset()
            raise ValueError('watchdog acknowledgement context changed')
        key = 'replans' if action == 'replan' else 'restarts'
        self._counts[key] = min(_MAX_COUNT, self._counts[key] + 1)
        self._last_action = action
        self._reset(sample, not sample.paused)
        self._stage = int(action == 'replan')

    def hint(self, frame):
        """Count one successful explicit reroute; start a fresh first-stage window."""
        sample = _sample(frame)
        if sample is None:
            self._reset()
            raise ValueError('invalid watchdog hint frame')
        self._counts['hints'] = min(_MAX_COUNT, self._counts['hints'] + 1)
        self._last_action = 'hint'
        self._reset(sample, sample.live_bot and not sample.paused)

    def cancel(self, frame=None):
        """Failed action/lifecycle reset: no counts; a new 60-second sim window.

A valid supplied frame is the new baseline. Missing/invalid data waits for the
next eligible frame instead. Pending requests and escalation are always cleared.
Executed counters and last_action survive every reset.
"""
        sample = _sample(frame)
        self._reset(sample, bool(sample and sample.live_bot and not sample.paused))

    def reset(self, frame=None):
        self.cancel(frame)

    def settings(self):
        elapsed = (self._last.leveltime - self._baseline.leveltime
                   if self._last is not None and self._baseline is not None else 0)
        return {**self._counts, 'last_action': self._last_action,
                'pending': self._pending, 'eligible': self._eligible,
                'stage': 'after-replan' if self._stage else 'watching',
                'stall_elapsed_ms': max(0, min(_I32_MAX, elapsed)),
                'stall_seconds': self._stall_ms // 1000, 'progress_units': self._progress_units}


@dataclass(frozen=True)
class _BypassSample:
    world: _Sample
    z: int
    owner: int
    seq: int

    @property
    def cell(self):
        # Native positions are signed milliunits. Floor division handles the
        # negative half of the world consistently, including exact boundaries.
        return (self.world.x // (BOT_CELL_XY_UNITS * 1000),
                self.world.y // (BOT_CELL_XY_UNITS * 1000),
                self.z // (BOT_CELL_Z_UNITS * 1000))

    @property
    def active(self):
        return (self.world.bot == 1 and self.owner == 0 and
                self.world.paused == 0 and self.world.gamestate == 0)

    @property
    def live(self):
        return (self.world.live_bot and self.owner == 0)


def _bypass_sample(frame):
    world = _sample(frame)
    if world is None or len(world.map) >= 15:
        return None
    fields = {}
    for key, low, high in (('z', _I32_MIN, _I32_MAX), ('owner', 0, _U32_MAX),
                           ('seq', 0, _U32_MAX)):
        value = frame.get(key)
        if type(value) is not int or not low <= value <= high:
            return None
        fields[key] = value
    if fields['seq'] & 1:
        return None
    return _BypassSample(world, **fields)


class BotLevelBypass:
    """Pure per-map attract-mode policy, independent of the old reroute API.

    The clock sums positive native ``leveltime`` millisecond deltas only between
    two sampled active bot frames on the same PID/map/death counter. An excluded
    sample (human, pause, menu/console/intermission, invalid data or controller
    ineligibility) breaks that chain. A death, PID change or time rewind starts a
    new baseline, preserving the accumulated map clock. Unobserved boundary
    intervals are deliberately discarded, so the clock can undercount but does
    not charge excluded time. Dead bot frames can count simulation time but can
    never request an action; death/respawn grants a fresh alive stuck interval.

    New ground is a previously unseen 128 x 128 x 256 world-unit cell. The set
    survives deaths/restarts and is cleared by a map change or explicit package
    reset. At the fixed cap it stops crediting new cells instead of evicting old
    cells and mistaking revisits for progress. Both clocks/counters are bounded.
    Successful timeout fallback restarts retain the clock and defer another
    skip attempt by a full map interval, preventing repeated restart loops.
    One acknowledged replan is offered after 60 active alive seconds without
    new ground. It preserves both clocks and visited cells, rearming only on
    new ground or a fresh life/map/restart. Failed replans retry no sooner than
    another replan interval, and never delay the stuck restart deadline.
    A kills/48-unit stale-recovery guard cancellation defers another stuck
    recovery attempt by 10 sampled eligible bot-play seconds. It grants no
    ground progress, resets no clock/latch, and cannot defer a timeout skip.
    """

    def __init__(self, stuck_seconds=BOT_STUCK_SECONDS, map_seconds=BOT_MAP_SECONDS,
                 cell_cap=BOT_CELL_CAP, replan_seconds=BOT_REPLAN_SECONDS):
        for value in (stuck_seconds, map_seconds, replan_seconds):
            if type(value) is not int or not 1 <= value <= 86400:
                raise ValueError('invalid bot bypass interval')
        if type(cell_cap) is not int or not 1 <= cell_cap <= BOT_CELL_CAP:
            raise ValueError('invalid bot bypass cell cap')
        self._stuck_ms = stuck_seconds * 1000
        self._map_ms = map_seconds * 1000
        self._replan_ms = replan_seconds * 1000
        self._cell_cap = cell_cap
        self._counts = {'replans': 0, 'restarts': 0, 'skips': 0}
        self._last_action = None
        self.reset()

    def reset(self):
        """Forget the current map/package; acknowledged counters survive."""
        self._map = None
        self._elapsed_ms = self._ground_ms = 0
        self._next_skip_ms = self._map_ms
        self._cells = set()
        self._last = self._request = None
        self._pending = None
        self._last_active = self._confirmed = self._progressed = False
        self._replanned = False
        self._replan_retry_ms = 0
        self._recovery_retry_ms = 0
        self._guard_changed = False

    def _fresh_alive_interval(self):
        self._ground_ms = self._elapsed_ms
        self._replanned = False
        self._replan_retry_ms = 0
        self._recovery_retry_ms = 0

    def _new_map(self, sample):
        self.reset()
        self._map = sample.world.map

    def _ground(self, sample):
        cell = sample.cell
        if cell in self._cells or len(self._cells) >= self._cell_cap:
            return False
        self._cells.add(cell)
        self._fresh_alive_interval()
        return True

    def _unchanged_recovery(self, sample):
        """Cancel stale recovery on combat or substantial pre-pause movement.

        This is the existing watchdog's 48-unit horizontal safety guard. It
        grants no coarse-cell progress and changes neither deadline clock.
        """
        request = self._request.world
        world = sample.world
        return (world.kills == request.kills and
                (world.x - request.x) ** 2 + (world.y - request.y) ** 2
                < _CONFIRM_HORIZONTAL_DISTANCE_SQUARED)

    def cancel(self, frame=None, *, failed=False):
        """Suspend observation without losing per-map time or visited ground.

        Failed timeout attempts wait one stuck interval before retry.
        Failed replans preserve the stuck deadline and retry after a full
        replan interval. Ordinary excluded samples never rearm a replan.
        A cancellation cannot manufacture an acknowledged event.
        """
        if failed and self._pending == 'skip':
            self._next_skip_ms = min(_MAX_COUNT, max(self._next_skip_ms,
                                                     self._elapsed_ms + self._stuck_ms))
        pending = self._pending
        replan = pending == 'replan'
        if failed and replan:
            self._replan_retry_ms = min(_MAX_COUNT, self._elapsed_ms + self._replan_ms)
        self._pending = self._request = None
        self._confirmed = self._progressed = self._guard_changed = self._last_active = False
        if frame is not None or not failed:
            self._last = _bypass_sample(frame)
        if failed and pending in ('restart', 'skip'):
            self._fresh_alive_interval()

    def observe(self, frame, eligible):
        sample = _bypass_sample(frame)
        if sample is None:
            self.cancel()
            return None
        if sample.world.map != self._map:
            self._new_map(sample)
        active = type(eligible) is bool and eligible and sample.active
        previous = self._last
        same_life = bool(previous and sample.world.context == previous.world.context
                         and sample.world.deaths == previous.world.deaths
                         and sample.world.leveltime >= previous.world.leveltime)
        if active and self._last_active and same_life:
            self._elapsed_ms = min(_MAX_COUNT, self._elapsed_ms +
                                   sample.world.leveltime - previous.world.leveltime)
        self._last, self._last_active = sample, active
        if not active:
            self.cancel(frame)
            return None
        if not same_life or not sample.live or previous and previous.world.health <= 0:
            # Death already restarts single-player Quake. Never react to it,
            # nor immediately restart the next life for a previous life's stall.
            self._pending = self._request = None
            self._confirmed = self._progressed = self._guard_changed = False
            self._fresh_alive_interval()
            if sample.live:
                self._ground(sample)
            return None
        if self._ground(sample) and self._pending in ('restart', 'replan'):
            self._progressed = True
        if self._pending is not None:
            if self._pending in ('restart', 'replan') and not self._unchanged_recovery(sample):
                self._progressed = True
                self._guard_changed = True
            return None
        action = ('skip' if self._elapsed_ms >= self._next_skip_ms else
                  'restart' if self._elapsed_ms >= self._recovery_retry_ms
                  and self._elapsed_ms - self._ground_ms >= self._stuck_ms else
                  'replan' if not self._replanned and self._elapsed_ms >= self._replan_retry_ms
                  and self._elapsed_ms >= self._recovery_retry_ms
                  and self._elapsed_ms - self._ground_ms >= self._replan_ms else None)
        if action is not None:
            self._pending, self._request = action, sample
            self._confirmed = self._guard_changed = False
        return action

    def confirm(self, frame):
        """Require a fresh, deliberately paused live bot world before acting."""
        if self._pending is None:
            return False
        sample = _bypass_sample(frame)
        request = self._request
        valid = (sample is not None and sample.live and sample.world.paused == 1
                 and sample.seq != request.seq
                 and sample.world.context == request.world.context
                 and sample.world.deaths == request.world.deaths
                 and sample.world.leveltime >= self._last.world.leveltime)
        if valid and self._pending in ('restart', 'replan'):
            guard_changed = self._guard_changed or not self._unchanged_recovery(sample)
            if guard_changed:
                self._recovery_retry_ms = min(_MAX_COUNT, self._elapsed_ms +
                                              BOT_RECOVERY_COOLDOWN_SECONDS * 1000)
            valid = (not self._progressed and self._unchanged_recovery(sample) and
                     (sample.cell in self._cells or len(self._cells) >= self._cell_cap))
        if not valid:
            self.cancel(frame)
            if (sample is not None and sample.live and
                    sample.world.map == self._map):
                self._ground(sample)
            return False
        self._last, self._last_active, self._confirmed = sample, False, True
        return True

    def acted(self, action, frame, *, outcome=None, destination=None):
        """Commit one confirmed native success; reject stale/wrong acknowledgements.

        A restart response may rewind native life counters. A skip must report
        the exact safe destination, which must also match the resulting frame.
        A replan must retain the same map/life and nondecreasing simulation time;
        it changes neither the no-new-ground anchor nor the accumulated clock.
        """
        if (action not in ('replan', 'restart', 'skip') or action != self._pending
                or not self._confirmed):
            raise ValueError('bot bypass action is not confirmed and pending')
        sample = _bypass_sample(frame)
        request = self._request
        if outcome is None:
            outcome = action
        valid = (sample is not None and sample.live and sample.world.paused == 1
                 and sample.world.pid == request.world.pid and sample.seq != self._last.seq
                 and outcome in ('replan', 'restart', 'skip')
                 and (action == 'skip' and outcome in ('restart', 'skip') or outcome == action))
        if outcome == 'replan':
            valid = (valid and destination is None
                     and sample.world.context == request.world.context
                     and sample.world.deaths == request.world.deaths
                     and sample.world.leveltime >= self._last.world.leveltime
                     and not self._progressed
                     and self._unchanged_recovery(sample)
                     and (sample.cell in self._cells or len(self._cells) >= self._cell_cap))
        elif outcome == 'restart':
            valid = valid and sample.world.map == request.world.map
            if destination is not None:
                valid = valid and destination == request.world.map
        else:
            valid = (valid and isinstance(destination, str) and len(destination) < 15
                     and _MAP.fullmatch(destination) is not None
                     and destination != request.world.map and sample.world.map == destination)
        if not valid:
            self.cancel(frame, failed=True)
            raise ValueError('bot bypass acknowledgement context changed')
        event = {'map': request.world.map,
                 'reason': ('stuck-replan' if action == 'replan' else 'stuck' if action == 'restart' else
                            'map-timeout' if outcome == 'skip' else 'map-timeout-fallback'),
                 'minutes': self._elapsed_ms / 60000, 'outcome': outcome}
        key = 'replans' if outcome == 'replan' else 'skips' if outcome == 'skip' else 'restarts'
        self._counts[key] = min(_MAX_COUNT, self._counts[key] + 1)
        self._last_action = outcome
        if outcome == 'skip':
            self._new_map(sample)
        elif action == 'skip':
            self._next_skip_ms = min(_MAX_COUNT, self._elapsed_ms + self._map_ms)
        self._pending = self._request = None
        self._confirmed = self._progressed = self._guard_changed = self._last_active = False
        self._last = sample
        if outcome == 'replan':
            self._replanned = True
        else:
            self._fresh_alive_interval()
            self._ground(sample)
        return event

    def settings(self):
        return {**self._counts, 'last_action': self._last_action,
                'pending': self._pending, 'eligible': self._last_active,
                'map_elapsed_ms': self._elapsed_ms,
                'stuck_elapsed_ms': max(0, self._elapsed_ms - self._ground_ms),
                'stuck_seconds': self._stuck_ms // 1000,
                'map_seconds': self._map_ms // 1000,
                'replan_seconds': self._replan_ms // 1000,
                'recovery_cooldown_seconds': BOT_RECOVERY_COOLDOWN_SECONDS,
                'visited_cells': len(self._cells), 'cell_cap': self._cell_cap}
