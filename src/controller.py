#!/usr/bin/env python3
# SPDX-License-Identifier: 0BSD
"""Live Quake policy and bounded Unix control server.

The daemon supplies a read-only desktop observer; private tests inject fixtures.
"""
import argparse
import copy
import hashlib
import json
import logging
import math
import os
from pathlib import Path
import re
import selectors
import secrets
import socket
import stat
import struct
import subprocess
import tempfile
import threading
import time

from frame import read_frame, MOUSE_DELTA_MAX
from policy import Snapshot, pause_reason, takeover_allowed
from bot_watchdog import BotWatchdog, BotLevelBypass
from steam_games import validate_manual_data
from data_farm import create_farm
from dm_pack import installed_lqdm_maps
from game_catalog import GameCatalog, CatalogError, known_package_id, revalidate_selection
try:  # The setup guide is optional: an install without the file still starts.
    import onboarding
except ImportError:
    onboarding = None

ROOT = Path(__file__).resolve().parents[1]
LOG = logging.getLogger('live-quake')

def journal_native_error(name, pid, line):
    text = line.decode('utf-8', errors='replace').strip()
    if text.startswith(('QUAKE ERROR:', 'Host_Error:', 'Sys_Error:')):
        LOG.error('Native %s pid=%s: %s', name, pid, text[:2048])

PUBLIC_NATIVE_FAILURES = {
    'native game startup failed; see engine.log': 'native game startup failed; see engine.log',
    'native game startup timed out': 'native game startup timed out',
    'ERR save unavailable': 'save unavailable',
    'checkpoint restoration is waiting; try again': 'checkpoint restoration is waiting; try again',
    'checkpoint restoration timed out; see logs': 'checkpoint restoration timed out; see logs',
    'ERR load transport unavailable': 'checkpoint transport unavailable; see logs',
    'ERR load interrupted': 'checkpoint restoration interrupted; see logs',
}
DEFAULTS = {
    'version': 1, 'sound': {'playing': True, 'screensaver': False, 'idle': False},
    'wallpaper': {'enabled': False, 'mode': 'empty', 'hide_chatter': False}, 'screensaver': {'enabled': False},
    'blur': {'percent': 20}, 'mouse': {'sensitivity': 1.0, 'invert': False},
    'game': {'mode': 'deathmatch', 'skill': 1, 'selected_package': 'librequake', 'human_viewsize': 100.0,
             'dm_timelimit': 6, 'dm_fraglimit': 25, 'dm_bots': 4, 'dm_disabled_maps': [], 'dm_enabled_sp_maps': []},
}
# Opt-in single-player arenas: every id episode map has at least 4 DM starts.
ID_SP_MAPS = tuple(f'e{episode}m{level}' for episode, level in
                   [(1, n) for n in range(1, 9)] + [(2, n) for n in range(1, 8)] +
                   [(3, n) for n in range(1, 8)] + [(4, n) for n in range(1, 9)])
DM_MAP_NAME = re.compile(r'[a-z0-9_]{1,14}')
PACKAGE_RESTORE_NOTES = {
    'invalid': 'Saved game choice is invalid; using LibreQuake.',
    'catalog': 'Game catalogue unavailable; using LibreQuake.',
    'missing': 'Saved game is missing; using LibreQuake.',
    'incompatible': 'Saved game is incompatible; using LibreQuake.',
    'timeout': 'Saved game lookup timed out; using LibreQuake.',
    'scan': 'Steam scan failed; using LibreQuake.',
    'validation': 'Saved game validation failed; using LibreQuake.',
}

def gpu_render_size(width, height):
    if (type(width) is not int or type(height) is not int or not 1 <= width <= 131072
            or not 1 <= height <= 131072):
        width, height = 1280, 720
    scale = min(1, 3840 / width, 2160 / height)
    return max(320, int(width * scale) // 8 * 8), max(200, int(height * scale))

def owned_directory(path):
    path = Path(path).absolute()
    if path.resolve() != path:
        raise ValueError('directory must be canonical')
    path.mkdir(mode=0o700, exist_ok=True)
    info = path.stat()
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise ValueError('directory must be user-owned mode 0700')
    return path

def bounded_json(path, maximum=65536):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > maximum or info.st_uid != os.getuid():
            raise ValueError('invalid settings file')
        data = os.read(fd, maximum + 1)
        if len(data) > maximum:
            raise ValueError('settings too large')
        return json.loads(data)
    finally:
        os.close(fd)

def atomic_json(path, value):
    fd, name = tempfile.mkstemp(dir=path.parent, prefix='.' + path.name + '-')
    try:
        with os.fdopen(fd, 'w') as output:
            json.dump(value, output, indent=2, allow_nan=False)
            output.write('\n')
            output.flush()
            os.fsync(output.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)

def request(path, command, timeout=.5):
    if len(command.encode()) > 8192 or '\n' in command:
        raise ValueError('invalid command')
    with socket.socket(socket.AF_UNIX) as client:
        client.settimeout(timeout)
        client.connect(str(path))
        client.sendall((command + '\n').encode())
        payload = bytearray()
        while b'\n' not in payload:
            chunk = client.recv(16384)
            if not chunk:
                break
            payload.extend(chunk)
            if len(payload) > 1024 * 1024:
                raise ValueError('response too large')
        if not payload.endswith(b'\n'):
            raise RuntimeError('connection closed without a complete reply')
        return payload.decode().strip()

def validate_setting(key, value):
    if key in ('sound.playing', 'sound.idle', 'sound.screensaver', 'wallpaper.enabled',
               'screensaver.enabled', 'mouse.invert', 'wallpaper.hide_chatter'):
        valid = type(value) is bool
    elif key == 'wallpaper.mode':
        valid = value in ('empty', 'all')
    elif key == 'blur.percent':
        valid = type(value) is int and 0 <= value <= 100 and value % 5 == 0
    elif key == 'mouse.sensitivity':
        valid = type(value) in (int, float) and .1 <= value <= 4 and math.isfinite(value) and abs(value*10-round(value*10)) < 1e-6
    elif key == 'game.human_viewsize':
        valid = type(value) in (int, float) and 30 <= value <= 130 and math.isfinite(value)
    elif key == 'game.skill':
        valid = type(value) is int and 0 <= value <= 3
    elif key == 'game.mode':
        valid = value in ('single', 'deathmatch')
    elif key == 'game.dm_timelimit':
        valid = type(value) is int and 1 <= value <= 30
    elif key == 'game.dm_fraglimit':
        valid = type(value) is int and (value == 0 or 5 <= value <= 100)
    elif key == 'game.dm_bots':
        valid = type(value) is int and 1 <= value <= 7
    elif key in ('game.dm_disabled_maps', 'game.dm_enabled_sp_maps'):
        valid = (type(value) is list and len(value) <= 64 and len(set(value)) == len(value)
                 and all(type(name) is str and DM_MAP_NAME.fullmatch(name) for name in value))
        if valid:
            return sorted(value)
    else:
        raise ValueError('setting is unknown or read-only')
    if not valid:
        raise ValueError('setting value is out of range')
    return round(value, 1) if key == 'mouse.sensitivity' else value

class Controller:
    def __init__(self, runtime, state, build, pak, environment, observer, start_map='lq_e0m1',
                 *, background=None, menu_launcher=None, idle=None, audio_mode='discard', watchdog=None,
                 catalog=None, catalog_home=None, policy_log=None, bypass=None,
                 allow_incomplete_dm=False, choose_dm_map=None):
        # The production daemon never enables this private-probe override.
        # Step 8 makes DM available only after the complete round/map review.
        self.allow_incomplete_dm = allow_incomplete_dm
        self.choose_dm_map = choose_dm_map or secrets.choice
        self.engine_mode = None
        self.takeover_refused = False
        self.dm_failed_package = None
        self.dm_failed_reason = ''
        self.gpu_capture_available = False
        self.gpu_capture_reason = 'OpenGL capture is starting.'
        self.gpu_capture_deadline = 0
        self.gpu_capture_report = None
        self.policy_log = policy_log
        self.diagnostic_failures = 0
        self.diagnostic_generation = 0
        self.diagnostic_open = False
        self.diagnostic_ack = [None, None, None]
        self.engine_backend = 'ironwail'
        self.gpu_generation = 0
        self.gpu_saver_active = False
        self.viewer_ready_heads = set()
        self.viewer_ready_identity = None
        self.saver_topology = None
        self.saver_cycle_counter = 0
        self.saver_cycle_id = 0
        self.saver_participants = set()
        self.saver_presented_heads = set()
        self.saver_present_deadline = 0
        self.saver_acknowledged = False
        self.saver_readiness_note = None
        self.runtime = owned_directory(runtime)
        self.state = owned_directory(state)
        started_before = os.path.lexists(self.state / 'config.json')
        self.build, self.pak = Path(build).resolve(), Path(pak).resolve()
        self.data = validate_manual_data(self.pak.parent.parent, self.pak.parent.name.casefold(), backend=self.engine_backend, start_map=start_map)
        self.farm = None
        self.environment = dict(environment)
        self.observer = observer
        self.background = background
        self.menu_launcher = menu_launcher
        self.idle = idle
        self.watchdog = watchdog if watchdog is not None else BotWatchdog()
        self.bypass = bypass if bypass is not None else BotLevelBypass()
        self.saver_preview = False
        if audio_mode not in ('discard', 'alsa'):
            raise ValueError('invalid audio mode')
        self.audio_mode = audio_mode
        self.audio_status = None
        self.audio_status_at = 0
        self.start_map = start_map
        if catalog is not None and catalog_home is not None:
            raise ValueError('choose one catalog source')
        self.catalog = catalog
        if catalog_home is not None:
            self.catalog = GameCatalog(catalog_home, {**self.data, 'id': 'librequake',
                'title': 'LibreQuake 0.09-beta', 'start_map': start_map}, backend=self.engine_backend)
        self.selection = None
        self.game_state = (owned_directory(self.state / 'ironwail-v2-default'))
        self.native_deadline = None
        self.selection_worker = None
        self.package_restore = None
        self.package_restore_worker = None
        self.package_activation = None
        self.selection_note = ''
        self.switch_blocked = False
        self.switching = False
        self.engine_committed = self.catalog is None
        if self.catalog is not None:
            self.bind_selection(self.catalog.resolve('librequake'))
        self.config = copy.deepcopy(DEFAULTS)
        config_error = None
        try:
            saved = bounded_json(self.state / 'config.json')
            if not isinstance(saved, dict):
                raise ValueError('settings document must be an object')
            for group in DEFAULTS:
                if isinstance(DEFAULTS[group], dict):
                    if not isinstance(saved.get(group, {}), dict):
                        config_error = 'invalid settings group ignored: ' + group
                        continue
                    for name, value in saved.get(group, {}).items():
                        if group == 'game' and name == 'selected_package':
                            if known_package_id(value):
                                self.config[group][name] = value
                            else:
                                self.selection_note = PACKAGE_RESTORE_NOTES['invalid']
                                config_error = 'invalid saved game ignored'
                            continue
                        try:
                            self.config[group][name] = validate_setting(f'{group}.{name}', value)
                        except ValueError:
                            config_error = f'invalid settings key ignored: {group}.{name}'
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as error:
            self.config = copy.deepcopy(DEFAULTS)
            LOG.warning('Invalid settings ignored: %s', error)
            config_error = 'invalid settings ignored; see logs'
            fd, name = tempfile.mkstemp(dir=self.state, prefix='config.invalid-')
            os.close(fd)
            try:
                os.replace(self.state / 'config.json', name)
            except OSError:
                Path(name).unlink(missing_ok=True)
        self.engine = self.viewer = None
        self.retired_children = []
        self.snapshot = Snapshot()
        self.human = None
        self.human_workspace = None
        self.generation = 0
        self.input_client = None
        self.menu_until = 0
        self.manual_pause = False
        self.reason = 'disabled'
        self.engine_settings = None
        self.checkpoint_restore_pending = None
        self.human_quits_seen = 0
        self.last_error = config_error
        self.retry_at = 0
        self.failure_count = 0
        self.gpu_recovery_blocked = False
        self.engine_started = 0
        self.saver_owner_seen = None
        self.last_checkpoint = time.monotonic()
        self.selector = selectors.DefaultSelector()
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        socket_path = self.runtime / 'control.sock'
        try:
            prior = socket_path.lstat()
            if not stat.S_ISSOCK(prior.st_mode) or prior.st_uid != os.getuid():
                raise ValueError('invalid existing control socket')
            with socket.socket(socket.AF_UNIX) as probe:
                probe.settimeout(.2)
                try:
                    probe.connect(str(socket_path))
                except ConnectionRefusedError:
                    current = socket_path.lstat()
                    if (prior.st_dev, prior.st_ino) != (current.st_dev, current.st_ino):
                        raise RuntimeError('control socket changed')
                    socket_path.unlink()
                else:
                    raise RuntimeError('live quake controller is already running')
        except FileNotFoundError:
            pass
        self.server.bind(str(self.runtime / 'control.sock'))
        os.chmod(self.runtime / 'control.sock', 0o600)
        self.server.listen(32)
        self.server.setblocking(False)
        self.selector.register(self.server, selectors.EVENT_READ, None)
        self.clients = {}
        self.running = True
        self.diagnostic_engine('stopped')  # Initial generation-zero anchor.
        self.begin_package_restore()
        if onboarding is not None:  # Only the instance that owns the socket creates the guide marker.
            try:
                onboarding.initialize(self.state, fresh=not started_before,
                                      saver=self.config['screensaver']['enabled'])
            except (OSError, ValueError) as error:
                LOG.warning('Setup guide state not created: %s', error)

    def native(self, command):
        if not self.engine or self.engine.poll() is not None:
            raise RuntimeError('engine is unavailable')
        timeout = (5 if command in ('save', 'load', 'load-preserve', 'restart-map', 'skip-map')
                   else .75 if command == 'capture' else .5)
        if self.native_deadline is not None:
            timeout = min(timeout, self.native_deadline-time.monotonic())
            if timeout <= 0:
                raise RuntimeError('game transition timed out')
        reply = request(self.runtime / 'engine.sock', command, timeout=timeout)
        if not (reply == 'OK' or reply.startswith('OK ')):
            raise RuntimeError(reply)
        if command in ('pause 0', 'pause 1', 'bot 0', 'bot 1', 'audio 0', 'audio 1'):
            index = {'pause': 0, 'bot': 1, 'audio': 2}[command.split()[0]]
            self.diagnostic_ack[index] = int(command[-1])
        return reply

    def bind_selection(self, selection):
        if ('/' in selection.profile_id or selection.profile_id in ('', '.', '..')
                or len(selection.profile_id) > 150 or selection.gamedir not in ('id1', 'hipnotic', 'rogue')):
            raise ValueError('invalid game profile')
        profiles = owned_directory(self.state / 'profiles')
        self.game_state = owned_directory(profiles / selection.profile_id)
        owned_directory(self.game_state / selection.gamedir)
        self.selection = selection
        self.data = selection.data
        self.pak = selection.paks[0]
        self.start_map = selection.start_map

    def mode_status(self, data=None):
        data = self.data if data is None else data
        if data['gamedir'] != 'id1':
            return False, 'Deathmatch is unavailable for mission packs.'
        if tuple(data['paks']) == self.dm_failed_package:
            return False, self.dm_failed_reason
        if not self.available_dm_maps(data):
            return False, 'This package has no available deathmatch maps.'
        return True, ''

    def available_dm_maps(self, data=None):
        """Package DM maps (id first), then the installed LibreQuake arena pack."""
        data = self.data if data is None else data
        if data['gamedir'] != 'id1':
            return []
        maps = [name for name in data.get('dm_maps', ()) if DM_MAP_NAME.fullmatch(name)]
        return maps + [name for name in installed_lqdm_maps(self.build) if name not in maps]

    def available_sp_maps(self, data=None):
        data = self.data if data is None else data
        if data['gamedir'] != 'id1' or not data.get('registered_data_present'):
            return []
        return list(ID_SP_MAPS)

    def enabled_dm_maps(self, data=None):
        game = self.config['game']
        maps = [name for name in self.available_dm_maps(data) if name not in game['dm_disabled_maps']]
        maps += [name for name in self.available_sp_maps(data) if name in game['dm_enabled_sp_maps']]
        return maps or self.available_dm_maps(data)

    def effective_mode(self, data=None, requested=None):
        requested = self.config['game']['mode'] if requested is None else requested
        return 'deathmatch' if requested == 'deathmatch' and self.mode_status(data)[0] else 'single'

    def first_dm_map(self):
        maps = self.enabled_dm_maps()
        preferred = ([name for name in maps if name.startswith('dm')]
                     or [name for name in maps if name.startswith('lqdm')] or maps)
        return self.choose_dm_map(preferred)

    def dm_engine_skill(self):
        return min(self.config['game']['skill'], 2) if self.engine_mode == 'deathmatch' else self.config['game']['skill']

    def dm_config_command(self):
        game = self.config['game']
        return f"dm-config {','.join(self.enabled_dm_maps())} {game['dm_timelimit']} {game['dm_fraglimit']} {game['dm_bots']}"

    def change_mode(self, value):
        if self.human is not None or self.package_restore is not None or self.checkpoint_restore_pending is not None or self.switching:
            raise ValueError('mode change unavailable while playing or loading')
        previous = self.engine_mode or self.effective_mode()
        target = self.effective_mode(requested=value)
        if target != previous:
            if self.catalog is None:
                raise ValueError('mode switching requires the game catalogue')
            self.use_game(self.selection.id, mode=value, mode_only=True)
            return json.dumps(self.settings(), separators=(',', ':'))
        updated = copy.deepcopy(self.config)
        updated['game']['mode'] = value
        atomic_json(self.state / 'config.json', updated)
        self.config = updated
        self.update()
        return json.dumps(self.settings(), separators=(',', ':'))

    @staticmethod
    def dispose_package_farm(farm):
        if farm is not None:
            try:
                farm.cleanup()
            except (OSError, ValueError, RuntimeError) as error:
                LOG.warning('Saved game staging cleanup failed: %s', error)

    def cancel_package_restore(self, *, note=''):
        attempt = self.package_restore
        if attempt is not None:
            with attempt['guard']:
                attempt['cancelled'].set()
                result = attempt.pop('result', None)
                self.package_restore = None
            if result is not None:
                self.dispose_package_farm(result.get('farm'))
            self.selection_note = note
        self.package_activation = None

    def fail_package_restore(self, reason):
        fallback = self.package_restore['fallback']
        self.cancel_package_restore(note=PACKAGE_RESTORE_NOTES[reason])
        self.selection, self.data, self.pak, self.start_map, self.game_state = fallback

    def begin_package_restore(self):
        identifier = self.config['game']['selected_package']
        if identifier == 'librequake':
            return
        if self.catalog is None:
            self.selection_note = PACKAGE_RESTORE_NOTES['catalog']
            return
        self.package_restore = {'id': identifier, 'deadline': time.monotonic()+5,
            'phase': 'scan', 'guard': threading.Lock(), 'cancelled': threading.Event(),
            'complete': threading.Event(),
            'fallback': (self.selection, self.data, self.pak, self.start_map, self.game_state)}
        self.selection_note = 'Checking the saved game.'
        try:
            self.catalog.scan()
        except (OSError, ValueError, RuntimeError):
            self.fail_package_restore('scan')

    def restage_package_activation(self):
        self.package_restore = {'id': self.selection.id, 'deadline': time.monotonic()+5,
            'phase': 'stage', 'guard': threading.Lock(), 'cancelled': threading.Event(),
            'complete': threading.Event(), 'fallback': self.package_activation,
            'success_note': self.selection_note}
        self.selection_note = 'Checking the saved game.'

    def poll_package_restore(self):
        """Restore only the exact saved catalogue ID; never wait for file reads."""
        attempt = self.package_restore
        if attempt is None:
            return True
        if time.monotonic() >= attempt['deadline']:
            self.fail_package_restore('timeout')
            return True
        if attempt['phase'] == 'scan':
            try:
                catalog = self.catalog
                state = catalog.settings()
                if state['scanning']:
                    return False
                if state['scan_error']:
                    reason = ('timeout' if state['scan_error'] == 'Steam scan timed out; kept the old list.'
                              else 'scan')
                    self.fail_package_restore(reason)
                    return True
                row = next((row for row in state['catalog'] if row['id'] == attempt['id']), None)
                if row is None or not row['playable']:
                    self.fail_package_restore('missing' if row is None else 'incompatible')
                    return True
            except (OSError, ValueError, RuntimeError):
                self.fail_package_restore('scan')
                return True
            if time.monotonic() >= attempt['deadline']:
                self.fail_package_restore('timeout')
                return True
            attempt['phase'] = 'stage'
        if attempt['phase'] == 'stage':
            attempt['phase'] = 'validate'
            catalog = self.catalog
            content, backend = self.state / 'content', self.engine_backend
            def restore():
                farm, selection, error = None, None, False
                try:
                    selection = catalog.resolve(attempt['id'])
                    if attempt['cancelled'].is_set() or time.monotonic() >= attempt['deadline']:
                        return
                    farm = create_farm(selection.data, owned_directory(content), backend=backend)
                    if attempt['cancelled'].is_set() or time.monotonic() >= attempt['deadline']:
                        return
                    if revalidate_selection(selection) != selection:
                        raise CatalogError('saved package changed during validation')
                except Exception as problem:
                    LOG.warning('Saved game could not be restored: %s', problem)
                    error = True
                finally:
                    with attempt['guard']:
                        if not attempt['cancelled'].is_set() and time.monotonic() < attempt['deadline']:
                            attempt['result'] = {'selection': selection, 'farm': farm, 'error': error}
                            farm = None
                            attempt['complete'].set()
                    self.dispose_package_farm(farm)
            self.package_restore_worker = threading.Thread(target=restore, daemon=True,
                                                           name='live-quake-package-restore')
            try:
                self.package_restore_worker.start()
            except RuntimeError:
                self.fail_package_restore('validation')
                return True
        if not attempt['complete'].is_set():
            return False
        with attempt['guard']:
            result = attempt.pop('result')
        if attempt['cancelled'].is_set() or time.monotonic() >= attempt['deadline']:
            self.dispose_package_farm(result['farm'])
            self.fail_package_restore('timeout')
            return True
        if result['error']:
            self.dispose_package_farm(result['farm'])
            self.fail_package_restore('validation')
            return True
        fallback = (self.selection, self.data, self.pak, self.start_map, self.game_state)
        try:
            self.bind_selection(result['selection'])
            if time.monotonic() >= attempt['deadline']:
                self.selection, self.data, self.pak, self.start_map, self.game_state = fallback
                self.dispose_package_farm(result['farm'])
                self.fail_package_restore('timeout')
                return True
        except (OSError, ValueError, RuntimeError):
            self.selection, self.data, self.pak, self.start_map, self.game_state = fallback
            self.dispose_package_farm(result['farm'])
            self.fail_package_restore('validation')
            return True
        self.farm = result['farm']
        self.package_activation = attempt['fallback']
        self.package_restore = None
        self.selection_note = attempt.get('success_note', '')
        return True

    def persist_selection(self, mode=None, *, mode_only=False):
        updated = copy.deepcopy(self.config)
        if not mode_only:
            updated['game']['selected_package'] = self.selection.id
        if mode is not None:
            updated['game']['mode'] = mode
        atomic_json(self.state / 'config.json', updated)
        self.config = updated
        if not mode_only:
            self.selection_note = ''

    def commit_session(self):
        if self.viewer is None or self.viewer.poll() is not None:
            raise RuntimeError('game viewer is unavailable')
        self.native('commit-session')
        self.engine_committed = True

    def validate_selection(self, function, deadline, *, dispose=None):
        """One staging worker; a blocked kernel read never freezes policy.

        The command waits at most five seconds while its game is paused. A
        late result is never applied. A late private farm is safely cleaned.
        A still-blocked worker prevents overlapping validation attempts.
        """
        if self.selection_worker is not None and self.selection_worker.is_alive():
            raise CatalogError('game validation is still finishing; try later')
        complete, result = threading.Event(), {}
        guard, cancelled = threading.Lock(), threading.Event()
        def validate():
            value, error = None, None
            try:
                value = function()
            except CatalogError as problem:
                error = str(problem)
            except Exception:
                error = 'game data validation failed'
            with guard:
                late = cancelled.is_set()
                if not late:
                    if error is not None:
                        result['error'] = error
                    else:
                        result['selection'] = value
                    complete.set()
            if late and value is not None and dispose is not None:
                try:
                    dispose(value)
                except (OSError, ValueError, RuntimeError):
                    pass  # An altered farm stays private for inspection.
        self.selection_worker = threading.Thread(target=validate, daemon=True,
                                                 name='live-quake-selection')
        self.selection_worker.start()
        if not complete.wait(max(0, min(5, deadline-time.monotonic()))):
            with guard:
                if not complete.is_set():
                    cancelled.set()
                    raise CatalogError('game validation timed out; try later')
        if 'error' in result:
            raise CatalogError(result['error'])
        return result['selection']

    @staticmethod
    def checkpoint_receipt(path):
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            before = os.fstat(fd)
            if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid()
                    or not 100 < before.st_size < 16*1024*1024):
                raise RuntimeError('checkpoint is unavailable')
            digest, remaining = hashlib.sha256(), before.st_size
            while remaining:
                block = os.read(fd, min(65536, remaining))
                if not block:
                    raise RuntimeError('checkpoint changed during validation')
                digest.update(block)
                remaining -= len(block)
            after = os.fstat(fd)
            if (before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                    after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                raise RuntimeError('checkpoint changed during validation')
            return digest.hexdigest(), before.st_size
        finally:
            os.close(fd)

    def use_game(self, identifier, *, mode=None, mode_only=False):
        if self.catalog is None:
            raise ValueError('game catalog is unavailable')
        old_mode = self.engine_mode or self.effective_mode()
        if self.human is not None and (mode is not None or old_mode == 'deathmatch'):
            raise ValueError('game change unavailable while playing')
        if self.switching or (mode is not None and (self.package_restore is not None or self.checkpoint_restore_pending is not None)):
            raise ValueError('mode change unavailable while loading')
        if (mode is None and self.config['game']['mode'] == 'single' and old_mode == 'single'
                and self.engine is not None and self.engine.poll() is None
                and self.checkpoint_restore_pending is None
                and read_frame(self.runtime / 'frame.bin').get('health', 0) <= 0):
            raise CatalogError('player is down; close the menu, then retry')
        self.cancel_package_restore(note='')
        deadline = time.monotonic() + 30
        phase_deadline = deadline - 12  # Reserve bounded teardown/restore time.
        old = (self.selection, self.data, self.pak, self.start_map, self.game_state)
        loaded = bool(self.engine and self.engine.poll() is None)
        pending_restore = self.checkpoint_restore_pending
        retired = False
        completed = False
        recovery = (self.gpu_recovery_blocked, self.failure_count, self.retry_at)
        dm_failure = (self.dm_failed_package, self.dm_failed_reason)
        staged = None
        saved_world = receipt = None
        dead_single = False
        self.native_deadline = phase_deadline
        self.switching = True
        try:
            if loaded:
                self.native('pause 1')
                if pending_restore is None and old_mode == 'single':
                    current = read_frame(self.runtime / 'frame.bin')
                    dead_single = current.get('health', 0) <= 0
                    if current.get('gamestate') != 0 or not current.get('map'):
                        raise CatalogError('game is not ready; close the menu and retry')
            catalog = self.catalog
            selection = self.validate_selection(lambda: catalog.resolve(identifier), phase_deadline)
            target_mode = self.effective_mode(selection.data, mode)
            if self.human is not None and target_mode != old_mode:
                raise ValueError('game change unavailable while playing')
            if dead_single and target_mode == old_mode:
                raise CatalogError('player is down; close the menu, then retry')
            self.revoke()
            if self.idle is not None and self.saver_active():
                self.idle.dismissed()
            self.clear_saver()
            self.engine_settings = None
            self.reason = 'switch'
            content = self.state / 'content'
            staged = self.validate_selection(lambda: create_farm(selection.data, owned_directory(content), backend=self.engine_backend),
                                             phase_deadline, dispose=lambda farm: farm.cleanup())
            checked = self.validate_selection(lambda: revalidate_selection(selection), phase_deadline)
            if checked != selection:
                raise CatalogError('game package changed; scan Steam again')

            if selection == self.selection and target_mode == old_mode and not self.switch_blocked and not self.gpu_recovery_blocked:
                staged.cleanup()
                staged = None
                if pending_restore is not None:
                    raise CatalogError('checkpoint restoration is still waiting; choose another game or try again')
                self.native_deadline = deadline
                if not self.update():
                    raise RuntimeError('current game could not be applied')
                self.gpu_recovery_blocked = False
                self.failure_count = self.retry_at = 0
                self.persist_selection(mode, mode_only=mode_only)
                completed = True
                return 'OK'
            if loaded and old_mode == 'single':
                if pending_restore is None and not dead_single:
                    self.native('save')  # Required before retiring the old player.
                    saved = read_frame(self.runtime / 'frame.bin')
                    saved_world = {key: saved[key] for key in ('map', 'health', 'x', 'y', 'z', 'leveltime')}
                elif pending_restore is not None:
                    saved_world = pending_restore['expected_world']
                checkpoint = self.game_state / self.data['gamedir'] / 'checkpoint.sav'
                if not dead_single or checkpoint.exists():
                    receipt = self.validate_selection(lambda: Controller.checkpoint_receipt(checkpoint), phase_deadline)
            if time.monotonic() >= phase_deadline:
                raise RuntimeError('game transition timed out')
            self.stop_engine(save=False, deadline=phase_deadline)
            retired = True
            if self.retired_children:
                raise RuntimeError('previous owned process did not stop')
            self.bind_selection(selection)
            self.farm, staged = staged, None
            self.switch_blocked = False
            self.gpu_recovery_blocked = False
            self.last_error = None
            if loaded or self.config['wallpaper']['enabled'] or (self.idle is not None and self.idle.armed):
                self.ensure_engine(strict_checkpoint=True, commit=False, requested_mode=mode,
                                   fallback_checkpoint=old_mode == 'deathmatch')
            self.native_deadline = deadline
            if not self.update():  # Fresh lock/workspace observation decides resume.
                raise RuntimeError('selected game could not be applied')
            if self.engine and self.engine.poll() is None:
                self.commit_session()
            self.gpu_recovery_blocked = False
            self.failure_count = self.retry_at = 0
            self.persist_selection(mode, mode_only=mode_only)
            if tuple(selection.data['paks']) != tuple(old[1]['paks']) and self.dm_failed_package == dm_failure[0]:
                self.dm_failed_package = None
                self.dm_failed_reason = ''
            # A committed package change may reuse a map name such as e1m1.
            # Failed switches and ordinary native restarts retain map progress.
            if selection != old[0]:
                self.bypass.reset()
            completed = True
            self.native_deadline = None
            return 'OK'
        except (OSError, ValueError, RuntimeError) as error:
            reason = (str(error) if isinstance(error, CatalogError) else
                      PUBLIC_NATIVE_FAILURES.get(str(error), 'game switch failed'))
            if staged is not None:
                try:
                    staged.cleanup()
                except (OSError, ValueError, RuntimeError):
                    self.last_error = 'private game staging retained for inspection'
            if retired:
                self.native_deadline = deadline
                try:
                    self.stop_engine(save=False, deadline=deadline)
                    self.selection, self.data, self.pak, self.start_map, self.game_state = old
                    self.farm = None
                    if loaded:
                        previous = self.selection
                        self.validate_selection(lambda: revalidate_selection(previous), deadline)
                        if old_mode == 'single' and receipt is not None:
                            checkpoint = self.game_state / self.data['gamedir'] / 'checkpoint.sav'
                            if self.validate_selection(lambda: Controller.checkpoint_receipt(checkpoint), deadline) != receipt:
                                raise RuntimeError('previous checkpoint changed')
                        self.ensure_engine(strict_checkpoint=old_mode == 'single',
                                           require_checkpoint=old_mode == 'single' and receipt is not None,
                                           expected_world=saved_world, commit=False, requested_mode=old_mode)
                        self.commit_session()
                    self.switch_blocked = False
                    self.last_error = 'game switch failed; previous package restored'
                except (OSError, ValueError, RuntimeError):
                    self.stop_engine(save=False, deadline=deadline)
                    self.selection, self.data, self.pak, self.start_map, self.game_state = old
                    self.farm = None
                    self.switch_blocked = True
                    self.reason = 'engine-error'
                    self.last_error = 'game switch and checkpoint restore failed; choose a game again or restart the daemon'
            self.engine_settings = None
            if not retired and self.checkpoint_restore_pending is not None:
                self.reason = 'checkpoint-loading'
            raise CatalogError(reason) from None
        finally:
            if not completed:
                self.gpu_recovery_blocked, self.failure_count, self.retry_at = recovery
                self.dm_failed_package, self.dm_failed_reason = dm_failure
            if self.switch_blocked:
                self.retry_at = 0  # No automatic start can repair a failed restore.
            self.native_deadline = None
            self.switching = False
            self.engine_settings = None

    def launch(self, command, name, *, pass_fds=()):
        # Drain capped logs continuously so an engine diagnostic cannot block
        # the child or grow its logfile indefinitely. Only the pump owns the
        # file descriptor, so truncation cannot create a sparse child output.
        path = self.runtime / (name + '.log')
        fd = os.open(path, os.O_CREAT | os.O_APPEND | os.O_WRONLY | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            os.close(fd)
            raise ValueError('invalid process log')
        try:
            child = subprocess.Popen(command, env=self.environment, stdout=subprocess.PIPE,
                                     stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, pass_fds=pass_fds)
        except Exception:
            os.close(fd)
            raise
        def pump():
            total = info.st_size
            pending = b''
            try:
                while True:
                    chunk = os.read(child.stdout.fileno(), 16384)
                    if not chunk:
                        break
                    lines = (pending + chunk).split(b'\n')
                    pending = lines.pop()[-4096:]
                    for line in lines:
                        journal_native_error(name, child.pid, line)
                    if total + len(chunk) > 1024*1024:
                        os.ftruncate(fd, 0)
                        os.lseek(fd, 0, os.SEEK_SET)
                        total = 0
                    view = memoryview(chunk)
                    while view:
                        view = view[os.write(fd, view):]
                    total += len(chunk)
            except OSError:
                pass
            finally:
                journal_native_error(name, child.pid, pending)
                os.close(fd)
                child.stdout.close()
        threading.Thread(target=pump, daemon=True).start()
        return child

    def restore_checkpoint_bounded(self, command):
        previous_deadline = self.native_deadline
        deadline = previous_deadline or time.monotonic() + 5
        self.native_deadline = deadline
        waiting = False
        try:
            while True:
                if time.monotonic() >= deadline:
                    raise RuntimeError('ERR load retryable')
                try:
                    return self.native(command)
                except TimeoutError:
                    if waiting:
                        raise RuntimeError('ERR load retryable') from None
                    raise
                except RuntimeError as error:
                    if str(error) != 'ERR load retryable':
                        raise
                    waiting = True
                    time.sleep(min(.25, max(0, deadline-time.monotonic())))
        finally:
            self.native_deadline = previous_deadline

    def checkpoint_fallback(self, checkpoint, error):
        # The native loader quarantines incompatible checkpoints. A live
        # fallback world can still be shown; a dead core must start fresh.
        if checkpoint.exists():
            fd, invalid = tempfile.mkstemp(dir=checkpoint.parent, prefix='checkpoint.invalid-')
            os.close(fd)
            os.replace(checkpoint, invalid)
        LOG.warning('Checkpoint not restored: %s', error)
        self.last_error = 'checkpoint not restored; starting fresh'
        if self.engine.poll() is not None:
            raise RuntimeError(self.last_error) from error

    def ensure_engine(self, *, strict_checkpoint=False, require_checkpoint=False,
                      expected_world=None, commit=True, requested_mode=None,
                      fallback_checkpoint=False):
        if self.package_activation is not None and self.package_restore is None and self.farm is None:
            self.restage_package_activation()
        if not self.poll_package_restore():
            return False
        self.retired_children = [child for child in self.retired_children if child.poll() is None]
        if self.retired_children:
            raise RuntimeError('a previous owned process has not stopped')
        if self.engine and self.engine.poll() is None:
            pending = self.checkpoint_restore_pending
            if pending is None:
                return True
            if self.viewer and self.viewer.poll() is not None:
                raise RuntimeError('GPU viewer exited; rebuilding the game session')
            current = time.monotonic()
            eligible = (self.snapshot.available and self.snapshot.locked is False and
                        any(output.awake for output in self.snapshot.outputs))
            if pending['eligible'] and eligible:
                pending['remaining'] -= max(0, current-pending['checked_at'])
            pending['checked_at'], pending['eligible'] = current, eligible
            if not eligible:
                return False
            if pending['remaining'] <= 0 or pending['attempts'] >= 120:
                raise RuntimeError('checkpoint restoration timed out; see logs')
            if current < pending['retry_at']:
                return False
            pending['attempts'] += 1
            try:
                self.native(pending['command'])
            except RuntimeError as error:
                if pending['command'] == 'load' and str(error) == 'ERR load failed; started fresh':
                    checkpoint = self.game_state / self.data['gamedir'] / 'checkpoint.sav'
                    self.checkpoint_fallback(checkpoint, error)
                elif str(error) != 'ERR load retryable':
                    raise
                else:
                    finished = time.monotonic()
                    # Charge the request itself even on the first retry after an
                    # unsafe observation; lock changes cannot evade the budget.
                    pending['remaining'] -= max(0, finished-current)
                    pending['checked_at'] = finished
                    pending['retry_at'] = finished + .25
                    return False
            self.checkpoint_restore_pending = None
            return self.complete_engine_start(expected_world=pending['expected_world'], commit=pending['commit'])
        if self.engine or self.viewer:
            self.stop_engine()
        self.engine_settings = None
        self.human_quits_seen = 0
        self.engine_committed = self.catalog is None
        self.audio_status = None
        self.audio_status_at = 0
        if self.selection is not None and self.package_activation is None:
            selection = self.selection
            self.validate_selection(lambda: revalidate_selection(selection),
                                    self.native_deadline or time.monotonic()+5)
        if not self.farm:
            if self.catalog is None:
                self.farm = create_farm(self.data, owned_directory(self.state / 'content'), backend=self.engine_backend)
            else:
                data, content = self.selection.data, self.state / 'content'
                self.farm = self.validate_selection(
                    lambda: create_farm(data, owned_directory(content), backend=self.engine_backend),
                    self.native_deadline or time.monotonic()+5, dispose=lambda farm: farm.cleanup())
        for leaf in ('frame.bin', 'engine.sock'):
            (self.runtime / leaf).unlink(missing_ok=True)
        output = next((o for o in self.snapshot.outputs if o.focused), None)
        output = output or next(iter(self.snapshot.outputs), None)
        width, height = (output.width, output.height) if output else (1280, 720)
        width, height = gpu_render_size(width, height)
        resolution = f'{width}x{height}'
        mode = self.effective_mode(requested=requested_mode)
        self.engine_mode = mode
        first_map = self.first_dm_map() if mode == 'deathmatch' else self.start_map
        command = [str(self.build / 'quake-engine'), '--core',
            str(self.build / 'ironwail/Quake/ironwail_libretro.so'), '--pak', str(self.farm.pak),
            '--state', str(self.game_state), '--frame', str(self.runtime / 'frame.bin'),
            '--socket', str(self.runtime / 'engine.sock'), '--map', first_map, '--mode', mode,
            '--resolution', resolution, '--skill', str(self.dm_engine_skill()),
            '--edition', 'rerelease' if self.data.get('edition') == 'rerelease' else 'original']
        if mode == 'deathmatch':
            game = self.config['game']
            command += ['--dm-maps', ','.join(self.enabled_dm_maps()), '--dm-timelimit', str(game['dm_timelimit']),
                        '--dm-fraglimit', str(game['dm_fraglimit']), '--dm-bots', str(game['dm_bots'])]
        command += (['--audio-mode', 'alsa'] if self.audio_mode == 'alsa' else [])
        command += (['--defer-exit-save', '1'] if self.catalog is not None else [])
        self.diagnostic_generation += 1
        self.diagnostic_open = True
        self.diagnostic_ack = [None, None, None]
        self.diagnostic_engine('starting')
        self.package_activation = None  # Native failures use ordinary renderer recovery.
        def fallback_to_single():
            self.dm_failed_package = tuple(self.data['paks'])
            self.dm_failed_reason = 'Deathmatch could not start; using single player.'
            self.stop_engine(save=False)
            started = self.ensure_engine(strict_checkpoint=strict_checkpoint,
                require_checkpoint=require_checkpoint, expected_world=expected_world,
                commit=commit, requested_mode='single', fallback_checkpoint=True)
            self.last_error = self.dm_failed_reason
            return started
        self.launch_gpu_session(command)
        deadline = time.monotonic() + 5
        if self.native_deadline is not None:
            deadline = min(deadline, self.native_deadline)
        while time.monotonic() < deadline:
            if self.engine.poll() is not None:
                if mode == 'deathmatch' and self.engine.poll() in (3, 4):
                    return fallback_to_single()
                raise RuntimeError('native game startup failed; see engine.log')
            try:
                frame = read_frame(self.runtime / 'frame.bin')
                if frame['pid'] == self.engine.pid and (self.runtime / 'engine.sock').is_socket():
                    break
            except (OSError, ValueError):
                pass
            time.sleep(.02)
        else:
            self.stop_child(self.engine, deadline=self.native_deadline)
            raise RuntimeError('native game startup timed out')
        self.native(f'skill {self.dm_engine_skill()}')
        self.native(f'hide-chatter {int(self.config["wallpaper"]["hide_chatter"])}')
        self.apply_human_viewsize()
        if mode == 'deathmatch':
            return self.complete_engine_start(commit=commit)
        checkpoint = self.game_state / self.data['gamedir'] / 'checkpoint.sav'
        load_command = 'load-preserve' if strict_checkpoint and self.catalog is not None else 'load'
        try:
            info = checkpoint.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or not 100 < info.st_size < 16*1024*1024:
                raise ValueError('invalid native checkpoint')
            if strict_checkpoint:
                self.restore_checkpoint_bounded(load_command)
            else:
                self.native(load_command)
        except FileNotFoundError:
            if require_checkpoint:
                raise RuntimeError('previous checkpoint is unavailable') from None
        except TimeoutError:
            # An unresponsive native request does not establish save corruption.
            raise
        except (OSError, ValueError, RuntimeError) as error:
            if str(error) in ('ERR load transport unavailable',
                              'ERR load interrupted'):
                # Unavailable readiness, transport or shutdown is not save corruption.
                raise
            if str(error) == 'ERR load retryable':
                if strict_checkpoint:
                    raise RuntimeError('checkpoint restoration is waiting; try again') from None
                current = time.monotonic()
                eligible = (self.snapshot.available and self.snapshot.locked is False and
                            any(output.awake for output in self.snapshot.outputs))
                self.checkpoint_restore_pending = {'command': load_command, 'expected_world': expected_world,
                    'commit': commit, 'retry_at': current+.25, 'remaining': 30.0, 'checked_at': current,
                    'eligible': eligible, 'attempts': 1}
                self.last_error = None
                self.engine_started = current
                return False
            if strict_checkpoint:
                if not fallback_checkpoint:
                    raise RuntimeError('checkpoint could not be restored') from None
                # Leaving DM may expose an invalid single-player checkpoint.
                # Try the ordinary loader, which quarantines it and starts fresh.
                try:
                    self.restore_checkpoint_bounded('load')
                except RuntimeError as fallback:
                    if str(fallback) != 'ERR load failed; started fresh':
                        raise
                    self.checkpoint_fallback(checkpoint, fallback)
                return self.complete_engine_start(expected_world=expected_world, commit=commit)
            self.checkpoint_fallback(checkpoint, error)
        return self.complete_engine_start(expected_world=expected_world, commit=commit)

    def complete_engine_start(self, *, expected_world=None, commit=True):
        if expected_world is not None:
            restored = read_frame(self.runtime / 'frame.bin')
            if any(restored.get(key) != value for key, value in expected_world.items()):
                raise RuntimeError('previous checkpoint restored a different world')
        if self.catalog is not None and commit:
            self.commit_session()
        self.engine_started = time.monotonic()
        if self.gpu_capture_report is None:
            self.gpu_capture_reason = 'OpenGL capture is starting.'
            self.gpu_capture_deadline = self.engine_started + 5
        self.retry_at = 0
        self.gpu_recovery_blocked = False
        if not self.last_error or not self.last_error.startswith('checkpoint not restored'):
            self.last_error = None
        self.diagnostic_engine('ready')
        return True

    def saver_active(self):
        return self.gpu_saver_active

    def clear_saver(self):
        self.gpu_saver_active = False
        self.saver_preview = False
        self.saver_cycle_id = 0
        self.saver_participants.clear()
        self.saver_presented_heads.clear()
        self.saver_present_deadline = 0
        self.saver_acknowledged = False

    def start_saver(self, *, preview=False):
        if self.package_restore is not None:
            raise ValueError('screensaver unavailable')
        if self.saver_active():
            return
        self.saver_cycle_counter += 1
        self.saver_cycle_id = self.saver_cycle_counter
        self.saver_participants = self.awake_saver_heads()
        self.saver_presented_heads.clear()
        self.saver_acknowledged = False
        self.saver_present_deadline = time.monotonic() + 2
        if self.idle is not None:
            self.snapshot = self.idle.ready(self.snapshot, self.config['screensaver']['enabled'],
                eligible=True, reason_code=None, state='presenting', cycle_id=self.saver_cycle_id,
                engine_generation=self.diagnostic_generation)
            if not self.idle.armed or self.snapshot.saver_owner != 'live-quake':
                self.clear_saver()
                raise ValueError('screensaver unavailable')
            self.idle.started()
            if not self.idle.armed:
                self.clear_saver()
                raise ValueError('screensaver unavailable')
        self.gpu_saver_active = True
        self.saver_preview = preview

    def launch_gpu_session(self, command):
        """Each session owns new endpoints, render fd and buffer generation.

        The viewer must negotiate formats before the core initializes EGL.
        Parent descriptors are closed even on a partial startup failure.
        """
        self.gpu_generation = secrets.randbits(64) or 1
        self.gpu_capture_available = False
        self.gpu_capture_reason = 'OpenGL capture is starting.'
        self.gpu_capture_deadline = 0
        self.gpu_capture_report = None
        viewer_end, engine_end = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        render_fd = None
        try:
            render_fd = os.open('/dev/dri/renderD128', os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW)
            info = os.fstat(render_fd)
            if not stat.S_ISCHR(info.st_mode):
                raise ValueError('GPU render node is unavailable')
            viewer_command = self.viewer_command() + ['--gpu-fd', str(viewer_end.fileno()),
                                      '--gpu-generation', str(self.gpu_generation)]
            self.viewer = self.launch(viewer_command, 'viewer', pass_fds=(viewer_end.fileno(),))
            command += ['--gpu-fd', str(engine_end.fileno()), '--render-fd', str(render_fd),
                        '--gpu-generation', str(self.gpu_generation)]
            self.engine = self.launch(command, 'engine', pass_fds=(engine_end.fileno(), render_fd))
        except BaseException:
            self.stop_child(self.viewer, deadline=self.native_deadline)
            self.viewer = None
            raise
        finally:
            viewer_end.close()
            engine_end.close()
            if render_fd is not None:
                os.close(render_fd)

    def viewer_command(self):
        command = [str(self.build / 'quake-viewer'), '--frame', str(self.runtime / 'frame.bin'),
                   '--socket', str(self.runtime / 'control.sock')]
        if self.config['screensaver']['enabled']:
            command += ['--saver-generation', str(self.diagnostic_generation)]
        return command

    @staticmethod
    def stop_child(child, *, deadline=None):
        if not child or child.poll() is not None:
            return
        child.terminate()
        try:
            child.wait(timeout=5 if deadline is None else max(0, min(5, deadline-time.monotonic())))
        except subprocess.TimeoutExpired:
            child.kill()
            try:
                child.wait(timeout=3 if deadline is None else max(.05, min(3, deadline-time.monotonic())))
            except subprocess.TimeoutExpired as error:
                raise RuntimeError('owned process did not exit after kill') from error

    @staticmethod
    def parse_human_viewsize(reply):
        # The native reply is a bounded scalar, independent of the integer
        # diagnostic viewsize in frame.bin. Reject malformed or nonfinite data.
        if len(reply) > 32 or not reply.startswith('OK '):
            raise ValueError('invalid screen size reply')
        value = float(reply[3:])
        return validate_setting('game.human_viewsize', value)

    def apply_human_viewsize(self):
        try:
            self.parse_human_viewsize(self.native(
                f'human-viewsize {self.config["game"]["human_viewsize"]:.9g}'))
        except (OSError, ValueError, RuntimeError) as error:
            LOG.warning('Screen size applies on next game start: %s', error)
            self.last_error = 'screen size applies on next game start'

    def remember_human_viewsize(self):
        # Native Quit already switched to bot ownership before this query;
        # the adapter returns its saved human value in that case, never the forced120.
        try:
            value = self.parse_human_viewsize(self.native('human-viewsize'))
            stored = self.config['game']['human_viewsize']
            # A float32 seed roundtrip is not a user preference change.
            seeded = float(f'{stored:.9g}')
            seeded = struct.unpack('!f', struct.pack('!f', seeded))[0]
            expected = float(f'{seeded:.9g}')
            if value != expected:
                updated = copy.deepcopy(self.config)
                updated['game']['human_viewsize'] = value
                self.config = updated  # Retain the session even if disk persistence fails.
                atomic_json(self.state / 'config.json', updated)
        except (OSError, ValueError, RuntimeError) as error:
            LOG.warning('Human screen size could not be saved: %s', error)
            self.last_error = 'screen size could not be saved'

    def revoke(self):
        if self.human is not None:
            self.human = self.human_workspace = None
            self.generation += 1
            if self.engine and self.engine.poll() is None:
                self.remember_human_viewsize()
                try:
                    self.native('bot 1')
                except (OSError, ValueError, RuntimeError):
                    self.engine_settings = None
        self.input_client = None

    def stop_engine(self, *, save=True, deadline=None):
        if deadline is None:
            deadline = self.native_deadline
        self.watchdog.reset()
        self.bypass.cancel()
        self.revoke()
        errors = []
        def stop(child):
            try:
                if deadline is None:
                    self.stop_child(child)
                else:
                    self.stop_child(child, deadline=deadline)
            except (OSError, RuntimeError) as error:
                LOG.warning('Owned process cleanup failed: %s', error)
                errors.append(str(error))
                if child and child.poll() is None and child not in self.retired_children:
                    self.retired_children.append(child)
        self.gpu_saver_active = False
        self.saver_cycle_id = 0
        self.saver_present_deadline = 0
        self.saver_participants.clear()
        self.saver_presented_heads.clear()
        self.saver_acknowledged = False
        if self.engine and self.engine.poll() is None:
            dm = self.engine_mode == 'deathmatch'
            abandoned = dm or (self.catalog is not None and (not save or not self.engine_committed))
            try:
                self.native('pause 1')
                if save and self.engine_committed and not dm:
                    self.native('save')
                elif dm or self.catalog is not None:
                    self.native('discard-session')
            except (OSError, ValueError, RuntimeError):
                if abandoned:
                    self.engine.kill()  # Unacknowledged discard must never auto-save on SIGTERM.
        stopped_engine = self.engine
        stop(self.engine)
        stop(self.viewer)
        self.engine = self.viewer = None
        self.engine_mode = None
        self.viewer_ready_heads.clear()
        self.viewer_ready_identity = None
        self.checkpoint_restore_pending = None
        self.gpu_capture_available = False
        self.gpu_capture_reason = 'OpenGL capture is unavailable; see logs.'
        self.gpu_capture_deadline = 0
        self.gpu_capture_report = None
        self.engine_settings = None
        self.diagnostic_ack = [None, None, None]
        if stopped_engine is not None or self.diagnostic_open:
            self.diagnostic_open = False
            self.diagnostic_engine('retired' if stopped_engine in self.retired_children else 'stopped')
        self.audio_status = None
        self.audio_status_at = 0
        if self.farm:
            # A refused cleanup means the view changed. Keep it for inspection
            # and detach it before cleanup so no later engine reuses its files.
            farm, self.farm = self.farm, None
            if stopped_engine in self.retired_children:
                self.last_error = 'owned engine did not stop; private content retained'
                return
            try:
                farm.cleanup()
            except (OSError, ValueError, RuntimeError) as error:
                LOG.warning('Private farm cleanup refused at %s: %s', farm.root, error)
                cleanup_error = 'private farm cleanup refused; retained for inspection'
                self.last_error = ((self.last_error[:100] + '; ' + cleanup_error[:154])
                                   if self.last_error else cleanup_error[:256])
        if errors:
            self.last_error = 'owned process cleanup failed; see logs'

    def schedule_runtime_retry(self, message):
        """Leave controls alive, but stop repeatedly probing a broken GPU."""
        self.failure_count += 1
        delay = min(30, 2 ** min(self.failure_count, 5))
        self.gpu_recovery_blocked = self.failure_count >= 3
        self.retry_at = 0 if self.gpu_recovery_blocked else time.monotonic() + delay
        self.last_error = ('OpenGL stopped repeatedly. Choose Retry OpenGL.'
                           if self.gpu_recovery_blocked else message)
        return delay

    def update(self):
        try:
            self._update()
            return self.reason != 'engine-error'
        except (OSError, ValueError, RuntimeError) as error:
            LOG.warning('Native runtime unavailable: %s', error)
            self.schedule_runtime_retry(PUBLIC_NATIVE_FAILURES.get(str(error), 'native runtime unavailable; see logs'))
            try:
                self.stop_engine(save=self.checkpoint_restore_pending is None)
            finally:
                self.reason = 'engine-error'
                if self.idle is not None:
                    self.idle.failed()
            return False
        finally:
            self.diagnostic_policy()

    def diagnostic_engine(self, state, *, exit_code=None):
        if self.policy_log is not None:
            try:
                self.policy_log.engine(state, self.diagnostic_generation, exit_code=exit_code)
            except Exception:
                self.diagnostic_failures += 1

    def diagnostic_policy(self):
        if self.policy_log is None:
            return
        try:
            observer = 'available' if self.snapshot.available else (
                'stale' if getattr(self.observer, 'observation_state', None) == 'stale' else 'unavailable')
            self.policy_log.policy(observer, self.reason, tuple(self.diagnostic_ack), self.diagnostic_generation,
                                   show_on=self.background_mode())
            views, refresh_millihz = {}, {}
            for output in self.snapshot.outputs[:128]:
                state = self.view_state(output.name).split()[1]
                reason = self.reason
                if state == 'hidden':
                    if not self.snapshot.available or self.snapshot.locked is None:
                        reason = 'unavailable'
                    elif self.snapshot.locked:
                        reason = 'lock'
                    elif not output.awake:
                        reason = 'asleep'
                    elif not self.config['wallpaper']['enabled']:
                        reason = 'disabled'
                    elif self.saver_active():
                        reason = 'saver'
                    elif not self.gpu_capture_available and output.busy:
                        reason = 'busy'
                elif state == 'paused' and reason is None and output.busy:
                    reason = 'busy'
                views[output.name] = (state, output.busy, reason)
                refresh_millihz[output.name] = output.refresh_millihz if self.snapshot.available else None
            self.policy_log.views(views, refresh_millihz=refresh_millihz)
        except Exception:
            self.diagnostic_failures += 1

    def awake_saver_heads(self):
        return {output.name for output in self.snapshot.outputs if output.awake}

    def sync_saver_readiness(self):
        topology = tuple(sorted((output.name, output.awake, output.width, output.height)
                                for output in self.snapshot.outputs))
        identity = (self.diagnostic_generation, self.viewer)
        if topology != self.saver_topology or identity != self.viewer_ready_identity:
            self.viewer_ready_heads.clear()
            self.saver_presented_heads.clear()
            self.saver_topology = topology
            self.viewer_ready_identity = identity
            if self.saver_active():
                # Reconfiguration cancels this attempt. Its old callbacks must
                # not certify a new surface or extend the fixed deadline.
                if self.idle is not None:
                    self.idle.dismissed()
                self.clear_saver()

    def saver_readiness(self):
        """Pure eligibility classification; absence never grants ownership."""
        if not self.config['screensaver']['enabled'] or (self.idle is not None and not self.idle.allow_claim):
            return False, 'disabled', 'disabled'
        if self.human or self.menu_until or self.manual_pause:
            return False, 'paused', 'blocked'
        if self.package_restore is not None or self.checkpoint_restore_pending is not None or self.switching:
            return False, 'loading', 'warming'
        if not self.snapshot.available or self.snapshot.locked is not False or not self.awake_saver_heads():
            return False, 'desktop-unavailable', 'blocked'
        if self.idle is not None and (not self.idle.idle.available or not self.idle.idle.enabled or self.idle.idle.stay_awake):
            return False, 'idle-unavailable', 'blocked'
        if (self.snapshot.saver_owner == 'unknown' or self.snapshot.saver_owner == 'other' and
                not (self.idle is not None and self.idle.adoption_pending)):
            return False, 'ownership-unavailable', 'blocked'
        if self.retry_at > time.monotonic() or self.gpu_recovery_blocked or self.switch_blocked:
            return False, 'failure-backoff', 'backoff'
        if (not self.engine or self.engine.poll() is not None or not self.viewer or self.viewer.poll() is not None or
                not self.diagnostic_generation or not self.engine_committed or self.engine_settings is None or
                not self.awake_saver_heads().issubset(self.viewer_ready_heads)):
            return False, 'runtime-unavailable', 'warming'
        try:
            frame = read_frame(self.runtime / 'frame.bin')
        except (OSError, ValueError):
            return False, 'runtime-unavailable', 'warming'
        # A valid coherent header is necessary but does not replace restored
        # world, acknowledged ownership/audio commands or configured viewers.
        if (frame.get('pid') != self.engine.pid or not frame.get('frame') or not frame.get('map') or
                frame.get('gamestate') not in (0, 2) or frame.get('bot') != 1 or frame.get('viewsize') != 120 or
                frame.get('console_visible') != 0 or frame.get('crosshair') != 0 or
                frame.get('paused') != self.engine_settings[0] or self.engine_settings[1] != 1):
            return False, 'runtime-unavailable', 'warming'
        return True, None, ('active' if self.saver_acknowledged else 'presenting' if self.saver_active() else 'ready')

    def persist_saver_disable(self):
        reason = self.idle.auto_disable_reason if self.idle is not None else None
        if reason is None or not self.config['screensaver']['enabled']:
            return
        updated = copy.deepcopy(self.config)
        updated['screensaver']['enabled'] = False
        self.config = updated  # Safety remains off even if persistence fails.
        self.clear_saver()
        try:
            atomic_json(self.state / 'config.json', updated)
        except (OSError, ValueError):
            self.last_error = 'screensaver disable could not be saved; see logs'
            LOG.warning('Screensaver disable persistence unavailable')

    def reconcile_saver(self, *, allow_start=False):
        if self.idle is None:
            self.check_saver_deadline()
            return
        eligible, code, state = self.saver_readiness()
        note = (eligible, code, state, self.diagnostic_generation, len(self.viewer_ready_heads),
                len(self.awake_saver_heads()))
        if note != self.saver_readiness_note:
            self.saver_readiness_note = note
            LOG.info('Saver readiness generation=%d ready_heads=%d awake_heads=%d state=%s reason_code=%s',
                     self.diagnostic_generation, len(self.viewer_ready_heads), len(self.awake_saver_heads()), state, code)
        self.snapshot = self.idle.ready(self.snapshot, self.config['screensaver']['enabled'],
            eligible=eligible, reason_code=code, state=state, cycle_id=self.saver_cycle_id,
            engine_generation=self.diagnostic_generation)
        self.saver_owner_seen = self.snapshot.saver_owner
        self.persist_saver_disable()
        if self.saver_active() and (not eligible or self.snapshot.saver_owner != 'live-quake'):
            self.idle.dismissed()
            self.clear_saver()
        self.check_saver_deadline()
        if (allow_start and eligible and not self.saver_active() and self.idle.should_start(
                enabled=self.config['screensaver']['enabled'], locked=self.snapshot.locked)):
            try:
                self.start_saver()
            except ValueError:
                # A saver launch failure uses idle recovery. The healthy game
                # and renderer session must not enter native runtime recovery.
                LOG.warning('Saver presentation launch unavailable generation=%d', self.diagnostic_generation)

    def check_saver_deadline(self):
        if (self.saver_active() and not self.saver_acknowledged and self.saver_present_deadline and
                time.monotonic() >= self.saver_present_deadline):
            LOG.warning('Saver presentation unavailable generation=%d cycle_id=%d reason_code=runtime-unavailable',
                        self.diagnostic_generation, self.saver_cycle_id)
            if self.idle is not None:
                self.idle.failed(presentation=True)
                self.snapshot = self.snapshot_with_saver_owner()
            self.clear_saver()

    def snapshot_with_saver_owner(self):
        from dataclasses import replace
        self.saver_owner_seen = self.idle.owner['owner']
        return replace(self.snapshot, saver_owner=self.saver_owner_seen)

    def apply_engine_policy(self, enabled):
        self.reason = pause_reason(self.snapshot, enabled, self.background_mode(),
                                   self.human, self.saver_active(), self.menu_until > 0, self.manual_pause)
        if self.switching:
            self.reason = 'switch'
        sound = self.config['sound']['playing' if self.human else 'screensaver' if self.saver_active() else 'idle']
        desired = (int(self.reason is not None), int(not self.human), int(sound and self.reason is None))
        if desired != self.engine_settings:
            self.native('pause 1')
            try:
                self.native(f'bot {desired[1]}')
            except RuntimeError as error:
                if str(error) != 'ERR takeover refused':
                    raise
                # The core kept the bot view (for example, the followed bot is
                # dead). Drop the human claim and resume the wallpaper as before.
                LOG.info('Takeover refused by the game')
                self.human = self.human_workspace = None
                self.generation += 1
                self.input_client = None
                self.engine_settings = None
                self.takeover_refused = True
                return self.apply_engine_policy(enabled)
            self.native(f'audio {desired[2]}')
            self.native(f'pause {desired[0]}')
            self.engine_settings = desired
            self.refresh_audio_status(force=True)

    def _update(self):
        package_ready = self.poll_package_restore()
        background_observed = self.background is None
        if self.background is not None:
            # Marker selection is the authority. Persisted prototype switches
            # cannot activate wallpaper on a fresh install or static background.
            try:
                self.config['wallpaper']['enabled'] = self.background.poll()
                background_observed = getattr(self.background, 'selection_available', False) is True
            except (OSError, ValueError, RuntimeError):
                self.config['wallpaper']['enabled'] = False
                self.last_error = 'background observation unavailable'
        try:
            self.snapshot = self.observer()
            if self.snapshot.saver_owner not in (None, 'live-quake', 'other', 'unknown'):
                raise ValueError('invalid screensaver owner observation')
            if self.snapshot.available:
                self.saver_owner_seen = self.snapshot.saver_owner
        except Exception as error:
            self.snapshot = Snapshot()
            LOG.warning('Desktop observation unavailable: %s', error)
            self.last_error = 'desktop observation unavailable'
        if self.idle is not None:
            self.snapshot = self.idle.observe(self.snapshot, self.config['screensaver']['enabled'])
            self.saver_owner_seen = self.snapshot.saver_owner
            self.persist_saver_disable()
        if self.saver_active() and ((self.engine is not None and self.engine.poll() is not None) or
                (self.gpu_saver_active and self.viewer is not None and self.viewer.poll() is not None)):
            if self.idle is not None:
                self.idle.failed(presentation=True)
            self.clear_saver()
        self.sync_saver_readiness()
        current = next((o for o in self.snapshot.outputs if o.name == self.human), None)
        if self.human and (not takeover_allowed(self.snapshot, self.human) or
                           not current or (current.workspace, current.special_workspace) != self.human_workspace):
            self.revoke()
        if self.human and self.engine and self.engine.poll() is None:
            try:
                frame = read_frame(self.runtime / 'frame.bin')
                if frame['human_quits'] != getattr(self, 'human_quits_seen', 0):
                    self.human_quits_seen = frame['human_quits']
                    self.revoke()
            except BlockingIOError:
                pass
            except (OSError, ValueError):
                self.revoke()
        if self.menu_until and time.monotonic() >= self.menu_until:
            self.menu_until = 0
        if self.saver_active() and (self.snapshot.locked is not False or
                           not self.snapshot.available or not any(o.awake for o in self.snapshot.outputs) or self.snapshot.saver_owner in ('other', 'unknown') or
                           (self.idle is not None and self.snapshot.saver_owner != 'live-quake') or
                           (self.idle is not None and not self.saver_preview and self.idle.should_stop(
                               enabled=self.config['screensaver']['enabled'], locked=self.snapshot.locked))):
            if self.idle is not None:
                self.idle.dismissed()
            self.clear_saver()
        enabled = self.config['wallpaper']['enabled']
        warm_saver = self.idle is not None and self.idle.requested
        if not enabled and not self.saver_active() and not warm_saver:
            self.reason = 'disabled'
            self.stop_engine()
            if (background_observed and (not self.config['screensaver']['enabled']) and (self.idle is None or self.idle.auto_disable_reason is None)):
                self.retry_at = self.failure_count = 0
                self.gpu_recovery_blocked = False
                if self.last_error in ('OpenGL stopped repeatedly. Choose Retry OpenGL.', 'Retrying OpenGL.'):
                    self.last_error = None
            self.reconcile_saver()
            return
        if not package_ready:
            self.reason = 'package-loading'
            self.reconcile_saver()
            return
        if self.engine and self.engine.poll() is not None:
            code = self.engine.returncode
            self.diagnostic_engine('exited', exit_code=code)
            if self.checkpoint_restore_pending is None and time.monotonic() - self.engine_started >= 30:
                self.failure_count = 0
            delay = self.schedule_runtime_retry(f'native engine exited ({code}); see logs')
            self.stop_engine()
            if self.idle is not None:
                self.idle.failed()
            if not self.gpu_recovery_blocked:
                self.last_error = f'native engine exited ({code}); retry in {delay} seconds'
        if self.gpu_recovery_blocked:
            self.reason = 'engine-error'
            self.reconcile_saver()
            return
        if not self.engine and time.monotonic() < self.retry_at:
            self.reason = 'engine-error'
            self.reconcile_saver()
            return
        if self.switch_blocked:
            self.reason = 'engine-error'
            self.reconcile_saver()
            return
        if (not self.gpu_capture_available and self.gpu_capture_deadline and (time.monotonic() >= self.gpu_capture_deadline)):
            self.gpu_capture_reason = 'OpenGL capture is unavailable; see logs.'
            self.gpu_capture_deadline = 0
        if self.ensure_engine() is False:
            self.reason = 'package-loading' if self.package_restore is not None else 'checkpoint-loading'
            self.reconcile_saver()
            return
        if time.monotonic() - self.engine_started >= 30:
            self.failure_count = 0
        if self.viewer and self.viewer.poll() is not None:
            # Old DMA-BUF leases cannot be inherited by a replacement viewer.
            raise RuntimeError('GPU viewer exited; rebuilding the game session')
        self.sync_saver_readiness()
        self.apply_engine_policy(enabled)
        self.reconcile_saver(allow_start=True)
        self.apply_engine_policy(enabled)
        if self.watch_bot_progress():
            return
        if self.engine_mode != 'deathmatch' and self.engine_committed and not self.switching and time.monotonic() - self.last_checkpoint >= 30 and not self.human:
            try:
                self.native('save')
            except (OSError, RuntimeError) as error:
                LOG.warning('Checkpoint unavailable: %s', error)
                self.last_error = PUBLIC_NATIVE_FAILURES.get(str(error), 'checkpoint unavailable; see logs')
            else:
                if self.last_error in ('save unavailable', 'checkpoint unavailable; see logs',
                                       'restarted map; checkpoint unavailable'):
                    self.last_error = None
            self.last_checkpoint = time.monotonic()

    def refresh_audio_status(self, force=False):
        """Observe the PCM sink, without making a failed sink stop the game."""
        live = bool(self.engine and self.engine.poll() is None)
        if not live or self.audio_mode == 'discard':
            self.audio_status = {'mode': self.audio_mode, 'ready': False, 'enabled': False,
                                 'errors': 0, 'written': 0}
            return self.audio_status
        now = time.monotonic()
        if not force and self.audio_status is not None and now - self.audio_status_at < 1:
            return self.audio_status
        result = {'mode': self.audio_mode, 'ready': False, 'enabled': False,
                  'errors': 0, 'written': 0}
        try:
            reply = self.native('audio-status')
            if len(reply) > 2048 or not reply.startswith('OK '):
                raise ValueError('invalid audio status')
            words = [word.split('=') for word in reply.split()[1:]]
            if any(len(word) != 2 for word in words):
                raise ValueError('invalid audio status')
            values = dict(words)
            if len(values) != len(words):
                raise ValueError('invalid audio status')
            counters = {}
            required = ('ready', 'enabled', 'errors', 'written')
            optional = ('xruns', 'recoveries', 'eagain', 'short_writes', 'trimmed', 'queued', 'delay', 'buffered')
            for key in (*required, *(key for key in optional if key in values)):
                value = values[key]
                if not value.isascii() or not value.isdigit() or len(value) > 20:
                    raise ValueError('invalid audio status')
                counters[key] = int(value)
                if counters[key] > 2**64-1:
                    raise ValueError('invalid audio status')
            if counters['ready'] not in (0, 1) or counters['enabled'] not in (0, 1):
                raise ValueError('invalid audio status')
            result.update(counters, ready=bool(counters['ready']),
                          enabled=bool(counters['ready'] and counters['enabled']))
        except (OSError, ValueError, RuntimeError, KeyError) as error:
            LOG.warning('Audio status unavailable: %s', error)
        self.audio_status, self.audio_status_at = result, now
        return result

    def watch_bot_progress(self):
        """Act while paused, then let the next fresh observation decide resume.

        The bypass uses native simulation time across lives. The legacy watchdog
        remains available for explicit reroute counters, but issues no automatic
        two-minute restart. Package staging cannot consume/reset the old clock.
        """
        if self.engine_mode == 'deathmatch' or self.switching or self.package_restore is not None:
            self.bypass.cancel()
            return False
        try:
            frame = read_frame(self.runtime / 'frame.bin')
        except BlockingIOError:
            return False
        except (OSError, ValueError):
            self.bypass.cancel()
            return False
        # A full-width ABI name may be truncated. Native restart refuses it;
        # avoid triggering a recovery attempt that cannot identify its map.
        name = frame.get('map')
        eligible = (self.reason is None and self.human is None
                    and not self.menu_until and not self.manual_pause
                    and self.checkpoint_restore_pending is None
                    and isinstance(name, str) and len(name) < 15)
        action = self.bypass.observe(frame, eligible)
        if action is None:
            return False
        fresh = frame
        try:
            self.native('pause 1')
            fresh = read_frame(self.runtime / 'frame.bin')
            if (self.human is not None or self.menu_until or self.manual_pause
                    or self.switching or self.package_restore is not None
                    or self.checkpoint_restore_pending is not None
                    or self.reason is not None or not self.bypass.confirm(fresh)):
                self.bypass.cancel(fresh)
                self.reason = 'bot-bypass'
                return True
            command = {'replan': 'replan', 'restart': 'restart-map', 'skip': 'skip-map'}[action]
            reply = self.native(command)
            outcome, destination = action, None
            if action in ('replan', 'restart'):
                if reply != 'OK':
                    raise ValueError('invalid bot bypass acknowledgement')
            else:
                if reply.startswith('OK skipped='):
                    outcome, destination = 'skip', reply[len('OK skipped='):]
                elif reply.startswith('OK restarted='):
                    outcome, destination = 'restart', reply[len('OK restarted='):]
                else:
                    raise ValueError('invalid bot bypass skip acknowledgement')
                if not destination or len(destination) >= 15:
                    raise ValueError('invalid bot bypass destination')
            event = self.bypass.acted(action, read_frame(self.runtime / 'frame.bin'),
                                      outcome=outcome, destination=destination)
            # The daemon uses Python's default warning-level stderr logger.
            # Keep successful events visible there without changing log policy.
            LOG.warning('Bot bypass map=%s reason=%s minutes=%.2f',
                        event['map'], event['reason'], event['minutes'])
            if action != 'replan':
                try:
                    self.native('save')
                    self.last_checkpoint = time.monotonic()
                except (OSError, ValueError, RuntimeError):
                    self.last_error = 'restarted map; checkpoint unavailable'
        except (OSError, ValueError, RuntimeError):
            self.bypass.cancel(fresh, failed=True)
            raise
        finally:
            self.engine_settings = None
        self.reason = 'bot-bypass'
        return True

    def background_mode(self):
        return ('empty' if not self.gpu_capture_available
                else self.config['wallpaper']['mode'])

    def renderer_state(self, *, loaded=None):
        if loaded is None:
            loaded = bool(self.engine and self.engine.poll() is None)
        if getattr(self, 'checkpoint_restore_pending', None) is not None:
            return 'starting'
        if getattr(self, 'package_restore', None) is not None and self.reason == 'package-loading':
            return 'starting'
        return ('unavailable' if self.gpu_recovery_blocked else 'ready' if loaded else
                'starting' if self.retry_at > 0 and not self.switch_blocked and self.reason != 'disabled' else 'idle')

    def capture_preferences(self):
        state = self.renderer_state()
        if state == 'idle':
            return True, ''
        if state == 'starting':
            return False, 'OpenGL capture is starting.'
        if state == 'unavailable':
            return False, 'OpenGL capture is unavailable; see logs.'
        return self.gpu_capture_available, '' if self.gpu_capture_available else self.gpu_capture_reason

    def settings(self):
        result = copy.deepcopy(self.config)
        result['wallpaper']['active'] = result['wallpaper']['enabled']
        if self.background is not None:
            result['wallpaper'].update(self.background.metadata())
        capture_available, capture_reason = self.capture_preferences()
        result['wallpaper'].update(mode=result['wallpaper']['mode'] if capture_available else 'empty',
            mode_available=capture_available, mode_reason=capture_reason)
        result['screensaver']['owner'] = self.saver_owner_seen
        if self.idle is not None:
            result['screensaver'].update(self.idle.settings())
        paks = self.data['paks']
        result['game'].update(selected_package='librequake', catalog=[], data={
            'id': 'librequake', 'title': 'LibreQuake 0.09-beta', 'basedir': str(self.pak.parent.parent),
            'gamedir': self.pak.parent.name, 'paks': paks})
        if self.catalog is not None:
            result['game'].update(self.catalog.settings())
            result['game']['selected_package'] = self.selection.id
            result['game']['data'] = self.selection.data
            result['game']['selection_note'] = self.selection_note
        elif self.selection_note:
            result['game']['selection_note'] = self.selection_note
        available, mode_reason = self.mode_status()
        result['game'].update(mode_available=available, mode_reason=mode_reason,
                              effective_mode=self.engine_mode or self.effective_mode(),
                              dm_available_maps=self.available_dm_maps(), dm_available_sp_maps=self.available_sp_maps())
        if onboarding is not None:  # Derived, read-only; the menu computes Steam choices from `game`.
            result['onboarding'] = onboarding.settings(self.state, home=getattr(self.background, 'home', None),
                                                       theme=result['wallpaper'].get('theme'))
        frame = {}
        if self.engine and self.engine.poll() is None:
            try:
                frame = read_frame(self.runtime / 'frame.bin')
            except (OSError, ValueError):
                pass
        loaded = bool(self.engine and self.engine.poll() is None)
        audio = self.refresh_audio_status()
        sound_output = ('discarded (default)' if self.audio_mode == 'discard' else
                        'ALSA not running' if not loaded else
                        'ALSA unavailable' if not audio['ready'] else
                        'ALSA enabled' if audio['enabled'] else 'ALSA muted')
        sound_preference = self.config['sound']['playing' if self.human else
                                              'screensaver' if self.saver_active() else 'idle']
        audio = {**audio, 'output': self.audio_mode,
                 'state': ('off' if self.audio_mode == 'discard' or not loaded else
                           'unavailable' if not audio['ready'] else
                           'playing' if audio['enabled'] else
                           'ready' if sound_preference else 'muted'),
                 'reason': ('Could not open the system audio output; see logs.'
                            if loaded and self.audio_mode == 'alsa' and not audio['ready'] else
                            'The game is not running.' if not loaded else '')}
        result['runtime'] = {'loaded': loaded,
            'owner': 'human' if self.human else 'bot', 'paused': self.reason is not None,
            'pause_reason': self.reason, 'map': frame.get('map'), 'pid': frame.get('pid'),
            'frame': frame, 'screensaver': self.saver_active(), 'last_error': self.last_error,
            'sound_output': sound_output, 'audio': audio}
        result['runtime']['renderer'] = {'name': 'opengl', 'backend': self.engine_backend,
            'transport': 'dmabuf',
            'state': self.renderer_state(loaded=loaded),
            'retry_available': self.gpu_recovery_blocked and not self.switch_blocked}
        result['blur'].update(available=capture_available, reason=capture_reason)
        result['runtime']['bot_watchdog'] = self.watchdog.settings()
        result['runtime']['bot_level_bypass'] = self.bypass.settings()
        result['runtime']['reroute_available'] = bool(result['runtime']['loaded'] and self.human is None
            and frame.get('bot') == 1 and frame.get('health', 0) > 0 and frame.get('gamestate') == 0)
        return result

    def open_menu(self, *, output=None, origin=None):
        if self.menu_launcher is None:
            raise ValueError('the settings menu is not installed')
        if output is not None and not any(o.name == output for o in self.snapshot.outputs):
            raise ValueError('settings output is unavailable')
        if not self.snapshot.available or self.snapshot.locked is not False:
            raise ValueError('settings menu is unavailable')
        self.revoke()
        if self.idle is not None and self.saver_active():
            self.idle.dismissed()
        self.clear_saver()
        previous = self.menu_until
        # Only overlays need a pre-lease while mapping. Floating windows follow
        # the ordinary desktop visibility policy and never renew a menu lease.
        if origin not in ('launcher', 'onboarding'):
            self.menu_until = time.monotonic() + 6
        self.update()
        try:
            self.menu_launcher.open(output=output, origin=origin)
        except (OSError, ValueError, RuntimeError):
            self.menu_until = previous
            self.update()
            raise
        return 'OK'

    def view_state(self, name):
        reply = self._view_state(name)
        # This existing roundtrip also carries the opt-in for a viewer that was
        # already running when the preference changed. Off replies stay exact.
        if self.config['screensaver']['enabled']:
            reply += f' {self.diagnostic_generation}'
        return reply

    def _view_state(self, name):
        """Pure presentation reply from the last reconciled state."""
        if (getattr(self, 'checkpoint_restore_pending', None) is not None or
                getattr(self, 'package_restore', None) is not None):
            return 'STATE hidden 0 0'
        output = next((o for o in self.snapshot.outputs if o.name == name), None)
        if (output and self.gpu_saver_active and self.snapshot.available and
                self.snapshot.locked is False and output.awake and
                self.snapshot.saver_owner not in ('other', 'unknown') and
                (self.idle is None or self.snapshot.saver_owner == 'live-quake')):
            return 'STATE saver 0 0'
        if not output or not self.snapshot.available or self.snapshot.locked is not False or not output.awake or not self.config['wallpaper']['enabled'] or self.saver_active():
            return 'STATE hidden 0 0'
        if not self.gpu_capture_available and output.busy:
            return 'STATE hidden 0 0'
        if self.human == output.name:
            return 'STATE human 0 0'
        paused = self.reason is not None or (output.busy and self.background_mode() == 'empty')
        blur_available = self.gpu_capture_available
        return f'STATE {"paused" if paused else "bot"} 0 {self.config["blur"]["percent"] if paused and blur_available else 0}'

    def command(self, line, client=None):
        parts = line.split()
        if not parts:
            raise ValueError('empty command')
        verb = parts[0]
        if verb in ('viewer-ready', 'saver-presented', 'saver-state'):
            expected = 4 if verb == 'saver-presented' else 3 if verb == 'viewer-ready' else 2
            if len(parts) != expected:
                raise ValueError('screensaver report is unavailable')
            numeric = parts[1:3] if verb == 'saver-presented' else parts[1:2]
            if any(not value.isascii() or not value.isdecimal() or len(value) > 20 or
                   not 0 < int(value) <= 2**64-1 for value in numeric):
                raise ValueError('screensaver report is unavailable')
            if (int(parts[1]) != self.diagnostic_generation or not self.engine or self.engine.poll() is not None or
                    not self.viewer or self.viewer.poll() is not None):
                raise ValueError('screensaver report is unavailable')
            reporter = self.viewer
            reporters = (reporter,)
            if client is not None and not any(child and child.poll() is None and
                    client.get('peer_pid') == child.pid for child in reporters):
                raise ValueError('screensaver report is unavailable')
            if verb == 'saver-state':
                cycle = self.saver_cycle_id if self.saver_active() else 0
                return f'SAVER {self.diagnostic_generation} {cycle}'
            name = parts[-1]
            if name not in self.awake_saver_heads() or not self.snapshot.available or self.snapshot.locked is not False:
                raise ValueError('screensaver report is unavailable')
            self.sync_saver_readiness()
            if verb == 'viewer-ready':
                self.viewer_ready_heads.add(name)
                return 'OK'
            self.check_saver_deadline()
            if (not self.saver_active() or int(parts[2]) != self.saver_cycle_id or
                    name not in self.saver_participants or not reporter or reporter.poll() is not None):
                raise ValueError('screensaver presentation is unavailable')
            self.saver_presented_heads.add(name)
            if not self.saver_acknowledged and self.saver_participants.issubset(self.saver_presented_heads):
                self.saver_acknowledged = True
                self.saver_present_deadline = 0
                if self.idle is not None:
                    self.idle.acknowledged()
                LOG.info('Saver presented generation=%d cycle_id=%d heads=%d', self.diagnostic_generation,
                         self.saver_cycle_id, len(self.saver_participants))
            return 'OK'
        if verb == 'capture-status':
            if (len(parts) != 3 or parts[2] not in ('ready', 'failed') or (not parts[1].isascii()) or (not parts[1].isdecimal()) or (not self.viewer) or (self.viewer.poll() is not None) or (int(parts[1]) != self.gpu_generation)):
                raise ValueError('OpenGL capture status is unavailable')
            report = (self.gpu_generation, parts[2])
            if self.gpu_capture_report == report:
                return 'OK'
            self.gpu_capture_report = report
            self.gpu_capture_available = parts[2] == 'ready'
            self.gpu_capture_reason = ('' if self.gpu_capture_available else
                                       'OpenGL capture is unavailable; see logs.')
            self.gpu_capture_deadline = 0
            self.update()
            return 'OK'
        self.update() # Revalidate policy before accepting any input/ownership.
        if verb in ('status', 'settings') and len(parts) == 1:
            return json.dumps(self.settings(), separators=(',', ':'))
        if verb == 'capture':
            if (len(parts) != 2 or not parts[1].isascii() or (not parts[1].isdecimal()) or (not self.viewer) or (self.viewer.poll() is not None) or (int(parts[1]) != self.gpu_generation) or (not self.gpu_capture_available) or (not self.snapshot.available) or (self.snapshot.locked is not False) or (not any((output.awake for output in self.snapshot.outputs)))):
                raise ValueError('OpenGL capture is unavailable')
            try:
                return self.native('capture')
            except TimeoutError:
                raise RuntimeError('OpenGL capture timed out.') from None
        if verb == 'scan-steam' and len(parts) == 1:
            if self.catalog is None:
                raise ValueError('game catalog is unavailable')
            if not self.catalog.scan():
                raise ValueError('a steam scan is still running')
            return 'OK'
        if verb == 'use-game' and len(parts) == 2:
            return self.use_game(parts[1])
        if verb == 'onboarding' and onboarding is not None:  # Saves the guide's inert place and choices.
            onboarding.command(self.state, parts[1:], saver=self.config['screensaver']['enabled'])
            return json.dumps(self.settings(), separators=(',', ':'))
        if verb == 'retry-renderer' and len(parts) == 1:
            if not self.gpu_recovery_blocked or self.switch_blocked:
                raise ValueError('OpenGL retry is unavailable')
            self.gpu_recovery_blocked = False
            self.failure_count = 0
            # Acknowledge the explicit request before another slow GPU probe.
            self.retry_at = time.monotonic() + .1
            self.last_error = 'Retrying OpenGL.'
            return 'OK'
        if verb == 'open-menu':
            output = origin = None
            for value in parts[1:]:
                if value.startswith('--origin=') and origin is None:
                    origin = value.removeprefix('--origin=')
                elif value.startswith('--output=') and output is None:
                    output = value.removeprefix('--output=')
                else:
                    raise ValueError('invalid menu opening arguments')
            return self.open_menu(output=output, origin=origin)
        if verb == 'view' and len(parts) == 2:
            return self.view_state(parts[1])
        if verb == 'takeover' and len(parts) == 2:
            if self.package_restore is not None or self.checkpoint_restore_pending is not None or self.menu_until or self.saver_active() or not self.config['wallpaper']['enabled'] or not takeover_allowed(self.snapshot, parts[1]):
                raise ValueError('takeover unavailable')
            self.revoke()
            self.human = parts[1]
            self.human_workspace = next((o.workspace, o.special_workspace) for o in self.snapshot.outputs if o.name == self.human)
            self.generation += 1
            self.human_quits_seen = read_frame(self.runtime / 'frame.bin')['human_quits']
            self.takeover_refused = False
            self.update()
            if self.takeover_refused or self.human != parts[1]:
                raise ValueError('takeover refused')
            return 'OK'
        if verb == 'input' and len(parts) == 2 and client is not None:
            if parts[1] != self.human or self.input_client is not None:
                raise ValueError('input ownership unavailable')
            client['input'] = self.generation
            self.input_client = client
            return 'OK'
        if verb in ('key', 'mouse') and client is not None:
            if self.human is None or client.get('input') != self.generation or self.reason is not None:
                raise ValueError('input ownership expired')
            if verb == 'key' and len(parts) in (3, 5):
                key, down = int(parts[1]), int(parts[2])
                if not 0 < key <= 204 or down not in (0, 1):
                    raise ValueError('invalid key')
                self.native(f'key {key} {down}')
            elif verb == 'mouse' and len(parts) == 3:
                dx, dy = map(int, parts[1:])
                if abs(dx) > MOUSE_DELTA_MAX or abs(dy) > MOUSE_DELTA_MAX:
                    raise ValueError('invalid mouse delta')
                sensitivity = self.config['mouse']['sensitivity']
                dx, dy = round(dx*sensitivity), round(dy*sensitivity*(-1 if self.config['mouse']['invert'] else 1))
                while dx or dy:
                    x, y = max(-MOUSE_DELTA_MAX, min(MOUSE_DELTA_MAX, dx)), max(-MOUSE_DELTA_MAX, min(MOUSE_DELTA_MAX, dy))
                    self.native(f'mouse {x} {y}')
                    dx -= x
                    dy -= y
            else:
                raise ValueError('invalid input')
            return 'OK'
        if verb == 'bot' and len(parts) == 1:
            self.revoke()
        elif verb == 'menu' and len(parts) == 2:
            if parts[1] == 'opened':
                self.revoke()
                if self.idle is not None and self.saver_active():
                    self.idle.dismissed()
                self.clear_saver()
                self.menu_until = time.monotonic() + 6
            elif parts[1] == 'renew' and self.menu_until:
                self.menu_until = time.monotonic() + 6
            elif parts[1] == 'closed':
                self.menu_until = 0
            else:
                raise ValueError('menu lease expired')
        elif verb == 'settings' and len(parts) == 2:
            return self.open_menu(output=parts[1])
        elif verb in ('use-live-wallpaper', 'remove-live-wallpaper') and len(parts) == 1:
            if self.background is None:
                raise ValueError('live background selection is unavailable')
            self.revoke()
            try:
                return self.background.change(verb)
            finally:
                self.update()
        elif verb == 'set' and len(parts) >= 3:
            key, encoded = line.split(' ', 2)[1:]
            value = json.loads(encoded)
            value = validate_setting(key, value)
            if key == 'game.mode':
                return self.change_mode(value)
            if key == 'game.dm_disabled_maps':
                available = self.available_dm_maps()
                if any(name not in available for name in value):
                    raise ValueError('map is not available')
                if not [name for name in available if name not in value] and not self.config['game']['dm_enabled_sp_maps']:
                    raise ValueError('at least one deathmatch map must stay enabled')
            if key == 'game.dm_enabled_sp_maps':
                if any(name not in self.available_sp_maps() for name in value):
                    raise ValueError('map is not available')
                if not value and not [name for name in self.available_dm_maps()
                                      if name not in self.config['game']['dm_disabled_maps']]:
                    raise ValueError('at least one deathmatch map must stay enabled')
            capture_available, capture_reason = self.capture_preferences()
            if not capture_available:
                if (key == 'blur.percent' and value) or (key == 'wallpaper.mode' and value != 'empty'):
                    raise ValueError(capture_reason)
            if key == 'wallpaper.enabled' and self.background is not None:
                raise ValueError('wallpaper follows the selected background')
            if key == 'screensaver.enabled' and value and self.idle is not None and not self.idle.allow_claim:
                raise ValueError('screensaver token claiming is disabled')
            if key == 'screensaver.enabled' and value and (not self.snapshot.available or self.snapshot.saver_owner == 'unknown'):
                raise ValueError('screensaver ownership is unknown')
            if (key == 'screensaver.enabled' and value and self.snapshot.saver_owner == 'other' and
                    (self.idle is None or not self.idle.ownership.can_adopt())):
                raise ValueError('screensaver suppression is controlled externally')
            updated = copy.deepcopy(self.config)
            group, name = key.split('.')
            updated[group][name] = value
            atomic_json(self.state / 'config.json', updated)
            self.config = updated
            if key == 'screensaver.enabled' and value and self.idle is not None:
                self.idle.explicit_reenable()
            if key == 'game.skill' and self.engine:
                try:
                    self.native(f'skill {self.dm_engine_skill()}')
                except (OSError, RuntimeError) as error:
                    LOG.warning('Skill applies on next engine start: %s', error)
                    self.last_error = 'skill applies on next engine start'
            if key == 'wallpaper.hide_chatter' and self.engine and self.engine.poll() is None:
                try:
                    self.native(f'hide-chatter {int(value)}')
                except (OSError, RuntimeError) as error:
                    LOG.warning('Hide chatter applies on next engine start: %s', error)
                    self.last_error = 'hide chatter applies on next engine start'
            if (key.startswith('game.dm_') and self.engine_mode == 'deathmatch'
                    and self.engine and self.engine.poll() is None):
                try:
                    self.native(self.dm_config_command())  # Applied from the next round.
                except (OSError, RuntimeError) as error:
                    LOG.warning('Deathmatch settings apply on next engine start: %s', error)
                    self.last_error = 'deathmatch settings apply on next engine start'
            if key == 'game.human_viewsize' and self.engine and self.engine.poll() is None:
                self.apply_human_viewsize()
            if key == 'screensaver.enabled' and not value:
                if self.idle is not None and self.saver_active():
                    self.idle.dismissed()
                self.clear_saver()
            self.update()
            return json.dumps(self.settings(), separators=(',', ':'))
        elif verb in ('screensaver', 'preview-screensaver') and len(parts) == 1:
            if self.package_restore is not None or self.checkpoint_restore_pending is not None:
                raise ValueError('screensaver unavailable')
            if self.gpu_recovery_blocked or self.switch_blocked or self.retry_at > time.monotonic():
                raise ValueError('screensaver unavailable')
            if not self.config['screensaver']['enabled'] or self.snapshot.saver_owner in ('other', 'unknown') or (self.idle is not None and (self.snapshot.saver_owner != 'live-quake' or not self.idle.allow_claim)) or not self.snapshot.available or self.snapshot.locked is not False or not any(o.awake for o in self.snapshot.outputs):
                raise ValueError('screensaver unavailable')
            self.revoke()
            self.menu_until = 0
            if self.ensure_engine() is False:
                raise ValueError('screensaver unavailable')
            self.sync_saver_readiness()
            self.apply_engine_policy(self.config['wallpaper']['enabled'])
            self.reconcile_saver()
            if not self.saver_readiness()[0] or (self.idle is not None and not self.idle.armed):
                raise ValueError('screensaver unavailable')
            self.start_saver(preview=True)
        elif verb == 'dismiss-screensaver' and len(parts) == 1:
            self.clear_saver()
            if self.idle is not None:
                self.idle.dismissed()
        elif verb == 'pause' and len(parts) == 1:
            self.manual_pause = True
            self.revoke()
        elif verb == 'resume' and len(parts) == 1:
            self.manual_pause = False
        elif verb == 'hint-bot' and len(parts) == 1:
            if not self.settings()['runtime']['reroute_available']:
                raise ValueError('bot reroute unavailable')
            self.native('replan')
            self.watchdog.hint(read_frame(self.runtime / 'frame.bin'))
        elif verb in ('save', 'replan') and len(parts) == 1:
            self.native(verb)
        elif verb == 'quit' and len(parts) == 1:
            if client is None:
                self.running = False
            else:
                client['quit'] = True
        else:
            raise ValueError('unknown command')
        self.update()
        return 'OK'

    def drop(self, connection):
        client = self.clients.pop(connection)
        self.selector.unregister(connection)
        connection.close()
        if client.get('quit'):
            self.running = False
        if client.get('input') == self.generation and self.input_client is client:
            self.revoke()

    def step(self, timeout=.05):
        self.update()
        for key, mask in self.selector.select(timeout):
            if key.fileobj is self.server:
                connection, _ = self.server.accept()
                if len(self.clients) >= 32:
                    connection.close()
                    continue
                connection.setblocking(False)
                try:
                    peer_pid, peer_uid, _ = struct.unpack('3i', connection.getsockopt(
                        socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize('3i')))
                    if peer_uid != os.getuid():
                        connection.close()
                        continue
                except (OSError, struct.error):
                    connection.close()
                    continue
                client = {'buffer': bytearray(), 'output': bytearray(), 'deadline': time.monotonic()+8,
                          'peer_pid': peer_pid}
                self.clients[connection] = client
                self.selector.register(connection, selectors.EVENT_READ, client)
                continue
            connection, client = key.fileobj, key.data
            try:
                if mask & selectors.EVENT_READ:
                    data = connection.recv(8192)
                    if not data and (not client['output'] or 'input' in client):
                        self.drop(connection)
                        continue
                    client['buffer'].extend(data)
                    if len(client['buffer']) > 16384:
                        self.drop(connection)
                        continue
                    for _ in range(128):
                        if b'\n' not in client['buffer']:
                            break
                        line, _, tail = client['buffer'].partition(b'\n')
                        client['buffer'] = bytearray(tail)
                        try:
                            reply = self.command(line.decode('utf-8'), client)
                        except OSError as error:
                            LOG.warning('Control command could not access local files: %s', error)
                            reply = 'ERR could not access local files; see logs'
                        except (ValueError, RuntimeError, UnicodeError) as error:
                            reply = 'ERR ' + (str(error).replace('\n', ' ') or type(error).__name__)[:256]
                        client['output'].extend((reply+'\n').encode())
                        client['deadline'] = time.monotonic()+2
                    if len(client['output']) > 1024*1024:
                        self.drop(connection)
                        continue
                    self.selector.modify(connection, selectors.EVENT_WRITE |
                                         (selectors.EVENT_READ if data else 0), client)
                if mask & selectors.EVENT_WRITE and client['output']:
                    sent = connection.send(client['output'])
                    del client['output'][:sent]
                    if not client['output']:
                        if 'input' not in client:
                            self.drop(connection)
                        else:
                            self.selector.modify(connection, selectors.EVENT_READ, client)
            except (OSError, ValueError):
                if connection in self.clients:
                    self.drop(connection)
        for connection, client in list(self.clients.items()):
            if 'input' not in client and time.monotonic() > client['deadline']:
                self.drop(connection)

    def close(self):
        self.running = False
        try:
            # Verified suppression is retired before any potentially slow
            # native, observer, socket or private-farm cleanup.
            if self.idle is not None:
                self.idle.close(dismiss_active=self.saver_active())
        finally:
            try:
                self.cancel_package_restore(note='')
                for connection in list(self.clients):
                    self.drop(connection)
                try:
                    self.stop_engine()
                finally:
                    self.selector.unregister(self.server)
                    self.server.close()
                    self.selector.close()
                    (self.runtime / 'control.sock').unlink(missing_ok=True)
                    for helper in (self.background, self.menu_launcher, self.catalog):
                        if helper is not None:
                            helper.close()
            finally:
                if self.policy_log is not None:
                    try:
                        self.policy_log.close()
                    except Exception:
                        self.diagnostic_failures += 1

def main():
    import sys
    if sys.argv[1:2] == ['daemon']:
        from daemon import main as daemon_main
        return daemon_main(sys.argv[2:])
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime', type=Path)
    parser.add_argument('--origin', choices=('launcher', 'context-menu', 'onboarding'))
    parser.add_argument('--output')
    parser.add_argument('verb', nargs='+')
    options = parser.parse_args()
    if (options.origin is not None or options.output is not None) and options.verb != ['open-menu']:
        raise ValueError('menu options require open-menu')
    command = list(options.verb)
    if options.origin is not None:
        command.append('--origin=' + options.origin)
    if options.output is not None:
        if not options.output or any(c.isspace() for c in options.output):
            raise ValueError('invalid settings output')
        command.append('--output=' + options.output)
    runtime = options.runtime
    if runtime is None:
        xdg = os.environ.get('XDG_RUNTIME_DIR')
        if not xdg:
            raise ValueError('XDG_RUNTIME_DIR is required')
        runtime = Path(xdg) / 'live-quake'
    try:
        # Package and mode switches restart the engine inside one bounded transaction.
        switch = command[0] == 'use-game' or command[:2] == ['set', 'game.mode']
        reply = request(runtime / 'control.sock', ' '.join(command), timeout=40 if switch else 8)
    except (FileNotFoundError, ConnectionRefusedError):
        raise RuntimeError('live quake is not running')
    except OSError:
        # The menu captures the wrapper's stderr. Keep private socket paths
        # out of that channel, including when permissions prevent connection.
        raise RuntimeError('live quake connection is unavailable') from None
    if reply == 'ERR' or reply.startswith('ERR '):
        raise ValueError(reply[4:] or 'controller error')
    if reply != 'OK':
        print(reply)

if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, RuntimeError) as error:
        raise SystemExit(str(error).lower())
