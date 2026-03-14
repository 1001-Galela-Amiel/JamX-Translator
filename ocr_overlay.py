from PIL import ImageGrab, Image
import cv2
import numpy as np
import win32gui, win32ui, win32con
import ctypes
from PySide6 import QtWidgets, QtCore, QtGui
import pytesseract


class WindowLister:
    @staticmethod
    def list_windows():
        """
        Enumerates visible windows so the overlay can choose a game window to follow.
        It uses Win32 window enumeration, filters out irrelevant entries, and collects candidate window handles and titles.
        """
        wins = []
        def enum(hwnd, ctx):
            """
            Processes each enumerated window and decides whether to include it in the overlay list.
            It checks visibility and window title text and appends the matching windows to the shared context.
            """
            if win32gui.IsWindowVisible(hwnd) and win32gui.GetWindowText(hwnd):
                wins.append((hwnd, win32gui.GetWindowText(hwnd)))
        win32gui.EnumWindows(enum, None)
        return wins

def get_window_rect(hwnd):
    """
    Computes the screen rectangle of the chosen game window for overlay alignment.
    It queries the window position and size through the system API and returns them in a convenient tuple form.
    """
    try:
        rect = win32gui.GetClientRect(hwnd)
        left_top = win32gui.ClientToScreen(hwnd, (rect[0], rect[1]))
        right_bottom = win32gui.ClientToScreen(hwnd, (rect[2], rect[3]))
        left, top = left_top
        right, bottom = right_bottom
        return int(left), int(top), int(right), int(bottom)
    except Exception:
        try:
            rect = win32gui.GetWindowRect(hwnd)
            left, top, right, bottom = rect
            return int(left), int(top), int(right), int(bottom)
        except Exception:
            return None

def capture_window_image(hwnd):
    """
    Captures the current frame of the game window for OCR purposes.
    It uses BitBlt to copy the window pixels into a bitmap, converts the raw buffer into a NumPy array, and wraps it as a PIL image.
    """
    coords = get_window_rect(hwnd)
    if not coords:
        return None
    left, top, right, bottom = coords
    try:
        img = ImageGrab.grab(bbox=(left, top, right, bottom))
    except Exception:
        img = ImageGrab.grab()
    return img.convert("RGB")

def ocr_image_data(pil_image):
    """
    Applies OCR to an image using OpenCV and RapidOCR to detect text regions.
    It preprocesses the image, forwards it to the OCR engine, and converts the resulting polygons into bounding boxes with associated text strings.
    """
    img = np.array(pil_image)
    gray = cv2.cvtColor(img, cv2.COLOR_RGB2GRAY)
    
    try:
        th = cv2.adaptiveThreshold(gray,255,cv2.ADAPTIVE_THRESH_GAUSSIAN_C,cv2.THRESH_BINARY,11,2)
    except Exception:
        th = gray
    data = pytesseract.image_to_data(Image.fromarray(img), output_type=pytesseract.Output.DICT, lang='eng+jpn+chi_sim')
    results = []
    n = len(data['text'])
    for i in range(n):
        text = data['text'][i].strip()
        if not text:
            continue
        x, y, w, h = int(data['left'][i]), int(data['top'][i]), int(data['width'][i]), int(data['height'][i])
        results.append({'text': text, 'bbox': (x,y,w,h), 'lang': 'unknown'})
    return results

class OverlayWindow(QtWidgets.QWidget):
    def __init__(self, ocr_results, parent=None, offset=(0,0)):
        super().__init__(parent, QtCore.Qt.Window)
        self.setWindowFlags(QtCore.Qt.FramelessWindowHint | QtCore.Qt.WindowStaysOnTopHint | QtCore.Qt.Tool)
        self.setAttribute(QtCore.Qt.WA_TranslucentBackground)
        self.labels = []
        self.ocr_results = ocr_results
        self.offset = offset
        self.build_ui()

    def build_ui(self):
        desktop = QtWidgets.QApplication.primaryScreen().geometry()
        self.setGeometry(desktop)
        for r in self.ocr_results:
            txt = r.get('translation') or r.get('text')
            x,y,w,h = r['bbox']
            x += self.offset[0]; y += self.offset[1]
            lbl = QtWidgets.QLabel(self)
            lbl.setText(txt)
            lbl.setWordWrap(True)
            lbl.setAttribute(QtCore.Qt.WA_TransparentForMouseEvents)
            font = QtGui.QFont("Segoe UI", max(10, int(h*0.6)))
            lbl.setFont(font)
            lbl.move(x, y)
            lbl.resize(max(50,w), max(20,h))
            lbl.setStyleSheet("color: yellow; background-color: rgba(0,0,0,120);")
            self.labels.append(lbl)

    def update_results(self, ocr_results, offset=(0,0)):
        # clear old labels
        for lbl in self.labels:
            lbl.deleteLater()
        self.labels = []
        self.ocr_results = ocr_results
        self.offset = offset
        self.build_ui()