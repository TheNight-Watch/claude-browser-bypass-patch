#!/usr/bin/env python3
# Copyright (c) 2026 TheNight-Watch
# SPDX-License-Identifier: MIT
"""Community macOS patch: honor session Bypass in third-party Claude Browser use.

Only patches an application already installed by its owner. No downloads,
credentials, telemetry, user-configuration edits or automatic app termination.
"""
from __future__ import annotations

import argparse
import contextlib
import copy
import datetime
import hashlib
import json
import mmap
import os
from pathlib import Path
import platform
import plistlib
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import uuid

VERSION = '0.1.0'
SUPPORTED_VERSIONS = {'4.16336.0'}
BUNDLE_ID = 'com.anthropic.claudefordesktop'
VENDOR_TEAM = 'Q6L2SF6YDW'
MARKER = b'permission-mode lookup threw; showing card'
SENTINEL = b'AGbevlPCksUGKNL8TSn7wGmJEuJsXb2A'
DEFAULT_APP = Path('/Applications/Claude.app')
DEFAULT_STATE = Path.home() / 'Library/Application Support/claude-browser-bypass-patch'
HELPERS = ('Claude Helper.app', 'Claude Helper (GPU).app',
           'Claude Helper (Plugin).app', 'Claude Helper (Renderer).app')
IDENTITY_KEYS = ('com.apple.application-identifier', 'com.apple.developer.team-identifier',
                 'keychain-access-groups')


class PatchError(Exception):
    """A precondition failed; do not guess or broaden the patch."""


def require(condition, message):
    if not condition:
        raise PatchError(message)


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def file_hash(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def read_info(app):
    try:
        data = plistlib.loads((Path(app) / 'Contents/Info.plist').read_bytes())
    except (OSError, plistlib.InvalidFileException) as error:
        raise PatchError(f'Cannot read app Info.plist: {error}') from error
    require(data.get('CFBundleIdentifier') == BUNDLE_ID, 'This is not the expected Claude app bundle.')
    require(data.get('CFBundleShortVersionString') in SUPPORTED_VERSIONS,
            'Unsupported Claude version. Supported: ' + ', '.join(sorted(SUPPORTED_VERSIONS)))
    return data


def read_archive(app):
    path = Path(app) / 'Contents/Resources/app.asar'
    try:
        with path.open('rb') as stream:
            prefix = stream.read(16)
            require(len(prefix) == 16, 'Truncated ASAR header.')
            layout = struct.unpack('<4I', prefix)
            require(layout[0] == 4 and 0 < layout[3] <= 32 * 1024 * 1024
                    and layout[1] >= layout[3] + 8, 'Unsupported ASAR layout.')
            raw_header = stream.read(layout[3])
            header = json.loads(raw_header)
            entries = header['files']['.vite']['files']['build']['files']
            files = {}
            for name, entry in entries.items():
                if not name.endswith('.js') or entry.get('unpacked'):
                    continue
                size, offset = entry['size'], int(entry['offset'])
                require(isinstance(size, int) and 0 <= size <= 64 * 1024 * 1024 and offset >= 0,
                        'Invalid ASAR file bounds.')
                stream.seek(8 + layout[1] + offset)
                payload = stream.read(size)
                require(len(payload) == size, 'Truncated ASAR file.')
                files[name] = payload
    except (OSError, KeyError, ValueError, TypeError) as error:
        raise PatchError(f'Cannot parse the expected ASAR layout: {error}') from error
    return {'path': path, 'layout': layout, 'header': header, 'raw_header': raw_header,
            'entries': entries, 'files': files}


def permission_function(payload):
    require(payload.count(MARKER) == 1, 'Expected exactly one browser permission-mode function.')
    marker = payload.index(MARKER)
    start = payload.rfind(b'function ', 0, marker)
    boundary = re.search(rb'}[ \t]*var ', payload[marker:marker + 2048])
    end = marker + boundary.start() if boundary else -1
    require(start >= 0 and end >= 0, 'Unrecognized permission-function boundaries.')
    source = payload[start:end + 1].decode('utf-8')
    identifier = r'[A-Za-z_$][\w$]*'
    prefix = (rf'function (?P<name>{identifier})\(e\)\{{if\(!(?P<lookup>{identifier})\|\|!e')
    middle = (rf'\)return!1;let t;try\{{t=(?P=lookup)\(e\)\}}catch\(e\)\{{return '
              rf'(?P<logger>{identifier})\.warn\("\[PreviewOriginPolicy\] '
              rf'permission-mode lookup threw; showing card %o",\{{error:e\}}\),!1\}}')
    stock = re.fullmatch(prefix + rf'\|\|!(?P<hosted>{identifier})\(\)' + middle
                         + r'return t==="auto"\|\|t==="bypassPermissions"\}', source)
    patched = re.fullmatch(prefix + middle + rf'return t==="bypassPermissions"\|\|'
                           rf'(?P<hosted>{identifier})\(\)&&t==="auto"\}}', source)
    match = stock or patched
    require(match is not None, 'Unknown permission-function shape; refusing to patch.')
    return {'start': start, 'end': end + 1, 'source': source,
            'state': 'stock' if stock else 'patched', **match.groupdict()}


def patch_module(payload):
    site = permission_function(payload)
    if site['state'] == 'patched':
        return payload
    guard = f"if(!{site['lookup']}||!e||!{site['hosted']}())return!1;"
    changed = site['source'].replace(guard, f"if(!{site['lookup']}||!e)return!1;", 1)
    changed = changed.replace('return t==="auto"||t==="bypassPermissions"',
                              f'return t==="bypassPermissions"||{site["hosted"]}()&&t==="auto"', 1)
    require(len(changed.encode()) <= site['end'] - site['start'], 'Patch would move ASAR offsets.')
    replacement = changed.encode().ljust(site['end'] - site['start'], b' ')
    return payload[:site['start']] + replacement + payload[site['end']:]


def framework_path(app):
    return Path(app) / 'Contents/Frameworks/Electron Framework.framework/Versions/A/Electron Framework'


def dictionary_digest(integrity):
    require(isinstance(integrity, dict) and integrity, 'Missing integrity dictionary.')
    for value in integrity.values():
        require(isinstance(value, dict) and value.get('algorithm') == 'SHA256'
                and re.fullmatch(r'[0-9a-f]{64}', value.get('hash', '')),
                'Unsupported integrity dictionary.')
    return hashlib.sha256(''.join(key + integrity[key]['algorithm'] + integrity[key]['hash']
                                  for key in sorted(integrity)).encode()).digest()


def framework_slots(app, expected):
    try:
        with framework_path(app).open('rb') as stream, mmap.mmap(stream.fileno(), 0, access=mmap.ACCESS_READ) as data:
            cursor, slots = 0, []
            while (cursor := data.find(SENTINEL, cursor)) >= 0:
                require(data[cursor + 32:cursor + 34] == b'\x01\x01',
                        'Unknown or disabled embedded integrity slot.')
                require(data[cursor + 34:cursor + 66] == expected,
                        'Embedded framework digest does not match Info.plist.')
                slots.append(cursor + 34)
                cursor += len(SENTINEL)
            require(1 <= len(slots) <= 2, 'Expected one or two embedded integrity slots.')
            return slots
    except (OSError, ValueError) as error:
        raise PatchError(f'Cannot inspect Electron framework: {error}') from error


def inspect_bundle(app):
    info = read_info(app)
    archive = read_archive(app)
    integrity = info.get('ElectronAsarIntegrity', {})
    require(sha256(archive['raw_header']) == integrity.get('Resources/app.asar', {}).get('hash'),
            'ASAR header hash does not match Info.plist.')
    targets = [name for name, payload in archive['files'].items() if MARKER in payload]
    require(len(targets) == 1, 'Expected exactly one browser policy module.')
    name = targets[0]
    entry, payload = archive['entries'][name], archive['files'][name]
    file_integrity = entry.get('integrity', {})
    block_size = file_integrity.get('blockSize', 0)
    require(file_integrity.get('algorithm') == 'SHA256' and isinstance(block_size, int)
            and 0 < block_size <= 64 * 1024 * 1024, 'Unknown ASAR block format.')
    require(sha256(payload) == file_integrity.get('hash'), 'Policy module hash mismatch.')
    require([sha256(payload[i:i + block_size]) for i in range(0, len(payload), block_size)]
            == file_integrity.get('blocks'), 'Policy module block hash mismatch.')
    site = permission_function(payload)
    slots = framework_slots(app, dictionary_digest(integrity))
    return {'version': info['CFBundleShortVersionString'], 'state': site['state'],
            'module': name, 'framework_slots': len(slots)}


def patch_bundle(app):
    """Modify an OFFLINE STAGING COPY only. CLI apply never patches a live bundle."""
    status = inspect_bundle(app)  # Validate every precondition before writing anything.
    if status['state'] == 'patched':
        return status
    info_path = Path(app) / 'Contents/Info.plist'
    info_bytes = info_path.read_bytes()
    info = plistlib.loads(info_bytes)
    archive = read_archive(app)
    name = status['module']
    payload = patch_module(archive['files'][name])
    entry = archive['entries'][name]
    previous = entry['integrity']
    updated = copy.deepcopy(previous)
    updated['hash'] = sha256(payload)
    size = previous['blockSize']
    updated['blocks'] = [sha256(payload[i:i + size]) for i in range(0, len(payload), size)]
    old_json = json.dumps(previous, separators=(',', ':')).encode()
    new_json = json.dumps(updated, separators=(',', ':')).encode()
    require(len(old_json) == len(new_json) and archive['raw_header'].count(old_json) == 1,
            'Cannot update this ASAR header without moving offsets.')
    raw_header = archive['raw_header'].replace(old_json, new_json, 1)
    slots = framework_slots(app, dictionary_digest(info['ElectronAsarIntegrity']))
    info['ElectronAsarIntegrity']['Resources/app.asar']['hash'] = sha256(raw_header)
    digest = dictionary_digest(info['ElectronAsarIntegrity'])
    with archive['path'].open('r+b') as stream:
        stream.seek(8 + archive['layout'][1] + int(entry['offset']))
        stream.write(payload)
        stream.seek(16)
        stream.write(raw_header)
    with framework_path(app).open('r+b') as stream:
        for offset in slots:
            stream.seek(offset)
            stream.write(digest)
    info_path.write_bytes(plistlib.dumps(info, sort_keys=False,
                          fmt=plistlib.FMT_BINARY if info_bytes.startswith(b'bplist') else plistlib.FMT_XML))
    return inspect_bundle(app)


def local_entitlements(original):
    result = copy.deepcopy(original)
    for key in IDENTITY_KEYS:
        result.pop(key, None)
    result['com.apple.security.cs.disable-library-validation'] = True
    return result


def command(arguments):
    result = subprocess.run([str(x) for x in arguments], capture_output=True, text=True)
    require(result.returncode == 0, f'{Path(str(arguments[0])).name} failed: '
            + (result.stderr or result.stdout).strip()[:1600])
    return result


def require_macos():
    require(platform.system() == 'Darwin', 'Application changes are supported only on macOS.')
    for name in ('codesign', 'ditto', 'ps'):
        require(shutil.which(name), f'Required macOS command not found: {name}')


def ensure_not_running(app, processes=None):
    if processes is None:
        processes = command(['/bin/ps', '-axo', 'comm=']).stdout
    prefix = str(Path(app).resolve() / 'Contents') + '/'
    require(not any(str(Path(line.strip()).resolve()).startswith(prefix)
                    for line in processes.splitlines() if line.strip()),
            'Claude or one of its helpers is running. Fully quit Claude first; this tool never stops your work.')


def verify_signature(app, official=False):
    command(['/usr/bin/codesign', '--verify', '--deep', '--strict', app])
    if official:
        details = command(['/usr/bin/codesign', '-dv', '--verbose=2', app]).stderr
        require(f'TeamIdentifier={VENDOR_TEAM}' in details,
                'The unpatched app must have the expected original vendor signature.')


def sign_bundle(app):
    command(['/usr/bin/codesign', '--force', '--sign', '-', '--timestamp=none',
             '--preserve-metadata=identifier,flags', Path(app) / 'Contents/Frameworks/Electron Framework.framework'])
    with tempfile.TemporaryDirectory(prefix='claude-local-signing-') as folder:
        for index, bundle in enumerate([*(Path(app) / 'Contents/Frameworks' / name for name in HELPERS), Path(app)]):
            require(bundle.is_dir(), 'A required Electron helper is missing.')
            result = command(['/usr/bin/codesign', '-d', '--entitlements', ':-', bundle])
            entitlements = plistlib.loads(result.stdout.encode()) if result.stdout.strip() else {}
            path = Path(folder) / f'{index}.plist'
            path.write_bytes(plistlib.dumps(local_entitlements(entitlements)))
            command(['/usr/bin/codesign', '--force', '--sign', '-', '--options', 'runtime',
                     '--timestamp=none', '--entitlements', path, bundle])
    verify_signature(app)


def copy_bundle(source, destination):
    command(['/usr/bin/ditto', source, destination])


def atomic_replace(app, staged, rollback):
    os.replace(app, rollback)
    try:
        os.replace(staged, app)
    except BaseException:
        os.replace(rollback, app)
        raise


@contextlib.contextmanager
def application_lock(app):
    import fcntl
    path = Path(app).parent / '.claude-browser-bypass-patch.lock'
    with path.open('a') as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise PatchError('Another patch or restore is in progress.') from error
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def apply_patch(app, state_dir, *, accept_local_signing=False):
    require_macos()
    app = Path(app).resolve()
    state_dir = Path(state_dir).expanduser().resolve()
    require(state_dir != app and app not in state_dir.parents,
            'The backup/state directory must not be inside the app bundle.')
    current = inspect_bundle(app)
    if current['state'] == 'patched':
        verify_signature(app)
        return {**current, 'message': 'Already patched; no changes made.'}
    require(accept_local_signing,
            'Pass --accept-local-signing after reading the signing tradeoffs. '
            'The patch changes app-scoped library validation and vendor shared-Keychain identity.')
    with application_lock(app):
        ensure_not_running(app)
        current = inspect_bundle(app)
        require(current['state'] == 'stock', 'The app changed while acquiring the installation lock.')
        verify_signature(app, official=True)
        original_hash = file_hash(app / 'Contents/Resources/app.asar')
        identifier = (datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
                      + '-' + uuid.uuid4().hex[:8])
        backup = state_dir / 'backups' / identifier
        backup.mkdir(parents=True, exist_ok=False)
        original = backup / 'original.app'
        copy_bundle(app, original)
        verify_signature(original, official=True)
        require(file_hash(original / 'Contents/Resources/app.asar') == original_hash,
                'The verified backup does not match the source app.')
        record = {'schema': 1, 'tool_version': VERSION, 'app': str(app),
                  'version': current['version'], 'original_asar_sha256': original_hash,
                  'status': 'backed_up'}
        manifest = backup / 'manifest.json'
        manifest.write_text(json.dumps(record, indent=2) + '\n')
        temporary = Path(tempfile.mkdtemp(prefix='.claude-browser-bypass-', dir=app.parent))
        staged, rollback = temporary / 'Claude.app', temporary / 'previous.app'
        finished = False
        try:
            copy_bundle(original, staged)
            patch_bundle(staged)
            sign_bundle(staged)
            inspect_bundle(staged)
            ensure_not_running(app)
            require(file_hash(app / 'Contents/Resources/app.asar') == original_hash,
                    'Claude changed during preparation, possibly due to an update. Installation cancelled.')
            atomic_replace(app, staged, rollback)
            try:
                verify_signature(app)
                result = inspect_bundle(app)
            except BaseException:
                os.replace(app, temporary / 'failed.app')
                os.replace(rollback, app)
                raise
            record.update(status='installed', patched_asar_sha256=file_hash(app / 'Contents/Resources/app.asar'))
            manifest.write_text(json.dumps(record, indent=2) + '\n')
            finished = True
            return {**result, 'backup': str(backup),
                    'message': 'Installed. Reopen Claude and choose Bypass for the session.'}
        finally:
            # Keep a displaced app for manual recovery if rollback itself failed.
            if finished or not rollback.exists():
                shutil.rmtree(temporary)


def restore_backup(app, backup):
    require_macos()
    app, backup = Path(app).resolve(), Path(backup).expanduser().resolve()
    try:
        record = json.loads((backup / 'manifest.json').read_text())
    except (OSError, ValueError) as error:
        raise PatchError(f'Cannot read the backup manifest: {error}') from error
    require(isinstance(record, dict), 'The backup manifest must be a JSON object.')
    require(record.get('schema') == 1 and isinstance(record.get('app'), str)
            and Path(record['app']).resolve() == app,
            'This backup belongs to a different application path.')
    original = backup / 'original.app'
    require(file_hash(original / 'Contents/Resources/app.asar') == record.get('original_asar_sha256'),
            'Backup checksum mismatch.')
    require(inspect_bundle(original)['state'] == 'stock', 'The backup is not an unpatched supported app.')
    require(read_info(original)['CFBundleShortVersionString'] == record.get('version'),
            'Backup version mismatch.')
    with application_lock(app):
        ensure_not_running(app)
        require(read_info(app)['CFBundleShortVersionString'] == record['version'],
                'Restore will not silently downgrade a different installed version.')
        verify_signature(original, official=True)
        archive = app / 'Contents/Resources/app.asar'
        fingerprint = file_hash(archive) if archive.exists() else None
        temporary = Path(tempfile.mkdtemp(prefix='.claude-browser-restore-', dir=app.parent))
        staged, rollback = temporary / 'Claude.app', temporary / 'previous.app'
        finished = False
        try:
            copy_bundle(original, staged)
            verify_signature(staged, official=True)
            ensure_not_running(app)
            require((file_hash(archive) if archive.exists() else None) == fingerprint,
                    'The installed app changed during restore preparation; restore cancelled.')
            atomic_replace(app, staged, rollback)
            try:
                verify_signature(app, official=True)
                result = inspect_bundle(app)
            except BaseException:
                os.replace(app, temporary / 'failed.app')
                os.replace(rollback, app)
                raise
            record['status'] = 'restored'
            (backup / 'manifest.json').write_text(json.dumps(record, indent=2) + '\n')
            finished = True
            return {**result, 'message': 'Official app restored. App configuration and Keychain were not edited.'}
        finally:
            if finished or not rollback.exists():
                shutil.rmtree(temporary)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--version', action='version', version=VERSION)
    commands = parser.add_subparsers(dest='action', required=True)
    check = commands.add_parser('check', help='Read-only compatibility and integrity check')
    check.add_argument('--app', type=Path, default=DEFAULT_APP)
    apply = commands.add_parser('apply', help='Back up, patch a staging copy, sign, verify and install')
    apply.add_argument('--app', type=Path, default=DEFAULT_APP)
    apply.add_argument('--state-dir', type=Path, default=DEFAULT_STATE)
    apply.add_argument('--accept-local-signing', action='store_true', help='Accept app-scoped signing and shared-Keychain tradeoffs')
    restore = commands.add_parser('restore', help='Restore a verified official backup')
    restore.add_argument('--app', type=Path, default=DEFAULT_APP)
    restore.add_argument('--backup', type=Path, required=True, help='Backup directory printed by apply')
    args = parser.parse_args(argv)
    try:
        if args.action == 'check':
            result = inspect_bundle(args.app.resolve())
            if platform.system() == 'Darwin':
                verify_signature(args.app)
                result['signature_verified'] = True
        elif args.action == 'apply':
            result = apply_patch(args.app, args.state_dir, accept_local_signing=args.accept_local_signing)
        else:
            result = restore_backup(args.app, args.backup)
        print(json.dumps(result, indent=2))
        return 0
    except (PatchError, OSError, ValueError, plistlib.InvalidFileException) as error:
        print(f'Error: {error}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
