"""Local instance discovery and ownership; contains no GUI or account data."""
from __future__ import annotations

import json
import hashlib
import os
import time
import urllib.request
from pathlib import Path

DEFAULT_PORT = 18766
PLUGIN_PORT = 18765
INSTANCE_FILE = ".hud_instance.json"


def directory_id(directory):
    return hashlib.sha256(str(Path(directory).resolve()).lower().encode("utf-8")).hexdigest()[:24]


class InstanceLock:
    """An OS lock is released even after a crash. One writer per data directory."""

    def __init__(self, directory):
        self.path = Path(directory) / ".hud.lock"
        self.file = None

    def acquire(self):
        f = self.path.open("a+b")
        try:
            if f.tell() == 0:
                f.write(b"0")
                f.flush()
            f.seek(0)
        except OSError:
            f.close()
            raise
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            f.close()
            return False
        self.file = f
        return True

    def close(self):
        if self.file:
            self.file.close()
            self.file = None


def local_json(port, path, *, token="", post=False, timeout=1.0):
    """Never send local control requests through system/environment proxies."""
    req = urllib.request.Request(f"http://127.0.0.1:{int(port)}{path}",
                                 data=b"" if post else None)
    if token:
        req.add_header("X-HUD-Token", token)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=timeout) as response:
        return json.loads(response.read(65536).decode("utf-8"))


def read_instance(directory):
    try:
        data = json.loads((Path(directory) / INSTANCE_FILE).read_text("utf-8"))
        if isinstance(data, dict) and data.get("app") == "BailianHUD" and 0 < int(data["port"]) < 65536:
            return data
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return None


def discover(directory, explicit_port=None):
    recorded = read_instance(directory)
    ports = [explicit_port] if explicit_port is not None else [
        recorded["port"] if recorded else None, DEFAULT_PORT, PLUGIN_PORT, 8765]
    for port in dict.fromkeys(p for p in ports if p is not None):
        try:
            ping = local_json(port, "/api/ping", timeout=0.5)
            if ping.get("app") != "BailianHUD":
                continue
            if ping.get("data_id") and ping["data_id"] != directory_id(directory):
                continue
            if recorded and int(port) == int(recorded["port"]):
                if ping.get("instance_id") != recorded.get("instance_id"):
                    continue
            return {**ping, "port": int(port)}
        except (OSError, ValueError, TypeError):
            pass
    return None


def publish_instance(directory, port, instance_id):
    path = Path(directory) / INSTANCE_FILE
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps({"app": "BailianHUD", "pid": os.getpid(),
                               "port": port, "instance_id": instance_id,
                               "started_at": time.time()}), encoding="utf-8")
    os.replace(temp, path)


def clear_instance(directory, instance_id):
    data = read_instance(directory)
    if data and data.get("instance_id") == instance_id:
        (Path(directory) / INSTANCE_FILE).unlink(missing_ok=True)


class ParentWatch:
    """Hold a process handle so PID reuse cannot keep an orphaned plugin worker alive."""
    def __init__(self, pid):
        import ctypes
        from ctypes import wintypes
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self.kernel.OpenProcess.restype = wintypes.HANDLE
        self.kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        self.kernel.WaitForSingleObject.restype = wintypes.DWORD
        self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.kernel.CloseHandle.restype = wintypes.BOOL
        self.handle = self.kernel.OpenProcess(0x00100000, False, int(pid))
        if not self.handle:
            raise OSError("DSH host process is not available")

    def alive(self):
        return bool(self.handle and self.kernel.WaitForSingleObject(self.handle, 0) == 0x102)

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None
