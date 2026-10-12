# SPDX-License-Identifier: 0BSD
"""Bounded, change-only diagnostics from already projected controller state.

No desktop/process reads, policy decisions, filesystem output, or worker thread.
The caller supplies only policy enums, output names, nominal mode refresh and
its native acknowledgments. Refresh is not presentation or simulation cadence.
When flapping exceeds the budget the newest state is retained and the record
reports how many changes were coalesced; those intervals are not an exact trace.
"""
from collections import OrderedDict
from datetime import datetime, timezone
import json
from itertools import islice
import re
import socket
import time

VIEWS = frozenset(('bot', 'paused', 'hidden', 'human', 'saver', 'removed'))
REASONS = frozenset(('disabled', 'busy', 'lock', 'asleep', 'menu', 'manual',
                     'unavailable', 'engine-error', 'switch', 'watchdog', 'bot-bypass', 'saver', 'checkpoint-loading',
                     'package-loading'))
OBSERVERS = frozenset(('available', 'unavailable', 'stale'))
SHOW_ON = frozenset(('empty', 'all'))
ENGINE_STATES = frozenset(('starting', 'ready', 'stopped', 'exited', 'retired'))
NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,79}\Z', re.ASCII)
MAX_OUTPUTS = 128
MAX_PENDING = 258
MAX_RECORD_BYTES = 768
WIRE_VALUE = re.compile(r'[A-Za-z0-9_.:-]{1,80}\Z', re.ASCII)


def journal_fields(record):
    fields = {'SYSLOG_IDENTIFIER': 'live-quake-policy', 'PRIORITY': '6'}
    for key, source in (('LQ_EVENT', 'event'), ('LQ_OUTPUT', 'output'),
                        ('LQ_VIEW', 'view'), ('LQ_REASON', 'pause_reason'),
                        ('LQ_OBSERVER', 'observer'), ('LQ_GENERATION', 'generation'),
                        ('LQ_ACK_PAUSED', 'paused'), ('LQ_SHOW_ON', 'show_on'),
                        ('LQ_REFRESH_MILLIHZ', 'refresh_millihz'),
                        ('LQ_COVERED_RUNNING', 'covered_running')):
        value = record.get(source)
        if value is None:
            continue
        if source == 'refresh_millihz' and _refresh_millihz(value) is None:
            raise ValueError('Invalid structured diagnostic field')
        if type(value) is bool:
            value = '1' if value else '0'
        elif type(value) is int:
            value = str(value)
        if type(value) is not str or not WIRE_VALUE.fullmatch(value):
            raise ValueError('Invalid structured diagnostic field')
        fields[key] = value
    return fields


class JournalSink:
    """One nonblocking local datagram; no stderr flags or background thread.

    Production uses the fixed journald socket. An explicit private socket is a
    callable fixture dependency, never a CLI/environment override.
    """
    def __init__(self, address='/run/systemd/journal/socket'):
        self._address, self._socket = address, None
        self._closed = False

    def __call__(self, record):
        if self._closed:
            raise OSError('Diagnostic sink is closed')
        payload = json.dumps(record, separators=(',', ':'), ensure_ascii=True).encode('ascii')
        if len(payload) > MAX_RECORD_BYTES:
            raise ValueError('Diagnostic record exceeds limit')
        fields = journal_fields(record)
        message = b'MESSAGE=' + payload + b'\n' + b''.join(
            k.encode('ascii') + b'=' + v.encode('ascii') + b'\n' for k, v in fields.items())
        if len(message) > 1536:
            raise ValueError('Diagnostic datagram exceeds limit')
        if self._socket is None:
            self._socket = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM | socket.SOCK_CLOEXEC)
            self._socket.setblocking(False)
        # MSG_DONTWAIT independently pins the nonblocking property on each send.
        sent = self._socket.sendto(message, socket.MSG_DONTWAIT, self._address)
        if sent != len(message):
            raise OSError('Diagnostic datagram was incomplete')

    def close(self):
        self._closed = True
        if self._socket is not None:
            self._socket.close()
            self._socket = None


def _enum(value, allowed, fallback='unknown'):
    return value if type(value) is str and value in allowed else fallback


def _refresh_millihz(value):
    return value if type(value) is int and 1 <= value <= 1000000 else None


def _utc(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat(timespec='milliseconds')


class PolicyLog:
    def __init__(self, *, emit=None, clock=time.monotonic, wall_clock=time.time,
                 rate=16.0, burst=32):
        if not 1 <= rate <= 32 or type(burst) is not int or not 1 <= burst <= 64:
            raise ValueError('Invalid diagnostic budget')
        self._emit = emit if emit is not None else JournalSink()
        self._clock, self._wall = clock, wall_clock
        self._rate, self._burst = float(rate), burst
        self._tokens = float(burst)
        self._last_time = clock()
        self._seen, self._pending = {}, OrderedDict()
        self._outputs = set()
        self._omitted = 0
        self._sink_failures = self._written = 0
        self._policy = {'observer': 'unknown', 'pause_reason': 'unknown', 'native_gate': 'unknown',
                        'show_on': 'unknown'}

    def _change(self, key, fields, *, remove=False):
        if self._seen.get(key) == fields:
            return
        at = _utc(self._wall())
        self._seen[key] = fields
        prior = self._pending.get(key)
        self._pending[key] = {'fields': fields, 'first': prior['first'] if prior else at,
                              'at': at, 'changes': prior['changes'] + 1 if prior else 1}
        if remove:
            self._seen.pop(key, None)
        if len(self._pending) > MAX_PENDING:
            lost_key, dropped = self._pending.popitem(last=False)
            self._seen.pop(lost_key, None)
            self._omitted += dropped['changes']

    def policy(self, observer, reason, applied, generation=None, *, show_on='unknown'):
        # applied is the last ACKed (paused, bot, audio) tuple; None means
        # unknown. It is never inferred from desired policy or an extra probe.
        valid = (type(applied) is tuple and len(applied) == 3
                 and all(x is None or type(x) is int and x in (0, 1) for x in applied))
        fields = {'event': 'policy', 'observer': _enum(observer, OBSERVERS),
                  'pause_reason': 'running' if reason is None else _enum(reason, REASONS),
                  'native_gate': 'acknowledged' if valid and applied[0] is not None else 'unknown',
                  'show_on': _enum(show_on, SHOW_ON)}
        if valid:
            for key, value in zip(('paused', 'bot', 'audio'), applied):
                if value is not None:
                    fields[key] = bool(value)
        if type(generation) is int and 0 <= generation < 2**64:
            fields['generation'] = generation
        self._policy = {k: v for k, v in fields.items() if k != 'event'}
        self._change(('policy',), fields)
        self.flush()

    def views(self, views, *, refresh_millihz=None):
        # Every production name passed here is ASCII-validated by the observer.
        # Reject names resembling paths or arbitrary text even if a test/custom
        # observer supplies them. No rejected string is retained or formatted.
        clean = {}
        rates = (refresh_millihz if type(refresh_millihz) is dict
                 and self._policy['observer'] not in ('stale', 'unavailable') else {})
        for name, value in islice(views.items(), MAX_OUTPUTS):
            if type(name) is str and NAME.fullmatch(name):
                if type(value) is tuple and len(value) in (2, 3):
                    view, busy = value[:2]
                    reason = value[2] if len(value) == 3 else self._policy['pause_reason']
                else:
                    view, busy, reason = value, False, self._policy['pause_reason']
                clean[name] = (_enum(view, VIEWS), busy is True,
                               'running' if reason is None else _enum(reason, REASONS | {'running'}),
                               _refresh_millihz(rates.get(name)))
        for name in sorted(self._outputs - clean.keys()):
            self._change(('output', name), dict(self._policy, event='output', output=name,
                                               view='removed', busy=False, covered_running=False,
                                               refresh_millihz=None), remove=True)
        for name in sorted(clean):
            view, busy, reason, rate = clean[name]
            covered = (busy and view in ('bot', 'human', 'saver') and self._policy.get('paused') is False
                       and self._policy['show_on'] == 'empty'
                       if self._policy['native_gate'] == 'acknowledged' else None)
            self._change(('output', name), dict(self._policy, event='output', output=name,
                                               view=view, busy=busy, pause_reason=reason,
                                               covered_running=covered, refresh_millihz=rate))
        self._outputs = set(clean)
        self.flush()

    def engine(self, state, generation, *, exit_code=None):
        fields = {'event': 'engine', 'state': _enum(state, ENGINE_STATES)}
        if type(generation) is int and 0 <= generation < 2**64:
            fields['generation'] = generation
        if type(exit_code) is int and -255 <= exit_code <= 255:
            fields['exit_code'] = exit_code
        self._change(('engine',), fields)
        self.flush()

    def flush(self):
        now = self._clock()
        self._tokens = min(self._burst, self._tokens + max(0, now - self._last_time) * self._rate)
        self._last_time = now
        while self._pending and self._tokens >= 1:
            key, pending = next(iter(self._pending.items()))
            record = dict(pending['fields'], observed_utc=pending['at'],
                          logged_utc=_utc(self._wall()))
            if pending['changes'] > 1:
                record.update(coalesced_changes=pending['changes'] - 1,
                              first_observed_utc=pending['first'])
            if self._omitted:
                record['omitted_changes'] = self._omitted
            if self._sink_failures:
                record['sink_failures'] = self._sink_failures
            self._tokens -= 1
            try:
                self._emit(record)
            except BlockingIOError:
                # Keep only the latest state while journald's socket is full.
                self._sink_failures += 1
                return
            except Exception:
                # A diagnostic sink must never interrupt policy or teardown.
                # A missing/unavailable socket drops this record, rather than
                # retrying forever during an otherwise quiet desktop.
                self._sink_failures += 1
                self._pending.pop(key)
                return
            self._pending.pop(key)
            self._omitted = 0
            self._written += 1

    def counters(self):
        return {'written': self._written, 'sink_failures': self._sink_failures,
                'pending': len(self._pending), 'omitted_changes': self._omitted}

    def close(self):
        # A nonresponsive journal must never delay process teardown. Pending
        # records can be lost on exit; counters expose that in private proofs.
        try:
            self.flush()
        except Exception:
            self._sink_failures += 1
        if hasattr(self._emit, 'close'):
            try:
                self._emit.close()
            except Exception:
                self._sink_failures += 1
