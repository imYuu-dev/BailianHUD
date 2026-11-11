"""Regression coverage for real socket conflicts, ownership and authenticated controls."""
import json
import queue
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import BailianHUD as B
import hud_runtime as R


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.log = patch.object(B, 'LOG_FILE', self.root / 'test.log')
        self.log.start()
        self.store = B.Store(self.root)

    def tearDown(self):
        self.doCleanups()
        self.log.stop()
        self.temp.cleanup()

    def server(self):
        server = B.ProxyService(self.store, 0, api_token='synthetic-token')
        server.start()
        self.addCleanup(server.stop)
        return server

    def test_unwritable_instance_directory_exits_with_a_readable_message(self):
        with patch.object(B, 'BASE_DIR', self.root), \
             patch.object(B, 'set_dpi_awareness'), \
             patch.object(B, 'discover', return_value=None), \
             patch.object(B.InstanceLock, 'acquire', side_effect=PermissionError('denied')), \
             patch.object(B, '_msgbox') as message:
            self.assertEqual(B.main([]), 2)
        self.assertEqual(message.call_count, 1)
        self.assertIn(str(self.root), message.call_args.args[0])

    def test_ephemeral_port_discovery_and_stale_identity(self):
        service = self.server()
        self.assertGreater(service.port, 0)
        service.server.instance_id = 'instance-a'
        service.server.data_id = R.directory_id(self.root)
        R.publish_instance(self.root, service.port, 'instance-a')
        self.assertEqual(R.discover(self.root, service.port)['port'], service.port)
        R.publish_instance(self.root, service.port, 'stale-instance')
        self.assertIsNone(R.discover(self.root, service.port))
        R.clear_instance(self.root, 'instance-a')
        self.assertTrue((self.root / R.INSTANCE_FILE).exists())
        R.clear_instance(self.root, 'stale-instance')
        self.assertFalse((self.root / R.INSTANCE_FILE).exists())

    def test_control_routes_require_token_and_return_to_ui_queue(self):
        service = self.server()
        commands = queue.Queue()
        service.server.hud_control = commands.put
        service.server.hud_state = {'shown': False, 'user_hidden': True}
        for action in ('show', 'hide', 'toggle', 'panel', 'quit'):
            with self.assertRaises(Exception) as denied:
                R.local_json(service.port, '/api/hud/' + action, post=True)
            self.assertEqual(denied.exception.code, 403)
            self.assertTrue(commands.empty())
            self.assertTrue(R.local_json(service.port, '/api/hud/' + action,
                                        token='synthetic-token', post=True)['ok'])
            self.assertEqual(commands.get_nowait(), action)
        result = R.local_json(service.port, '/api/hud/status', token='synthetic-token')
        self.assertEqual(result, {'shown': False, 'user_hidden': True})
        with self.assertRaises(Exception) as unknown:
            R.local_json(service.port, '/api/hud/arbitrary-command', token='synthetic-token', post=True)
        self.assertEqual(unknown.exception.code, 404)

    def test_controls_ignore_environment_proxy(self):
        service = self.server()
        with patch.dict('os.environ', {'HTTP_PROXY': 'http://127.0.0.1:1',
                                      'http_proxy': 'http://127.0.0.1:1', 'NO_PROXY': '', 'no_proxy': ''}):
            self.assertEqual(R.local_json(service.port, '/api/ping')['app'], 'BailianHUD')

    def test_lock_released_on_close(self):
        first, second = R.InstanceLock(self.root), R.InstanceLock(self.root)
        self.addCleanup(first.close)
        self.addCleanup(second.close)
        self.assertTrue(first.acquire())
        self.assertFalse(second.acquire())
        first.close()
        self.assertTrue(second.acquire())

    def test_real_port_conflict_falls_back_and_preserves_other_service(self):
        occupied = self.server()
        observed = {}

        class FakeApp:
            def __init__(self, *args, service, port, **kwargs):
                self.controls, self.tray = queue.Queue(), None
                self.service, self.port = service, port
            def start(self): pass
            def _publish_hud_state(self): pass
            def mainloop(self):
                observed.update(R.read_instance(self_root))
                assert self.port != occupied.port

        class FakeTray:
            def __init__(self, *args): self.hwnd = 1
            def start(self): pass
            def close(self): pass

        self_root = self.root
        with patch.object(B, 'BASE_DIR', self.root), patch.object(B, 'LISTEN_PORT', occupied.port), \
             patch.object(B, 'discover', return_value=None), patch.object(B, 'load_prefs', return_value={'api_token': 'synthetic-token'}), \
             patch.object(B, 'seed_state', create=True), patch.object(B, 'CouponSync'), patch.object(B, 'HudApp', FakeApp), \
             patch.object(B, 'set_dpi_awareness'), patch('hud_tray.TrayIcon', FakeTray):
            self.assertEqual(B.main(['--background']), 0)
        self.assertNotEqual(observed['port'], occupied.port)
        self.assertEqual(R.local_json(occupied.port, '/api/ping')['app'], 'BailianHUD')
        self.assertFalse((self.root / R.INSTANCE_FILE).exists())

    def test_x_stays_hidden_then_show_recovers(self):
        app = object.__new__(B.HudApp)
        app._t_dpi = time.time()
        app._last_fg, app.hwnd = 100, 200
        app._fg_settle, app._fg_hold = 0, 0
        app.prefs = {'follow': 'both'}
        app._was_matched = True
        app._vis_live_ts = time.time()
        app._user_hidden, app.shown_target = True, False
        app._hidden_since = time.time() - 3600
        app._set_shown = lambda value: setattr(app, 'shown_target', value)
        with patch.object(B, 'foreground_window', return_value=100), patch.object(B, 'foreground_matches', return_value=True):
            B.HudApp._check_visibility(app)
            self.assertFalse(app.shown_target)
            B.HudApp._control(app, 'show')
            self.assertFalse(app._user_hidden)
            self.assertTrue(app.shown_target)


if __name__ == '__main__':
    unittest.main()
