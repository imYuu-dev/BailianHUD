"""Smoke a built Windows executable with synthetic, isolated user data."""
import argparse
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
parser = argparse.ArgumentParser()
parser.add_argument("exe", type=Path)
parser.add_argument("--desktop", action="store_true", help="also verify interactive desktop visibility")
parser.add_argument("--plugin", action="store_true", help="verify plugin worker ownership and no tray")
args = parser.parse_args()
exe = args.exe.resolve(strict=True)
assert exe.name.lower() == "bailianhud.exe"

with tempfile.TemporaryDirectory(prefix="exe-smoke-", dir=ROOT / "tests") as raw:
    root = Path(raw).resolve()
    assert root.is_relative_to((ROOT / "tests").resolve())
    profile = root / "profile"
    profile.mkdir()
    data_dir = root / "data"
    env = os.environ.copy()
    env.update(BAILIAN_HUD_HOME=str(data_dir), USERPROFILE=str(profile), HOME=str(profile),
               LOCALAPPDATA=str(root / "local"), APPDATA=str(root / "roaming"))
    system_root = env.get("SystemRoot", r"C:\Windows")
    env["PATH"] = os.pathsep.join([str(Path(system_root) / "System32"), system_root,
                                   str(Path(system_root) / "System32" / "Wbem")])
    for key in ("PYTHONPATH", "PYTHONHOME", "CONDA_PREFIX", "CONDA_DEFAULT_ENV",
                "TCL_LIBRARY", "TK_LIBRARY"):
        env.pop(key, None)
    for key in ("ALIYUN_AK_ID", "ALIYUN_AK_SECRET", "ALIYUN_STS_TOKEN",
                "ALIBABA_CLOUD_ACCESS_KEY_ID", "ALIBABA_CLOUD_ACCESS_KEY_SECRET",
                "ALIBABA_CLOUD_SECURITY_TOKEN", "ALIYUN_ACCESS_KEY_ID",
                "ALIYUN_ACCESS_KEY_SECRET", "ALIYUN_SECURITY_TOKEN", "BAILIAN_UPSTREAM"):
        env.pop(key, None)
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    owner = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(90)'],
                             creationflags=subprocess.CREATE_NO_WINDOW) if args.plugin else None
    plugin_args = ["--plugin", "--parent-pid", str(owner.pid)] if owner else []
    proc = subprocess.Popen([str(exe), "--port", str(port), *plugin_args], cwd=str(root), env=env,
                            creationflags=subprocess.CREATE_NO_WINDOW)
    try:
        deadline = time.monotonic() + 30
        stats = None
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                raise RuntimeError(f"executable exited: {proc.returncode}")
            try:
                with opener.open(base + "/api/stats", timeout=1) as response:
                    stats = json.load(response)
                break
            except (OSError, ValueError):
                time.sleep(0.25)
        if stats is None:
            log = data_dir / ".hud.log"
            raise RuntimeError("startup timed out: " + (log.read_text(encoding="utf-8") if log.exists() else "no log"))
        assert stats["app"] == "BailianHUD"
        assert stats["status"] == "unknown" and not stats["balance_available"]
        assert stats["totals"]["count"] == 0
        prefs = json.loads((data_dir / ".hud_prefs.json").read_text(encoding="utf-8"))
        assert prefs["follow"] == ("dsh" if args.plugin else "zcode") and prefs["api_token"]
        request = urllib.request.Request(base + '/api/hud/status', headers={'X-HUD-Token': prefs['api_token']})
        deadline = time.monotonic() + 10
        while True:
            with opener.open(request, timeout=3) as response:
                hud_status = json.load(response)
            if 'mode' in hud_status:
                break
            if time.monotonic() >= deadline:
                raise RuntimeError('HUD status was not initialized')
            time.sleep(0.1)
        assert hud_status['mode'] == ('plugin' if args.plugin else 'standalone')
        if args.plugin or args.desktop:
            assert hud_status['tray'] is (not args.plugin), (hud_status,
                (data_dir / '.hud.log').read_text(encoding='utf-8'))
        if args.desktop:
            request = urllib.request.Request(base + '/api/hud/show', data=b'',
                                             headers={'X-HUD-Token': prefs['api_token']})
            with opener.open(request, timeout=3) as response:
                assert json.load(response)['ok']
            request = urllib.request.Request(base + "/api/debug", headers={"X-HUD-Token": prefs["api_token"]})
            deadline = time.monotonic() + 5
            while True:
                with opener.open(request, timeout=3) as response:
                    debug = json.load(response)
                if debug["shown_target"] and debug["shown"] > 0.9 and debug["cursor"] != [0, 0]:
                    break
                if time.monotonic() >= deadline:
                    raise RuntimeError(f"desktop visibility not verified: {debug}")
                time.sleep(0.2)
            print("PASS: floating window is visible and reads the interactive desktop")
        with opener.open(base + "/api/plugin/balance", timeout=3) as response:
            assert json.load(response)["remaining"] is None
        request = urllib.request.Request(base + "/api/calibrate", data=b'{"remain":87.65,"face":200}',
                                         headers={"X-HUD-Token": prefs["api_token"],
                                                  "Content-Type": "application/json"})
        with opener.open(request, timeout=3) as response:
            assert json.load(response)["ok"]
        with opener.open(base + "/api/plugin/balance", timeout=3) as response:
            balance = json.load(response)
        assert balance["remaining"] == 87.65 and balance["face"] == 200
        print("PASS: packaged exe starts with unknown balance and isolated data; manual calibration works")
        if owner:
            owner.terminate()
            owner.wait(timeout=5)
            assert proc.wait(timeout=8) == 0
            assert not (data_dir / '.hud_instance.json').exists()
            print('PASS: plugin has no tray and exits after its DSH host ends')
    finally:
        if owner and owner.poll() is None:
            owner.terminate()
            owner.wait(timeout=5)
        if proc.poll() is None:
            # Only the test process tree created above is stopped; the user's live HUD is untouched.
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
