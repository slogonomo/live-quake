# SPDX-License-Identifier: 0BSD
"""Production controller lifecycle, with explicit XDG paths and no installer effects."""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import os
from pathlib import Path
import resource
import signal
import stat
import threading

from controller import Controller, owned_directory
from runtime_lock import RuntimeLock, absolute_path




def session_environment(source):
    """Keep session endpoints, never executable/library overrides or shell state."""
    result = {'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8', 'PYTHONDONTWRITEBYTECODE': '1'}
    for name in ('HOME', 'USER', 'LOGNAME', 'WAYLAND_DISPLAY', 'XDG_RUNTIME_DIR',
                 'HYPRLAND_INSTANCE_SIGNATURE', 'DBUS_SESSION_BUS_ADDRESS',
                 'XDG_SESSION_TYPE', 'XDG_CURRENT_DESKTOP', 'XDG_SESSION_DESKTOP',
                 'XDG_CONFIG_HOME', 'XDG_DATA_HOME', 'XDG_STATE_HOME', 'XDG_CACHE_HOME'):
        value = source.get(name)
        if value is not None:
            if not isinstance(value, str) or len(value) > 4096 or '\0' in value or '\n' in value:
                raise ValueError('invalid session value: ' + name)
            if name in ('XDG_CONFIG_HOME', 'XDG_DATA_HOME', 'XDG_STATE_HOME', 'XDG_CACHE_HOME') and not value.startswith('/'):
                continue  # The XDG specification treats these as unset.
            result[name] = value
    return result


def make_private_directory(path):
    """Create missing parents; existing parents are never chmodded or replaced."""
    missing = []
    current = path
    while not current.exists():
        if current.is_symlink():
            raise ValueError('directory cannot be a symbolic link')
        missing.append(current)
        current = current.parent
    if current.resolve() != current or not current.is_dir():
        raise ValueError('invalid directory parent')
    for item in reversed(missing):
        item.mkdir(mode=0o700)
    return owned_directory(path)


@dataclass(frozen=True)
class Paths:
    home: Path
    runtime: Path
    state: Path
    data: Path
    build: Path
    pak: Path

    @classmethod
    def from_environment(cls, source, *, runtime=None, state=None, data=None, build=None, pak=None):
        home = absolute_path(source.get('HOME', ''), 'HOME')
        runtime_base = source.get('XDG_RUNTIME_DIR')
        if runtime is None and not runtime_base:
            raise ValueError('XDG_RUNTIME_DIR is required')
        if runtime_base:
            runtime_path = absolute_path(runtime_base, 'XDG_RUNTIME_DIR')
            info = runtime_path.stat()
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
                raise ValueError('XDG_RUNTIME_DIR must be owned and mode 0700')
        def xdg_home(key, default):
            value = source.get(key)
            if not isinstance(value, str) or not value.startswith('/'):
                value = str(default)
            return absolute_path(value, key)
        state_base = xdg_home('XDG_STATE_HOME', home / '.local/state')
        data_base = xdg_home('XDG_DATA_HOME', home / '.local/share')
        cache_base = xdg_home('XDG_CACHE_HOME', home / '.cache')
        data = absolute_path(data if data is not None else data_base / 'live-quake', 'data directory')
        return cls(home,
                   absolute_path(runtime if runtime is not None else Path(runtime_base) / 'live-quake', 'runtime directory'),
                   absolute_path(state if state is not None else state_base / 'live-quake', 'state directory'),
                   data,
                   absolute_path(build if build is not None else cache_base / 'live-quake/build', 'build directory'),
                   absolute_path(pak if pak is not None else data / 'librequake/full/id1/pak0.pak', 'game archive'))

    def prepare(self):
        for path in (self.runtime, self.state, self.data):
            make_private_directory(path)




def serve(controller, observer=None, *, stop_event=None):
    """The installed loop and private harness share the same cleanup path."""
    try:
        while controller.running and not (stop_event and stop_event.is_set()):
            controller.step()
    finally:
        try:
            controller.close()
        finally:
            if observer is not None and hasattr(observer, 'close'):
                observer.close()


def run_daemon(paths, environment, *, observer=None, controller_factory=Controller, stop_event=None,
               controller_options=None):
    """Dependencies are callable APIs for tests, never executable environment hooks."""
    paths.prepare()
    with RuntimeLock(paths.runtime):
        if observer is None:
            from desktop import DesktopObserver
            observer = DesktopObserver(environment)
        try:
            if hasattr(observer, 'start'):
                observer.start()
            for integration in (controller_options or {}).values():
                if hasattr(integration, 'start'):
                    integration.start()
            controller = controller_factory(paths.runtime, paths.state, paths.build, paths.pak,
                                            environment, observer, **(controller_options or {}))
        except BaseException:
            for integration in (controller_options or {}).values():
                if hasattr(integration, 'close'):
                    integration.close()
            if hasattr(observer, 'close'):
                observer.close()
            raise
        serve(controller, observer, stop_event=stop_event)


def main(argv=None):
    resource.setrlimit(resource.RLIMIT_CORE, (1, 1))
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime-dir', type=Path)
    parser.add_argument('--state-dir', type=Path)
    parser.add_argument('--data-dir', type=Path)
    parser.add_argument('--build-dir', type=Path)
    parser.add_argument('--pak', type=Path)
    # Production sound preferences must control a real sink. Private/native
    # harnesses keep their explicit silent default; --audio-mode discard also
    # provides an intentional silent production override.
    parser.add_argument('--audio-mode', choices=('discard', 'alsa'), default='alsa')
    options = parser.parse_args(argv)
    paths = Paths.from_environment(os.environ, runtime=options.runtime_dir, state=options.state_dir,
                                   data=options.data_dir, build=options.build_dir, pak=options.pak)
    environment = session_environment(os.environ)
    from background_controller import BackgroundIntegration
    from menu_launcher import MenuLauncher
    from idle_controller import IdleIntegration
    from policy_log import PolicyLog
    integrations = {'background': BackgroundIntegration(paths.home, paths.state, paths.runtime.parent,
                              Path(__file__).resolve().parents[1] / 'assets/live-quake.webp', environment),
                    'menu_launcher': MenuLauncher(paths.home, environment),
                    'idle': IdleIntegration(paths.home, paths.state, environment, allow_claim=True),
                    'audio_mode': options.audio_mode,
                    'catalog_home': paths.home, 'policy_log': PolicyLog()}
    stop = threading.Event()
    previous = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)}
    try:
        for sig in previous:
            signal.signal(sig, lambda *_: stop.set())
        run_daemon(paths, environment, stop_event=stop, controller_options=integrations)
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (OSError, ValueError, RuntimeError) as error:
        raise SystemExit(str(error))
