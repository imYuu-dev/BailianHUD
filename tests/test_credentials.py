"""Credential import must work on a fresh running instance without exposing secrets."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import BailianHUD as B


class CredentialTests(unittest.TestCase):
    def setUp(self):
        self.process_env = dict(os.environ)
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)
        for mocked in (patch.dict(os.environ, {}, clear=True),
                       patch.object(B, 'AK_FILE', self.root / 'bss-credentials.json'),
                       patch.object(B, 'LEGACY_AK_FILE', self.root / 'missing-legacy.json'),
                       patch.object(B, 'LOG_FILE', self.root / 'test.log')):
            mocked.start()
            self.addCleanup(mocked.stop)
        self.store = B.Store(self.root)
        self.sync = B.CouponSync(self.store)
        self.service = B.ProxyService(self.store, 0, api_token='test-control-token')
        self.service.start()
        self.service.server.coupon_sync = self.sync
        self.addCleanup(self.service.stop)
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def call(self, body=None, token='test-control-token'):
        request = urllib.request.Request(
            f'http://127.0.0.1:{self.service.port}/api/credentials',
            data=json.dumps(body).encode() if body is not None else None,
            headers={'X-HUD-Token': token, 'Content-Type': 'application/json'})
        with self.opener.open(request, timeout=15) as response:
            return json.load(response)

    def test_import_validates_both_fields_and_never_returns_the_pair(self):
        self.assertFalse(self.call()['configured'])
        for body in ({}, {'aliyunAkId': 'id'}, [],
                     {'aliyunAkId': 'sk-wrong-key', 'aliyunAkSecret': 'secret'},
                     {'aliyunAkId': 'id', 'aliyunAkSecret': 'two words'}):
            with self.assertRaises(urllib.error.HTTPError) as error:
                self.call(body)
            self.assertEqual(error.exception.code, 400)
            self.assertFalse(B.AK_FILE.exists())
        pair = {'aliyunAkId': '  LTAI_TEST_ID  ', 'aliyunAkSecret': ' TEST_SECRET '}
        result = self.call(pair)
        self.assertTrue(result['configured'])
        self.assertEqual(json.loads(B.AK_FILE.read_text()),
                         {'aliyunAkId': 'LTAI_TEST_ID', 'aliyunAkSecret': 'TEST_SECRET'})
        self.assertNotIn('LTAI_TEST_ID', json.dumps(result))
        self.assertNotIn('TEST_SECRET', json.dumps(self.call()))
        self.assertEqual(list(self.root.glob('.ak-*.tmp')), [])

    def test_read_and_write_require_the_control_token(self):
        for body in (None, {'aliyunAkId': 'id', 'aliyunAkSecret': 'secret'}):
            with self.assertRaises(urllib.error.HTTPError) as error:
                self.call(body, token='')
            self.assertEqual(error.exception.code, 403)
        self.assertFalse(B.AK_FILE.exists())

    def test_failed_replace_preserves_previous_pair_and_cleans_temporary_file(self):
        old = {'aliyunAkId': 'old-id', 'aliyunAkSecret': 'old-secret'}
        self.call(old)
        with patch.object(B.os, 'replace', side_effect=PermissionError('synthetic failure')):
            with self.assertRaises(urllib.error.HTTPError) as error:
                self.call({'aliyunAkId': 'new-id', 'aliyunAkSecret': 'new-secret'})
        self.assertEqual(error.exception.code, 500)
        self.assertEqual(json.loads(B.AK_FILE.read_text()), old)
        self.assertEqual(list(self.root.glob('.ak-*.tmp')), [])

    def test_first_import_wakes_sync_and_account_change_rebinds_without_losing_usage(self):
        self.store.record('deepseek-v4.1-flash-1017', 100, 0, 0, 10, peak=True)
        coupon = {'id': 'coupon-a', 'no': 'no-a', 'status': 'Available',
                  'nominal': 200, 'balance': 123.45, 'desc': '百炼', 'remark': ''}
        with patch.object(B, 'fetch_coupons', return_value=[coupon]):
            self.sync.start()
            self.addCleanup(self.sync.join, 3)
            self.addCleanup(self.sync.stop)
            self.assertTrue(self.sync.is_alive())
            self.call({'aliyunAkId': 'id-a', 'aliyunAkSecret': 'secret-a'})
            deadline = time.monotonic() + 3
            while not self.sync.status()['ok'] and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(self.sync.status()['ok'])
            self.assertEqual(self.store.snapshot()['remain'], 123.45)
            coupon.update(id='coupon-b', no='no-b', balance=67.89)
            self.call({'aliyunAkId': 'id-b', 'aliyunAkSecret': 'secret-b'})
            deadline = time.monotonic() + 3
            while self.store.coupon_identity()['coupon_no'] != 'no-b' and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertEqual(self.store.coupon_identity()['coupon_no'], 'no-b')
            self.assertEqual(self.store.snapshot()['remain'], 67.89)
            self.assertEqual(self.store.snapshot()['totals']['count'], 1)
            self.sync.stop()
            self.sync.join(timeout=3)
        self.assertFalse(self.sync.is_alive())

    def test_poll_errors_redact_credentials_and_report_environment_priority(self):
        self.call({'aliyunAkId': 'test-id', 'aliyunAkSecret': 'test-secret'})
        with patch.object(B, 'fetch_coupons', side_effect=B.BssError('test-id test-secret denied')):
            self.sync._poll()
        result = self.call()
        self.assertEqual(result['sync']['error'], '*** *** denied')
        self.assertNotIn('test-secret', B.LOG_FILE.read_text())
        with patch.dict(os.environ, {'ALIYUN_AK_ID': 'environment-id', 'ALIYUN_AK_SECRET': 'environment-secret'}):
            self.assertTrue(self.call()['environment'])

    @unittest.skipUnless(os.name == 'nt', 'Windows command-line entry point')
    def test_powershell_import_uses_the_running_instance(self):
        server = self.service.server
        server.instance_id = 'test-script-instance'
        server.data_id = B.directory_id(self.root)
        B.publish_instance(self.root, self.service.port, server.instance_id)
        (self.root / '.hud_prefs.json').write_text(json.dumps({'api_token': 'test-control-token'}))
        script = Path(B.__file__).parent / 'Import-AK.ps1'
        command = (
            "function Read-Host { param($Prompt,[switch]$AsSecureString) "
            "if($AsSecureString){$value=[Security.SecureString]::new(); "
            "foreach($char in 'script-secret'.ToCharArray()){$value.AppendChar($char)}; $value}"
            "else{'script-id'} }; & $env:BAILIAN_TEST_SCRIPT"
        )
        # Return a sync result immediately so the public script exits after importing.
        original = self.sync.credential_status
        powershell = Path(self.process_env.get('SystemRoot', r'C:\Windows')) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
        with patch.object(self.sync, 'credential_status', side_effect=lambda: {
                **original(), 'sync': {'ok': True, 'error': ''}}):
            result = subprocess.run([str(powershell), '-NoProfile', '-ExecutionPolicy', 'Bypass',
                                     '-Command', command], capture_output=True, timeout=30,
                                    env={**self.process_env, 'BAILIAN_HUD_HOME': str(self.root),
                                         'BAILIAN_TEST_SCRIPT': str(script)})
        self.assertEqual(result.returncode, 0, result.stdout.decode(errors='replace') + result.stderr.decode(errors='replace'))
        self.assertEqual(json.loads(B.AK_FILE.read_text()),
                         {'aliyunAkId': 'script-id', 'aliyunAkSecret': 'script-secret'})
        self.assertNotIn(b'script-secret', result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
