"""Windows 10 / 11 platform layer (pure ctypes + mss).

* Per-monitor DPI awareness v2 -> every coordinate is a real (physical) pixel,
  so capture, window geometry and clicks agree at any display scaling
  (100 / 125 / 150 % ...) and across mixed-DPI monitors.
* Window: finds the Roblox client (RobloxPlayerBeta.exe, also when started via
  Bloxstrap/Fishstrap, or the Microsoft Store app titled "Roblox"), reads its
  client area on every frame (moves / resizes are followed live), brings it to
  the front.
* Capture: mss (GDI BitBlt of the desktop, ~10-25 ms); pure-ctypes GDI fallback.
* Mouse: SendInput absolute move (+ SetCursorPos correction), then a 1 px
  relative wiggle like a real mouse so Roblox really updates its pointer;
  button events carry no position, exactly like a real mouse.
* Stop: F8 (GetAsyncKeyState) or the user moving / clicking the real mouse.
"""
import ctypes
import os
import sys
import threading
import time
from ctypes import wintypes
from dataclasses import dataclass

import cv2
import numpy as np

from .geom import Rect

if sys.platform != "win32":
    raise ImportError("bgsi_enchanter.win only works on Windows")

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
try:
    dwmapi = ctypes.WinDLL("dwmapi")
except OSError:
    dwmapi = None


# ------------------------------------------------------------------ DPI awareness
def _enable_dpi_awareness() -> str:
    """Must run before any window/GUI is created. Qt / mss may try to set it too;
    whichever call comes first wins, all of them ask for per-monitor awareness."""
    try:
        user32.SetProcessDpiAwarenessContext.argtypes = [ctypes.c_void_p]
        user32.SetProcessDpiAwarenessContext.restype = wintypes.BOOL
        user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))   # PER_MONITOR_AWARE_V2
    except AttributeError:
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)              # PER_MONITOR_AWARE
        except (AttributeError, OSError):
            try:
                user32.SetProcessDPIAware()
            except AttributeError:
                pass
    try:
        user32.GetThreadDpiAwarenessContext.restype = ctypes.c_void_p
        user32.GetAwarenessFromDpiAwarenessContext.argtypes = [ctypes.c_void_p]
        user32.GetAwarenessFromDpiAwarenessContext.restype = ctypes.c_int
        a = user32.GetAwarenessFromDpiAwarenessContext(user32.GetThreadDpiAwarenessContext())
        return {0: "unaware", 1: "system", 2: "per-monitor"}.get(a, f"unknown({a})")
    except AttributeError:
        return "legacy"


DPI_MODE = _enable_dpi_awareness()

# ------------------------------------------------------------------ prototypes
WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
HWND, RECT, POINT, DWORD = wintypes.HWND, wintypes.RECT, wintypes.POINT, wintypes.DWORD


def _proto(fn, res, *args):
    fn.restype = res
    fn.argtypes = list(args)
    return fn


_proto(user32.EnumWindows, wintypes.BOOL, WNDENUMPROC, wintypes.LPARAM)
_proto(user32.IsWindow, wintypes.BOOL, HWND)
_proto(user32.IsWindowVisible, wintypes.BOOL, HWND)
_proto(user32.IsIconic, wintypes.BOOL, HWND)
_proto(user32.GetWindowTextLengthW, ctypes.c_int, HWND)
_proto(user32.GetWindowTextW, ctypes.c_int, HWND, wintypes.LPWSTR, ctypes.c_int)
_proto(user32.GetClassNameW, ctypes.c_int, HWND, wintypes.LPWSTR, ctypes.c_int)
_proto(user32.GetWindowThreadProcessId, DWORD, HWND, ctypes.POINTER(DWORD))
_proto(user32.GetClientRect, wintypes.BOOL, HWND, ctypes.POINTER(RECT))
_proto(user32.GetWindowRect, wintypes.BOOL, HWND, ctypes.POINTER(RECT))
_proto(user32.ClientToScreen, wintypes.BOOL, HWND, ctypes.POINTER(POINT))
_proto(user32.GetForegroundWindow, HWND)
_proto(user32.SetForegroundWindow, wintypes.BOOL, HWND)
_proto(user32.BringWindowToTop, wintypes.BOOL, HWND)
_proto(user32.ShowWindow, wintypes.BOOL, HWND, ctypes.c_int)
_proto(user32.GetSystemMetrics, ctypes.c_int, ctypes.c_int)
_proto(user32.GetCursorPos, wintypes.BOOL, ctypes.POINTER(POINT))
_proto(user32.SetCursorPos, wintypes.BOOL, ctypes.c_int, ctypes.c_int)
_proto(user32.GetAsyncKeyState, ctypes.c_short, ctypes.c_int)
_proto(user32.keybd_event, None, ctypes.c_ubyte, ctypes.c_ubyte, DWORD, ctypes.c_size_t)
_proto(kernel32.OpenProcess, wintypes.HANDLE, DWORD, wintypes.BOOL, DWORD)
_proto(kernel32.QueryFullProcessImageNameW, wintypes.BOOL, wintypes.HANDLE, DWORD,
       wintypes.LPWSTR, ctypes.POINTER(DWORD))
_proto(kernel32.CloseHandle, wintypes.BOOL, wintypes.HANDLE)
_proto(kernel32.GetCurrentProcessId, DWORD)
if dwmapi is not None:
    _proto(dwmapi.DwmGetWindowAttribute, ctypes.c_long, HWND, DWORD, ctypes.c_void_p, DWORD)


# ------------------------------------------------------------------ windows
@dataclass
class Window:
    hwnd: int
    title: str
    cls: str
    exe: str
    rect: Rect          # client area, screen pixels
    minimized: bool


def _text(fn, hwnd, size=512):
    buf = ctypes.create_unicode_buffer(size)
    fn(hwnd, buf, size)
    return buf.value


def _exe_of(hwnd) -> str:
    pid = DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    h = kernel32.OpenProcess(0x1000, False, pid.value)      # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        n = DWORD(1024)
        if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(n)):
            return os.path.basename(buf.value)
    finally:
        kernel32.CloseHandle(h)
    return ""


def _pid_of(hwnd) -> int:
    pid = DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


def _cloaked(hwnd) -> bool:
    if dwmapi is None:
        return False
    v = DWORD(0)
    dwmapi.DwmGetWindowAttribute(hwnd, 14, ctypes.byref(v), ctypes.sizeof(v))   # DWMWA_CLOAKED
    return v.value != 0


def client_rect(hwnd):
    """Client area in screen pixels, or None if the window is gone / minimised."""
    if not hwnd or not user32.IsWindow(hwnd) or user32.IsIconic(hwnd):
        return None
    rc = RECT()
    pt = POINT(0, 0)
    if not user32.GetClientRect(hwnd, ctypes.byref(rc)) or not user32.ClientToScreen(hwnd, ctypes.byref(pt)):
        return None
    w, h = rc.right - rc.left, rc.bottom - rc.top
    if w <= 0 or h <= 0:
        return None
    return Rect(int(pt.x), int(pt.y), int(w), int(h))


def window_rect(hwnd):
    """Visible window bounds incl. title bar (DWM extended frame bounds)."""
    rc = RECT()
    if dwmapi is not None and dwmapi.DwmGetWindowAttribute(hwnd, 9, ctypes.byref(rc), ctypes.sizeof(rc)) == 0:
        pass                                                    # DWMWA_EXTENDED_FRAME_BOUNDS
    elif not user32.GetWindowRect(hwnd, ctypes.byref(rc)):
        return None
    return Rect(int(rc.left), int(rc.top), int(rc.right - rc.left), int(rc.bottom - rc.top))


def list_windows():
    out = []
    me = kernel32.GetCurrentProcessId()

    @WNDENUMPROC
    def cb(hwnd, _lparam):
        try:
            if user32.IsWindowVisible(hwnd) and not _cloaked(hwnd) and _pid_of(hwnd) != me:
                title = _text(user32.GetWindowTextW, hwnd)
                cls = _text(user32.GetClassNameW, hwnd, 256)
                minimized = bool(user32.IsIconic(hwnd))
                rect = client_rect(hwnd) or Rect(0, 0, 0, 0)
                out.append(Window(int(hwnd), title, cls, _exe_of(hwnd), rect, minimized))
        except Exception:
            pass
        return True

    user32.EnumWindows(cb, 0)
    return out


def find_game_window(patterns):
    """patterns ending in '.exe' match the process name; others must equal the
    window title (case-insensitive) - browser tabs titled 'Roblox - ...' never match."""
    exes = [p.lower() for p in patterns if p.lower().endswith(".exe")]
    titles = [p.strip().lower() for p in patterns if not p.lower().endswith(".exe")]
    wins = list_windows()
    by_exe = [w for w in wins if w.exe and any(e in w.exe.lower() for e in exes)]
    by_title = [w for w in wins if w.title.strip().lower() in titles]
    hits = by_exe or by_title
    return max(hits, key=lambda w: w.rect.w * w.rect.h) if hits else None


def is_foreground(hwnd) -> bool:
    return int(user32.GetForegroundWindow() or 0) == int(hwnd)


def activate(hwnd) -> bool:
    """Restore + bring to front. Windows only lets a process steal focus right
    after keyboard input, hence the harmless Alt tap if the first try fails."""
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, 9)                              # SW_RESTORE
        time.sleep(0.25)
    if is_foreground(hwnd):
        return True
    user32.SetForegroundWindow(hwnd)
    time.sleep(0.05)
    if not is_foreground(hwnd):
        user32.keybd_event(0x12, 0, 0, 0)                       # Alt down
        user32.keybd_event(0x12, 0, 2, 0)                       # Alt up
        user32.SetForegroundWindow(hwnd)
        user32.BringWindowToTop(hwnd)
        time.sleep(0.05)
    return is_foreground(hwnd)


def virtual_screen() -> Rect:
    g = user32.GetSystemMetrics
    return Rect(g(76), g(77), g(78), g(79))   # SM_X/Y/CX/CYVIRTUALSCREEN


def cursor_pos():
    p = POINT()
    user32.GetCursorPos(ctypes.byref(p))
    return int(p.x), int(p.y)


# ------------------------------------------------------------------ capture
class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", ctypes.c_uint32), ("biWidth", ctypes.c_int32), ("biHeight", ctypes.c_int32),
                ("biPlanes", ctypes.c_uint16), ("biBitCount", ctypes.c_uint16),
                ("biCompression", ctypes.c_uint32), ("biSizeImage", ctypes.c_uint32),
                ("biXPelsPerMeter", ctypes.c_int32), ("biYPelsPerMeter", ctypes.c_int32),
                ("biClrUsed", ctypes.c_uint32), ("biClrImportant", ctypes.c_uint32)]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", ctypes.c_uint32 * 3)]


_proto(user32.GetDC, wintypes.HDC, HWND)
_proto(user32.ReleaseDC, ctypes.c_int, HWND, wintypes.HDC)
_proto(gdi32.CreateCompatibleDC, wintypes.HDC, wintypes.HDC)
_proto(gdi32.CreateCompatibleBitmap, wintypes.HBITMAP, wintypes.HDC, ctypes.c_int, ctypes.c_int)
_proto(gdi32.SelectObject, wintypes.HGDIOBJ, wintypes.HDC, wintypes.HGDIOBJ)
_proto(gdi32.BitBlt, wintypes.BOOL, wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
       wintypes.HDC, ctypes.c_int, ctypes.c_int, DWORD)
_proto(gdi32.GetDIBits, ctypes.c_int, wintypes.HDC, wintypes.HBITMAP, ctypes.c_uint, ctypes.c_uint,
       ctypes.c_void_p, ctypes.POINTER(BITMAPINFO), ctypes.c_uint)
_proto(gdi32.DeleteObject, wintypes.BOOL, wintypes.HGDIOBJ)
_proto(gdi32.DeleteDC, wintypes.BOOL, wintypes.HDC)


def _gdi_grab(r: Rect) -> np.ndarray:
    hdc = user32.GetDC(None)
    mdc = gdi32.CreateCompatibleDC(hdc)
    bmp = gdi32.CreateCompatibleBitmap(hdc, r.w, r.h)
    old = gdi32.SelectObject(mdc, bmp)
    try:
        gdi32.BitBlt(mdc, 0, 0, r.w, r.h, hdc, r.x, r.y, 0x00CC0020 | 0x40000000)  # SRCCOPY|CAPTUREBLT
        bmi = BITMAPINFO()
        bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bmi.bmiHeader.biWidth, bmi.bmiHeader.biHeight = r.w, -r.h           # top-down rows
        bmi.bmiHeader.biPlanes, bmi.bmiHeader.biBitCount = 1, 32
        buf = ctypes.create_string_buffer(r.w * r.h * 4)
        if not gdi32.GetDIBits(mdc, bmp, 0, r.h, buf, ctypes.byref(bmi), 0):
            raise OSError("GetDIBits failed")
    finally:
        gdi32.SelectObject(mdc, old)
        gdi32.DeleteObject(bmp)
        gdi32.DeleteDC(mdc)
        user32.ReleaseDC(None, hdc)
    img = np.frombuffer(buf, np.uint8).reshape(r.h, r.w, 4)
    return cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)


class Capture:
    """Screenshot of a screen rectangle in physical pixels (BGR)."""

    def __init__(self):
        try:
            import mss  # noqa: F401
            self.backend = "mss"
        except ImportError:
            self.backend = "gdi"
        self._local = threading.local()      # mss handles are per thread

    def grab(self, r: Rect) -> np.ndarray:
        if self.backend == "mss":
            sct = getattr(self._local, "sct", None)
            if sct is None:
                import mss
                sct = self._local.sct = mss.mss()
            shot = sct.grab({"left": r.x, "top": r.y, "width": r.w, "height": r.h})
            img = np.frombuffer(shot.bgra, np.uint8).reshape(shot.height, shot.width, 4)
            return cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)
        return _gdi_grab(r)

    def close(self):
        sct = getattr(self._local, "sct", None)
        if sct is not None:
            try:
                sct.close()
            except Exception:
                pass
            self._local.sct = None


# ------------------------------------------------------------------ mouse
class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", ctypes.c_int32), ("dy", ctypes.c_int32), ("mouseData", ctypes.c_uint32),
                ("dwFlags", ctypes.c_uint32), ("time", ctypes.c_uint32), ("dwExtraInfo", ctypes.c_size_t)]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", ctypes.c_uint16), ("wScan", ctypes.c_uint16), ("dwFlags", ctypes.c_uint32),
                ("time", ctypes.c_uint32), ("dwExtraInfo", ctypes.c_size_t)]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", ctypes.c_uint32), ("wParamL", ctypes.c_uint16), ("wParamH", ctypes.c_uint16)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", ctypes.c_uint32), ("u", _INPUTUNION)]


_proto(user32.SendInput, ctypes.c_uint, ctypes.c_uint, ctypes.POINTER(INPUT), ctypes.c_int)

_MOVE, _LDOWN, _LUP, _VDESK, _ABS = 0x0001, 0x0002, 0x0004, 0x4000, 0x8000


class Mouse:
    """Pointer moves + left clicks via SendInput (physical pixels), shaped like a
    real mouse. `expected` / `busy` let the InputWatcher tell the bot's moves from yours.

    Why not just absolute moves: a real mouse only ever sends RELATIVE motion, and
    Roblox on Windows tracks the pointer from that. After an absolute "teleport"
    it sometimes keeps the OLD pointer position - the button under the real
    cursor never lights up and the press lands where Roblox thinks the pointer
    still is (seen in the field: 4 presses dead-centre on Reroll, all ignored).
    So every move ends with a small relative wiggle (raw input like a real mouse),
    and button events carry no position, exactly like a real mouse."""

    NUDGE_GAP = 0.010         # between the two halves of a wiggle (> 1 frame at 120 fps)

    def __init__(self):
        self.lock = threading.Lock()
        self.busy = False
        self.expected = None

    @staticmethod
    def _send(flags, dx=0, dy=0):
        inp = INPUT(type=0)
        inp.mi = MOUSEINPUT(dx, dy, 0, flags, 0, 0)
        if user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT)) != 1:
            raise OSError(ctypes.get_last_error(),
                          "SendInput was blocked (is Roblox running as administrator? "
                          "Then run the bot as administrator too)")

    @staticmethod
    def _norm(x, y):
        v = virtual_screen()
        return (int(round((x - v.x) * 65535 / max(1, v.w - 1))),
                int(round((y - v.y) * 65535 / max(1, v.h - 1))))

    def _pin(self, x, y):
        """Make sure the real cursor sits exactly on (x, y) (relative moves are
        subject to pointer acceleration, absolute ones to rounding)."""
        cx, cy = cursor_pos()
        if cx != x or cy != y:
            user32.SetCursorPos(x, y)

    def move(self, x, y, nudge=True):
        x, y = int(round(x)), int(round(y))
        nx, ny = self._norm(x, y)
        with self.lock:
            self.expected = (x, y)
            self._send(_MOVE | _ABS | _VDESK, nx, ny)
            time.sleep(0.002)
            self._pin(x, y)
        if nudge:
            self.nudge()

    def nudge(self):
        """1 px relative motion out and back (two separate game frames), then
        back onto the exact spot. Makes Roblox re-read where the pointer is."""
        with self.lock:
            exp = self.expected
            self._send(_MOVE, 1, 1)
        time.sleep(self.NUDGE_GAP)
        with self.lock:
            self._send(_MOVE, -1, -1)
            time.sleep(0.002)
            if exp is not None:
                self._pin(*exp)

    def press(self, x, y, hold=0.06, release_hold=0.06):
        """Left press + release on the spot the pointer already rests on (call
        move() first). Held across several frames, and the pointer stays still
        for a few frames after the release - moving away in the same frame turns
        the click into 'dragged off the button' and Roblox ignores it."""
        x, y = int(round(x)), int(round(y))
        self.busy = True
        try:
            with self.lock:
                self.expected = (x, y)
                self._pin(x, y)
                self._send(_LDOWN)
            time.sleep(hold)
            with self.lock:
                self._pin(x, y)
                self._send(_LUP)
            time.sleep(release_hold)
        finally:
            self.busy = False

    def click(self, x, y, hold=0.06, hover=0.05, release_hold=0.06):
        """Move there (with the wiggle), hover for a few frames, press, release, stay."""
        self.busy = True
        try:
            self.move(x, y)
            time.sleep(hover)
            self.press(x, y, hold, release_hold)
        finally:
            self.busy = False

    def close(self):
        pass


# ------------------------------------------------------------------ stop key / mouse watch
VK = {f"F{i}": 0x6F + i for i in range(1, 13)}
VK.update({"ESCAPE": 0x1B, "ESC": 0x1B, "PAUSE": 0x13, "END": 0x23, "HOME": 0x24})


class InputWatcher(threading.Thread):
    """Calls callback('hotkey') when the stop key is pressed (works while Roblox
    has focus) and callback('mouse') when YOU move or click the real mouse - the
    bot owns the pointer while it runs and a nudge would misplace its clicks."""

    def __init__(self, key: str, callback, watch_mouse=True, mouse: Mouse = None):
        super().__init__(daemon=True)
        name = key.upper().replace("KEY_", "")
        self.vk = VK.get(name, 0x77)                            # default F8
        self.callback = callback
        self.watch_mouse = watch_mouse and mouse is not None
        self.mouse = mouse
        self._stop = threading.Event()
        self.error = None

    def run(self):
        prev_key = bool(user32.GetAsyncKeyState(self.vk) & 0x8000)
        away = clicked = 0
        while not self._stop.wait(0.02):
            try:
                down = bool(user32.GetAsyncKeyState(self.vk) & 0x8000)
                if down and not prev_key:
                    self.callback("hotkey")
                prev_key = down
                if not self.watch_mouse or self.mouse.busy:
                    away = clicked = 0
                    continue
                with self.mouse.lock:                           # never mid-move
                    exp = self.mouse.expected
                    pos = cursor_pos()
                if exp is not None and (abs(pos[0] - exp[0]) > 25 or abs(pos[1] - exp[1]) > 25):
                    away += 1
                else:
                    away = 0
                btn = (user32.GetAsyncKeyState(0x01) | user32.GetAsyncKeyState(0x02)) & 0x8000
                clicked = clicked + 1 if btn else 0
                if away >= 2 or clicked >= 2:
                    self.callback("mouse")
                    away = clicked = 0
            except Exception as exc:
                self.error = str(exc)

    def stop(self):
        self._stop.set()
