"""Native Windows notification icon. All UI commands return to the Tk thread."""
from __future__ import annotations

import ctypes as C
import threading
from ctypes import wintypes as W


class TrayIcon:
    def __init__(self, dispatch, icon_path=None, log=lambda message: None):
        self.dispatch, self.icon_path, self.log = dispatch, icon_path, log
        self.hwnd = None
        self.ready = threading.Event()
        self.error = None
        self._thread = None

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True, name="hud-tray")
        self._thread.start()
        self.ready.wait(3)
        if self.error:
            self.log(f"tray unavailable: {self.error!r}")
        return bool(self.hwnd)

    def notify_hidden(self):
        if self.hwnd:
            self.user.PostMessageW(self.hwnd, 0x8002, 0, 0)

    def close(self):
        if self.hwnd:
            self.user.PostMessageW(self.hwnd, 0x0010, 0, 0)
        if self._thread:
            self._thread.join(timeout=2)

    def _run(self):
        try:
            self._message_loop()
        except Exception as exc:
            self.error = exc
            self.log(f"tray failed: {exc!r}")
        finally:
            self.hwnd = None
            self.ready.set()

    def _message_loop(self):
        user = self.user = C.WinDLL("user32", use_last_error=True)
        shell = C.WinDLL("shell32", use_last_error=True)
        kernel = C.WinDLL("kernel32", use_last_error=True)
        LRESULT = C.c_ssize_t
        CALLBACK = C.WINFUNCTYPE(LRESULT, W.HWND, W.UINT, W.WPARAM, W.LPARAM)

        class GUID(C.Structure):
            _fields_ = [("a", W.DWORD), ("b", W.WORD), ("c", W.WORD), ("d", W.BYTE * 8)]

        class NOTIFY(C.Structure):
            _fields_ = [("cbSize", W.DWORD), ("hWnd", W.HWND), ("uID", W.UINT),
                        ("uFlags", W.UINT), ("uCallbackMessage", W.UINT), ("hIcon", W.HICON),
                        ("szTip", W.WCHAR * 128), ("dwState", W.DWORD), ("dwStateMask", W.DWORD),
                        ("szInfo", W.WCHAR * 256), ("uTimeout", W.UINT),
                        ("szInfoTitle", W.WCHAR * 64), ("dwInfoFlags", W.DWORD),
                        ("guidItem", GUID), ("hBalloonIcon", W.HICON)]

        class WNDCLASS(C.Structure):
            _fields_ = [("style", W.UINT), ("lpfnWndProc", CALLBACK), ("cbClsExtra", C.c_int),
                        ("cbWndExtra", C.c_int), ("hInstance", W.HINSTANCE), ("hIcon", W.HICON),
                        ("hCursor", W.HANDLE), ("hbrBackground", W.HANDLE),
                        ("lpszMenuName", W.LPCWSTR), ("lpszClassName", W.LPCWSTR)]

        def sig(dll, name, result, *args):
            fn = getattr(dll, name)
            fn.restype, fn.argtypes = result, args
            return fn

        sig(kernel, "GetModuleHandleW", W.HMODULE, W.LPCWSTR)
        sig(user, "RegisterClassW", W.ATOM, C.POINTER(WNDCLASS))
        sig(user, "UnregisterClassW", W.BOOL, W.LPCWSTR, W.HINSTANCE)
        sig(user, "CreateWindowExW", W.HWND, W.DWORD, W.LPCWSTR, W.LPCWSTR, W.DWORD,
            C.c_int, C.c_int, C.c_int, C.c_int, W.HWND, W.HMENU, W.HINSTANCE, C.c_void_p)
        sig(user, "DefWindowProcW", LRESULT, W.HWND, W.UINT, W.WPARAM, W.LPARAM)
        sig(user, "PostMessageW", W.BOOL, W.HWND, W.UINT, W.WPARAM, W.LPARAM)
        sig(user, "DestroyWindow", W.BOOL, W.HWND)
        sig(user, "GetMessageW", W.BOOL, C.POINTER(W.MSG), W.HWND, W.UINT, W.UINT)
        sig(user, "TranslateMessage", W.BOOL, C.POINTER(W.MSG))
        sig(user, "DispatchMessageW", LRESULT, C.POINTER(W.MSG))
        sig(user, "RegisterWindowMessageW", W.UINT, W.LPCWSTR)
        sig(user, "LoadIconW", W.HICON, W.HINSTANCE, C.c_void_p)
        sig(user, "LoadImageW", W.HANDLE, W.HINSTANCE, W.LPCWSTR, W.UINT, C.c_int, C.c_int, W.UINT)
        sig(user, "DestroyIcon", W.BOOL, W.HICON)
        sig(user, "CreatePopupMenu", W.HMENU)
        sig(user, "AppendMenuW", W.BOOL, W.HMENU, W.UINT, C.c_size_t, W.LPCWSTR)
        sig(user, "TrackPopupMenu", W.UINT, W.HMENU, W.UINT, C.c_int, C.c_int,
            C.c_int, W.HWND, C.c_void_p)
        sig(user, "DestroyMenu", W.BOOL, W.HMENU)
        sig(user, "SetForegroundWindow", W.BOOL, W.HWND)
        sig(user, "GetCursorPos", W.BOOL, C.POINTER(W.POINT))
        sig(shell, "Shell_NotifyIconW", W.BOOL, W.DWORD, C.POINTER(NOTIFY))

        taskbar_created = user.RegisterWindowMessageW("TaskbarCreated")
        own_icon = user.LoadImageW(None, str(self.icon_path), 1, 32, 32, 0x10) if self.icon_path else None
        icon = own_icon or user.LoadIconW(None, C.c_void_p(32512))
        nid = NOTIFY()
        nid.cbSize, nid.uID, nid.uFlags = C.sizeof(nid), 1, 1 | 2 | 4
        nid.uCallbackMessage, nid.hIcon = 0x8001, icon
        nid.szTip = "百炼监控 · 单击显示 · 右键管理"
        self._nid = nid

        @CALLBACK
        def proc(hwnd, message, wp, lp):
            try:
                if message == taskbar_created:
                    shell.Shell_NotifyIconW(0, C.byref(nid))
                elif message == 0x8002:
                    nid.uFlags = 1 | 2 | 4 | 16
                    nid.szInfoTitle = "百炼监控已收起"
                    nid.szInfo = "点击托盘图标或按 Ctrl+Alt+B 恢复浮窗。"
                    nid.dwInfoFlags = 1
                    shell.Shell_NotifyIconW(1, C.byref(nid))
                    nid.uFlags = 1 | 2 | 4
                elif message == 0x8001:
                    if lp in (0x0202, 0x0203, 0x0405):
                        self.dispatch("show")
                    elif lp in (0x0205, 0x007B):
                        menu = user.CreatePopupMenu()
                        try:
                            items = [(1, "显示浮窗"), (2, "隐藏浮窗"),
                                     (3, "打开监控面板"), (4, "退出服务")]
                            for item, label in items:
                                user.AppendMenuW(menu, 0, item, label)
                            pos = W.POINT()
                            user.GetCursorPos(C.byref(pos))
                            user.SetForegroundWindow(hwnd)
                            choice = user.TrackPopupMenu(menu, 0x100 | 0x002, pos.x, pos.y, 0, hwnd, None)
                            if choice:
                                self.dispatch({1: "show", 2: "hide", 3: "panel", 4: "quit"}[choice])
                            user.PostMessageW(hwnd, 0, 0, 0)
                        finally:
                            user.DestroyMenu(menu)
                elif message == 0x0010:
                    user.DestroyWindow(hwnd)
                elif message == 0x0002:
                    shell.Shell_NotifyIconW(2, C.byref(nid))
                    user.PostQuitMessage(0)
                else:
                    return user.DefWindowProcW(hwnd, message, wp, lp)
                return 0
            except Exception as exc:
                self.log(f"tray event failed: {exc!r}")
                return 0

        self._proc = proc
        instance = kernel.GetModuleHandleW(None)
        classname = f"BailianHUDTray_{id(self)}"
        wc = WNDCLASS(lpfnWndProc=proc, hInstance=instance, lpszClassName=classname)
        if not user.RegisterClassW(C.byref(wc)):
            raise OSError(f"RegisterClassW failed: {C.get_last_error()}")
        try:
            self.hwnd = user.CreateWindowExW(0, classname, "百炼监控", 0,
                                            0, 0, 0, 0, None, None, instance, None)
            if not self.hwnd:
                raise OSError(f"CreateWindowExW failed: {C.get_last_error()}")
            nid.hWnd = self.hwnd
            if not shell.Shell_NotifyIconW(0, C.byref(nid)):
                error = C.get_last_error()
                user.DestroyWindow(self.hwnd)
                raise OSError(f"Shell_NotifyIconW failed: {error}")
            self.ready.set()
            msg = W.MSG()
            while user.GetMessageW(C.byref(msg), None, 0, 0) > 0:
                user.TranslateMessage(C.byref(msg))
                user.DispatchMessageW(C.byref(msg))
        finally:
            user.UnregisterClassW(classname, instance)
            if own_icon:
                user.DestroyIcon(own_icon)
