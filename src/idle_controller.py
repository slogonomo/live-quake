# SPDX-License-Identifier: 0BSD
"""Paused warmup, verified ownership and durable next-cycle saver recovery."""
from dataclasses import replace
import logging
import math
import time

from idle import IdleObserver, SaverOwnership, SaverRecovery, decision

STATES = ('disabled', 'warming', 'ready', 'presenting', 'active', 'blocked', 'backoff')
TRANSIENT_REASONS = ('disabled', 'paused', 'loading', 'runtime-unavailable',
                     'desktop-unavailable', 'idle-unavailable', 'ownership-unavailable', 'failure-backoff')
STICKY_REASONS = ('claim-revoked', 'repeated-presentation-failure', 'external-suppression',
                  'recovery-unavailable')
REASONS = {
    'disabled': 'Screensaver disabled',
    'paused': 'Screensaver is paused',
    'loading': 'Screensaver is loading',
    'runtime-unavailable': 'Screensaver runtime is unavailable',
    'desktop-unavailable': 'Desktop or lock state is unavailable',
    'idle-unavailable': 'First-party idle status unavailable',
    'ownership-unavailable': 'Screensaver ownership is unavailable',
    'recovery-unavailable': 'Screensaver recovery needs a retry',
    'failure-backoff': 'Waiting for screensaver recovery',
    'claim-revoked': 'Screensaver claim was removed or replaced; enable it again to retry',
    'repeated-presentation-failure': 'Screensaver failed three times; enable it again to retry',
    'external-suppression': 'Screensaver suppression is controlled externally; enable it again to retry',
}
_LOG = logging.getLogger('live-quake.screensaver')
# The daemon has no global logging setup. Emit only these bounded transitions
# to stderr, which its user service already sends to the journal.
if not _LOG.handlers:
    _LOG.addHandler(logging.StreamHandler())
_LOG.setLevel(logging.INFO)
_LOG.propagate = False


class StatusOnlyOwnership:
    """Class default remains read only, including shutdown."""
    def __init__(self, ownership):
        self.ownership = ownership

    def status(self):
        return self.ownership.status()

    def claim(self, enabled):
        return self.status()

    def release(self):
        return self.status()


class IdleIntegration:
    def __init__(self, home, state, environment, *, observer=None, ownership=None, allow_claim=False):
        if type(allow_claim) is not bool:
            raise ValueError('Token claiming must be an explicit boolean')
        self.allow_claim = allow_claim
        self.observer = IdleObserver(environment) if observer is None else observer
        ownership = SaverOwnership(home, state) if ownership is None else ownership
        self.ownership = ownership if allow_claim else StatusOnlyOwnership(ownership)
        self.recovery = SaverRecovery(ownership) if allow_claim else None
        self.recovery_available = True
        try:
            self._recovery = self.recovery.load() if self.recovery else SaverRecovery.initial()
        except (OSError, ValueError, RecursionError):
            self._recovery = SaverRecovery.initial()
            self.recovery_available = False
        self._interrupted = self._recovery['attempt']
        self._adopt_identity = None
        self._next_ownership_reconcile = 0.0
        self.owner, self.idle = self.ownership.status(), self.observer()
        self.cycle_consumed = self._recovery['consumed']
        self.cycle_failed = self._recovery['skip'] != 'none'
        self.requested, self.armed = False, False
        self.state, self.reason_code, self.reason = 'disabled', 'disabled', REASONS['disabled']
        self.cycle_id = self._recovery['cycle_id']
        self.engine_generation = self._recovery['engine_generation']

    @property
    def auto_disable_reason(self):
        value = self._recovery['disable_reason']
        return value if value in ('claim-revoked', 'repeated-presentation-failure') else None

    def start(self):
        if self.allow_claim:
            # Production starts integrations while holding its runtime lock.
            # Only then can a complete interrupted claim be reconciled.
            self._next_ownership_reconcile = time.monotonic() + 1
            result = self.ownership.resolve_pending()
            if result.get('released'):
                self._log('release', 'disabled')
            self.owner = self.ownership.status()
        self.observer.start()

    def _save(self):
        self.cycle_consumed = self._recovery['consumed']
        self.cycle_failed = self._recovery['skip'] != 'none'
        if self.recovery and self.recovery_available:
            try:
                self.recovery.save(self._recovery)
            except (OSError, ValueError, RecursionError):
                self.recovery_available = False
                self.requested = self.armed = False
                self._release('recovery-unavailable')
                self._log('failure', 'recovery-unavailable')
                self._public('blocked', 'recovery-unavailable')

    def _log(self, transition, reason_code):
        _LOG.info('screensaver %s reason_code=%s cycle_id=%d engine_generation=%d',
                  transition, reason_code or 'ready', self.cycle_id, self.engine_generation)

    def _release(self, reason_code, *, retire=False, recognize=False):
        # An unknown status is not authority to mutate an ownership record.
        if self.allow_claim and (self.owner.get('owned') is True or retire):
            result = self.ownership.release(recognize=True) if recognize else self.ownership.release()
            if result.get('released'):
                self._log('release', reason_code)
            self.owner = result
        self.armed = False

    def _public(self, state, code):
        if not self.recovery_available and code != 'disabled':
            state, code = 'blocked', 'recovery-unavailable'
        self.state, self.reason_code = state, code
        if code in STICKY_REASONS:
            self.requested = False
        self.reason = REASONS[code] if code else decision(
            self.idle, self.owner, enabled=True, locked=False).reason

    def _blocker(self, snapshot, enabled):
        sticky = self._recovery['disable_reason']
        if sticky:
            return sticky
        if not enabled or not self.allow_claim:
            return 'disabled'
        if not self.recovery_available:
            return 'recovery-unavailable'
        if self.owner['owner'] == 'unknown':
            return 'ownership-unavailable'
        if not snapshot.available or snapshot.locked is not False or not any(o.awake for o in snapshot.outputs):
            return 'desktop-unavailable'
        if not self.idle.available or not self.idle.enabled or self.idle.stay_awake:
            return 'idle-unavailable'
        if self._recovery['skip'] != 'none':
            return 'failure-backoff'
        return None

    def _observe_cycle(self, enabled):
        if not self.idle.available:
            return
        old = dict(self._recovery)
        active = self.idle.in_idle_cycle
        if active and (enabled or self._recovery['cycle_open'] or self._recovery['skip'] != 'none'):
            self._recovery['cycle_open'] = True
            if self._recovery['skip'] == 'next':
                self._recovery['skip'] = 'skipping'
        elif not active:
            skip = self._recovery['skip']
            if skip == 'current':
                self._recovery['skip'] = 'next'
            elif skip == 'skipping':
                self._recovery['skip'] = 'none'
            if (self._recovery['cycle_open'] and self._recovery['acknowledged']
                    and skip == 'none'):
                self._recovery['failures'] = 0
            self._recovery.update(cycle_open=False, consumed=False, acknowledged=False, attempt=False)
        if old != self._recovery:
            self._save()

    def observe(self, snapshot, enabled):
        if type(enabled) is not bool:
            raise ValueError('Saver enabled must be a boolean')
        # Ownership recovery still runs while disabled; only the optional shell
        # idle observer is dormant. Injected status-only observers need no worker.
        setter = getattr(self.observer, 'set_enabled', None)
        if setter is not None:
            setter(enabled and self.allow_claim and snapshot.available
                   and snapshot.locked is False and any(output.awake for output in snapshot.outputs))
        self.owner = self.ownership.status()
        resolver = getattr(self.ownership, 'resolve_pending', None)
        if (self.allow_claim and self.owner['owner'] == 'unknown'
                and self.owner.get('journal_retained') is True and resolver is not None):
            now = time.monotonic()
            if now >= self._next_ownership_reconcile:
                # Production retains its runtime lock for this integration's
                # lifetime. The existing resolver takes a nonblocking ownership
                # lock and validates both records before any mutation.
                self._next_ownership_reconcile = now + 1
                result = resolver()
                self.owner = self.ownership.status()
                if result.get('released'):
                    self._log('release', 'ownership-unavailable')
                elif result.get('recovered') and self.owner.get('owned'):
                    self._log('claim', None)
        self.idle = self.observer()
        if self._adopt_identity is not None and (not enabled or not self.adoption_pending):
            self._adopt_identity = None
        if enabled and self.allow_claim and self._interrupted:
            self._interrupted = False
            self.failed(presentation=True)
        self._record_ownership(enabled)
        self._observe_cycle(enabled)
        self.requested = (self.allow_claim and enabled and self.recovery_available
                          and self._recovery['disable_reason'] is None)
        code = self._blocker(snapshot, enabled)
        if code:
            self._release(code)
            self._public('disabled' if code == 'disabled' else 'backoff' if code == 'failure-backoff' else 'blocked', code)
        return replace(snapshot, saver_owner=self.owner['owner'])

    def _record_ownership(self, enabled):
        # Explicit re-enable may warm the runtime before adopting the user's
        # touched entry. A second touch invalidates this exact pending identity.
        if enabled and self.adoption_pending:
            return
        if enabled and self.allow_claim and self.owner.get('revoked') is True:
            if self._recovery['disable_reason'] != 'claim-revoked':
                self._recovery['disable_reason'] = 'claim-revoked'
                self._save()
                self._log('failure', 'claim-revoked')
        elif (enabled and self.allow_claim and self.owner['owner'] == 'other'
              and not self._recovery['disable_reason']):
            self._recovery['disable_reason'] = 'external-suppression'
            self._save()

    def ready(self, snapshot, enabled, *, eligible=False, reason_code='runtime-unavailable',
              state='warming', cycle_id=0, engine_generation=0):
        if (type(eligible) is not bool or state not in STATES
                or reason_code not in (None,) + TRANSIENT_REASONS + STICKY_REASONS
                or any(type(value) is not int or not 0 <= value <= 2**64 - 1
                       for value in (cycle_id, engine_generation))):
            raise ValueError('Invalid saver readiness')
        self.cycle_id, self.engine_generation = cycle_id, engine_generation
        self._recovery.update(cycle_id=cycle_id, engine_generation=engine_generation)
        code = self._blocker(snapshot, enabled)
        if code or not eligible:
            code = code or reason_code or 'runtime-unavailable'
            self._release(code)
            phase = 'disabled' if code == 'disabled' else 'backoff' if code == 'failure-backoff' else 'blocked' if self._blocker(snapshot, enabled) else state
            self._public(phase, code)
            return replace(snapshot, saver_owner=self.owner['owner'])
        if self.adoption_pending or (not self.owner.get('owned') and not self.idle.in_idle_cycle):
            # Claim and adoption use the validated reply arrival, while the
            # observer still expires its cache from command start. Only legacy
            # injected observations lack an arrival timestamp.
            observed = self.idle.reply_received_at
            if observed is None:
                observed = self.idle.command_started_at
            age = time.monotonic() - observed if observed is not None else math.inf
            if not 0 <= age <= .5:
                refresh = getattr(self.observer, 'request_fresh', None)
                if refresh is not None:
                    refresh()  # Async, coalesced into the one-query-per-second budget.
                self.armed = False
                self._public('blocked', 'idle-unavailable')
                return replace(snapshot, saver_owner=self.owner['owner'])
        if self.adoption_pending:
            self.owner = self.ownership.adopt(expected_identity=self._adopt_identity)
            self._adopt_identity = None
            self._record_ownership(enabled)
            if self.owner.get('adopted') and self.owner.get('owned'):
                self._log('claim', None)
        # A new claim only enters between complete idle cycles. Removing a
        # blocker halfway through a stock cycle must not suppress that cycle.
        if not self.owner.get('owned') and not self.idle.in_idle_cycle:
            self.owner = self.ownership.claim(True)
            self._record_ownership(enabled)
            if self.owner.get('owned'):
                self._log('claim', None)
        self.armed = self.owner.get('owned') is True
        if self.armed:
            phase = 'active' if self._recovery['attempt'] and self._recovery['acknowledged'] else 'presenting' if self._recovery['attempt'] else 'ready'
            self._public(phase, None)
        else:
            self._public('blocked', 'idle-unavailable' if self.owner['owner'] is None else self.owner.get('reason_code') or 'ownership-unavailable')
        return replace(snapshot, saver_owner=self.owner['owner'])

    def should_start(self, *, enabled, locked):
        result = decision(self.idle, self.owner, enabled=enabled, locked=locked)
        return result.should_start and not self.cycle_consumed and self.armed

    def should_stop(self, *, enabled, locked):
        return decision(self.idle, self.owner, enabled=enabled, locked=locked).should_stop or not self.armed

    def started(self):
        if not self.armed or self._recovery['skip'] != 'none':
            return
        self._recovery.update(consumed=True, attempt=True, acknowledged=False,
                              cycle_id=self.cycle_id, engine_generation=self.engine_generation)
        self._save()
        self._public('presenting', None)

    def acknowledged(self):
        if self._recovery['attempt'] and not self._recovery['acknowledged']:
            self._recovery['acknowledged'] = True
            self._save()
            self._public('active', None)

    def dismissed(self):
        self._recovery.update(consumed=True, attempt=False)
        self._save()
        if self.armed:
            self._public('ready', None)

    def failed(self, *, presentation=False):
        if type(presentation) is not bool:
            raise ValueError('Presentation failure must be an explicit boolean')
        count = self._recovery['attempt'] or presentation and self._recovery['skip'] == 'none'
        self._release('presentation-failed' if count else 'runtime-unavailable')
        if count:
            self._recovery.update(failures=min(3, self._recovery['failures'] + 1),
                                  skip='current' if self._recovery['cycle_open'] or self.idle.available and self.idle.in_idle_cycle else 'next',
                                  consumed=True, acknowledged=False, attempt=False)
            if self._recovery['failures'] == 3:
                self._recovery['disable_reason'] = 'repeated-presentation-failure'
            self._save()
            self._log('failure', 'presentation-failed')
        code = self._recovery['disable_reason'] or ('failure-backoff' if count else 'runtime-unavailable')
        self._public('backoff' if code == 'failure-backoff' else 'blocked', code)

    def explicit_reenable(self):
        if not self.allow_claim:
            return False
        self.owner = self.ownership.status()
        candidate = self.ownership.adoption_identity() if self.owner['owner'] == 'other' else None
        if self.owner['owner'] == 'other' and candidate is None:
            self.requested = self.armed = False
            self._public('blocked', self.owner.get('reason_code') or 'external-suppression')
            return False
        self._adopt_identity = candidate
        if candidate is None:
            self._release('loading', retire=True)
        try:
            restored = self.recovery.reset()
        except (OSError, ValueError, RecursionError):
            self._adopt_identity = None
            self.recovery_available = False
            self.requested = self.armed = False
            self._public('blocked', 'recovery-unavailable')
            return False
        self._recovery = restored
        self.recovery_available = True
        self.cycle_consumed = self.cycle_failed = False
        self._interrupted = False
        self._public('warming', 'loading')
        return True

    @property
    def adoption_pending(self):
        return (self._adopt_identity is not None and self.ownership.can_adopt(
            expected_identity=self._adopt_identity))

    def settings(self):
        return {'owner': self.owner['owner'],
                'reason': self.reason if self.allow_claim else 'Shared toggle observation only; token claiming is disabled',
                'armed': self.armed, 'requested': self.requested,
                'state': self.state, 'reason_code': self.reason_code,
                'idle_available': self.idle.available, 'claim_enabled': self.allow_claim,
                'adoption_available': self.allow_claim and self.owner['owner'] == 'other' and self.ownership.can_adopt()}

    def close(self, *, dismiss_active=False):
        if type(dismiss_active) is not bool:
            raise ValueError('Active saver dismissal must be an explicit boolean')
        try:
            self.owner = self.ownership.status()
            self._release('disabled')
            if dismiss_active:
                self.dismissed()
        finally:
            self.observer.close()
