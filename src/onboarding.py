# SPDX-License-Identifier: 0BSD
"""First-run setup guide state: inert choices only; the menu applies them at Finish.

The controller is the only writer and its loop is single-threaded, so no lock is needed.
"""
import json
import os
from pathlib import Path
import stat
import tempfile

MARKER = 'onboarding.json'
STEPS = ('background', 'game', 'screensaver', 'summary')
BACKGROUND_CHOICES = ('current', 'slipgate', 'skip')
THEME = {'name': 'slipgate', 'url': None}   # set the GitHub URL once the theme is published
UNREADABLE = 'the setup guide state is unreadable'
_LIMIT = 16384


def _valid(value):
    choices = value.get('choices') if isinstance(value, dict) else None
    return (isinstance(choices, dict) and type(value.get('schema')) is int and value['schema'] == 1
            and all(type(value.get(key)) is bool for key in ('show', 'fresh', 'opened'))
            and value.get('step') in STEPS and choices.get('background') in BACKGROUND_CHOICES
            and type(choices.get('screensaver')) is bool
            and 'outcome' in value and value['outcome'] in (None, 'finished', 'skipped')
            and not (value['show'] and value['outcome'] is not None))


def read_marker(state):
    """The validated marker, or None when absent; anything else is unreadable."""
    try:
        fd = os.open(Path(state) / MARKER, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    except OSError:
        raise ValueError(UNREADABLE) from None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_size > _LIMIT:
            raise ValueError(UNREADABLE)
        data = os.read(fd, _LIMIT + 1)
        value = json.loads(data) if len(data) <= _LIMIT else None
    except (OSError, ValueError, RecursionError):
        raise ValueError(UNREADABLE) from None
    finally:
        os.close(fd)
    if not _valid(value):
        raise ValueError(UNREADABLE)
    return value


def _write(state, value):
    path = Path(state) / MARKER
    fd, name = tempfile.mkstemp(dir=path.parent, prefix='.' + MARKER + '-')
    try:
        with os.fdopen(fd, 'w') as output:
            json.dump(value, output, indent=2)
            output.write('\n')
            output.flush()
            os.fsync(output.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def new_marker(*, fresh, saver):
    return {'schema': 1, 'show': True, 'fresh': fresh, 'opened': False, 'step': 'background',
            'choices': {'background': 'current', 'screensaver': True if fresh else bool(saver)},
            'outcome': None}


def initialize(state, *, fresh, saver):
    """Create the marker once; an existing one, valid or not, is never touched."""
    if os.path.lexists(Path(state) / MARKER):
        try:
            return read_marker(state)
        except ValueError:
            return None
    value = new_marker(fresh=fresh, saver=saver)
    _write(state, value)
    return value


def command(state, words, *, saver):
    """One `onboarding ...` socket verb; every input is validated before any write."""
    verb, rest = (words[0], list(words[1:])) if words else ('', [])
    if verb == 'step':
        if not rest or rest[0] not in STEPS:
            raise ValueError('unknown onboarding step')
        choices = {}
        for word in rest[1:]:
            key, separator, choice = word.partition('=')
            if not separator or key not in ('background', 'screensaver') or key in choices:
                raise ValueError('unknown or repeated onboarding choice')
            if key == 'background' and choice not in BACKGROUND_CHOICES:
                raise ValueError('onboarding background choice must be current, slipgate or skip')
            if key == 'screensaver' and choice not in ('on', 'off'):
                raise ValueError('onboarding screensaver choice must be on or off')
            choices[key] = choice if key == 'background' else choice == 'on'
        value = read_marker(state) or new_marker(fresh=False, saver=saver)
        if not value['show']:
            raise ValueError('the setup guide is already closed')
        value.update(step=rest[0], choices={**value['choices'], **choices})
    elif verb not in ('opened', 'finish', 'dismiss', 'skip') or rest:
        raise ValueError('unknown onboarding command')
    elif verb == 'opened':
        value = read_marker(state)
        if value is None or value['opened']:
            return  # Nothing to open without a marker; a repeat changes nothing.
        value['opened'] = True
    else:
        value = read_marker(state) or new_marker(fresh=False, saver=saver)
        if not value['show']:
            return  # A repeated or stale close keeps the first outcome.
        value.update(show=False, outcome='skipped' if verb == 'skip' else 'finished')
        if verb != 'skip':
            value['step'] = 'summary'
    _write(state, value)


def settings(state, *, home, theme):
    """The derived `onboarding` settings key; a bad marker hides the guide and never raises."""
    error = None
    try:
        value = read_marker(state)
    except ValueError as failure:
        value, error = None, str(failure)
    if value is None:
        value = dict(new_marker(fresh=False, saver=False), show=False)
    installed = home is not None and os.path.isdir(Path(home) / '.config/omarchy/themes' / THEME['name'])
    return {'show': value['show'], 'step': value['step'],
            'choices': {key: value['choices'][key] for key in ('background', 'screensaver')},
            'outcome': value['outcome'], 'fresh': value['fresh'], 'opened': value['opened'],
            'theme': dict(THEME, installed=installed, active=theme == THEME['name']), 'error': error}
