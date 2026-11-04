import copy
import hashlib
import importlib.util
import json
import pathlib
import plistlib
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
TOOL = ROOT / 'claude_browser_bypass.py'
if TOOL.exists():
    spec = importlib.util.spec_from_file_location('claude_browser_bypass', TOOL)
    patch = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = patch
    spec.loader.exec_module(patch)
else:
    patch = None

MARKER = 'permission-mode lookup threw; showing card'
FUNCTION = ('function policy(e){if(!lookup||!e||!hosted())return!1;let t;'
            'try{t=lookup(e)}catch(e){return logger.warn("[PreviewOriginPolicy] ' + MARKER + ' %o",{error:e}),!1}'
            'return t==="auto"||t==="bypassPermissions"}')
PAYLOAD = ('const preceding=17;' + FUNCTION + 'var next=23;').encode()
SENTINEL = b'AGbevlPCksUGKNL8TSn7wGmJEuJsXb2A'


def file_integrity(payload, size=128):
    return {'algorithm': 'SHA256', 'hash': hashlib.sha256(payload).hexdigest(),
            'blockSize': size, 'blocks': [hashlib.sha256(payload[i:i + size]).hexdigest()
                                         for i in range(0, len(payload), size)]}


def dictionary_digest(integrity):
    return hashlib.sha256(''.join(key + integrity[key]['algorithm'] + integrity[key]['hash']
                                  for key in sorted(integrity)).encode()).digest()


def fixture(parent, *, version='4.16336.0', payload=PAYLOAD, slots=2):
    app = parent / 'Claude.app'
    resources = app / 'Contents/Resources'
    resources.mkdir(parents=True)
    untouched = b'console.log("synthetic fixture");'
    entries = {'index.fake.js': {'size': len(payload), 'offset': '0',
                                'integrity': file_integrity(payload)},
               'untouched.js': {'size': len(untouched), 'offset': str(len(payload)),
                                'integrity': file_integrity(untouched)}}
    header = json.dumps({'files': {'.vite': {'files': {'build': {'files': entries}}}}},
                        separators=(',', ':')).encode()
    padding = b'\0' * (-len(header) % 4)
    inner = struct.pack('<II', 4 + len(header) + len(padding), len(header)) + header + padding
    archive = struct.pack('<II', 4, len(inner)) + inner + payload + untouched
    (resources / 'app.asar').write_bytes(archive)
    integrity = {'Resources/app.asar': {'algorithm': 'SHA256',
                                      'hash': hashlib.sha256(header).hexdigest()},
                 'Resources/ion-dist.asar': {'algorithm': 'SHA256', 'hash': '1' * 64}}
    info = {'CFBundleIdentifier': 'com.anthropic.claudefordesktop',
            'CFBundleShortVersionString': version, 'ElectronAsarIntegrity': integrity,
            'UnrelatedSetting': ['preserve', 123]}
    (app / 'Contents/Info.plist').write_bytes(plistlib.dumps(info))
    framework = app / 'Contents/Frameworks/Electron Framework.framework/Versions/A/Electron Framework'
    framework.parent.mkdir(parents=True)
    framework.write_bytes(b'SYNTHETIC' + (SENTINEL + b'\x01\x01' + dictionary_digest(integrity)
                                        + b'padding') * slots)
    return app


class PatchTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(patch, 'The public patch tool has not been implemented yet')
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = pathlib.Path(self.temp.name)

    def test_mode_policy_and_unrelated_javascript_are_preserved(self):
        result = patch.patch_module(PAYLOAD)
        self.assertEqual(len(result), len(PAYLOAD))
        self.assertTrue(result.startswith(b'const preceding=17;'))
        self.assertTrue(result.endswith(b'var next=23;'))
        self.assertEqual(patch.patch_module(result), result)
        if not shutil.which('node'):
            self.skipTest('Node is needed only for policy execution tests')
        program = r'''
const vm=require('node:vm'), assert=require('node:assert/strict');
const source=JSON.parse(process.argv[1]);
function decide(mode, official=false, session='session', missing=false, throws=false) {
  return vm.runInNewContext(source+';policy('+JSON.stringify(session)+')', {
    lookup:missing?null:()=>{if(throws)throw Error('offline');return mode;},
    hosted:()=>official,logger:{warn(){}}
  });
}
assert.equal(decide('bypassPermissions'),true);
for (const mode of ['auto','default','acceptEdits','plan',undefined])
  assert.equal(decide(mode),false);
assert.equal(decide('auto',true),true);
assert.equal(decide('bypassPermissions',true),true);
assert.equal(decide('default',true),false);
assert.equal(decide('bypassPermissions',false,null),false);
assert.equal(decide('bypassPermissions',false,'session',true),false);
assert.equal(decide('bypassPermissions',false,'session',false,true),false);
'''
        subprocess.run(['node', '-e', program, json.dumps(result.decode())], check=True)

    def test_bundle_patch_preserves_integrity_and_is_idempotent(self):
        app = fixture(self.root)
        before_info = plistlib.loads((app / 'Contents/Info.plist').read_bytes())
        before = patch.read_archive(app)
        self.assertEqual(patch.inspect_bundle(app)['state'], 'stock')
        patch.patch_bundle(app)
        self.assertEqual(patch.inspect_bundle(app)['state'], 'patched')
        after = patch.read_archive(app)
        self.assertEqual(before['files']['untouched.js'], after['files']['untouched.js'])
        info = plistlib.loads((app / 'Contents/Info.plist').read_bytes())
        self.assertEqual(info['UnrelatedSetting'], before_info['UnrelatedSetting'])
        self.assertNotEqual(info['ElectronAsarIntegrity'], before_info['ElectronAsarIntegrity'])
        snapshot = {p: p.read_bytes() for p in app.rglob('*') if p.is_file()}
        patch.patch_bundle(app)
        self.assertEqual(snapshot, {p: p.read_bytes() for p in snapshot})

    def test_unknown_version_is_rejected_before_any_write(self):
        app = fixture(self.root, version='99.0.0')
        self.assert_rejected_without_writes(app)

    def test_unknown_function_shape_is_rejected_before_any_write(self):
        app = fixture(self.root, payload=PAYLOAD.replace(b'!hosted()', b'!newPolicy()&&false'))
        self.assert_rejected_without_writes(app)

    def test_multiple_policy_functions_are_rejected(self):
        app = fixture(self.root, payload=PAYLOAD + PAYLOAD)
        self.assert_rejected_without_writes(app)

    def test_tampered_module_is_rejected(self):
        app = fixture(self.root)
        p = app / 'Contents/Resources/app.asar'
        data = p.read_bytes().replace(b'preceding=17', b'preceding=18')
        p.write_bytes(data)
        self.assert_rejected_without_writes(app)

    def test_missing_framework_digest_is_rejected_before_any_write(self):
        self.assert_rejected_without_writes(fixture(self.root, slots=0))

    def test_corrupted_framework_digest_is_rejected_before_any_write(self):
        app = fixture(self.root)
        framework = patch.framework_path(app)
        data = framework.read_bytes()
        offset = data.index(SENTINEL) + len(SENTINEL) + 2
        framework.write_bytes(data[:offset] + bytes([data[offset] ^ 1]) + data[offset + 1:])
        self.assert_rejected_without_writes(app)

    def test_local_signing_only_changes_required_entitlements(self):
        original = {'com.apple.application-identifier': 'VENDOR.app',
                    'com.apple.developer.team-identifier': 'VENDOR',
                    'keychain-access-groups': ['VENDOR.secret'],
                    'com.apple.security.cs.allow-jit': True,
                    'com.apple.security.virtualization': True}
        saved = copy.deepcopy(original)
        result = patch.local_entitlements(original)
        self.assertEqual(original, saved)
        self.assertNotIn('keychain-access-groups', result)
        self.assertNotIn('com.apple.application-identifier', result)
        self.assertNotIn('com.apple.developer.team-identifier', result)
        self.assertTrue(result['com.apple.security.cs.disable-library-validation'])
        self.assertTrue(result['com.apple.security.virtualization'])
        self.assertTrue(result['com.apple.security.cs.allow-jit'])

    def test_running_target_is_rejected_but_other_claude_copy_is_allowed(self):
        app = self.root / 'Claude.app'
        patch.ensure_not_running(app, '/Other/Claude.app/Contents/MacOS/Claude\n')
        for process in ['Contents/MacOS/Claude', 'Contents/Frameworks/Claude Helper.app/Contents/MacOS/Claude Helper']:
            with self.assertRaises(patch.PatchError):
                patch.ensure_not_running(app, str(app / process) + '\n')

    def test_atomic_replacement_restores_original_on_install_failure(self):
        app, staged, rollback = [self.root / name for name in ['Claude.app', 'stage.app', 'old.app']]
        app.mkdir(); staged.mkdir()
        (app / 'identity').write_text('original')
        (staged / 'identity').write_text('patched')
        real_replace = patch.os.replace
        def fail_second(source, destination):
            if pathlib.Path(source) == staged:
                raise OSError('simulated install failure')
            return real_replace(source, destination)
        with mock.patch.object(patch.os, 'replace', side_effect=fail_second):
            with self.assertRaises(OSError):
                patch.atomic_replace(app, staged, rollback)
        self.assertEqual((app / 'identity').read_text(), 'original')
        self.assertFalse(rollback.exists())

    def test_cli_help_works_without_claude_or_macos(self):
        result = subprocess.run([sys.executable, str(TOOL), '--help'], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0)
        for word in ['check', 'apply', 'restore']:
            self.assertIn(word, result.stdout)

    def platform_stubs(self):
        self.real_copy = lambda source, destination: shutil.copytree(source, destination, symlinks=True)
        for name, replacement in [('require_macos', lambda: None),
                                  ('ensure_not_running', lambda app: None),
                                  ('verify_signature', lambda app, official=False: None),
                                  ('sign_bundle', lambda app: None),
                                  ('copy_bundle', self.real_copy)]:
            stub = mock.patch.object(patch, name, side_effect=replacement)
            self.addCleanup(stub.stop)
            stub.start()

    def test_apply_backup_and_restore_roundtrip(self):
        self.platform_stubs()
        app = fixture(self.root)
        original = (app / 'Contents/Resources/app.asar').read_bytes()
        try:
            result = patch.apply_patch(app, self.root / 'state', accept_local_signing=True)
        except patch.PatchError as error:
            self.fail(f'Expected a successful transactional apply: {error}')
        self.assertEqual(result['state'], 'patched')
        backup = pathlib.Path(result['backup'])
        self.assertEqual((backup / 'original.app/Contents/Resources/app.asar').read_bytes(), original)
        repeated = patch.apply_patch(app, self.root / 'state')
        self.assertEqual(repeated['state'], 'patched')
        result = patch.restore_backup(app, backup)
        self.assertEqual(result['state'], 'stock')
        self.assertEqual((app / 'Contents/Resources/app.asar').read_bytes(), original)
        self.assertEqual(json.loads((backup / 'manifest.json').read_text())['status'], 'restored')

    def test_apply_requires_explicit_signing_acceptance(self):
        self.platform_stubs()
        app = fixture(self.root)
        before = (app / 'Contents/Resources/app.asar').read_bytes()
        with self.assertRaisesRegex(patch.PatchError, 'accept-local-signing'):
            patch.apply_patch(app, self.root / 'state')
        self.assertEqual((app / 'Contents/Resources/app.asar').read_bytes(), before)
        self.assertFalse((self.root / 'state').exists())

    def test_signing_failure_does_not_replace_installed_app(self):
        self.platform_stubs()
        app = fixture(self.root)
        before = (app / 'Contents/Resources/app.asar').read_bytes()
        with mock.patch.object(patch, 'sign_bundle', side_effect=patch.PatchError('signing failed')):
            with self.assertRaisesRegex(patch.PatchError, 'signing failed'):
                patch.apply_patch(app, self.root / 'state', accept_local_signing=True)
        self.assertEqual((app / 'Contents/Resources/app.asar').read_bytes(), before)
        self.assertTrue(list((self.root / 'state/backups').glob('*/original.app')))

    def test_failed_postinstall_verification_restores_original(self):
        self.platform_stubs()
        app = fixture(self.root)
        before = (app / 'Contents/Resources/app.asar').read_bytes()
        def signature_check(bundle, official=False):
            if pathlib.Path(bundle).resolve() == app.resolve() and patch.inspect_bundle(app)['state'] == 'patched':
                raise patch.PatchError('post-install failure')
        with mock.patch.object(patch, 'verify_signature', side_effect=signature_check):
            with self.assertRaisesRegex(patch.PatchError, 'post-install failure'):
                patch.apply_patch(app, self.root / 'state', accept_local_signing=True)
        self.assertEqual((app / 'Contents/Resources/app.asar').read_bytes(), before)

    def test_restore_can_repair_a_missing_or_corrupted_installed_archive(self):
        self.platform_stubs()
        app = fixture(self.root)
        before = (app / 'Contents/Resources/app.asar').read_bytes()
        result = patch.apply_patch(app, self.root / 'state', accept_local_signing=True)
        archive = app / 'Contents/Resources/app.asar'
        archive.write_bytes(b'broken')
        patch.restore_backup(app, pathlib.Path(result['backup']))
        self.assertEqual(archive.read_bytes(), before)
        archive.unlink()
        patch.restore_backup(app, pathlib.Path(result['backup']))
        self.assertEqual(archive.read_bytes(), before)

    def test_restore_refuses_wrong_target_or_tampered_backup(self):
        self.platform_stubs()
        app = fixture(self.root)
        result = patch.apply_patch(app, self.root / 'state', accept_local_signing=True)
        backup = pathlib.Path(result['backup'])
        with self.assertRaises(patch.PatchError):
            patch.restore_backup(self.root / 'different.app', backup)
        (backup / 'original.app/Contents/Resources/app.asar').write_bytes(b'broken')
        with self.assertRaises(patch.PatchError):
            patch.restore_backup(app, backup)
        self.assertEqual(patch.inspect_bundle(app)['state'], 'patched')

    def test_mutation_is_rejected_off_macos(self):
        with mock.patch.object(patch.platform, 'system', return_value='Linux'):
            with self.assertRaisesRegex(patch.PatchError, 'macOS'):
                patch.apply_patch(self.root / 'Claude.app', self.root / 'state', accept_local_signing=True)

    def test_backup_destination_inside_the_app_is_rejected_before_copy(self):
        self.platform_stubs()
        app = fixture(self.root)
        with mock.patch.object(patch, 'copy_bundle', side_effect=AssertionError('Unsafe recursive backup attempted')):
            with self.assertRaisesRegex(patch.PatchError, 'inside'):
                patch.apply_patch(app, app / 'Contents/state', accept_local_signing=True)

    def test_nonobject_backup_manifest_is_rejected(self):
        self.platform_stubs()
        backup = self.root / 'backup'
        backup.mkdir()
        (backup / 'manifest.json').write_text('[]')
        with self.assertRaises(patch.PatchError):
            patch.restore_backup(self.root / 'Claude.app', backup)

    def assert_rejected_without_writes(self, app):
        before = {p: p.read_bytes() for p in app.rglob('*') if p.is_file()}
        with self.assertRaises(patch.PatchError):
            patch.patch_bundle(app)
        self.assertEqual(before, {p: p.read_bytes() for p in before})


if __name__ == '__main__':
    unittest.main()
