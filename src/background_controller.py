# SPDX-License-Identifier: 0BSD
"""Marker policy adapter with public metadata and a bounded selection command."""
from pathlib import Path
import threading
import time

from background import (BackgroundSelection, live_wallpaper_metadata,
                        use_live_wallpaper, remove_live_wallpaper)
from desktop import bounded_command, session_environment


def public_reason(value):
    """Filesystem exceptions may contain a selected image or private HOME path."""
    if not isinstance(value, str) or '/' in value or '\\' in value or value.startswith('[Errno'):
        return 'Background settings are unavailable'
    return value.replace('\n', ' ')[:256]


class BackgroundIntegration:
    def __init__(self, home, state, runtime, marker, environment, *, select=None, clock=time.monotonic):
        self.home, self.state, self.runtime, self.marker = map(Path, (home, state, runtime, marker))
        self.selection = BackgroundSelection(self.home, self.state)
        self.environment = session_environment(environment)
        self.select = select if select is not None else self._select
        self.clock = clock
        self.stop = threading.Event()
        self.next_poll = self.next_metadata = 0
        self.live = False
        self._metadata = {'theme': '', 'live_choice_present': False, 'can_remove': False,
                          'remove_reason': 'Background settings are unavailable'}

    def _select(self, target):
        try:
            bounded_command(('/usr/bin/omarchy', 'theme', 'bg', 'set', str(target)),
                            environment=self.environment, timeout=2, limit=16384, stop=self.stop)
        except TimeoutError as error:
            raise RuntimeError('Omarchy background selection is not responding') from error
        except (OSError, ValueError, RuntimeError) as error:
            raise RuntimeError('Omarchy could not select the background') from error

    def poll(self, *, force=False):
        now = self.clock()
        if force or now >= self.next_poll:
            self.selection.poll()
            self.live = self.selection.live
            self.next_poll = now + .25
        return self.live

    @property
    def selection_available(self):
        """A readable marker or static choice; cache failure is not read failure."""
        return self.selection.selected_path is not None

    def metadata(self):
        now = self.clock()
        if now >= self.next_metadata:
            source = live_wallpaper_metadata(self.home, self.state)
            self._metadata = {'theme': source['name'],
                'live_choice_present': source['live_choice_present'],
                'can_remove': source['can_remove_live'],
                'remove_reason': public_reason(source['remove_reason'])}
            self.next_metadata = now + 1
        return dict(self._metadata, active=self.live)

    def change(self, verb):
        try:
            if verb == 'use-live-wallpaper':
                use_live_wallpaper(self.home, self.marker, self.select, self.runtime, self.state)
            elif verb == 'remove-live-wallpaper':
                remove_live_wallpaper(self.home, self.select, self.runtime, self.state)
            else:
                raise ValueError('Unknown live background operation')
        except (OSError, ValueError, RuntimeError) as error:
            raise RuntimeError(public_reason(str(error))) from error
        finally:
            self.next_metadata = 0
            self.poll(force=True)
        return 'OK'

    def close(self):
        self.stop.set()
