"""Regression checks using only synthetic state and loopback HTTP servers."""
from __future__ import annotations

import http.client
import json
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
AUDIT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
import BailianHUD as B


class SyntheticCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="fix-test-", dir=AUDIT_DIR)
        self.folder = Path(self.temp.name).resolve()
        self.assertTrue(self.folder.is_relative_to(AUDIT_DIR.resolve()))
        self.old_log = B.LOG_FILE
        B.LOG_FILE = self.folder / "test.log"
        self.store = B.Store(self.folder)
        self.store.base_remain = 300.0
        self.store.balance_source = "real"
        self.store.balance_ts = time.time()

    def tearDown(self):
        B.LOG_FILE = self.old_log
        self.temp.cleanup()

    def test_time_boundaries(self):
        for hour, minute in [(0, 0), (7, 59), (8, 0), (21, 59), (22, 0), (23, 0)]:
            current = datetime(2026, 10, 27, hour, minute, tzinfo=B.TZ)
            start, end = B.window_bounds(current)
            self.assertLessEqual(start, current)
            self.assertGreater(end, current)
        with patch.object(B, "now_dt", return_value=datetime(2026, 10, 27, 23, tzinfo=B.TZ)):
            snap = self.store.snapshot()
        self.assertEqual(snap["switch_in"], 9 * 3600)
        self.assertEqual(snap["switch_at"], "08:00")

    def test_historical_savings_are_stable_and_persisted(self):
        self.store.record("deepseek-v4.1-flash-1017", 0, 1_000_000, 0, 0, True)
        with patch.object(B, "now_dt", return_value=datetime(2026, 10, 27, 21, tzinfo=B.TZ)):
            before = self.store.snapshot()["cache_saved"]
        with patch.object(B, "now_dt", return_value=datetime(2026, 10, 27, 23, tzinfo=B.TZ)):
            after = self.store.snapshot()["cache_saved"]
        self.assertAlmostEqual(before, 1.8)
        self.assertEqual(before, after)
        # New records preserve the price used when they were written, including on restart.
        with patch.dict(B.PRICING, {"deepseek-v4.1-flash-1017": {"flat": (50.0, 50.0)}}):
            reloaded = B.Store(self.folder)
            self.assertAlmostEqual(reloaded.snapshot()["cache_saved"], 1.8)
        # Legacy records carry peak/offpeak and remain stable across the current clock boundary.
        legacy = self.folder / "legacy"
        legacy.mkdir()
        entry = dict(t=time.time(), model="deepseek-v4.1-flash-1017", input=0,
                     cached=1_000_000, cache_write=0, output=0, cost=0.2, peak=True)
        (legacy / B.USAGE_LOG.name).write_text(json.dumps(entry) + "\n", encoding="utf-8")
        old_store = B.Store(legacy)
        with patch.object(B, "now_dt", return_value=datetime(2026, 10, 27, 23, tzinfo=B.TZ)):
            self.assertAlmostEqual(old_store.snapshot()["cache_saved"], 1.8)

    def test_rate_uses_all_recent_requests_and_replay(self):
        for _ in range(100):
            self.store.record("deepseek-v4.1-flash-1017", 150_000, 0, 0, 0, True)
        snap = self.store.snapshot()
        self.assertAlmostEqual(snap["burn_per_hour"], 10.0)
        self.assertAlmostEqual(snap["days_left"], 270 / 240)
        reloaded = B.Store(self.folder)
        self.assertAlmostEqual(reloaded.snapshot()["burn_per_hour"], 10.0)
        reloaded.cost_factor = 2.0
        self.assertAlmostEqual(reloaded.snapshot()["burn_per_hour"], 20.0)
        reloaded.cost_factor = 1.0
        # Expired entries must not contribute, even if their order is irregular.
        reloaded._cost_events.append((time.time() - 3 * 3600 - 1, 1000.0))
        self.assertAlmostEqual(reloaded.snapshot()["burn_per_hour"], 10.0)

    def test_zero_coupon_and_binding(self):
        self.store.configure_coupon("A")
        sync = B.CouponSync(self.store, "A")
        zero = dict(id="ID-A", no="A", status="Available", balance=0.0,
                    nominal=300.0, desc="百炼", remark="")
        chosen, _ = sync._pick([zero])
        self.assertEqual(chosen, zero)
        self.store.push_balance(0, kind="bss", coupon_no="A", coupon_id="ID-A", nominal=300)
        self.assertEqual(self.store.snapshot()["remain"], 0)
        with self.assertRaises(B.CouponBindingError):
            self.store.push_balance(222, kind="push", coupon_no="B", coupon_id="ID-B", nominal=500)
        self.assertEqual(self.store.face, 300)
        self.assertEqual(self.store.snapshot()["remain"], 0)
        reloaded = B.Store(self.folder)
        self.assertEqual(reloaded.coupon_identity()["coupon_no"], "A")
        with self.assertRaises(B.CouponBindingError):
            reloaded.push_balance(222, kind="push", coupon_no="B", nominal=500)
        reloaded.configure_coupon("B")
        self.assertEqual(reloaded.snapshot()["status"], "unknown")
        reloaded.push_balance(120, kind="bss", coupon_no="B", coupon_id="ID-B", nominal=300)
        self.assertEqual(reloaded.snapshot()["remain"], 120)

    def test_coupon_missing_balance_and_failure_state(self):
        response = {"CashCoupon": [
            {"CashCouponNo": "A", "Status": "Available", "Description": "百炼"},
            {"CashCouponNo": "B", "Status": "Available", "Description": "百炼", "Balance": "0"},
            {"CashCouponNo": "C", "Status": "Available", "Description": "百炼", "Balance": "nan"},
        ]}
        with patch.object(B, "bss_call", return_value=response):
            coupons = B.fetch_coupons()
        self.assertEqual([(c["no"], c["balance"]) for c in coupons], [("B", 0.0)])
        self.store.push_balance(12, kind="bss", coupon_no="A")
        self.store.set_coupon_sync_error(True)
        snapshot = self.store.snapshot()
        self.assertEqual(snapshot["status"], "stale")
        self.assertTrue(snapshot["balance_sync_error"])
        self.store.set_coupon_sync_error(False)
        self.assertEqual(self.store.snapshot()["status"], "live")
        self.store.set_coupon_sync_error(True, "unavailable")
        self.assertEqual(self.store.snapshot()["balance_sync_issue"], "unavailable")
        self.store.set_coupon_sync_error(False)
        sync = B.CouponSync(self.store, "A")
        expired = dict(no="A", id="", balance=12.0, nominal=300.0,
                       status="Expired", desc="百炼", remark="")
        self.assertIsNone(sync._pick([expired])[0])

    def test_menu_cache_and_animation_keys(self):
        fake = object.__new__(B.HudApp)
        fake.plugin_mode = False
        fake.prefs = dict(follow="both", through=True, glass=1.0)
        fake.menu_open, fake.glass_menu, fake.expand, fake.anchor = True, False, 0.0, "top"
        fake.menu_index, fake.dark, fake.backdrop_seq = 4, 0.0, 1
        fake.data_sig, fake._hint_until, fake.hover_close, fake._quit_armed_until = ("fixture",), 0, False, 0
        fake._pulse, fake._shine = lambda: 0.0, lambda: -1.0
        fake.win_w, fake.win_h, fake.scale = 528, 420, 1.5
        ui1 = fake._ui_dict()
        fake.glass_menu = True
        ui2 = fake._ui_dict()
        self.assertNotEqual(fake._pix_key_of(ui1), fake._pix_key_of(ui2))
        renderer = B.Renderer(1.5)
        backdrop = B._demo_backdrop("light", (528, 420))
        first = renderer.render(B._demo_snapshot(), ui1, backdrop)
        second = renderer.render(B._demo_snapshot(), ui2, backdrop)
        self.assertNotEqual(first.tobytes(), second.tobytes())
        fake._pulse = lambda: 0.7
        self.assertNotEqual(fake._pix_key_of(ui2), fake._pix_key_of(fake._ui_dict()))

    def test_preview_without_cache_key_reflects_new_data(self):
        renderer = B.Renderer(1.5)
        backdrop = B._demo_backdrop("light", (528, 420))
        ui = dict(content_h=B.H, card_y=B.PAD, dark=0.0, expand=0.0)
        normal = renderer.render(B._demo_snapshot(remain=173.25), ui, backdrop)
        low = renderer.render(B._demo_snapshot(remain=12.34), ui, backdrop)
        close = renderer.render(B._demo_snapshot(remain=173.25), dict(ui, hover_close=True), backdrop)
        self.assertNotEqual(normal.tobytes(), low.tobytes())
        self.assertNotEqual(normal.tobytes(), close.tobytes())

    def test_render_states_at_common_scaling_levels(self):
        for scale in (1.0, 1.25, 1.5, 2.0):
            size = (round((B.W + 2 * B.PAD) * scale), round((B.MAX_H + 2 * B.PAD) * scale))
            renderer = B.Renderer(scale)
            for state in ("collapsed", "expanded", "dark", "low", "stale", "unavailable", "menu"):
                with self.subTest(scale=scale, state=state):
                    snap = B._demo_snapshot(remain=12.34 if state == "low" else 143.75)
                    if state in ("stale", "unavailable"):
                        snap.update(status="stale", balance_kind="bss", balance_ts=time.time(),
                                    balance_sync_error=state == "unavailable",
                                    balance_sync_issue="unavailable" if state == "unavailable" else "")
                    expanded = state == "expanded"
                    ui = dict(content_h=B.H_MENU if state == "menu" else B.H_EXP if expanded else B.H,
                              card_y=B.PAD, dark=1.0 if state == "dark" else 0.0,
                              expand=1.0 if expanded else 0.0, menu=state == "menu",
                              menu_items=B.MENU_ITEMS_PREVIEW, ink_sig=(scale, state), backdrop_seq=1)
                    backdrop = B._demo_backdrop("dark" if state == "dark" else "light", size)
                    self.assertEqual(renderer.render(snap, ui, backdrop).size, size)

    def test_bottom_close_is_stable_while_hovered(self):
        fake = object.__new__(B.HudApp)
        fake.scale, fake.pos_x, fake.pos_y, fake.anchor = 1.5, 800, 880, "bottom"
        fake.expand, fake.expand_want, fake._hold_expand = 0.0, 0.0, False
        fake.shown, fake.shown_target, fake.menu_open = 1.0, True, False
        fake._inside, fake._inside_since, fake._outside_since = False, 0.0, 0.0
        fake.prefs = dict(through=True)
        fake._dragging, fake._hint_seen, fake.hover_close, fake.menu_index = False, True, False, -1
        fake.dark, fake.dark_target = 0.0, 0.0
        fake._pulse, fake._shine, fake._arm = lambda: 0.0, lambda: -1.0, lambda _: None
        x = fake.pos_x + 305 * fake.scale
        y = fake._card_top_px() + 17.4 * fake.scale
        fake._hover_update(x, y, 10.0)
        fake._hover_update(x, y, 10.3)
        self.assertTrue(fake._hold_expand)
        for _ in range(10):
            fake._animate(0.04)
        self.assertEqual(fake.expand, 0.0)
        self.assertTrue(fake._hit_close(x, y))
        fake._hover_update(fake.pos_x + 100, fake.pos_y + 80, 10.4)
        self.assertFalse(fake._hold_expand)
        fake._animate(0.04)
        self.assertGreater(fake.expand, 0)

    def test_staggered_monitor_gap_and_work_area(self):
        fake = object.__new__(B.HudApp)
        fake.scale = 1.0
        with patch.object(B, "work_area_at", return_value=(1920, 1080, 3840, 2120)):
            x, y = fake._clamp_pos(2500, 100)
            self.assertGreaterEqual(y, 1080)
            self.assertLessEqual(y + B.H, 2120)
            self.assertGreaterEqual(x, 1920)

    def test_api_binding_and_auth(self):
        self.store.configure_coupon("A")
        self.store.push_balance(12, kind="bss", coupon_no="A", coupon_id="ID-A")
        handler = type("TestHandler", (B.Handler,), {"store": self.store})
        server = B.QuietServer(("127.0.0.1", 0), handler)
        server.api_token = "fixture-token"
        server.request_show = lambda: None
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        base = f"http://127.0.0.1:{server.server_address[1]}"
        try:
            req = urllib.request.Request(base + "/api/coupon-binding", headers={"Origin": next(iter(B.PUSH_ORIGINS))})
            with opener.open(req, timeout=3) as response:
                self.assertEqual(json.loads(response.read())["coupon_no"], "A")
                self.assertEqual(response.headers.get("Access-Control-Allow-Origin"), next(iter(B.PUSH_ORIGINS)))
            payload = json.dumps(dict(coupon="B", nominal=500, remain=222)).encode()
            req = urllib.request.Request(base + "/api/balance", data=payload,
                                         headers={"Origin": next(iter(B.PUSH_ORIGINS)),
                                                  "Content-Type": "application/json"})
            with self.assertRaises(urllib.error.HTTPError) as caught:
                opener.open(req, timeout=3)
            self.assertEqual(caught.exception.code, 409)
            self.assertEqual(self.store.base_remain, 12)
            self.assertEqual(self.store.face, 300)
            payload = json.dumps(dict(coupon="A", coupon_id="ID-A", nominal=300, remain=0)).encode()
            req = urllib.request.Request(base + "/api/balance", data=payload,
                                         headers={"Origin": next(iter(B.PUSH_ORIGINS)),
                                                  "Content-Type": "application/json"})
            with opener.open(req, timeout=3) as response:
                self.assertEqual(response.status, 200)
                self.assertEqual(json.loads(response.read())["remain"], 0)
            req = urllib.request.Request(base + "/api/calibrate", data=b'{"remain":1}',
                                         headers={"Content-Type": "application/json"})
            with self.assertRaises(urllib.error.HTTPError) as caught:
                opener.open(req, timeout=3)
            self.assertEqual(caught.exception.code, 403)
        finally:
            server.shutdown()
            server.server_close()

    def test_small_sse_event_arrives_before_upstream_finishes(self):
        release = threading.Event()

        class Upstream(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_):
                pass

            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                first = b'data: {"type":"message_start","message":{"model":"deepseek-v4.1-flash-1017","usage":{"input_tokens":10}}}\n\n'
                self.wfile.write(b"%X\r\n%s\r\n" % (len(first), first))
                self.wfile.flush()
                release.wait(1.5)
                last = b'data: {"type":"message_delta","usage":{"output_tokens":5}}\n\n'
                self.wfile.write(b"%X\r\n%s\r\n0\r\n\r\n" % (len(last), last))
                self.wfile.flush()

        upstream = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
        upstream.daemon_threads = True
        handler = type("TestHandler", (B.Handler,), {"store": self.store})
        proxy = B.QuietServer(("127.0.0.1", 0), handler)
        proxy.api_token = "fixture-token"
        proxy.request_show = lambda: None
        for server in (upstream, proxy):
            threading.Thread(target=server.serve_forever, daemon=True).start()
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), B.SafeRedirect())
        try:
            with patch.object(B, "UPSTREAM", f"http://127.0.0.1:{upstream.server_address[1]}"), \
                 patch.object(B, "_UPSTREAM_OPENER", opener):
                started = time.perf_counter()
                with opener.open(f"http://127.0.0.1:{proxy.server_address[1]}/stream", timeout=4) as response:
                    first = response.read(1)
                    elapsed = time.perf_counter() - started
                    release.set()
                    response.read()
                self.assertEqual(first, b"d")
                self.assertLess(elapsed, 0.8, f"first byte delayed {elapsed:.3f}s")
                deadline = time.monotonic() + 1
                while self.store.totals["count"] != 1 and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertEqual(self.store.totals["count"], 1)
                self.assertEqual(self.store.totals["output"], 5)
                print(f"  SSE first byte: {elapsed * 1000:.1f} ms")
        finally:
            release.set()
            for server in (proxy, upstream):
                server.shutdown()
                server.server_close()

    def test_interrupted_chunked_stream_is_not_marked_complete(self):
        class Upstream(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_):
                pass

            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                part = b'data: {"type":"message_delta","usage":{"output_tokens":5}}\n\n'
                self.wfile.write(b"%X\r\n%s\r\n" % (len(part), part))
                self.wfile.flush()
                self.close_connection = True  # Deliberately omit the terminating chunk.

        upstream = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
        upstream.daemon_threads = True
        handler = type("TestHandler", (B.Handler,), {"store": self.store})
        proxy = B.QuietServer(("127.0.0.1", 0), handler)
        proxy.api_token = "fixture-token"
        proxy.request_show = lambda: None
        for server in (upstream, proxy):
            threading.Thread(target=server.serve_forever, daemon=True).start()
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), B.SafeRedirect())
        try:
            with patch.object(B, "UPSTREAM", f"http://127.0.0.1:{upstream.server_address[1]}"), \
                 patch.object(B, "_UPSTREAM_OPENER", opener):
                with opener.open(f"http://127.0.0.1:{proxy.server_address[1]}/stream", timeout=4) as response:
                    with self.assertRaises(http.client.IncompleteRead):
                        response.read()
        finally:
            for server in (proxy, upstream):
                server.shutdown()
                server.server_close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
