# -*- coding: utf-8 -*-
"""FPS 悬浮窗 —— 实时显示屏幕合成帧率（Windows 10/11，纯标准库，无需安装依赖）。

原理
----
用 DXGI Desktop Duplication 订阅桌面合成器每一次 Present，读取
DXGI_OUTDUPL_FRAME_INFO.LastPresentTime（QPC 时间戳）计算帧率与帧时间。
只取时间戳、不复制画面像素，常驻开销通常在 1% CPU 以内。

统计的是"屏幕真正被合成显示的帧"，即显示器实际刷新内容的速度
（游戏帧率被刷新率/V-Sync 封顶后就是这个值），不是显卡渲染帧数。

热键（全局，按住 Ctrl+Alt 再按）
--------------------------------
F  显示 / 隐藏
L  锁定 / 解锁（解锁后才能拖动、右键菜单）
1  小号    2  中号    3  大号
M  切换 帧率 / 帧时间 / 1% Low
Q  退出

命令行
------
python fps_overlay.pyw             启动悬浮窗
python fps_overlay.pyw --selftest  跑 10 秒并打印采样结果（不显示窗口）
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import json
import os
import queue
import sys
import threading
import time
import tkinter as tk
import tkinter.font as tkfont

APP_TITLE = "FPS Overlay"
CONFIG_DIR = os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), "FpsOverlay")
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")

_DEBUG = os.environ.get("FPS_OVERLAY_DEBUG") == "1"
_DEBUG_PATH = os.path.join(os.environ.get("TEMP") or ".", "fps_overlay_debug.log")


def _flush_debug(lines: list[str]) -> None:
    try:
        with open(_DEBUG_PATH, "a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    except Exception:
        pass

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
d3d11 = ctypes.WinDLL("d3d11")

DXGI_ERROR_WAIT_TIMEOUT = 0x887A0027
DXGI_ERROR_ACCESS_LOST = 0x887A0026
DXGI_ERROR_DEVICE_REMOVED = 0x887A0005
DXGI_ERROR_SESSION_DISCONNECTED = 0x887A0028

GWL_EXSTYLE = -20
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_LAYERED = 0x00080000
WS_EX_TRANSPARENT = 0x00000020
WS_EX_NOACTIVATE = 0x08000000

VK_CONTROL, VK_MENU = 0x11, 0x12
HOTKEYS = {
    0x46: "toggle_visible",   # F
    0x4C: "toggle_lock",      # L
    0x4D: "cycle_mode",       # M
    0x51: "quit",             # Q
    0x31: "size_small",       # 1
    0x32: "size_medium",      # 2
    0x33: "size_large",       # 3
}

DEFAULT_CONFIG = {
    "x": None,
    "y": None,
    "size": "medium",
    "opacity": 0.92,
    "locked": True,
    "visible": True,
    "mode": "fps",
    "show_app": True,
    "topmost": True,
}


# ---------------------------------------------------------------- win32 基础

def enable_dpi_awareness() -> None:
    try:
        user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))  # PER_MONITOR_AWARE_V2
    except Exception:
        try:
            ctypes.WinDLL("shcore").SetProcessDpiAwareness(2)
        except Exception:
            pass


def get_style(hwnd: int, index: int) -> int:
    fn = user32.GetWindowLongPtrW if hasattr(user32, "GetWindowLongPtrW") else user32.GetWindowLongW
    return fn(hwnd, index)


def set_style(hwnd: int, index: int, value: int) -> None:
    fn = user32.SetWindowLongPtrW if hasattr(user32, "SetWindowLongPtrW") else user32.SetWindowLongW
    fn(hwnd, index, value)


def top_level_hwnd(hwnd: int) -> int:
    h = user32.GetAncestor(hwnd, 2)  # GA_ROOT
    return h or hwnd


class RECT(ctypes.Structure):
    _fields_ = [("left", wt.LONG), ("top", wt.LONG), ("right", wt.LONG), ("bottom", wt.LONG)]


class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("rcMonitor", RECT), ("rcWork", RECT), ("dwFlags", wt.DWORD)]


def primary_rect() -> tuple[int, int, int, int]:
    mi = MONITORINFO()
    mi.cbSize = ctypes.sizeof(MONITORINFO)
    mon = user32.MonitorFromPoint(wt.POINT(0, 0), 1)  # MONITOR_DEFAULTTOPRIMARY
    user32.GetMonitorInfoW(mon, ctypes.byref(mi))
    r = mi.rcWork
    return r.left, r.top, r.right, r.bottom


def window_rect(hwnd: int) -> tuple[int, int, int, int]:
    r = RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    return r.left, r.top, r.right, r.bottom


class DEVMODEW(ctypes.Structure):
    _fields_ = [
        ("dmDeviceName", wt.WCHAR * 32), ("dmSpecVersion", wt.WORD), ("dmDriverVersion", wt.WORD),
        ("dmSize", wt.WORD), ("dmDriverExtra", wt.WORD), ("dmFields", wt.DWORD),
        ("dmPositionX", wt.LONG), ("dmPositionY", wt.LONG), ("dmDisplayOrientation", wt.DWORD),
        ("dmDisplayFixedOutput", wt.DWORD), ("dmColor", ctypes.c_short), ("dmDuplex", ctypes.c_short),
        ("dmYResolution", ctypes.c_short), ("dmTTOption", ctypes.c_short), ("dmCollate", ctypes.c_short),
        ("dmFormName", wt.WCHAR * 32), ("dmLogPixels", wt.WORD), ("dmBitsPerPel", wt.DWORD),
        ("dmPelsWidth", wt.DWORD), ("dmPelsHeight", wt.DWORD), ("dmDisplayFlags", wt.DWORD),
        ("dmDisplayFrequency", wt.DWORD), ("dmICMMethod", wt.DWORD), ("dmICMIntent", wt.DWORD),
        ("dmMediaType", wt.DWORD), ("dmDitherType", wt.DWORD), ("dmReserved1", wt.DWORD),
        ("dmReserved2", wt.DWORD), ("dmPanningWidth", wt.DWORD), ("dmPanningHeight", wt.DWORD),
    ]


def current_refresh_hz() -> int:
    dm = DEVMODEW()
    dm.dmSize = ctypes.sizeof(DEVMODEW)
    if user32.EnumDisplaySettingsW(None, -1, ctypes.byref(dm)) and dm.dmDisplayFrequency > 1:
        return int(dm.dmDisplayFrequency)
    return 60


def process_name_of(hwnd: int) -> str:
    if not hwnd:
        return ""
    pid = wt.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    if not pid.value:
        return ""
    h = kernel32.OpenProcess(0x1000, False, pid.value)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(512)
        size = wt.DWORD(len(buf))
        if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            name = os.path.basename(buf.value)
            return name[:-4] if name.lower().endswith(".exe") else name
        return ""
    finally:
        kernel32.CloseHandle(h)


# ---------------------------------------------------------------- DXGI 采样

class GUID(ctypes.Structure):
    _fields_ = [("Data1", ctypes.c_uint32), ("Data2", ctypes.c_uint16),
                ("Data3", ctypes.c_uint16), ("Data4", ctypes.c_ubyte * 8)]

    def __init__(self, text: str | None = None):
        super().__init__()
        if text:
            import uuid
            u = uuid.UUID(text)
            self.Data1, self.Data2, self.Data3 = u.fields[0], u.fields[1], u.fields[2]
            for i, b in enumerate(u.bytes[8:]):
                self.Data4[i] = b


IID_IDXGIDevice = GUID("54ec77fa-1377-44e6-8c32-88fd5f44c84c")
IID_IDXGIOutput1 = GUID("00cddea8-939b-4b83-a340-a685226666cc")


class POINT(ctypes.Structure):
    _fields_ = [("x", wt.LONG), ("y", wt.LONG)]


class OUTDUPL_POINTER_POSITION(ctypes.Structure):
    _fields_ = [("Position", POINT), ("Visible", wt.BOOL)]


class LARGE_INTEGER(ctypes.Structure):
    _fields_ = [("QuadPart", ctypes.c_int64)]


class OUTDUPL_FRAME_INFO(ctypes.Structure):
    _fields_ = [
        ("LastPresentTime", LARGE_INTEGER),
        ("LastMouseUpdateTime", LARGE_INTEGER),
        ("AccumulatedFrames", ctypes.c_uint32),
        ("RectsCoalesced", wt.BOOL),
        ("ProtectedContentMaskedOut", wt.BOOL),
        ("PointerPosition", OUTDUPL_POINTER_POSITION),
        ("TotalMetadataBufferSize", ctypes.c_uint32),
        ("PointerShapeBufferSize", ctypes.c_uint32),
    ]


class Com:
    """最小 COM 指针包装：按 vtable 槽位调用（x64 下 this 为第一个参数）。"""

    __slots__ = ("ptr", "_vt")

    def __init__(self, ptr):
        self.ptr = ptr
        self._vt = ctypes.cast(ptr, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents

    def call(self, index: int, restype, argtypes, *args):
        proto = ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)
        return proto(self._vt[index])(self.ptr, *args)

    def release(self):
        if self.ptr:
            try:
                self.call(2, ctypes.c_long, [])
            except Exception:
                pass
            self.ptr = None


class _Snapshot:
    """采样线程与 UI 之间共享的状态。"""

    __slots__ = ("fps", "frame_ms", "low1_ms", "spark", "idle", "status", "note", "refresh")

    def __init__(self):
        self.fps = 0.0
        self.frame_ms = 0.0
        self.low1_ms = 0.0
        self.spark: list[float] = []
        self.idle = True
        self.status = "init"      # init / ok / lost / unsupported
        self.note = ""
        self.refresh = 60


class FrameSampler(threading.Thread):
    """后台线程：阻塞式 AcquireNextFrame，只统计帧时间戳。"""

    RECONNECT_DELAY = 1.0

    def __init__(self, snapshot: _Snapshot, lock: threading.Lock):
        super().__init__(name="frame-sampler", daemon=True)
        self.snapshot = snapshot
        self.lock = lock
        self._stop = threading.Event()
        self._dupl: Com | None = None
        self._chain: list[Com] = []
        self._freq = self._query_freq()
        self._ticks: list[int] = []          # 最近 2 秒的 present 时间戳
        self._spark_clock = 0.0
        self._spark_bucket: list[float] = []
        self._refresh = 60
        self._refresh_at = 0.0
        self._last_compute = 0.0
        self.last_ui_update = 0.0
        self.overlay_rect: tuple[int, int, int, int] | None = None
        self.overlay_active = True
        self._rect_buf = (RECT * 128)()
        self._need = wt.UINT()
        self._debug_log: list[str] = []

    # -- 生命周期 ---------------------------------------------------------
    def stop(self):
        self._stop.set()

    @staticmethod
    def _query_freq() -> int:
        freq = ctypes.c_longlong()
        kernel32.QueryPerformanceFrequency(ctypes.byref(freq))
        return freq.value or 10000000

    def run(self):
        while not self._stop.is_set():
            if self._dupl is None:
                if not self._connect():
                    self._publish(status="unsupported", note="桌面复制不可用")
                    self._stop.wait(self.RECONNECT_DELAY)
                    continue
            try:
                self._pump_once()
            except Exception:
                self._teardown()
                self._stop.wait(0.3)
        self._teardown()

    # -- DXGI 连接 --------------------------------------------------------
    def _connect(self) -> bool:
        try:
            dev = ctypes.c_void_p()
            level = ctypes.c_int()
            ctx = ctypes.c_void_p()
            hr = d3d11.D3D11CreateDevice(None, 1, None, 0x20, None, 0, 7,
                                         ctypes.byref(dev), ctypes.byref(level), ctypes.byref(ctx))
            if hr < 0:
                return False
            device = Com(dev)
            dxdev_p = ctypes.c_void_p()
            device.call(0, ctypes.c_long,
                        [ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p)],
                        ctypes.byref(IID_IDXGIDevice), ctypes.byref(dxdev_p))
            dxdev = Com(dxdev_p)
            adapter_p = ctypes.c_void_p()
            dxdev.call(7, ctypes.c_long, [ctypes.POINTER(ctypes.c_void_p)], ctypes.byref(adapter_p))
            adapter = Com(adapter_p)
            output_p = ctypes.c_void_p()
            adapter.call(7, ctypes.c_long, [wt.UINT, ctypes.POINTER(ctypes.c_void_p)],
                         0, ctypes.byref(output_p))
            output = Com(output_p)
            out1_p = ctypes.c_void_p()
            output.call(0, ctypes.c_long,
                        [ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p)],
                        ctypes.byref(IID_IDXGIOutput1), ctypes.byref(out1_p))
            out1 = Com(out1_p)
            dupl_p = ctypes.c_void_p()
            hr = out1.call(22, ctypes.c_long,
                           [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)],
                           device.ptr, ctypes.byref(dupl_p))
            if hr < 0 or not dupl_p.value:
                for c in (out1, output, adapter, dxdev, device):
                    c.release()
                return False
            self._chain = [output, adapter, dxdev, device, out1]
            self._dupl = Com(dupl_p)
            self._refresh = current_refresh_hz()
            self._rect_buf = (RECT * 128)()
            return True
        except Exception:
            self._teardown()
            return False

    def _teardown(self):
        if self._dupl:
            try:
                self._dupl.call(14, ctypes.c_long, [])
            except Exception:
                pass
            self._dupl.release()
            self._dupl = None
        for c in self._chain:
            c.release()
        self._chain = []

    # -- 单帧 -------------------------------------------------------------
    def _pump_once(self):
        info = OUTDUPL_FRAME_INFO()
        res = ctypes.c_void_p()
        hr = self._dupl.call(8, ctypes.c_long,
                             [wt.UINT, ctypes.POINTER(OUTDUPL_FRAME_INFO),
                              ctypes.POINTER(ctypes.c_void_p)],
                             100, ctypes.byref(info), ctypes.byref(res))
        if hr < 0:
            code = hr & 0xFFFFFFFF
            if code == DXGI_ERROR_WAIT_TIMEOUT:
                self._publish(status="ok")
                return
            if code in (DXGI_ERROR_ACCESS_LOST, DXGI_ERROR_DEVICE_REMOVED,
                        DXGI_ERROR_SESSION_DISCONNECTED):
                self._publish(status="lost", note="显示模式切换，重连中")
                self._teardown()
                self._stop.wait(0.25)
                return
            self._publish(status="lost", note=f"0x{code:08X}")
            self._teardown()
            self._stop.wait(self.RECONNECT_DELAY)
            return

        tick = int(info.LastPresentTime.QuadPart)
        self_frame = tick > 0 and self._is_own_frame()
        try:
            self._dupl.call(14, ctypes.c_long, [])
        except Exception:
            pass
        if res.value:
            Com(res).release()

        if tick > 0 and not self_frame:
            self._record(tick)
        self._publish(status="ok")

    def _is_own_frame(self) -> bool:
        """判断这一帧是否只是悬浮窗自己在刷新（避免自己给自己刷帧数）。"""
        rect = self.overlay_rect
        if not rect or not self.overlay_active:
            return False
        # 只有刚刷新过 UI 的这段时间才可能是自己造成的，省掉大量无关的脏矩形查询
        if time.perf_counter() - self.last_ui_update > 0.05:
            return False
        try:
            hr = self._dupl.call(9, ctypes.c_long,
                                 [wt.UINT, ctypes.POINTER(RECT), ctypes.POINTER(wt.UINT)],
                                 ctypes.sizeof(self._rect_buf), self._rect_buf,
                                 ctypes.byref(self._need))
        except Exception:
            return False
        if hr < 0:
            return False
        count = min(self._need.value, 128)
        if _DEBUG:
            self._debug_log.append(
                f"rects={count} first={(self._rect_buf[0].left, self._rect_buf[0].top, self._rect_buf[0].right, self._rect_buf[0].bottom) if count else None} panel={rect}")
            if len(self._debug_log) > 20:
                _flush_debug(self._debug_log)
                self._debug_log.clear()
        if count <= 0:
            return False
        left, top, right, bottom = rect
        for i in range(count):
            r = self._rect_buf[i]
            if (r.left < left - 8 or r.top < top - 8
                    or r.right > right + 8 or r.bottom > bottom + 8):
                return False
        return True

    # -- 统计 -------------------------------------------------------------
    def _record(self, tick: int):
        self._ticks.append(tick)
        cutoff = tick - int(self._freq * 2.0)
        while self._ticks and self._ticks[0] < cutoff:
            self._ticks.pop(0)

    def _publish(self, status: str | None = None, note: str | None = None):
        now_s = time.perf_counter()
        if status is not None or note:
            with self.lock:
                if status is not None:
                    self.snapshot.status = status
                    if status == "ok":
                        self.snapshot.note = note or ""
                elif note:
                    self.snapshot.note = note
        # 统计只在 20Hz 上重算：每帧都排序几百个间隔会吃掉几个点的 CPU
        if now_s - self._last_compute < 0.05:
            return
        self._last_compute = now_s

        if now_s - self._refresh_at > 2.0:
            self._refresh_at = now_s
            self._refresh = current_refresh_hz()

        ticks = self._ticks
        fps = frame_ms = low1 = 0.0
        idle = True
        if ticks:
            latest = ticks[-1]
            age = (self._qpc_now() - latest) / self._freq
            window_start = latest - int(self._freq * 1.0)
            recent = [t for t in ticks if t > window_start]
            fps = float(len(recent))
            if len(recent) > 1:
                gaps = [(recent[i + 1] - recent[i]) / self._freq for i in range(len(recent) - 1)]
                frame_ms = sum(gaps) / len(gaps) * 1000.0
            if len(ticks) > 2:
                all_gaps = sorted((ticks[i + 1] - ticks[i]) / self._freq
                                  for i in range(len(ticks) - 1))
                worst = all_gaps[min(len(all_gaps) - 1, int(len(all_gaps) * 0.99))]
                low1 = worst * 1000.0
            idle = age > 0.8

        # 30 秒迷你曲线（每 0.5 秒一个点）
        if now_s - self._spark_clock >= 0.5:
            self._spark_clock = now_s
            self._spark_bucket.append(0.0 if idle else fps)
            if len(self._spark_bucket) > 60:
                self._spark_bucket.pop(0)

        with self.lock:
            snap = self.snapshot
            snap.refresh = self._refresh
            snap.fps = 0.0 if idle else fps
            snap.frame_ms = 0.0 if idle else frame_ms
            snap.low1_ms = low1
            snap.idle = idle
            snap.spark = list(self._spark_bucket)

    @staticmethod
    def _qpc_now() -> int:
        v = ctypes.c_longlong()
        kernel32.QueryPerformanceCounter(ctypes.byref(v))
        return v.value


# ---------------------------------------------------------------- 配置

def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg.update(json.load(f))
    except Exception:
        pass
    return cfg


def save_config(cfg: dict) -> None:
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        tmp = CONFIG_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        os.replace(tmp, CONFIG_PATH)
    except Exception:
        pass


# ---------------------------------------------------------------- 主题

BG = "#0B0E13"
BORDER = "#1E2836"
TEXT = "#E9EFF6"
DIM = "#66788D"
ACCENT = "#4CC2FF"
GOOD = "#3DDC97"
WARN = "#F5C451"
BAD = "#FF6B6B"

# 96 DPI 下的基准尺寸：(宽度, 数字像素高, 说明文字像素高, 内边距)
SIZES = {
    "small": (168, 22, 8, 9),
    "medium": (206, 30, 9, 11),
    "large": (252, 38, 10, 13),
}


# ---------------------------------------------------------------- 悬浮窗

class Overlay:
    def __init__(self):
        self.cfg = load_config()
        self.snapshot = _Snapshot()
        self.lock = threading.Lock()
        self.sampler = FrameSampler(self.snapshot, self.lock)
        self.actions: queue.Queue[str] = queue.Queue()
        self.hwnd = 0
        self.scale = 1.0
        self.locked = bool(self.cfg["locked"])
        self.mode = self.cfg["mode"] if self.cfg["mode"] in ("fps", "ms", "low") else "fps"
        self.drag_origin = None
        self._items: dict[str, int] = {}
        self._app_at = 0.0
        self._app_name = ""
        self._last_key = None

        self.root = tk.Tk()
        self.root.title(APP_TITLE)
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", bool(self.cfg["topmost"]))
        self.root.attributes("-alpha", float(self.cfg["opacity"]))
        self.root.configure(bg=BG)
        try:
            self.root.wm_attributes("-toolwindow", True)
        except tk.TclError:
            pass

        self._build_window()
        self._bind_events()

        self.sampler.start()
        threading.Thread(target=self._hotkey_loop, name="hotkeys", daemon=True).start()
        self.root.after(60, self._tick)

    # -- 窗口与布局 -------------------------------------------------------
    def _build_window(self):
        self.root.geometry("+320+240")
        self.root.update_idletasks()
        self.hwnd = top_level_hwnd(self.root.winfo_id())
        self._apply_scale()
        self._make_metrics()

        x, y = self.cfg.get("x"), self.cfg.get("y")
        if not isinstance(x, int) or not isinstance(y, int):
            left, top, right, bottom = primary_rect()
            x = right - self.W - int(24 * self.scale)
            y = top + int(24 * self.scale)
        self.root.geometry(f"{self.W}x{self.H}+{x}+{y}")

        ex = get_style(self.hwnd, GWL_EXSTYLE)
        set_style(self.hwnd, GWL_EXSTYLE,
                  ex | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE | WS_EX_LAYERED
                  | (WS_EX_TRANSPARENT if self.locked else 0))

        self.canvas = tk.Canvas(self.root, width=self.W, height=self.H, bg=BG,
                                highlightthickness=0, bd=0)
        self.canvas.pack(fill="both", expand=True)
        self._build_menu()
        self._redraw_static()

    def _apply_scale(self):
        dpi = user32.GetDpiForWindow(self.hwnd) if self.hwnd else 96
        self.scale = max(0.75, min((dpi or 96) / 96.0, 3.0))

    def _font(self, px: float, bold: bool = False, family: str = "Segoe UI"):
        return tkfont.Font(root=self.root, family=family, size=-max(6, int(round(px))),
                           weight="bold" if bold else "normal")

    def _make_metrics(self):
        width, num_px, meta_px, pad = SIZES.get(self.cfg["size"], SIZES["medium"])
        s = self.scale
        self.num_px = num_px * s
        self.f_num = self._font(self.num_px, bold=True, family="Consolas")
        self.f_unit = self._font((meta_px + 1) * s, bold=True)
        self.f_meta = self._font(meta_px * s)
        self._font_cache = {}
        self.P = int(round(pad * s))
        num_h = self.f_num.metrics("linespace")
        meta_h = self.f_meta.metrics("linespace")
        self.num_h, self.meta_h = num_h, meta_h
        self.spark_h = max(int(round(9 * s)), int(num_h * 0.44))
        self.W = int(round(width * s))
        self.H = (self.P + num_h + int(round(6 * s)) + self.spark_h
                  + int(round(6 * s)) + meta_h + self.P)

    def _font_at(self, px: float):
        key = int(round(px))
        font = self._font_cache.get(key)
        if font is None:
            font = self._font(key, bold=True, family="Consolas")
            self._font_cache[key] = font
        return font

    def _redraw_static(self):
        self.canvas.delete("all")
        self._last_key = None
        self.canvas.config(width=self.W, height=self.H)
        self.root.geometry(f"{self.W}x{self.H}")
        W, H, P, s = self.W, self.H, self.P, self.scale

        self._items["bg"] = self.canvas.create_rectangle(
            0, 0, W - 1, H - 1, fill=BG, outline=BORDER, width=1)
        self._items["accent"] = self.canvas.create_rectangle(
            0, 0, W - 1, max(2, int(round(2 * s))), fill=ACCENT, outline="")

        self.num_top = int(round(P * 0.8))
        self._items["num"] = self.canvas.create_text(
            P, self.num_top, text="0", anchor="nw", fill=TEXT, font=self.f_num)
        self._items["unit"] = self.canvas.create_text(
            P, self.num_top, text="FPS", anchor="nw", fill=DIM, font=self.f_unit)

        self.spark_top = self.num_top + self.num_h + int(round(6 * s))
        self.spark_bottom = self.spark_top + self.spark_h
        self.bar = (P, self.spark_top, W - P, self.spark_bottom)
        self._items["spark_bg"] = self.canvas.create_rectangle(
            *self.bar, fill="#111A24", outline="")
        self._items["spark_fill"] = self.canvas.create_polygon(
            0, 0, 0, 0, 0, 0, fill="#15364B", outline="")
        self._items["spark_line"] = self.canvas.create_line(
            0, 0, 0, 0, fill=ACCENT, width=max(1, int(round(1.4 * s))))

        self.meta_y = H - self.P
        self._items["meta"] = self.canvas.create_text(
            P, self.meta_y, text=f"{current_refresh_hz()}Hz", anchor="sw", fill=DIM,
            font=self.f_meta)
        self._items["dot"] = self.canvas.create_oval(0, 0, 0, 0, fill=GOOD, outline="")
        self._layout_parts()

    def _layout_parts(self):
        if "num" not in self._items:
            return
        bbox = self.canvas.bbox(self._items["num"])
        if not bbox:
            return
        s = self.scale
        unit_x = bbox[2] + int(round(5 * s))
        unit_y = bbox[3] - self.f_unit.metrics("linespace")
        self.canvas.coords(self._items["unit"], unit_x, unit_y)
        r = max(2, int(round(2.4 * s)))
        cx = self.W - self.P - r - int(round(2 * s))
        cy = self.num_top + r + int(round(2 * s))
        self.canvas.coords(self._items["dot"], cx - r, cy - r, cx + r, cy + r)

    # -- 事件 -------------------------------------------------------------
    def _bind_events(self):
        self.canvas.bind("<Button-1>", self._on_press)
        self.canvas.bind("<B1-Motion>", self._on_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        self.canvas.bind("<Button-3>", self._on_menu)

    def _on_press(self, event):
        if not self.locked:
            self.drag_origin = (event.x_root, event.y_root,
                                self.root.winfo_x(), self.root.winfo_y())

    def _on_drag(self, event):
        if self.locked or not self.drag_origin:
            return
        ox, oy, wx, wy = self.drag_origin
        self.root.geometry(f"+{wx + event.x_root - ox}+{wy + event.y_root - oy}")

    def _on_release(self, event):
        if self.drag_origin:
            self.drag_origin = None
            self._persist_position()

    def _on_menu(self, event):
        if self.locked:
            return
        try:
            self.menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.menu.grab_release()

    def _build_menu(self):
        menu_kw = dict(tearoff=0, bg="#11161D", fg=TEXT, activebackground="#1E2A38",
                       activeforeground=TEXT, bd=0, font=("Segoe UI", 9))
        m = tk.Menu(self.root, **menu_kw)
        m.add_command(label="锁定（鼠标穿透）", command=lambda: self.set_locked(True))
        sizes = tk.Menu(m, **menu_kw)
        for key, label in (("small", "小"), ("medium", "中"), ("large", "大")):
            sizes.add_command(label=label, command=lambda k=key: self.set_size(k))
        m.add_cascade(label="大小", menu=sizes)
        modes = tk.Menu(m, **menu_kw)
        for key, label in (("fps", "帧率 FPS"), ("ms", "帧时间 ms"), ("low", "1% Low")):
            modes.add_command(label=label, command=lambda k=key: self.set_mode(k))
        m.add_cascade(label="主显示", menu=modes)
        m.add_command(label="回到右上角", command=self.reset_position)
        m.add_separator()
        m.add_command(label="退出", command=self.quit)
        self.menu = m

    # -- 状态变更 ---------------------------------------------------------
    def set_locked(self, locked: bool):
        self.locked = locked
        self.cfg["locked"] = locked
        ex = get_style(self.hwnd, GWL_EXSTYLE)
        ex = (ex | WS_EX_TRANSPARENT) if locked else (ex & ~WS_EX_TRANSPARENT)
        set_style(self.hwnd, GWL_EXSTYLE, ex)
        save_config(self.cfg)

    def toggle_lock(self):
        self.set_locked(not self.locked)

    def set_size(self, key: str):
        if key not in SIZES:
            return
        self.cfg["size"] = key
        self._make_metrics()
        self._redraw_static()
        self._persist_position()

    def size_small(self):
        self.set_size("small")

    def size_medium(self):
        self.set_size("medium")

    def size_large(self):
        self.set_size("large")

    def set_mode(self, key: str):
        if key in ("fps", "ms", "low"):
            self.mode = key
            self.cfg["mode"] = key
            save_config(self.cfg)

    def cycle_mode(self):
        order = ["fps", "ms", "low"]
        self.set_mode(order[(order.index(self.mode) + 1) % len(order)])

    def reset_position(self):
        left, top, right, bottom = primary_rect()
        self.root.geometry(f"+{right - self.W - int(24 * self.scale)}+{top + int(24 * self.scale)}")
        self._persist_position()

    def toggle_visible(self):
        self.cfg["visible"] = not self.cfg["visible"]
        if self.cfg["visible"]:
            self.root.deiconify()
            self.root.attributes("-topmost", True)
        else:
            self.root.withdraw()
        save_config(self.cfg)

    def _persist_position(self):
        try:
            self.cfg["x"] = self.root.winfo_x()
            self.cfg["y"] = self.root.winfo_y()
            save_config(self.cfg)
        except Exception:
            pass

    def quit(self):
        self._persist_position()
        self.sampler.stop()
        try:
            self.root.destroy()
        except tk.TclError:
            pass

    # -- 热键轮询 ---------------------------------------------------------
    def _hotkey_loop(self):
        pressed: set[int] = set()
        while True:
            time.sleep(0.03)
            try:
                if not (user32.GetAsyncKeyState(VK_CONTROL) & 0x8000
                        and user32.GetAsyncKeyState(VK_MENU) & 0x8000):
                    pressed.clear()
                    continue
                for vk, action in HOTKEYS.items():
                    down = bool(user32.GetAsyncKeyState(vk) & 0x8000)
                    if down and vk not in pressed:
                        self.actions.put(action)
                    if down:
                        pressed.add(vk)
                    else:
                        pressed.discard(vk)
            except Exception:
                return

    # -- 主循环 -----------------------------------------------------------
    def _tick(self):
        try:
            while True:
                try:
                    action = self.actions.get_nowait()
                except queue.Empty:
                    break
                handler = getattr(self, action, None)
                if handler:
                    try:
                        handler()
                    except Exception:
                        pass
            self._render()
        except Exception:
            pass
        try:
            if self.root.winfo_exists():
                self.root.after(100, self._tick)
        except tk.TclError:
            pass

    def _render(self):
        with self.lock:
            snap = self.snapshot
            status, idle = snap.status, snap.idle
            fps, frame_ms, low1 = snap.fps, snap.frame_ms, snap.low1_ms
            refresh, spark, note = snap.refresh, snap.spark, snap.note

        if status in ("lost", "unsupported"):
            main, unit = "--", "FPS"
        elif idle:
            main, unit = "0", "FPS"
        elif self.mode == "fps":
            main, unit = f"{fps:.0f}", "FPS"
        elif self.mode == "ms":
            main, unit = f"{frame_ms:.1f}", "MS"
        else:
            main, unit = (f"{1000.0 / low1:.0f}" if low1 > 0 else "0"), "FPS"

        # 数字过长（四五位数）时自动缩字号，避免顶出面板
        avail = self.W - 2 * self.P - self.f_unit.measure(unit) - int(round(8 * self.scale))
        font = self.f_num
        if avail > 8 and self.f_num.measure(main) > avail:
            ratio = avail / float(self.f_num.measure(main))
            font = self._font_at(max(8.0, self.num_px * ratio))

        color = GOOD if (status == "ok" and not idle) else (
            DIM if status == "ok" else (WARN if status == "lost" else BAD))

        info = f"{refresh}Hz"
        if low1 > 0:
            info += f" · 1% {1000.0 / low1:.0f}"
        if note:
            info = f"{refresh}Hz · {note}"
        elif self.cfg.get("show_app", True):
            app = self._foreground_app()
            if app and app.lower() not in ("fps_overlay", "python", "pythonw", "explorer"):
                candidate = f"{info} · {app}"
                if self.f_meta.measure(candidate) <= self.W - 2 * self.P:
                    info = candidate

        # 画面没变化就不重绘：常驻刷新时这一条能把 UI 开销再压掉一大半
        key = (main, unit, info, color,
               len(spark), round(spark[-1], 1) if spark else -1.0)
        if key == self._last_key:
            self._sync_overlay_rect()
            return
        self._last_key = key

        self.canvas.itemconfigure(self._items["num"], text=main, font=font,
                                  fill=(TEXT if not idle and status == "ok" else "#40546A"))
        self.canvas.itemconfigure(self._items["unit"], text=unit)
        self._layout_parts()
        self.canvas.itemconfigure(self._items["accent"], fill=color)
        self.canvas.itemconfigure(self._items["dot"], fill=color)
        self.canvas.itemconfigure(self._items["meta"], text=info)
        self._draw_spark(spark, refresh)
        self._sync_overlay_rect()
        self.sampler.last_ui_update = time.perf_counter()

    def _sync_overlay_rect(self):
        try:
            left, top, right, bottom = window_rect(self.hwnd)
            self.sampler.overlay_rect = (left, top, right, bottom)
            self.sampler.overlay_active = bool(self.cfg["visible"])
        except Exception:
            pass

    def _foreground_app(self) -> str:
        now = time.perf_counter()
        if now - self._app_at > 1.0:
            self._app_at = now
            try:
                self._app_name = process_name_of(user32.GetForegroundWindow())
            except Exception:
                self._app_name = ""
        return self._app_name

    def _draw_spark(self, spark: list[float], refresh: int):
        x1, y1, x2, y2 = self.bar
        if x2 - x1 < 4 or y2 - y1 < 3:
            return
        points = list(spark[-60:]) or [0.0]
        peak = max(max(points), max(30.0, refresh * 0.55))
        n = len(points)
        coords: list[float] = []
        for i, v in enumerate(points):
            px = x1 + (x2 - x1) * (i / max(1, n - 1)) if n > 1 else (x1 + x2) / 2
            ratio = max(0.0, min(1.0, v / peak))
            coords.extend((px, y2 - (y2 - y1) * ratio))
        if n == 1:
            coords = [x1, y2, x2, y2]
        self.canvas.coords(self._items["spark_line"], *coords)
        self.canvas.coords(self._items["spark_fill"],
                           *coords, x2, y2, coords[0], y2)


# ---------------------------------------------------------------- 入口

def selftest(seconds: float = 10.0) -> int:
    snap = _Snapshot()
    lock = threading.Lock()
    sampler = FrameSampler(snap, lock)
    sampler.start()
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < seconds:
        time.sleep(0.5)
        with lock:
            print(f"  {snap.status:11s} fps={snap.fps:6.1f} frame={snap.frame_ms:5.2f}ms "
                  f"1%low={snap.low1_ms:5.2f}ms refresh={snap.refresh}Hz")
    sampler.stop()
    time.sleep(0.2)
    print(f"done in {time.perf_counter() - t0:.1f}s")
    return 0


def single_instance() -> bool:
    kernel32.CreateMutexW.restype = wt.HANDLE
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wt.BOOL, wt.LPCWSTR]
    handle = kernel32.CreateMutexW(None, False, "Local\\FpsOverlayMutex")
    already = ctypes.get_last_error() == 183  # ERROR_ALREADY_EXISTS
    return not already


def main() -> int:
    if "--selftest" in sys.argv:
        return selftest()
    if not single_instance():
        try:
            import tkinter.messagebox as mb
            r = tk.Tk()
            r.withdraw()
            mb.showinfo(APP_TITLE, "FPS 悬浮窗已经在运行了。")
            r.destroy()
        except Exception:
            pass
        return 0
    enable_dpi_awareness()
    overlay = Overlay()
    if not overlay.cfg["visible"]:
        overlay.root.withdraw()
    overlay.root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
