#!/usr/bin/python3
# SPDX-License-Identifier: 0BSD
"""Build only pinned/reviewed Ironwail sources in an explicit private tree."""
import sys
sys.dont_write_bytecode = True
import resource
resource.setrlimit(resource.RLIMIT_CORE, (1, 1))
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location('live_quake_iron_bootstrap', ROOT / 'scripts/bootstrap-ironwail.py')
iron = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(iron)
from build import private_log, replace_bytes

SOURCE_DATE_EPOCH = '1790991502'
MAX_INPUT = 2 * 1024 * 1024
MAX_ARTIFACT = 64 * 1024 * 1024
MAX_TREE_FILES = 20000
IRON_SOURCES = ('live_quake.c', 'lq_egl_gbm.c', 'lq_egl_offscreen.h',
                'lq_libretro.c', 'lq_libretro.h', 'lq_session.h', 'dm_rotation.h', 'dm_director.h', 'lq_sound.c', 'lq_steam_disabled.c')
GPU_SOURCES = ('gpu_gbm.c', 'gpu_gbm.h', 'gpu_transport.c', 'gpu_transport.h')
BOT_HEADERS = ('level_bypass.h',)
PROTOCOLS = ('xdg-shell', 'xdg-output-unstable-v1', 'wlr-layer-shell-unstable-v1',
             'relative-pointer-unstable-v1', 'pointer-constraints-unstable-v1',
             'linux-dmabuf-v1', 'viewporter')
WORK_OUTPUTS = {'quake-engine', 'quake-viewer'} | {
    'viewer-protocols/' + name + suffix for name in PROTOCOLS
    for suffix in ('-client-protocol.h', '-protocol.c')}
MAKE_ARGS = ('LIVE_QUAKE_SHARED=1', 'LIVE_QUAKE_GPU=1', 'DO_USERDIRS=0', 'USE_CURL=0',
             'USE_CODEC_MP3=0', 'USE_CODEC_VORBIS=0', 'USE_CODEC_FLAC=0', 'USE_CODEC_OPUS=0',
             'USE_CODEC_WAVE=1')
_PATH = re.compile(r'[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*\Z')
_HUNK = re.compile(rb'@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@[^\n]*\n?\Z')
_GENERATED = re.compile(r'Quake/[A-Za-z0-9_.-]+\.(?:o|d)\Z')


def environment(work):
    result = iron.environment(work)
    result['SOURCE_DATE_EPOCH'] = SOURCE_DATE_EPOCH
    return result


def run(command, work, **kwargs):
    return subprocess.run(iron.CORE_LIMIT + list(command), check=True,
                          timeout=kwargs.pop('timeout', 30), env=environment(work), **kwargs)


def digest(payload):
    return hashlib.sha256(payload).hexdigest()


def safe_relative(value):
    if not isinstance(value, str) or not _PATH.fullmatch(value) or any(part in ('.', '..') for part in value.split('/')):
        raise ValueError('Unsafe build-relative path')
    return value


def patch_hunks(payload):
    """Decode bounded canonical text hunks, not executable patch directives."""
    if len(payload) > MAX_INPUT or b'\0' in payload:
        raise ValueError('Reviewed patch exceeds bounds')
    lines, index, result = payload.splitlines(keepends=True), 0, {}
    while index < len(lines):
        if not lines[index].startswith(b'--- a/') or index + 1 >= len(lines):
            raise ValueError('Reviewed patch requires canonical a/b prefixes')
        try:
            name = safe_relative(lines[index][6:].rstrip(b'\n').decode('ascii'))
        except UnicodeError:
            raise ValueError('Invalid patch path') from None
        if lines[index + 1] != b'+++ b/' + name.encode() + b'\n' or name in result or len(result) >= 32:
            raise ValueError('Reviewed patch paths differ or repeat')
        index += 2
        hunks = []
        while index < len(lines) and lines[index].startswith(b'@@ '):
            match = _HUNK.fullmatch(lines[index])
            if match is None:
                raise ValueError('Invalid reviewed hunk header')
            old_start, old_count, new_start, new_count = (int(value) if value is not None else 1 for value in match.groups())
            index += 1
            old, new = [], []
            while len(old) < old_count or len(new) < new_count:
                if index >= len(lines) or lines[index][:1] not in (b' ', b'-', b'+'):
                    raise ValueError('Incomplete reviewed patch hunk')
                line = lines[index]
                if line[:1] in (b' ', b'-'):
                    old.append(line[1:])
                if line[:1] in (b' ', b'+'):
                    new.append(line[1:])
                index += 1
                if index < len(lines) and lines[index].startswith(b'\\ No newline at end of file'):
                    if line[:1] in (b' ', b'-'):
                        old[-1] = old[-1].removesuffix(b'\n')
                    if line[:1] in (b' ', b'+'):
                        new[-1] = new[-1].removesuffix(b'\n')
                    index += 1
            if len(old) != old_count or len(new) != new_count:
                raise ValueError('Reviewed hunk count differs')
            hunks.append((old_start, new_start, old, new))
        if not hunks:
            raise ValueError('Reviewed patch file has no hunks')
        result[name] = hunks
    if not result:
        raise ValueError('Reviewed patch is empty')
    return result


def patched_bytes(original, hunks):
    lines, result, position = original.splitlines(keepends=True), [], 0
    for old_start, new_start, old, new in hunks:
        start = old_start if not old else max(0, old_start - 1)
        if start < position or lines[start:start + len(old)] != old:
            raise ValueError('Reviewed patch does not exactly match pinned source')
        result.extend(lines[position:start])
        if len(result) != (new_start if not new else max(0, new_start - 1)):
            raise ValueError('Reviewed new hunk position differs')
        result.extend(new)
        position = start + len(old)
    result.extend(lines[position:])
    return b''.join(result)


def tree_files(source):
    """Refuse redirected files/directories before invoking Git or builders."""
    found, pending, count = {}, [source], 0
    while pending:
        directory = pending.pop()
        with os.scandir(directory) as entries:
            for entry in entries:
                count += 1
                if count > MAX_TREE_FILES:
                    raise ValueError('Source tree exceeds its entry limit')
                info = entry.stat(follow_symlinks=False)
                if info.st_uid != os.getuid() or stat.S_ISLNK(info.st_mode):
                    raise ValueError('Source tree contains an unowned or redirected entry')
                path = Path(entry.path)
                relative = str(path.relative_to(source))
                if stat.S_ISDIR(info.st_mode):
                    pending.append(path)
                elif not stat.S_ISREG(info.st_mode):
                    raise ValueError('Source tree contains a nonregular entry')
                elif relative.startswith('.git/'):
                    if relative == '.git/index' and info.st_size > MAX_INPUT:
                        raise ValueError('Source Git index exceeds bounds')
                else:
                    if info.st_nlink != 1:
                        raise ValueError('Source file has an unexpected hard link')
                    found[relative] = info
    return found


def prior_record(work):
    path = work / 'build-record.json'
    if not os.path.lexists(path):
        return {}
    value = json.loads(iron.bounded_bytes(path, MAX_INPUT))
    if (not isinstance(value, dict) or value.get('backend') != 'ironwail'
            or value.get('engine_commit') != iron.ENGINE_COMMIT):
        raise ValueError('Prior build record is not for this pinned Ironwail tree')
    for group in ('managed_source_files', 'overlay_destinations', 'managed_work_files'):
        if not isinstance(value.get(group), dict) or len(value[group]) > 4096:
            raise ValueError('Prior build record has invalid ownership metadata')
        for name, checksum in value[group].items():
            safe_relative(name)
            if not isinstance(checksum, str) or re.fullmatch('[0-9a-f]{64}', checksum) is None:
                raise ValueError('Prior build record has an invalid hash')
    return value


def work_outputs(work, previous):
    """A private directory alone does not authorize replacing foreign outputs."""
    found = {}
    protocols = work / 'viewer-protocols'
    if os.path.lexists(protocols):
        iron.private_dir(protocols)
        with os.scandir(protocols) as entries:
            if any('viewer-protocols/' + entry.name not in WORK_OUTPUTS for entry in entries):
                raise ValueError('Unknown viewer protocol output must be preserved')
    for name in WORK_OUTPUTS:
        path = work / name
        if os.path.lexists(path):
            checksum = digest(iron.bounded_bytes(path, MAX_ARTIFACT))
            if checksum != previous.get('managed_work_files', {}).get(name):
                raise ValueError('Existing build output changed or is unowned; preserve it')
            found[name] = checksum
    log = work / 'ironwail-build.log'
    if os.path.lexists(log):
        iron.bounded_bytes(log, MAX_ARTIFACT)
    return found


def overlay_inputs(*, diagnostic=False):
    result = {'Quake/' + name: ROOT / 'src/ironwail' / name for name in IRON_SOURCES}
    if diagnostic:
        del result['Quake/lq_egl_gbm.c']
        result['Quake/lq_egl_offscreen.c'] = ROOT / 'src/ironwail/lq_egl_offscreen.c'
    result.update({'Quake/' + name: ROOT / 'src' / name for name in GPU_SOURCES})
    result['Quake/lq_bridge.h'] = ROOT / 'src/bridge.h'
    result.update({'Quake/' + name: ROOT / 'src' / name for name in BOT_HEADERS})
    return result


def preflight(work, *, diagnostic=False):
    work = iron.private_dir(work)
    source = work / 'ironwail'
    iron.source_directory(source)
    existing = tree_files(source)
    iron.safe_git_metadata(source)
    header = iron.verified_header(work)
    git = iron.git_command(source)
    revision = run(git + ['rev-parse', 'HEAD'], work, capture_output=True, text=True).stdout.strip()
    if revision != iron.ENGINE_COMMIT:
        raise ValueError('Ironwail revision is not pinned')
    flags = run(git + ['ls-files', '-v', '-z'], work, capture_output=True).stdout.split(b'\0')
    if any(value[:2] != b'H ' for value in flags if value):
        raise ValueError('Source index hides or skips worktree changes')
    tracked = {value[2:].decode('utf-8') for value in flags if value}
    modes = run(git + ['ls-files', '--stage', '-z'], work, capture_output=True).stdout.split(b'\0')
    for entry in modes:
        if entry:
            mode, remainder = entry.split(b' ', 1)
            name = remainder.split(b'\t', 1)[1].decode('utf-8')
            if mode not in (b'100644', b'100755') or name not in existing:
                raise ValueError('Source index is not a regular pinned worktree')
            if bool(existing[name].st_mode & 0o111) != (mode == b'100755'):
                raise ValueError('Source executable mode differs from the pinned tree')
    if run(git + ['diff', '--cached', '--name-only'], work, capture_output=True).stdout:
        raise ValueError('Source contains staged edits; preserve it and use a fresh tree')
    patch_path = ROOT / 'patches/ironwail-desktop.patch'
    patch = iron.bounded_bytes(patch_path, MAX_INPUT)
    decoded = patch_hunks(patch)
    expected = {}
    originals = {}
    for name, hunks in decoded.items():
        original = run(git + ['show', 'HEAD:' + name], work, capture_output=True).stdout
        if len(original) > MAX_INPUT:
            raise ValueError('Pinned source file exceeds bounds')
        originals[name] = original
        expected[name] = patched_bytes(original, hunks)
    changed = run(git + ['diff', '--name-only', '-z', '--no-ext-diff', '--no-textconv'], work,
                  capture_output=True).stdout.split(b'\0')
    changed = {value.decode('utf-8') for value in changed if value}
    if changed - set(expected):
        raise ValueError('Source has unrecognized tracked edits; preserve it and use a fresh tree')
    current = {name: iron.bounded_bytes(source / name, MAX_INPUT) for name in expected}
    pristine = all(current[name] == originals[name] for name in expected)
    patched = all(current[name] == expected[name] for name in expected)
    if not pristine and not patched:
        raise ValueError('Source differs from both pristine and the exact reviewed patch')
    previous = prior_record(work)
    if previous and bool(previous.get('diagnostic')) != diagnostic:
        raise ValueError('Diagnostic and product builds require separate work directories')
    owned_work = work_outputs(work, previous)
    old_record = iron.bounded_bytes(work / 'build-record.json', MAX_INPUT) if previous else None
    inputs = overlay_inputs(diagnostic=diagnostic)
    payloads = {name: iron.bounded_bytes(path, MAX_INPUT) for name, path in inputs.items()}
    for name in existing.keys() - tracked:
        # Installer/bootstrap data, never a compiler input or native build output.
        if name == 'frikbot/dm-maps.pak':
            continue
        checksum = digest(iron.bounded_bytes(source / name, MAX_ARTIFACT))
        if name in payloads and checksum in (digest(payloads[name]), previous.get('overlay_destinations', {}).get(name)):
            continue
        if ((_GENERATED.fullmatch(name) or name in ('Quake/ironwail_libretro.so', 'frikbot/pak0.pak'))
                and checksum == previous.get('managed_source_files', {}).get(name)):
            continue
        raise ValueError('Source has unrecognized untracked files; preserve them and use a fresh tree')
    resource = 'Quake/ironwail.pak'
    source_resource = iron.bounded_bytes(source / resource, MAX_ARTIFACT) if resource in tracked else None
    return source, header, patch_path, patch, expected, pristine, inputs, payloads, owned_work, old_record, source_resource


def build_plan(work, *, core_only=False, diagnostic=False):
    """Pure command plan: no fetch, build, service, install or resource-Pak step."""
    source = work / 'ironwail'
    make_args = tuple('LIVE_QUAKE_GPU=0' if diagnostic and value == 'LIVE_QUAKE_GPU=1' else value for value in MAKE_ARGS)
    result = [{'command': ['/usr/bin/make', '-j2', 'ironwail_libretro.so', *make_args], 'cwd': source / 'Quake', 'timeout': 240}]
    if not (core_only or diagnostic):
        result += [{'command': ['/usr/bin/cc', '-std=c11', '-O2', '-Wall', '-Wextra',
                                '-I' + str(work / 'include'), str(ROOT / 'src/engine.c'), str(ROOT / 'src/audio.c'),
                                '-o', str(work / '.frontend-temporary'), '-ldl', '-lm', '-lasound', '-pthread'], 'timeout': 30},
                   {'command': [str(ROOT / 'scripts/build-viewer.sh'), '--build-dir', str(work)], 'timeout': 60}]
    return result


def build(work, *, core_only=False, diagnostic=False, frikbot_pack=None, frikbot_receipt=None):
    work = iron.private_dir(work)
    core_only = core_only or diagnostic
    os.umask(0o077)
    source, header, patch_path, patch, expected, pristine, inputs, payloads, owned_work, old_record, source_resource = preflight(work, diagnostic=diagnostic)
    source_inputs = {str(path.relative_to(ROOT)): payloads[name] for name, path in inputs.items()}
    source_inputs['patches/ironwail-desktop.patch'] = patch
    for name in ('scripts/bootstrap-ironwail.py', 'scripts/build-ironwail.py', 'scripts/bootstrap.py', 'scripts/build.py'):
        source_inputs[name] = iron.bounded_bytes(ROOT / name, MAX_INPUT)
    if not core_only:
        for name in ('src/engine.c', 'src/audio.c', 'src/audio.h', 'src/viewer.c',
                     'src/gpu_wayland.c', 'src/gpu_wayland.h', 'scripts/build-viewer.sh'):
            source_inputs[name] = iron.bounded_bytes(ROOT / name, MAX_INPUT)
        for name in PROTOCOLS:
            source_inputs['src/protocols/' + name + '.xml'] = iron.bounded_bytes(ROOT / 'src/protocols' / (name + '.xml'), MAX_INPUT)
    if pristine:
        for name, payload in expected.items():
            replace_bytes(source / name, payload)
    for name, payload in payloads.items():
        replace_bytes(source / name, payload)
    plans = build_plan(work, core_only=core_only, diagnostic=diagnostic)
    with private_log(work / 'ironwail-build.log') as log:
        run(plans[0]['command'], work, cwd=plans[0]['cwd'], timeout=plans[0]['timeout'], stdout=log, stderr=subprocess.STDOUT)
    artifacts = {'core': source / 'Quake/ironwail_libretro.so'}
    if not core_only:
        fd, temporary = tempfile.mkstemp(dir=work, prefix='.iron-frontend-')
        os.close(fd)
        try:
            command = list(plans[1]['command'])
            command[command.index('-o') + 1] = temporary
            run(command, work, timeout=plans[1]['timeout'])
            os.chmod(temporary, 0o700)
            destination = work / 'quake-engine'
            if os.path.lexists(destination):
                iron.bounded_bytes(destination, MAX_ARTIFACT)
            os.replace(temporary, destination)
        finally:
            Path(temporary).unlink(missing_ok=True)
        run(plans[2]['command'], work, timeout=plans[2]['timeout'])
        artifacts.update(frontend=work / 'quake-engine', viewer=work / 'quake-viewer')
    bot_receipt = None
    if frikbot_pack is not None:
        pack_payload = iron.bounded_bytes(frikbot_pack.resolve(), MAX_ARTIFACT)
        bot_receipt = json.loads(iron.bounded_bytes(frikbot_receipt.resolve(), MAX_INPUT))
        if (bot_receipt.get('local_pack_sha256') != digest(pack_payload)
                or bot_receipt.get('compiler_pin') != 'f937b9d88f71fc4429db5fe56c6a98d922711b2e'
                or bot_receipt.get('base_pin') != 'c0d1b91c74eb654365ac7755bc837e497caaca73'):
            raise ValueError('Local bot data differs from its pinned build receipt')
        directory = source / 'frikbot'
        if not directory.exists(): directory.mkdir(mode=0o700)
        iron.private_dir(directory)
        replace_bytes(directory / 'pak0.pak', pack_payload)
        artifacts['frikbot'] = directory / 'pak0.pak'
    if any(iron.bounded_bytes(ROOT / name, MAX_INPUT) != payload for name, payload in source_inputs.items()):
        raise ValueError('Project inputs changed during build; preserve artifacts and rebuild a frozen source')
    iron.verified_header(work)
    current = tree_files(source)
    resource = source / 'Quake/ironwail.pak'
    if (iron.bounded_bytes(resource, MAX_ARTIFACT) if os.path.lexists(resource) else None) != source_resource:
        raise ValueError('The source resource PAK must never be built, changed or copied')
    managed = {name: digest(iron.bounded_bytes(source / name, MAX_ARTIFACT)) for name in current
               if _GENERATED.fullmatch(name) or name in ('Quake/ironwail_libretro.so', 'frikbot/pak0.pak')}
    files = {str(ROOT / name): digest(payload) for name, payload in source_inputs.items()}
    files[str(header)] = digest(iron.bounded_bytes(header, iron.HEADER_BYTES, exact_size=iron.HEADER_BYTES))
    files.update({str(path): digest(iron.bounded_bytes(path, MAX_ARTIFACT)) for path in artifacts.values()})
    for name in WORK_OUTPUTS:
        if os.path.lexists(work / name):
            owned_work[name] = digest(iron.bounded_bytes(work / name, MAX_ARTIFACT))
    record = {'backend': 'ironwail', 'engine_commit': iron.ENGINE_COMMIT, 'source_date_epoch': int(SOURCE_DATE_EPOCH),
              'diagnostic': diagnostic,
              'components': list(artifacts), 'frikbot': bot_receipt, 'patch_sha256': digest(patch), 'header_sha256': iron.HEADER_SHA256,
              'source_hashes': {name: digest(payload) for name, payload in source_inputs.items()}, 'files': files,
              'overlay_destinations': {name: digest(payload) for name, payload in payloads.items()},
              'managed_source_files': managed,
              'managed_work_files': owned_work,
              'artifacts': {name: {'path': str(path.relative_to(work)), 'sha256': files[str(path)]}
                            for name, path in artifacts.items()}}
    record_path = work / 'build-record.json'
    if (iron.bounded_bytes(record_path, MAX_INPUT) if os.path.lexists(record_path) else None) != old_record:
        raise ValueError('Build ownership record changed during build; preserve it')
    replace_bytes(record_path, (json.dumps(record, indent=2) + '\n').encode())
    return record


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--work-dir', required=True, type=iron.private_dir)
    parser.add_argument('--core-only', action='store_true')
    parser.add_argument('--diagnostic', action='store_true', help='Dev-only offscreen capture core; use a separate work directory. No frontend or viewer.')
    parser.add_argument('--frikbot-pack', type=Path)
    parser.add_argument('--frikbot-receipt', type=Path)
    args = parser.parse_args(argv)
    if bool(args.frikbot_pack) != bool(args.frikbot_receipt): parser.error("both local bot pack and receipt required")
    record = build(args.work_dir, core_only=args.core_only, diagnostic=args.diagnostic,
                   frikbot_pack=args.frikbot_pack, frikbot_receipt=args.frikbot_receipt)
    print('Built private Ironwail components: ' + ', '.join(record['components']))


if __name__ == '__main__':
    main()
