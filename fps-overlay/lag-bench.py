# -*- coding: utf-8 -*-
"""lag-bench —— "开这个软件到底会不会让电脑卡"的 A/B 基线测试（Windows 10/11，纯标准库）。

思路
----
平均帧率 / CPU 占用看不出卡顿。真正决定手感的是尾巴指标：
  1. 帧时间 p99 与长帧次数（>2× 中位帧时间、>50ms）
  2. 调度抖动：Sleep(1ms) 实际多睡了多少（系统忙不忙的直接体现）
  3. 窗口响应：给前台窗口发 WM_NULL，多久回（"点一下多久有反应"）
  4. 磁盘队列 / 单次 IO 延迟 / 硬缺页（磁盘或内存打满时全系统顿）
  5. DPC 时间（驱动级卡顿）与 CPU 占用

用法
----
    python lag-bench.py --run "C:\\Path\\App.exe"          # 自动启动被测软件
    python lag-bench.py --run "app.exe" --args "--foo" --kill
    python lag-bench.py                                     # 手动：到点按回车，你去启动软件

常用参数
    --baseline 20   基线采样秒数（默认 20）
    --sample 20     加载后采样秒数（默认 20）
    --warmup 3      启动软件后的预热秒数（默认 3，不计入统计）
    --motion        开一个持续动画的小窗，保证屏幕一直在变（帧率类指标需要）
    --out report.md 额外输出 Markdown 报告
    --json out.json 输出原始统计

注意：桌面复制同一时刻只能被一个进程持有。若 FPS 悬浮窗在跑，请先退出它，
否则帧率类指标会显示不可用（其余指标照常）。
"""

from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes as wt
import json
import os
import statistics
import subprocess
import sys
import threading
import time


def configure_stdio() -> None:
    """让中文在 cmd / PowerShell / 重定向下都不乱码。"""
    try:
        cp = ctypes.WinDLL("kernel32").GetConsoleOutputCP()
        enc = f"cp{cp}" if cp else "utf-8"
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.reconfigure(encoding=enc, errors="replace")
            except Exception:
                pass
    except Exception:
        pass


def screen_locked() -> bool:
    """当前是否锁屏（锁屏时桌面复制和窗口消息都不可用）。"""
    try:
        h = user32.OpenInputDesktop(0, False, 0x0100)
        if h:
            user32.CloseDesktop(h)
            return False
        return True
    except Exception:
        return False


configure_stdio()

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
d3d11 = ctypes.WinDLL("d3d11")
pdh = ctypes.WinDLL("pdh")

user32.SendMessageTimeoutW.restype = ctypes.c_size_t
user32.SendMessageTimeoutW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM,
                                       wt.UINT, wt.UINT, ctypes.POINTER(ctypes.c_size_t)]
user32.GetForegroundWindow.restype = wt.HWND

WM_NULL = 0x0000
SMTO_BLOCK = 0x0001
SMTO_ABORTIFHUNG = 0x0002

DXGI_ERROR_WAIT_TIMEOUT = 0x887A0027
DXGI_ERROR_ACCESS_LOST = 0x887A0026
DXGI_ERROR_DEVICE_REMOVED = 0x887A0005
DXGI_ERROR_SESSION_DISCONNECTED = 0x887A0028


# ============================================================ 小工具

def pct(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    v = sorted(values)
    idx = min(len(v) - 1, max(0, int(round(q * (len(v) - 1)))))
    return v[idx]


def summarize(values: list[float]) -> dict:
    if not values:
        return {"n": 0, "mean": 0.0, "p50": 0.0, "p95": 0.0, "p99": 0.0, "max": 0.0}
    return {
        "n": len(values),
        "mean": statistics.fmean(values),
        "p50": pct(values, 0.50),
        "p95": pct(values, 0.95),
        "p99": pct(values, 0.99),
        "max": max(values),
    }


def fmt(value: float, digits: int = 2) -> str:
    return f"{value:.{digits}f}"


# ============================================================ 帧时间（DXGI 桌面复制）

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


class LARGE_INTEGER(ctypes.Structure):
    _fields_ = [("QuadPart", ctypes.c_int64)]


class POINT(ctypes.Structure):
    _fields_ = [("x", wt.LONG), ("y", wt.LONG)]


class PTR_POS(ctypes.Structure):
    _fields_ = [("Position", POINT), ("Visible", wt.BOOL)]


class FRAME_INFO(ctypes.Structure):
    _fields_ = [
        ("LastPresentTime", LARGE_INTEGER),
        ("LastMouseUpdateTime", LARGE_INTEGER),
        ("AccumulatedFrames", ctypes.c_uint32),
        ("RectsCoalesced", wt.BOOL),
        ("ProtectedContentMaskedOut", wt.BOOL),
        ("PointerPosition", PTR_POS),
        ("TotalMetadataBufferSize", ctypes.c_uint32),
        ("PointerShapeBufferSize", ctypes.c_uint32),
    ]


class Com:
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


class FrameRecorder(threading.Thread):
    """只读 present 时间戳，不复制像素。"""

    def __init__(self):
        super().__init__(name="frames", daemon=True)
        self.ticks: list[int] = []
        self.available = False
        self.error = ""
        self._stop = threading.Event()
        self._dupl: Com | None = None
        self._chain: list[Com] = []
        self._lock = threading.Lock()
        self._freq = 0

    def _qpc_freq(self) -> int:
        v = ctypes.c_longlong()
        kernel32.QueryPerformanceFrequency(ctypes.byref(v))
        return v.value or 10000000

    def connect(self) -> bool:
        try:
            dev = ctypes.c_void_p()
            level = ctypes.c_int()
            ctx = ctypes.c_void_p()
            hr = d3d11.D3D11CreateDevice(None, 1, None, 0x20, None, 0, 7,
                                         ctypes.byref(dev), ctypes.byref(level), ctypes.byref(ctx))
            if hr < 0:
                self.error = "D3D11 设备创建失败"
                return False
            device = Com(dev)
            dxdev_p = ctypes.c_void_p()
            device.call(0, ctypes.c_long, [ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p)],
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
            output.call(0, ctypes.c_long, [ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p)],
                        ctypes.byref(IID_IDXGIOutput1), ctypes.byref(out1_p))
            out1 = Com(out1_p)
            dupl_p = ctypes.c_void_p()
            hr = out1.call(22, ctypes.c_long, [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)],
                           device.ptr, ctypes.byref(dupl_p))
            if hr < 0 or not dupl_p.value:
                self.error = "桌面复制被占用或不可用（FPS 悬浮窗是否在运行？）"
                for c in (out1, output, adapter, dxdev, device):
                    c.release()
                return False
            self._chain = [output, adapter, dxdev, device, out1]
            self._dupl = Com(dupl_p)
            self._freq = self._qpc_freq()
            self.available = True
            return True
        except Exception as exc:  # pragma: no cover
            self.error = f"{type(exc).__name__}: {exc}"
            return False

    def run(self):
        while not self._stop.is_set():
            if self._dupl is None:
                if not self.connect():
                    return
            info = FRAME_INFO()
            res = ctypes.c_void_p()
            hr = self._dupl.call(8, ctypes.c_long,
                                 [wt.UINT, ctypes.POINTER(FRAME_INFO), ctypes.POINTER(ctypes.c_void_p)],
                                 100, ctypes.byref(info), ctypes.byref(res))
            if hr < 0:
                code = hr & 0xFFFFFFFF
                if code == DXGI_ERROR_WAIT_TIMEOUT:
                    continue
                if code in (DXGI_ERROR_ACCESS_LOST, DXGI_ERROR_DEVICE_REMOVED,
                            DXGI_ERROR_SESSION_DISCONNECTED):
                    self._teardown()
                    self._stop.wait(0.3)
                    continue
                self.error = f"0x{code:08X}"
                self._teardown()
                return
            tick = int(info.LastPresentTime.QuadPart)
            try:
                self._dupl.call(14, ctypes.c_long, [])
            except Exception:
                pass
            if res.value:
                Com(res).release()
            if tick > 0:
                with self._lock:
                    self.ticks.append(tick)
        self._teardown()

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

    def reset(self):
        with self._lock:
            self.ticks.clear()

    def snapshot(self, seconds: float) -> dict:
        with self._lock:
            ticks = list(self.ticks)
        if not self.available:
            return {"available": False, "error": self.error}
        if len(ticks) < 3:
            return {"available": True, "frames": len(ticks), "fps": 0.0,
                    "p50": 0.0, "p99": 0.0, "long_2x": 0, "long_50": 0, "stutter_ratio": 0.0}
        gaps = [(ticks[i + 1] - ticks[i]) / self._freq * 1000.0 for i in range(len(ticks) - 1)]
        gaps = [g for g in gaps if g > 0]
        if not gaps:
            return {"available": True, "frames": len(ticks), "fps": 0.0,
                    "p50": 0.0, "p99": 0.0, "long_2x": 0, "long_50": 0, "stutter_ratio": 0.0}
        med = statistics.median(gaps)
        return {
            "available": True,
            "frames": len(ticks),
            "fps": len(ticks) / max(seconds, 1e-6),
            "p50": med,
            "p99": pct(gaps, 0.99),
            "long_2x": sum(1 for g in gaps if g > 2.0 * med),
            "long_50": sum(1 for g in gaps if g > 50.0),
            "stutter_ratio": sum(1 for g in gaps if g > 2.0 * med) / len(gaps),
        }

    def stop(self):
        self._stop.set()
        self.join(timeout=1.0)


# ============================================================ 调度抖动

class JitterProbe(threading.Thread):
    """每 1ms 睡一次，统计实际多睡了多少毫秒。"""

    def __init__(self):
        super().__init__(name="jitter", daemon=True)
        self.samples: list[float] = []
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._period_set = False

    def run(self):
        try:
            winmm = ctypes.WinDLL("winmm")
            winmm.timeBeginPeriod(1)
            self._period_set = True
        except Exception:
            pass
        while not self._stop.is_set():
            t0 = time.perf_counter()
            time.sleep(0.001)
            overshoot = (time.perf_counter() - t0) * 1000.0 - 1.0
            with self._lock:
                self.samples.append(max(0.0, overshoot))
        if self._period_set:
            try:
                ctypes.WinDLL("winmm").timeEndPeriod(1)
            except Exception:
                pass

    def reset(self):
        with self._lock:
            self.samples.clear()

    def snapshot(self) -> dict:
        with self._lock:
            s = list(self.samples)
        st = summarize(s)
        st["hitch_10"] = sum(1 for v in s if v > 10.0)
        st["hitch_50"] = sum(1 for v in s if v > 50.0)
        return st

    def stop(self):
        self._stop.set()
        self.join(timeout=1.0)


# ============================================================ 窗口响应

class ResponseProbe(threading.Thread):
    """每 200ms 给前台窗口发 WM_NULL，测量往返毫秒数。"""

    def __init__(self):
        super().__init__(name="response", daemon=True)
        self.samples: list[float] = []
        self.timeouts = 0
        self.skipped = 0
        self._stop = threading.Event()
        self._lock = threading.Lock()

    def run(self):
        while not self._stop.is_set():
            hwnd = user32.GetForegroundWindow()
            if hwnd:
                t0 = time.perf_counter()
                result = ctypes.c_size_t(0)
                ok = user32.SendMessageTimeoutW(
                    hwnd, WM_NULL, 0, 0,
                    SMTO_BLOCK | SMTO_ABORTIFHUNG, 100, ctypes.byref(result))
                ms = (time.perf_counter() - t0) * 1000.0
                with self._lock:
                    if ok:
                        self.samples.append(ms)
                    elif ms < 2.0:
                        # 目标窗口根本不接受消息（锁屏 / 更高完整性级别 / 别的桌面）
                        self.skipped += 1
                    else:
                        self.timeouts += 1
                        self.samples.append(100.0)
            time.sleep(0.2)

    def reset(self):
        with self._lock:
            self.samples.clear()
            self.timeouts = 0
            self.skipped = 0

    def snapshot(self) -> dict:
        with self._lock:
            s = list(self.samples)
            timeouts = self.timeouts
            skipped = self.skipped
        st = summarize(s)
        st["timeouts"] = timeouts
        st["skipped"] = skipped
        st["available"] = len(s) > 0
        return st

    def stop(self):
        self._stop.set()
        self.join(timeout=1.0)


# ============================================================ 性能计数器（PDH）

class PDH_FMT_COUNTERVALUE(ctypes.Structure):
    _fields_ = [("CStatus", wt.DWORD), ("doubleValue", ctypes.c_double)]


PDH_FMT_DOUBLE = 0x00000200

pdh.PdhOpenQueryW.restype = ctypes.c_long
pdh.PdhOpenQueryW.argtypes = [wt.LPCWSTR, ctypes.c_size_t, ctypes.POINTER(ctypes.c_void_p)]
pdh.PdhAddEnglishCounterW.restype = ctypes.c_long
pdh.PdhAddEnglishCounterW.argtypes = [ctypes.c_void_p, wt.LPCWSTR, ctypes.c_size_t,
                                      ctypes.POINTER(ctypes.c_void_p)]
pdh.PdhCollectQueryData.restype = ctypes.c_long
pdh.PdhCollectQueryData.argtypes = [ctypes.c_void_p]
pdh.PdhGetFormattedCounterValue.restype = ctypes.c_long
pdh.PdhGetFormattedCounterValue.argtypes = [ctypes.c_void_p, wt.DWORD,
                                            ctypes.POINTER(wt.DWORD),
                                            ctypes.POINTER(PDH_FMT_COUNTERVALUE)]
pdh.PdhCloseQuery.restype = ctypes.c_long
pdh.PdhCloseQuery.argtypes = [ctypes.c_void_p]


class PerfCounters(threading.Thread):
    PATHS = [
        ("cpu", r"\Processor(_Total)\% Processor Time", 1.0),
        ("dpc", r"\Processor(_Total)\% DPC Time", 1.0),
        ("disk_queue", r"\PhysicalDisk(_Total)\Avg. Disk Queue Length", 1.0),
        ("disk_ms", r"\PhysicalDisk(_Total)\Avg. Disk sec/Transfer", 1000.0),
        ("hard_faults", r"\Memory\Pages Input/sec", 1.0),
        ("mem_free_mb", r"\Memory\Available MBytes", 1.0),
    ]

    def __init__(self):
        super().__init__(name="pdh", daemon=True)
        self.samples: dict[str, list[float]] = {k: [] for k, _, _ in self.PATHS}
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._query = ctypes.c_void_p()
        self._counters: dict[str, ctypes.c_void_p] = {}
        self._ok = False

    def connect(self) -> bool:
        status = pdh.PdhOpenQueryW(None, 0, ctypes.byref(self._query))
        if status != 0:
            return False
        for key, path, _ in self.PATHS:
            handle = ctypes.c_void_p()
            if pdh.PdhAddEnglishCounterW(self._query, path, 0, ctypes.byref(handle)) == 0:
                self._counters[key] = handle
        if not self._counters:
            return False
        pdh.PdhCollectQueryData(self._query)  # 预热一次，让 rate 计数器有基线
        self._ok = True
        return True

    def run(self):
        if not self.connect():
            return
        while not self._stop.is_set():
            pdh.PdhCollectQueryData(self._query)
            for key, handle in self._counters.items():
                scale = next(s for k, _, s in self.PATHS if k == key)
                value = PDH_FMT_COUNTERVALUE()
                if pdh.PdhGetFormattedCounterValue(handle, PDH_FMT_DOUBLE, None,
                                                   ctypes.byref(value)) == 0:
                    if value.CStatus == 0:
                        with self._lock:
                            self.samples[key].append(value.doubleValue * scale)
            time.sleep(0.5)
        try:
            pdh.PdhCloseQuery(self._query)
        except Exception:
            pass

    def reset(self):
        with self._lock:
            for key in self.samples:
                self.samples[key].clear()

    def snapshot(self) -> dict:
        out = {}
        with self._lock:
            for key, values in self.samples.items():
                st = summarize(values)
                st["peak"] = max(values) if values else 0.0
                out[key] = st
        out["available"] = self._ok and any(v["n"] for v in out.values() if isinstance(v, dict))
        return out

    def stop(self):
        self._stop.set()
        self.join(timeout=1.0)


# ============================================================ 持续画面（可选）

class MotionWindow(threading.Thread):
    """一个 180×120 的置顶小窗，用 GDI 画循环移动的方块。

    不用 tkinter：Tk 对象在跨线程销毁时会触发 Tcl_AsyncDelete 崩溃。
    """

    CLS = "LagBenchMotion"
    W, H = 180, 120

    def __init__(self):
        super().__init__(name="motion", daemon=True)
        self.hwnd = 0
        self.error = ""
        self.ready = threading.Event()
        self._t0 = time.perf_counter()
        self._x, self._y = 90.0, 60.0
        self._proc = None

    def run(self):
        try:
            self._create()
        except Exception as exc:  # pragma: no cover
            self.error = f"{type(exc).__name__}: {exc}"
            self.ready.set()
        return

    def _create(self):
        u = user32
        g = ctypes.WinDLL("gdi32")

        class WNDCLASSW(ctypes.Structure):
            _fields_ = [("style", wt.UINT), ("lpfnWndProc", ctypes.c_void_p),
                        ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                        ("hInstance", wt.HINSTANCE), ("hIcon", wt.HICON),
                        ("hCursor", wt.HANDLE), ("hbrBackground", wt.HBRUSH),
                        ("lpszMenuName", wt.LPCWSTR), ("lpszClassName", wt.LPCWSTR)]

        class MSG(ctypes.Structure):
            _fields_ = [("hwnd", wt.HWND), ("message", wt.UINT), ("wParam", wt.WPARAM),
                        ("lParam", wt.LPARAM), ("time", wt.DWORD), ("pt", wt.POINT)]

        class PAINTSTRUCT(ctypes.Structure):
            _fields_ = [("hdc", wt.HDC), ("fErase", wt.BOOL), ("rcPaint", RECT),
                        ("fRestore", wt.BOOL), ("fIncUpdate", wt.BOOL),
                        ("rgbReserved", ctypes.c_byte * 32)]

        LRESULT = ctypes.c_ssize_t
        WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)

        def wndproc(hwnd, msg, wp, lp):
            if msg == 0x0113:  # WM_TIMER
                import math
                t = time.perf_counter() - self._t0
                self._x = 90 + math.cos(t * 3.0) * 62
                self._y = 60 + math.sin(t * 4.2) * 42
                u.InvalidateRect(hwnd, None, False)
                return 0
            if msg == 0x000F:  # WM_PAINT
                ps = PAINTSTRUCT()
                hdc = u.BeginPaint(hwnd, ctypes.byref(ps))
                bg = g.CreateSolidBrush(0x00161010)
                g.FillRect(hdc, ctypes.byref(ps.rcPaint), bg)
                g.DeleteObject(bg)
                brush = g.CreateSolidBrush(0x00FFC24C)
                old = g.SelectObject(hdc, brush)
                g.Ellipse(hdc, int(self._x - 13), int(self._y - 13),
                          int(self._x + 13), int(self._y + 13))
                g.SelectObject(hdc, old)
                g.DeleteObject(brush)
                u.EndPaint(hwnd, ctypes.byref(ps))
                return 0
            if msg == 0x0014:  # WM_ERASEBKGND
                return 1
            if msg == 0x0002:  # WM_DESTROY
                u.PostQuitMessage(0)
                return 0
            return u.DefWindowProcW(hwnd, msg, wp, lp)

        self._proc = WNDPROC(wndproc)
        hinst = kernel32.GetModuleHandleW(None)
        wc = WNDCLASSW()
        wc.lpfnWndProc = ctypes.cast(self._proc, ctypes.c_void_p)
        wc.hInstance = hinst
        wc.lpszClassName = self.CLS
        u.RegisterClassW(ctypes.byref(wc))

        WS_EX_TOPMOST, WS_EX_TOOLWINDOW, WS_EX_NOACTIVATE = 0x8, 0x80, 0x08000000
        self.hwnd = u.CreateWindowExW(
            WS_EX_TOPMOST | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE,
            self.CLS, "lag-bench-motion", 0x80000000, 40, 40, self.W, self.H,
            None, None, hinst, None)
        if not self.hwnd:
            raise OSError(f"CreateWindowExW 失败: {ctypes.get_last_error()}")
        u.ShowWindow(self.hwnd, 8)  # SW_SHOWNA
        u.SetTimer(self.hwnd, 1, 8, None)
        self.ready.set()

        msg = MSG()
        while u.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            u.TranslateMessage(ctypes.byref(msg))
            u.DispatchMessageW(ctypes.byref(msg))

    def stop(self):
        if self.hwnd:
            try:
                user32.PostMessageW(self.hwnd, 0x0010, 0, 0)  # WM_CLOSE
            except Exception:
                pass
        self.join(timeout=2.0)


# ============================================================ 采集编排

class Bench:
    def __init__(self, durations: dict, motion: bool):
        self.durations = durations
        self.frames = FrameRecorder()
        self.jitter = JitterProbe()
        self.response = ResponseProbe()
        self.perf = PerfCounters()
        self.motion = MotionWindow() if motion else None
        self._stop = threading.Event()
        self._phase_start = 0.0
        self._threads_started = False

    def start(self):
        if self.motion:
            self.motion.start()
            self.motion.ready.wait(2.0)
        self.frames.start()
        if self.motion:
            self.motion.ready.wait(0.1)
        self.jitter.start()
        self.response.start()
        self.perf.start()
        self._threads_started = True
        time.sleep(0.8)  # 预热，丢掉首批样本
        self._phase_start = time.perf_counter()
        self.frames.reset()
        self.jitter.reset()
        self.response.reset()
        self.perf.reset()

    def phase(self, label: str, seconds: float, callback=None) -> dict:
        # 预热 / 启动软件放在计时之前，不计入统计
        if callback:
            callback()
        self.frames.reset()
        self.jitter.reset()
        self.response.reset()
        self.perf.reset()
        t_start = time.perf_counter()
        deadline = t_start + seconds
        last = 0
        while True:
            now = time.perf_counter()
            remaining = deadline - now
            if remaining <= 0:
                break
            if int(now - t_start) != last:
                last = int(now - t_start)
                print(f"\r  {label} 采集中 {last:2d}/{int(seconds)} 秒", end="", flush=True)
            time.sleep(0.05)
        elapsed = time.perf_counter() - t_start
        print(f"\r  {label} 采集完成（{elapsed:.1f} 秒）           ")
        return {
            "label": label,
            "seconds": elapsed,
            "frames": self.frames.snapshot(elapsed),
            "jitter": self.jitter.snapshot(),
            "response": self.response.snapshot(),
            "perf": self.perf.snapshot(),
        }

    def stop(self):
        self.frames.stop()
        self.jitter.stop()
        self.response.stop()
        self.perf.stop()
        if self.motion:
            self.motion.stop()


# ============================================================ 报告

def ratio(before: float, after: float) -> str:
    if before <= 0.0001:
        return "—" if after <= 0.0001 else "新增"
    delta = (after - before) / before * 100.0
    return f"{delta:+.0f}%"


def render(base: dict, load: dict, title: str) -> tuple[str, list[str]]:
    issues: list[str] = []
    notes: list[str] = []
    rows: list[tuple[str, str, str, str]] = []

    fb, fl = base["frames"], load["frames"]
    if fb.get("available") and fb.get("fps", 0) >= 5:
        rows.append(("帧率 (fps)", fmt(fb.get("fps", 0), 1), fmt(fl.get("fps", 0), 1),
                     ratio(fb.get("fps", 0), fl.get("fps", 0))))
        rows.append(("帧时间 p50 (ms)", fmt(fb["p50"]), fmt(fl["p50"]),
                     ratio(fb["p50"], fl["p50"])))
        rows.append(("帧时间 p99 (ms)", fmt(fb["p99"]), fmt(fl["p99"]),
                     ratio(fb["p99"], fl["p99"])))
        rows.append(("长帧 >2×中位 (次)", str(fb["long_2x"]), str(fl["long_2x"]), "—"))
        rows.append(("长帧 >50ms (次)", str(fb["long_50"]), str(fl["long_50"]), "—"))
        if fb["p99"] > 0 and fl["p99"] > max(12.0, 1.5 * fb["p99"]):
            issues.append(f"帧时间 p99 从 {fb['p99']:.1f}ms 涨到 {fl['p99']:.1f}ms")
        if fl["long_50"] > max(2, fb["long_50"] * 2):
            issues.append(f"超过 50ms 的长帧从 {fb['long_50']} 次增到 {fl['long_50']} 次")
    else:
        rows.append(("帧率", "不可用", "不可用", "—"))
        if not fb.get("available"):
            notes.append(f"帧率不可用：{fb.get('error', '未知原因')}")
        else:
            notes.append("帧率不可用：屏幕几乎没有画面变化，加 --motion 或开着视频/游戏再测")

    jb, jl = base["jitter"], load["jitter"]
    rows.append(("调度抖动 p50 (ms)", fmt(jb["p50"]), fmt(jl["p50"]), ratio(jb["p50"], jl["p50"])))
    rows.append(("调度抖动 p99 (ms)", fmt(jb["p99"]), fmt(jl["p99"]), ratio(jb["p99"], jl["p99"])))
    rows.append(("抖动尖峰 >10ms (次)", str(jb["hitch_10"]), str(jl["hitch_10"]), "—"))
    if jl["p99"] > max(5.0, 3.0 * jb["p99"]) and jl["p99"] > 2.0:
        issues.append(f"调度抖动 p99 从 {jb['p99']:.1f}ms 升到 {jl['p99']:.1f}ms")

    rb, rl = base["response"], load["response"]
    if rb.get("available") and rl.get("available"):
        rows.append(("窗口响应 p50 (ms)", fmt(rb["p50"]), fmt(rl["p50"]), ratio(rb["p50"], rl["p50"])))
        rows.append(("窗口响应 p99 (ms)", fmt(rb["p99"]), fmt(rl["p99"]), ratio(rb["p99"], rl["p99"])))
        rows.append(("响应超时 (次)", str(rb["timeouts"]), str(rl["timeouts"]), "—"))
        if rl["p99"] > max(30.0, 3.0 * rb["p99"]):
            issues.append(f"窗口响应 p99 从 {rb['p99']:.0f}ms 升到 {rl['p99']:.0f}ms")
    else:
        rows.append(("窗口响应", "不可用", "不可用", "—"))
        notes.append("窗口响应不可用：屏幕锁定，或前台是系统/更高权限窗口")

    pb, pl = base["perf"], load["perf"]
    if pb.get("available"):
        rows.append(("CPU 均值/峰值 (%)", f"{fmt(pb['cpu']['mean'], 1)}/{fmt(pb['cpu']['peak'], 1)}",
                     f"{fmt(pl['cpu']['mean'], 1)}/{fmt(pl['cpu']['peak'], 1)}", "—"))
        rows.append(("DPC 均值/峰值 (%)", f"{fmt(pb['dpc']['mean'], 1)}/{fmt(pb['dpc']['peak'], 1)}",
                     f"{fmt(pl['dpc']['mean'], 1)}/{fmt(pl['dpc']['peak'], 1)}", "—"))
        rows.append(("磁盘队列 均值/峰值", f"{fmt(pb['disk_queue']['mean'])}/{fmt(pb['disk_queue']['peak'])}",
                     f"{fmt(pl['disk_queue']['mean'])}/{fmt(pl['disk_queue']['peak'])}", "—"))
        rows.append(("磁盘单次 IO (ms)", fmt(pb["disk_ms"]["mean"]), fmt(pl["disk_ms"]["mean"]),
                     ratio(pb["disk_ms"]["mean"], pl["disk_ms"]["mean"])))
        rows.append(("硬缺页 (页/秒)", fmt(pb["hard_faults"]["mean"], 0),
                     fmt(pl["hard_faults"]["mean"], 0),
                     ratio(pb["hard_faults"]["mean"], pl["hard_faults"]["mean"])))
        rows.append(("可用内存 (MB)", fmt(pb["mem_free_mb"]["mean"], 0),
                     fmt(pl["mem_free_mb"]["mean"], 0), "—"))
        if pl["dpc"]["peak"] > max(5.0, 3.0 * pb["dpc"]["peak"]):
            issues.append(f"DPC 时间峰值 {pl['dpc']['peak']:.1f}%（驱动层可能有问题）")
        if pl["disk_queue"]["peak"] > max(2.0, 3.0 * pb["disk_queue"]["peak"]):
            issues.append(f"磁盘队列峰值 {pl['disk_queue']['peak']:.1f}（IO 在排队）")
        if pl["hard_faults"]["mean"] > max(200.0, 3.0 * pb["hard_faults"]["mean"]):
            issues.append(f"硬缺页从 {pb['hard_faults']['mean']:.0f} 涨到 "
                          f"{pl['hard_faults']['mean']:.0f} 页/秒（内存在换页）")
    else:
        notes.append("性能计数器不可用（PDH 初始化失败）")

    lines = [
        "",
        "=" * 64,
        f"  卡顿 A/B 对比  {title}",
        "=" * 64,
        f"{'指标':<18}{'基线':>18}{'加载后':>18}{'变化':>10}",
        "-" * 64,
    ]
    for name, a, b, c in rows:
        lines.append(f"{name:<18}{a:>18}{b:>18}{c:>10}")
    lines.append("-" * 64)
    for note in notes:
        lines.append(f"  注：{note}")
    if issues:
        lines.append("结论：检测到以下变慢迹象")
        for issue in issues[:6]:
            lines.append(f"  ! {issue}")
    else:
        lines.append("结论：未发现明显变慢。")
    lines.append("=" * 64)
    return "\n".join(lines), issues


def to_markdown(result: dict, issues: list[str]) -> str:
    base, load = result["baseline"], result["loaded"]
    fb, fl = base["frames"], load["frames"]
    jb, jl = base["jitter"], load["jitter"]
    rb, rl = base["response"], load["response"]
    pb, pl = base["perf"], load["perf"]
    md = [
        f"# 卡顿 A/B 报告：{result['target']}",
        "",
        f"- 基线：{result['durations']['baseline']} 秒",
        f"- 加载后：{result['durations']['sample']} 秒",
        f"- 时间：{result['timestamp']}",
        "",
        "| 指标 | 基线 | 加载后 |",
        "| --- | ---: | ---: |",
    ]
    if fb.get("available"):
        md.append(f"| 帧率 (fps) | {fb.get('fps', 0):.1f} | {fl.get('fps', 0):.1f} |")
        md.append(f"| 帧时间 p99 (ms) | {fb['p99']:.2f} | {fl['p99']:.2f} |")
        md.append(f"| 长帧 >50ms | {fb['long_50']} | {fl['long_50']} |")
    md.append(f"| 调度抖动 p99 (ms) | {jb['p99']:.2f} | {jl['p99']:.2f} |")
    md.append(f"| 窗口响应 p99 (ms) | {rb['p99']:.2f} | {rl['p99']:.2f} |")
    if pb.get("available"):
        md.append(f"| CPU 均值/峰值 (%) | {pb['cpu']['mean']:.1f}/{pb['cpu']['peak']:.1f} "
                  f"| {pl['cpu']['mean']:.1f}/{pl['cpu']['peak']:.1f} |")
        md.append(f"| DPC 均值/峰值 (%) | {pb['dpc']['mean']:.1f}/{pb['dpc']['peak']:.1f} "
                  f"| {pl['dpc']['mean']:.1f}/{pl['dpc']['peak']:.1f} |")
        md.append(f"| 磁盘队列 均值/峰值 | {pb['disk_queue']['mean']:.2f}/{pb['disk_queue']['peak']:.2f} "
                  f"| {pl['disk_queue']['mean']:.2f}/{pl['disk_queue']['peak']:.2f} |")
    md.extend(["", "## 结论", ""])
    if issues:
        md.extend([f"- {i}" for i in issues[:8]])
    else:
        md.append("- 未发现明显变慢。")
    return "\n".join(md) + "\n"


# ============================================================ 入口

def launch_target(command: str, args: str | None, cwd: str | None):
    cmd = [command] + (args.split() if args else [])
    try:
        proc = subprocess.Popen(cmd, cwd=cwd or None,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return proc
    except Exception as exc:
        print(f"[!] 启动失败：{exc}")
        return None


def main() -> int:
    parser = argparse.ArgumentParser(description="卡顿 A/B 基线测试（Windows）")
    parser.add_argument("--baseline", type=float, default=20, help="基线采样秒数（默认 20）")
    parser.add_argument("--sample", type=float, default=20, help="加载后采样秒数（默认 20）")
    parser.add_argument("--warmup", type=float, default=3, help="启动后预热秒数（默认 3）")
    parser.add_argument("--run", dest="command", help="要启动并测试的程序")
    parser.add_argument("--args", dest="args", help="传给被测程序的参数")
    parser.add_argument("--cwd", dest="cwd", help="被测程序的工作目录")
    parser.add_argument("--kill", action="store_true", help="测试结束后关闭被测程序")
    parser.add_argument("--motion", action="store_true", help="显示一个持续动画的小窗（帧率指标需要）")
    parser.add_argument("--out", help="输出 Markdown 报告路径")
    parser.add_argument("--json", dest="json_out", help="输出原始统计 JSON 路径")
    opts = parser.parse_args()

    if os.name != "nt":
        print("仅支持 Windows。")
        return 2

    try:
        ctypes.WinDLL("shcore").SetProcessDpiAwareness(2)
    except Exception:
        pass

    print("卡顿 A/B 测试")
    print(f"  基线 {opts.baseline:.0f}s → 加载 {opts.sample:.0f}s（预热 {opts.warmup:.0f}s）")
    if screen_locked():
        print("  [!] 屏幕当前已锁定：帧率、窗口响应这两项测不到，其余指标照常。")
    if opts.command:
        print(f"  被测程序：{opts.command} {opts.args or ''}")
    else:
        print("  手动模式：基线结束后按回车，再去启动要测的软件")

    bench = Bench({"baseline": opts.baseline, "sample": opts.sample}, opts.motion)
    bench.start()
    proc = None
    try:
        base = bench.phase("基线", opts.baseline)

        def start_target():
            nonlocal proc
            if opts.command:
                proc = launch_target(opts.command, opts.args, opts.cwd)
                print(f"  已启动被测程序，预热 {opts.warmup:.0f} 秒…")
                time.sleep(opts.warmup)
            else:
                try:
                    input("  请启动要测试的软件，然后按回车开始采集…")
                except EOFError:
                    print("  非交互环境：等待 5 秒后自动开始采集…")
                    time.sleep(5)
                time.sleep(opts.warmup if opts.warmup > 0 else 0)

        load = bench.phase("加载后", opts.sample, callback=start_target)
    finally:
        bench.stop()
        if proc and opts.kill:
            try:
                proc.terminate()
                print("  已关闭被测程序。")
            except Exception:
                pass

    if not opts.command:
        base["label"] = "基线"

    import datetime
    result = {
        "target": opts.command or "手动模式",
        "timestamp": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "durations": {"baseline": round(base["seconds"], 1), "sample": round(load["seconds"], 1)},
        "baseline": base,
        "loaded": load,
    }
    text, issues = render(base, load, opts.command or "手动模式")
    print(text)

    if opts.out:
        try:
            with open(opts.out, "w", encoding="utf-8") as f:
                f.write(to_markdown(result, issues))
            print(f"Markdown 报告已写入：{opts.out}")
        except Exception as exc:
            print(f"[!] 写报告失败：{exc}")

    if opts.json_out:
        try:
            with open(opts.json_out, "w", encoding="utf-8") as f:
                json.dump(result, f, ensure_ascii=False, indent=2)
            print(f"原始数据已写入：{opts.json_out}")
        except Exception as exc:
            print(f"[!] 写 JSON 失败：{exc}")

    return 0 if not issues else 1


if __name__ == "__main__":
    sys.exit(main())
