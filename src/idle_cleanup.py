# SPDX-License-Identifier: 0BSD
"""Release a verified retired saver claim without observing the desktop."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import json
import os
import resource
import stat
import sys

from runtime_lock import RuntimeLock, absolute_path
from idle import SaverOwnership, SaverRecovery

PRESERVED_NOTE = ("Omarchy's screensaver-off toggle was switched on after Live Quake set it, "
                  "so it was left on; turn Omarchy's screensaver back on in its menu if you want it.")

def _result(outcome, *, safe=False, released=False, note=None):
    # Never expose a token, path, PID or exception text in the service journal.
    result = {'outcome': outcome, 'safe': safe, 'released': released}
    if note is not None:
        result['note'] = note
    return result


def runtime_parent(runtime):
    """Read-only validation of the parent used to recreate a retired lock."""
    path = absolute_path(runtime, 'runtime directory')
    parent = absolute_path(path.parent, 'runtime parent')
    fd = os.open(parent, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_DIRECTORY)
    try:
        info = os.fstat(fd)
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise ValueError('unsafe runtime parent')
    finally:
        os.close(fd)
    return path


def _create_runtime_directory(runtime):
    """Create only the final private runtime component, under a private parent."""
    path = runtime_parent(runtime)
    fd = os.open(path.parent, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_DIRECTORY)
    try:
        try:
            os.mkdir(path.name, 0o700, dir_fd=fd)
        except FileExistsError:
            pass
    finally:
        os.close(fd)


@contextmanager
def cleanup_guard(home, state_dir, runtime_dir, *, purpose='stop', before_release=None):
    """Hold the retired controller's lock through cleanup and caller removal.

    Missing runtime information is harmless only when there is no ownership
    record to retire. A missing lock after reboot is created and acquired before
    verified recovery. Incomplete or unverifiable records always block removal.
    Stop cleanup requires exact recorded identity. Uninstall may recognize a
    changed private token only to preserve it and its journal, never to unlink.
    Its optional preflight runs before any ownership mutation and receives the
    actual runtime-lock status. All file/lock operations are bounded and
    nonblocking; no process is started.
    """
    if (type(purpose) is not str or purpose not in ('stop', 'uninstall') or
            (before_release is not None and not callable(before_release))):
        raise ValueError('Invalid cleanup purpose or preflight')

    def missing_result(recorded):
        result = _result('missing', safe=not recorded)
        if result['safe'] and before_release is not None:
            before_release(runtime_held=False)
        return result

    try:
        ownership = SaverOwnership(home, state_dir)
        before = ownership.cleanup_status()
    except (OSError, ValueError, RecursionError):
        yield _result('unsafe')
        return
    if not before['safe']:
        yield _result('unsafe')
        return
    if runtime_dir is None:
        yield missing_result(before['recorded'])
        return
    try:
        lock = RuntimeLock(runtime_dir, create=False, record_pid=False)
        lock.__enter__()
    except FileNotFoundError:
        try:
            _create_runtime_directory(runtime_dir)
            lock = RuntimeLock(runtime_dir, create=True, record_pid=False)
            lock.__enter__()
        except RuntimeError:
            yield _result('busy')
            return
        except FileNotFoundError:
            # After logout the entire runtime parent can be absent. With no
            # ownership authority there is nothing to release; removal relies
            # on its documented manual service retirement, never a held lock.
            yield missing_result(before['recorded'])
            return
        except (OSError, ValueError):
            yield _result('unsafe')
            return
    except RuntimeError:
        yield _result('busy')
        return
    except (OSError, ValueError):
        yield _result('unsafe')
        return
    released = False
    preflight_running = False
    cycle_id = engine_generation = 0
    try:
        try:
            # A controller could have changed the journal before we acquired
            # its lock. Recheck only after its lifetime is excluded.
            lock.verify()
            current = ownership.cleanup_status()
            if current['safe'] and before_release is not None:
                preflight_running = True
                before_release(runtime_held=True)
                preflight_running = False
                lock.verify()
                current = ownership.cleanup_status()
            if not current['safe']:
                result = _result('unsafe')
            elif not current['recorded']:
                result = _result('unclaimed', safe=True)
            else:
                # Recovery metadata describes the retired attempt, never the
                # authority to remove its token. Missing/unsafe records add no
                # identity; ownership verification still decides release.
                try:
                    recovery = SaverRecovery(ownership).load()
                    cycle_id, engine_generation = recovery['cycle_id'], recovery['engine_generation']
                except (OSError, ValueError, RecursionError):
                    pass
                pending = ownership.resolve_pending()
                released = pending['released']
                lock.verify()
                if not pending['safe']:
                    result = _result('unsafe', released=released)
                else:
                    retirement = ownership.release(recognize=False)
                    released = retirement['released'] or released
                    after = ownership.cleanup_status()
                    lock.verify()
                    preserved_changed = after['safe'] and all(
                        value.get(key) is True for value in (retirement, after)
                        for key in ('preserved_changed', 'toggle_safe', 'journal_retained', 'toggle_retained'))
                    safe = (after['safe'] and not after['recorded']) or (
                        purpose == 'uninstall' and preserved_changed)
                    result = _result('released' if released else 'preserved',
                                     released=released, safe=safe,
                                     note=PRESERVED_NOTE if preserved_changed else None)
        except (OSError, ValueError, RecursionError):
            if preflight_running:
                # The installer reports its concrete checked-plan blocker.
                # No ownership mutation has happened, and finally drops lock.
                raise
            result = _result('unsafe', released=released)
        if released:
            # A later refusal cannot erase a verified release that completed.
            print('screensaver release reason_code=disabled cycle_id=%d engine_generation=%d'
                  % (cycle_id, engine_generation), file=sys.stderr)
        yield result
    finally:
        lock.__exit__()


def release_verified(home, state_dir, runtime_dir, *, purpose='stop', before_release=None):
    with cleanup_guard(home, state_dir, runtime_dir, purpose=purpose, before_release=before_release) as result:
        return result


def main(argv=None):
    resource.setrlimit(resource.RLIMIT_CORE, (1, 1))
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--home', required=True)
    parser.add_argument('--state-dir', required=True)
    parser.add_argument('--runtime-dir', required=True)
    options = parser.parse_args(argv)
    result = release_verified(options.home, options.state_dir, options.runtime_dir)
    print(json.dumps(result, separators=(',', ':'), sort_keys=True))
    # Refusal is a successful preservation outcome. The unit also ignores
    # command failure and applies its own short timeout to this cleanup hook.
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
