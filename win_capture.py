from PIL import ImageGrab, Image
import sys
import numpy as np

WINDOWS = sys.platform.startswith("win32")
MAC = sys.platform.startswith("darwin")

if WINDOWS:
    import win32gui

if MAC:
    import Quartz

class WindowLister:
    @staticmethod
    def list_windows():
        if WINDOWS:
            wins = []

            def enum(hwnd, ctx):
                if win32gui.IsWindowVisible(hwnd) and win32gui.GetWindowText(hwnd):
                    wins.append((hwnd, win32gui.GetWindowText(hwnd)))
            win32gui.EnumWindows(enum, None)
            return wins

        if MAC:
            window_info = Quartz.CGWindowListCopyWindowInfo(
                Quartz.kCGWindowListOptionOnScreenOnly,
                Quartz.kCGNullWindowID
            )
            out = []
            for w in window_info:
                window_id = w.get("kCGWindowNumber")
                owner = w.get("kCGWindowOwnerName", " ")
                name = w.get("kCGWindowName", " ")
                if window_id and (owner or name):
                    out.append((window_id, f"{owner} - {name}"))
            return out
        return []

def get_window_rect(hwnd):
    if WINDOWS:
        try:
            rect = win32gui.GetClientRect(hwnd)
            lt = win32gui.ClientToScreen(hwnd, (rect[0], rect[1]))
            rb = win32gui.ClientToScreen(hwnd, (rect[2], rect[3]))
            left, top = lt
            right, bottom = rb
            return int(left), int(top), int(right), int(bottom)
        except Exception:
            try:
                left, top, right, bottom = win32gui.GetWindowRect(hwnd)
                return int(left), int(top), int(right), int(bottom)
            except Exception:
                return None
    return None

def capture_window_image(hwnd):
    if WINDOWS:
        coords = get_window_rect(hwnd)
        if not coords:
            return None
        left, top, right, bottom = coords
        img = ImageGrab.grab(bbox=(left, top, right, bottom))
        return img.convert("RGB")

    if MAC:
        from mac_capture import capture_window_image
        return capture_window_image(hwnd)

    return None