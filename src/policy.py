# SPDX-License-Identifier: 0BSD
"""Pure desktop policy. No desktop calls or input side effects."""
from dataclasses import dataclass

@dataclass(frozen=True)
class Output:
    name: str
    workspace: int = 1
    focused: bool = True
    busy: bool = False
    awake: bool = True
    width: int = 1280
    height: int = 720
    special_workspace: int = 0
    refresh_millihz: int | None = None

@dataclass(frozen=True)
class Snapshot:
    outputs: tuple[Output, ...] = ()
    locked: bool | None = None
    available: bool = False
    saver_owner: str | None = 'unknown'

def from_hyprland(monitors, clients, locked):
    """Normal and visible special workspace windows count independently."""
    outputs = []
    for monitor in monitors:
        active = monitor['activeWorkspace']['id']
        special = monitor.get('specialWorkspace', {}).get('id', 0)
        busy = any(client.get('mapped', True) and not client.get('hidden', False) and
                   client.get('workspace', {}).get('id') in ({active, special} if special else {active})
                   for client in clients)
        outputs.append(Output(monitor['name'], active, monitor.get('focused', False),
                              busy, monitor.get('dpmsStatus', True) is not False,
                              monitor.get('width', 1280), monitor.get('height', 720), special))
    return Snapshot(tuple(outputs), locked, True)

def takeover_allowed(snapshot, output):
    if not snapshot.available or snapshot.locked is not False:
        return False
    return any(item.name == output and item.focused and not item.busy and item.awake
               for item in snapshot.outputs)

def pause_reason(snapshot, enabled, mode, human, saver, menu, manual=False):
    if not snapshot.available or snapshot.locked is None:
        return 'unavailable'
    if snapshot.locked:
        return 'lock'
    if menu:
        return 'menu'
    if manual:
        return 'manual'
    if not any(output.awake for output in snapshot.outputs):
        return 'asleep'
    if saver or human:
        return None
    if not enabled:
        return 'disabled'
    if mode == 'empty' and not any(output.awake and not output.busy for output in snapshot.outputs):
        return 'busy'
    return None
