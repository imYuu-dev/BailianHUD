"""Public build: fresh state must never pretend to know another user's balance."""
import json
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import BailianHUD as B


class FirstRunTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="first-run-", dir=ROOT / "tests")
        self.root = Path(self.tmp.name).resolve()
        self.assertTrue(self.root.is_relative_to((ROOT / "tests").resolve()))
        self.log = B.LOG_FILE
        B.LOG_FILE = self.root / "test.log"

    def tearDown(self):
        B.LOG_FILE = self.log
        self.tmp.cleanup()

    def test_no_fake_balance_even_with_existing_usage(self):
        event = dict(t=time.time(), model="deepseek-v4.1-flash-1017", peak=True,
                     input=10000, cached=0, cache_write=0, output=0, cost=0.02)
        (self.root / B.USAGE_LOG.name).write_text(json.dumps(event) + "\n", encoding="utf-8")
        store = B.Store(self.root)
        snap = store.snapshot()
        self.assertEqual(snap["totals"]["count"], 1)
        self.assertFalse(snap["balance_available"])
        self.assertEqual(snap["status"], "unknown")
        self.assertIsNone(snap["days_left"])
        self.assertIsNone(snap["synced_remain"])

    def test_user_can_calibrate_and_change_face_value(self):
        store = B.Store(self.root)
        store.calibrate(105.0, face=200.0)
        snap = B.Store(self.root).snapshot()
        self.assertTrue(snap["balance_available"])
        self.assertEqual(snap["remain"], 105.0)
        self.assertEqual(snap["face"], 200.0)
        with self.assertRaises(ValueError):
            store.calibrate(999.0, face=-1)
        self.assertEqual(store.face, 200.0)
        self.assertEqual(store.base_remain, 105.0)

    def test_standalone_defaults_to_zcode(self):
        with patch.object(B, "PREFS_FILE", self.root / "prefs.json"):
            self.assertEqual(B.load_prefs()["follow"], "zcode")

    def test_unknown_balance_endpoint_and_manual_setup(self):
        store = B.Store(self.root)
        handler = type("TestHandler", (B.Handler,), {"store": store})
        server = B.QuietServer(("127.0.0.1", 0), handler)
        server.api_token = "fixture-token"
        server.request_show = lambda: None
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{server.server_address[1]}"
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(base + "/api/plugin/balance", timeout=3) as response:
                data = json.loads(response.read())
            self.assertFalse(data["ok"])
            self.assertIsNone(data["remaining"])
            req = urllib.request.Request(base + "/api/calibrate", data=b'{"remain":105,"face":200}',
                                         headers={"X-HUD-Token": "fixture-token", "Content-Type": "application/json"})
            with opener.open(req, timeout=3) as response:
                self.assertTrue(json.loads(response.read())["ok"])
            with opener.open(base + "/api/plugin/balance", timeout=3) as response:
                data = json.loads(response.read())
            self.assertTrue(data["ok"])
            self.assertEqual(data["remaining"], 105.0)
            self.assertEqual(data["face"], 200.0)
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
