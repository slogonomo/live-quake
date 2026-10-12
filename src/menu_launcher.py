# SPDX-License-Identifier: 0BSD
"""Bounded installed-plugin summon transport; no fallback window or grab."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import stat
import subprocess
import threading

from desktop import bounded_command, session_environment

PLUGIN_ID = 'io.github.slogonomo.live-quake'
SUMMON = ('/usr/bin/omarchy-shell', 'shell', 'summon')
_OUTPUT = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z', re.ASCII)
_MANIFEST_LIMIT = 32768
_REPLY_LIMIT = 16384


class MenuLaunchError(RuntimeError):
    """A public, path-free diagnostic suitable for quakectl's ERR reply."""


def _installed(home, config):
    """Read an owned manifest through anchored, nofollow directory handles."""
    path = Path(home)
    if not path.is_absolute() or len(path.parts) < 2 or '..' in path.parts:
        return False
    descriptors = []
    try:
        directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        descriptor = os.open('/', directory_flags)
        descriptors.append(descriptor)
        for part in path.parts[1:]:
            descriptor = os.open(part, directory_flags, dir_fd=descriptor)
            descriptors.append(descriptor)
        info = os.fstat(descriptor)
        if info.st_uid != os.getuid() or info.st_mode & 0o022:
            return False
        for part in (*config.relative_to(path).parts, 'omarchy', 'plugins', PLUGIN_ID):
            descriptor = os.open(part, directory_flags, dir_fd=descriptor)
            descriptors.append(descriptor)
            info = os.fstat(descriptor)
            if info.st_uid != os.getuid() or info.st_mode & 0o022:
                return False
        manifest = os.open('manifest.json', os.O_RDONLY | os.O_NOFOLLOW |
                           os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=descriptor)
        descriptors.append(manifest)
        info = os.fstat(manifest)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o022 or not 2 <= info.st_size <= _MANIFEST_LIMIT):
            return False
        data = os.pread(manifest, _MANIFEST_LIMIT + 1, 0)
        if len(data) != info.st_size or len(data) > _MANIFEST_LIMIT:
            return False
        document = json.loads(data)
        return isinstance(document, dict) and document.get('id') == PLUGIN_ID
    except (OSError, ValueError, TypeError, RecursionError):
        return False
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


class MenuLauncher:
    """Explicit home/session, fixed production command, constructor-only DI.

The optional command prefix and callable runner exist for private harnesses;
the production daemon provides neither. The runner uses desktop.bounded_command's
signature and must return bytes only after a successful exit. Controller owns
pause/revoke/lease handling; this object only validates and sends one summon.
"""
    def __init__(self, home, environment, *, command=None, runner=None):
        self.home = Path(home)
        configured = environment.get('XDG_CONFIG_HOME')
        if configured in (None, ''):
            self.config = self.home / '.config'
        elif (not isinstance(configured, str) or '\0' in configured
                or not Path(configured).is_absolute() or '..' in Path(configured).parts
                or not Path(configured).is_relative_to(self.home)):
            raise ValueError('Menu config must be an absolute path within supplied home')
        else:
            self.config = Path(configured)
        self.environment = session_environment(environment)
        self.command = SUMMON if command is None else command
        if (not isinstance(self.command, (tuple, list)) or not self.command
                or len(self.command) > 24 or not all(isinstance(part, str)
                    and len(part) <= 4096 and '\0' not in part for part in self.command)
                or not os.path.isabs(self.command[0])):
            raise ValueError('Menu command must have an absolute executable')
        self.command = tuple(self.command)
        self._runner = bounded_command if runner is None else runner
        if not callable(self._runner):
            raise ValueError('Menu runner must be callable')
        self._stop = threading.Event()

    def installed(self):
        return _installed(self.home, self.config)

    def open(self, *, output=None, origin=None):
        payload = {}
        if output is not None:
            if not isinstance(output, str) or _OUTPUT.fullmatch(output) is None:
                raise MenuLaunchError('invalid menu output')
            payload['output'] = output
        if origin is not None:
            if origin not in ('launcher', 'context-menu', 'onboarding'):
                raise MenuLaunchError('invalid menu origin')
            payload['origin'] = origin
        if not self.installed():
            raise MenuLaunchError('the settings menu is not installed')
        if self._stop.is_set():
            raise MenuLaunchError('the settings menu could not be opened')
        command = (*self.command, PLUGIN_ID,
                   json.dumps(payload, ensure_ascii=True, separators=(',', ':')))
        try:
            reply = self._runner(command, environment=dict(self.environment), timeout=2,
                                 limit=_REPLY_LIMIT, stop=self._stop)
        except (TimeoutError, subprocess.TimeoutExpired):
            raise MenuLaunchError('omarchy-shell is not responding') from None
        except Exception:
            # Tool failures may contain paths or private payloads. The public
            # response never includes helper stdout/stderr or exception text.
            raise MenuLaunchError('the settings menu could not be opened') from None
        # IPC tools print the exact acknowledgement with an optional LF.
        if not isinstance(reply, bytes) or reply not in (b'ok', b'ok\n'):
            raise MenuLaunchError('the settings menu could not be opened')
        return 'OK'

    def close(self):
        self._stop.set()
