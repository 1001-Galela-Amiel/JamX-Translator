from __future__ import annotations

import sys
import time
from typing import Optional, List, Tuple

import numpy as np
from PIL import Image

WINDOWS = sys.platform.startswith("win32")
MAC = sys.platform.startswith("darwin")
if WINDOWS:
    import win32gui
    import win32ui
    import win32con

try:
    if WINDOWS:
        import dxcam
        _HAS_DXCAM = True
    else:
        _HAS_DXCAM = False
except Exception:
    _HAS_DXCAM = False

if MAC:
    from Quartz import (
        CGWindowListCopyWindowInfo,
        CGWindowListCreateImage,
        kCGWindowListOptionOnScreenOnly,
        kCGNullWindowID,
        kCGWindowListOptionIncludingWindow,
        kCGWindowImageDefault,
    )
    import Quartz

_dxcam_cam = None
_dxcam_region = None
_dxcam_last_restart = 0.0

if WINDOWS:
    def _client_rect_on_screen(hwnd: int) -> Tuple[int, int, int, int]:
        """Client rect in screen coordinates."""
        l, t, r, b = win32gui.GetClientRect(hwnd)
        w = max(0, r - l)
        h = max(0, b - t)
        x0, y0 = win32gui.ClientToScreen(hwnd, (0, 0))
        return (x0, y0, x0 + w, y0 + h)

class WindowLister:
    """List visible top-level windows (handle, title)."""
    @staticmethod
    def list_windows():
        """
        Lists visible top level windows that can be targeted by the tool.
        It calls the platform specific enumeration routine and collects window handles and titles into a list.
        """
        if WINDOWS:  
         wins = []
         def enum(hwnd, ctx):
             """
             Processes each window during Win32 enumeration and decides whether to keep it.
             It checks visibility and window text, then appends suitable windows into the context list.
             """
            if win32gui.IsWindowVisible(hwnd) and win32gui.GetWindowText(hwnd):
                wins.append((hwnd, win32gui.GetWindowText(hwnd)))
         win32gui.EnumWindows(enum, None)
         wins.sort(key=lambda x: x[1].lower())
         return wins
#Check if its Mac, then import Mac specific libraries
        if MAC:
            window_info = CGWindowListCopyWindowInfo(
                kCGWindowListOptionOnScreenOnly,
                kCGNullWindowID,
            )
            for w in window_info:
                window_id = w.get("kCGWindowNumber")
                owner = w.get("kCGWindowOwnerName", " ")
                name = w.get("kCGWindowName", " ")
                if window_id and (owner or name):
                    windows.append((window_id, f"{owner} - {name}"))
            return windows

        return windows

if WINDOWS:
    def _ensure_dxcam(region: Tuple[int, int, int, int], target_fps: int = 60) -> None:
        """Start/restart dxcam with a region crop (screen coords)."""
        global _dxcam_cam, _dxcam_region, _dxcam_last_restart
        now = time.time()
        if _dxcam_cam is None:
            _dxcam_cam = dxcam.create(output_idx=0, output_color="BGRA")
            _dxcam_cam.start(target_fps=target_fps, region=region, video_mode=True)
            _dxcam_region = region
            _dxcam_last_restart = now
            return
        if _dxcam_region != region:
            try:
                _dxcam_cam.stop()
            except Exception:
                pass
            _dxcam_cam.start(target_fps=target_fps, region=region, video_mode=True)
            _dxcam_region = region
            _dxcam_last_restart = now

if WINDOWS:
    def _dxgi_capture(hwnd: int) -> Optional[np.ndarray]:
        """Fast path: grab latest frame from dxcam cropped to client rect region."""
        if not _HAS_DXCAM:
            return None
        left, top, right, bottom = _client_rect_on_screen(hwnd)
        if right <= left or bottom <= top:
            return None
        region = (left, top, right, bottom)
        _ensure_dxcam(region, target_fps=60)
        try:
            frame = _dxcam_cam.get_latest_frame()
            return frame
        except Exception:
            return None
if WINDOWS:
    def _gdi_capture(hwnd: int) -> Optional[np.ndarray]:
        """
        Robust fallback: BitBlt the CLIENT area correctly.
        IMPORTANT: GetDC(hwnd) (client DC) => origin (0,0) is client top-left.
        """
        l, t, r, b = win32gui.GetClientRect(hwnd)
        w = max(0, r - l)
        h = max(0, b - t)
        if w == 0 or h == 0:
            return None

        hdc_src = win32gui.GetDC(hwnd)
        if not hdc_src:
            return None

        src_dc = None
        mem_dc = None
        bmp = None
        old_obj = None

        try:
            src_dc = win32ui.CreateDCFromHandle(hdc_src)
            mem_dc = src_dc.CreateCompatibleDC()

            bmp = win32ui.CreateBitmap()
            bmp.CreateCompatibleBitmap(src_dc, w, h)
            old_obj = mem_dc.SelectObject(bmp)

            mem_dc.BitBlt((0, 0), (w, h), src_dc, (0, 0), win32con.SRCCOPY)

            raw = bmp.GetBitmapBits(True)
            img = np.frombuffer(raw, dtype=np.uint8).reshape((h, w, 4))  # BGRA
            return img.copy()
        finally:
            if mem_dc is not None and old_obj is not None:
                try:
                    mem_dc.SelectObject(old_obj)
                except Exception:
                    pass
            if bmp is not None:
                try:
                    win32gui.DeleteObject(bmp.GetHandle())
                except Exception:
                    pass
            if mem_dc is not None:
                try:
                    mem_dc.DeleteDC()
                except Exception:
                    pass
            if src_dc is not None:
                try:
                    src_dc.DeleteDC()
                except Exception:
                    pass
            try:
                win32gui.ReleaseDC(hwnd, hdc_src)
            except Exception:
                pass

def _mac_capture_rgba(hwnd: int) -> Optional[np.ndarray]:
    if not MAC:
        return None
    image_ref = CGWindowListCreateImage(
        Quartz.CGRectNull,
        kCGWindowListOptionIncludingWindow,
        hwnd,
        kCGWindowImageDefault,
    )
    if image_ref is None:
        return None

    width = Quartz.CGImageGetWidth(image_ref)
    height = Quartz.CGImageGetHeight(image_ref)
    pixel_data = Quartz.CGDataProviderCopyData(
        Quartz.CGImageGetDataProvider(image_ref)
    )
    arr = np.frombuffer(pixel_data, dtype=np.uint8)
    if arr.size < width * height * 4:
        return None
    arr = arr[: width * height * 4]
    return arr.reshape((height, width, 4))

def capture_window_bgra(hwnd: int) -> Optional[np.ndarray]:
    """Return BGRA frame as numpy array."""
    if WINDOWS:
        frame = _dxgi_capture(hwnd)
        if frame is not None:
            return frame
        return _gdi_capture(hwnd)

    if MAC:
        rgba = _mac_capture_rgba(hwnd)
        if rgba is None:
            return None
        r = rgba[..., 0]
        g = rgba[..., 1]
        b = rgba[..., 2]
        a = rgba[..., 3]
        return np.dstack([b, g, r, a]).astype(np.uint8)

    return None

def capture_window_image(hwnd: int) -> Optional[Image.Image]:
    """Compatibility API: Return PIL Image (RGB)."""
    frame = capture_window_bgra(hwnd)
    if frame is None:
        return None
    # BGRA -> RGB
    b = frame[..., 0]
    g = frame[..., 1]
    r = frame[..., 2]
    rgb = np.dstack([r, g, b]).astype(np.uint8)
    return Image.fromarray(rgb, mode="RGB")
