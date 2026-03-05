from __future__ import annotations
import sys
from typing import Optional, List, Tuple
import numpy as np
from PIL import Image

WINDOWS = sys.platform.startswith("win32")
MAC = sys.platform.startswith("darwin")

# ------------------ Windows imports ------------------
if WINDOWS:
    import win32gui
    import win32ui
    import win32con
    import dxcam
    from PIL import ImageGrab

# ------------------ macOS imports ------------------
if MAC:
    import Quartz

# ------------------ Windows helpers ------------------
if WINDOWS:
    def _client_rect_on_screen(hwnd: int) -> Tuple[int, int, int, int]:
        l, t, r, b = win32gui.GetClientRect(hwnd)
        w, h = max(0, r - l), max(0, b - t)
        x0, y0 = win32gui.ClientToScreen(hwnd, (0, 0))
        return (x0, y0, x0 + w, y0 + h)

    _dxcam_cam = None
    _dxcam_region = None

    def _ensure_dxcam(region: Tuple[int, int, int, int], target_fps: int = 60):
        global _dxcam_cam, _dxcam_region
        if _dxcam_cam is None:
            _dxcam_cam = dxcam.create(output_idx=0, output_color="BGRA")
            _dxcam_cam.start(target_fps=target_fps, region=region, video_mode=True)
            _dxcam_region = region
            return
        if _dxcam_region != region:
            try:
                _dxcam_cam.stop()
            except Exception:
                pass
            _dxcam_cam.start(target_fps=target_fps, region=region, video_mode=True)
            _dxcam_region = region

    def _dxgi_capture(hwnd: int) -> Optional[np.ndarray]:
        left, top, right, bottom = _client_rect_on_screen(hwnd)
        if right <= left or bottom <= top:
            return None
        _ensure_dxcam((left, top, right, bottom))
        try:
            return _dxcam_cam.get_latest_frame()
        except Exception:
            return None

    def _gdi_capture(hwnd: int) -> Optional[np.ndarray]:
        l, t, r, b = win32gui.GetClientRect(hwnd)
        w, h = max(0, r - l), max(0, b - t)
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
<<<<<<< HEAD
            old_obj = mem_dc.SelectObject(bmp)

=======
            mem_dc.SelectObject(bmp)
>>>>>>> 004f9d8 (got rid of frida, edited the subtitle display and overlay, and website)
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

# ------------------ Window Lister ------------------
class WindowLister:
    @staticmethod
    def list_windows() -> List[Tuple[int, str]]:
        windows: List[Tuple[int, str]] = []

        if WINDOWS:
            def enum_cb(h, _):
                if not win32gui.IsWindowVisible(h):
                    return
                title = win32gui.GetWindowText(h) or ""
                if title.strip() and title not in ("Program Manager",):
                    windows.append((h, title))
            win32gui.EnumWindows(enum_cb, None)
            return windows

        if MAC:
            window_info = Quartz.CGWindowListCopyWindowInfo(
                Quartz.kCGWindowListOptionOnScreenOnly, Quartz.kCGNullWindowID
            )
            for w in window_info:
                window_id = w.get("kCGWindowNumber")
                owner = w.get("kCGWindowOwnerName", " ")
                name = w.get("kCGWindowName", " ")
                if window_id and (owner or name):
                    windows.append((window_id, f"{owner} - {name}"))
            return windows

        return []

# ------------------ Capture functions ------------------
def capture_window_bgra(hwnd: int) -> Optional[np.ndarray]:
    """Return BGRA frame as numpy array."""
    if WINDOWS:
        frame = _dxgi_capture(hwnd)
        if frame is not None:
            return frame
        return _gdi_capture(hwnd)

    if MAC:
        from mac_capture import capture_window_image  # use new Mac live capture
        img = capture_window_image(hwnd)
        if img is None:
            return None
        arr = np.array(img)
        if arr.shape[2] == 3:
            b, g, r = arr[:, :, 0], arr[:, :, 1], arr[:, :, 2]
            a = np.full_like(b, 255)
            arr = np.dstack([b, g, r, a])
        elif arr.shape[2] == 4:
            b, g, r, a = arr[:, :, 0], arr[:, :, 1], arr[:, :, 2], arr[:, :, 3]
            arr = np.dstack([b, g, r, a])
        return arr.astype(np.uint8)

    return None

def capture_window_image(hwnd: int) -> Optional[Image.Image]:
    """Return a PIL Image (RGB)."""
    frame = capture_window_bgra(hwnd)
    if frame is None:
        return None
    b, g, r = frame[:, :, 0], frame[:, :, 1], frame[:, :, 2]
    rgb = np.dstack([r, g, b]).astype(np.uint8)
    return Image.fromarray(rgb, mode="RGB")