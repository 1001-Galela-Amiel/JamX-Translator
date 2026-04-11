"""
Main application for the GameTranslationTool with OCR and injection UI.

This module provides a Qt GUI that allows the user to attach to a
running visual novel window, perform OCR on the live game screen,
translate the extracted text, and display the results. It also
provides an injection tab that can be backed by an external hook.
"""

from __future__ import annotations

import os
import sys
import json
import time
from difflib import SequenceMatcher
from typing import List, Dict, Any, Optional
from pynput import keyboard
from image_preprocessor import removeBackground

from PySide6 import QtWidgets, QtCore, QtGui

from capture import WindowLister, capture_window_image, capture_window_bgra
from ocr_backend import ocr_image_data
from translate_backend import LANG_MAP
from translation_worker import Translator
from snipper import Snipper

try:
    from luna_worker import LunaHookWorker
except Exception:
    LunaHookWorker = None


APP_DIR = os.path.dirname(__file__)
TRANSLATION_FILE = os.path.join(APP_DIR, "translations.json")
LOG_DIR = os.path.join(APP_DIR, "logs")
os.makedirs(LOG_DIR, exist_ok=True)

HOOK_CODEPAGE_MAP = {
    "ja": 932,
    "zh-cn": 936,
    "zh-tw": 950,
    "zh": 936,
    "ko": 949,
    "ru": 1251,
    "en": 1252,
}


class CaptureWorker(QtCore.QThread):
    """Background thread that captures frames continuously."""

    frame_ready = QtCore.Signal(object)

    def __init__(
        self,
        hwnd: int,
        interval_ms: int = 120,
        parent: Optional[QtCore.QObject] = None,
    ) -> None:
        super().__init__(parent)
        self.hwnd = hwnd
        self.interval = max(5, int(interval_ms)) / 1000.0
        self._running = False

    def run(self) -> None:
        self._running = True
        while self._running:
            start = time.time()
            bgra = capture_window_bgra(self.hwnd)
            if bgra is not None:
                self.frame_ready.emit(bgra)
            dt = time.time() - start
            sleep_t = self.interval - dt
            if sleep_t > 0:
                time.sleep(sleep_t)

    def stop(self) -> None:
        self._running = False
        self.wait(2000)


class OCRWorker(QtCore.QThread):
    """Background thread that periodically captures image and runs OCR."""

    ocr_ready = QtCore.Signal(list)

    def __init__(
        self,
        hwnd: int,
        ocr_every_ms: int = 1200,
        prefer_lang: str = "auto",
        parent: Optional[QtCore.QObject] = None,
    ) -> None:
        super().__init__(parent)
        self.hwnd = hwnd
        self.ocr_interval = max(100, int(ocr_every_ms)) / 1000.0
        self.prefer_lang = prefer_lang
        self._running = False
        self._last_error_message: str = ""
        self._last_error_ts: float = 0.0
        self.enable_preprocessing = False

    def run(self) -> None:
        self._running = True
        while self._running:
            start = time.time()
            try:
                pil_img = capture_window_image(self.hwnd)
                if pil_img is not None:
                    data = ocr_image_data(pil_img, self.prefer_lang, self.enable_preprocessing)
                    self.ocr_ready.emit(data)
            except Exception as e:
                msg = str(e)
                now = time.time()
                if msg != self._last_error_message or (now - self._last_error_ts) >= 2.0:
                    print("[OCR ERROR]", e)
                    self._last_error_message = msg
                    self._last_error_ts = now
                if "CreateCompatibleDC failed" in msg:
                    time.sleep(0.2)

            dt = time.time() - start
            sleep_t = self.ocr_interval - dt
            if sleep_t > 0:
                time.sleep(sleep_t)

    def stop(self) -> None:
        self._running = False
        self.wait(2000)


class ShortcutWorker(QtCore.QObject):
    """Background thread for detecting keyboard presses for shortcut key purposes"""
    pressed = QtCore.Signal()

    def run(self):
        def on_press(key):
            if key == keyboard.Key.f1:
                self.pressed.emit()

        with keyboard.Listener(on_press=on_press) as listener:
            listener.join()

class PreviewWidget(QtWidgets.QWidget):
    """Renders the captured game frame with translated overlays."""
    
    def __init__(self, parent: Optional[QtWidgets.QWidget] = None) -> None:
            super().__init__(parent)
            self.qimage: Optional[QtGui.QImage] = None
            self.overlay_entries: List[Dict[str, Any]] = []
            self.selected_bbox: Optional[tuple[int, int, int, int]] = None
            self.text_overlay_color = QtGui.QColor(255, 255, 0)
            self.setMinimumSize(480, 270)
            self.setStyleSheet("background-color: #202225; border-radius: 8px;")

    def sizeHint(self) -> QtCore.QSize:
        return QtCore.QSize(640, 360)
    
    def update_frame(self, frame_bgra) -> None:
            if frame_bgra is None:
                return
            try:
                import numpy as np
                if not isinstance(frame_bgra, np.ndarray) or frame_bgra.ndim != 3 or frame_bgra.shape[2] != 4:
                    try:
                        pil_img = frame_bgra.convert("RGBA")
                        w, h = pil_img.size
                        buf = pil_img.tobytes("raw", "BGRA")
                        self._qimage_buf = buf
                        self.qimage = QtGui.QImage(self._qimage_buf, w, h, 4 * w, QtGui.QImage.Format.Format_ARGB32)
                        self.update()
                        return
                    except Exception:
                        return

                h, w, _ = frame_bgra.shape
                if not frame_bgra.flags["C_CONTIGUOUS"]:
                    frame_bgra = np.ascontiguousarray(frame_bgra)
                if frame_bgra.shape[2] == 4:
                    amax = int(frame_bgra[..., 3].max())
                    if amax == 0:
                        frame_bgra = frame_bgra.copy()
                        frame_bgra[..., 3] = 255
                self._qimage_buf = frame_bgra.tobytes()
                self.qimage = QtGui.QImage(self._qimage_buf, w, h, 4 * w, QtGui.QImage.Format.Format_ARGB32)
                self.update()
            except Exception:
                return

    def update_overlay(self, entries: List[Dict[str, Any]]) -> None:
        self.overlay_entries = entries or []
        self.update()

    def setTextColor(self, color: QtGui.QColor) -> None:
        self.text_overlay_color = color
        self.update()

    def set_selected_bbox(self, bbox: Optional[tuple[int, int, int, int]]) -> None:
        self.selected_bbox = bbox
        self.update()


    def reset_view(self) -> None:
        self.qimage = None
        self.overlay_entries = []
        self.selected_bbox = None
        self.update()


    def paintEvent(self, event: QtGui.QPaintEvent) -> None:
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.Antialiasing, True)

        tgt_rect = self.rect()

        # Background Reset color
        painter.fillRect(tgt_rect, QtGui.QColor(32, 34, 37))

        #Capture frame and draw
        if self.qimage is not None and not self.qimage.isNull():
            src_w, src_h = self.qimage.width(), self.qimage.height()
            scale = min(tgt_rect.width() / src_w, tgt_rect.height() / src_h)
            draw_w, draw_h = int(src_w * scale), int(src_h * scale)
            offset_x, offset_y = (tgt_rect.width() - draw_w) // 2, (tgt_rect.height() - draw_h) // 2
            draw_rect = QtCore.QRect(offset_x, offset_y, draw_w, draw_h)
            painter.drawImage(draw_rect, self.qimage, self.qimage.rect())
        else:
            draw_rect = tgt_rect

        if not self.overlay_entries:
            painter.end()
            return

        #Merge lines together, in order to avoid too many small text boxes
        lines = []
        y_threshold = 15
        for e in sorted(self.overlay_entries, key=lambda x: x.get('bbox', (0,0,0,0))[1]):
            text = e.get('translation') or e.get('text')
            if not text:
                continue
            x, y, w, h = e.get('bbox', (0,0,0,0))
            placed = False
            for line in lines:
                if abs(y - line['y']) <= y_threshold:
                    line['text'] += " " + text
                    line['y'] = min(line['y'], y)
                    line['h'] = max(line['h'], y + h - line['y'])
                    placed = True
                    break
            if not placed:
                lines.append({'text': text, 'y': y, 'h': h})

       #Subtitle overlay position
        if self.qimage is not None and not self.qimage.isNull():
            painter.setFont(QtGui.QFont("Helvetica", 14))
            metrics = QtGui.QFontMetrics(painter.font())
            line_height = metrics.lineSpacing()

            # Find bottom of all OCR boxes
            bottom_y = max([line['y'] + line['h'] for line in lines])
            vertical_padding = 100  

            overlay_x = draw_rect.left() + 10
            overlay_y = draw_rect.top() + int(bottom_y * scale) + vertical_padding

            overlay_width = int(draw_rect.width() * 0.8)
            overlay_height = line_height * len(lines) + 8

            # Ensure the overlay doesn't go beyond the bottom of the widget
            if overlay_y + overlay_height > draw_rect.bottom():
                overlay_y = draw_rect.bottom() - overlay_height - 5

            #Semi-transparent background for text
            painter.setPen(QtCore.Qt.NoPen)
            painter.setBrush(QtGui.QColor(0, 0, 0, 180))
            painter.drawRect(overlay_x - 4, overlay_y - 4, overlay_width + 8, overlay_height + 8)

            #Text overlay/lines
            painter.setPen(self.text_overlay_color)
            for i, line in enumerate(lines):
                painter.drawText(
                    QtCore.QRect(overlay_x, overlay_y + i * line_height, overlay_width, line_height),
                    QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter,
                    line['text']
                )

        painter.end()


class WindowComboBox(QtWidgets.QComboBox):
    """Combo box that emits a signal right before the popup is shown."""

    popup_about_to_show = QtCore.Signal()

    def showPopup(self) -> None:
        self.popup_about_to_show.emit()
        super().showPopup()


class MainWindow(QtWidgets.QWidget):
    """Main application window."""

    translate_signal = QtCore.Signal(str, str, str)
    display_signal = QtCore.Signal(str)
    def __init__(self, display_window: 'DisplayWindow') -> None:
        super().__init__()
        self.setWindowTitle("Game Translation Tool")
        self.resize(1200, 700)
        self.display_window = display_window
        self.translate_signal.connect(self.translate_and_update)

        self.translator = Translator()
        self.translator.translation_ready.connect(self.on_translation_ready)
        self.translation_cache: Dict[str, str] = {}
        self.pending_translation_keys: set[str] = set()

        self.worker: Optional[CaptureWorker] = None
        self.ocr_worker: Optional[OCRWorker] = None
        self.hook_worker: Optional[QtCore.QThread] = None
        self.attached_hwnd: Optional[int] = None
        self.ocr_results: List[Dict[str, Any]] = []
        self.latest_ocr: List[Dict[str, Any]] = []
        self._active_text_signature: Optional[tuple[str, ...]] = None
        self._last_text_switch_ts: float = 0.0
        self._text_switch_lock_ms: int = 220
        self._text_similarity_threshold: float = 0.30
        self.selected_bbox: Optional[tuple[int, int, int, int]] = None
        
        root = QtWidgets.QHBoxLayout(self)
        left_col = QtWidgets.QVBoxLayout()
        right_col = QtWidgets.QVBoxLayout()
        root.addLayout(left_col, 1)
        root.addLayout(right_col, 1)

        bar = QtWidgets.QHBoxLayout()
        self.win_list = WindowComboBox()
        self.attach_btn = QtWidgets.QPushButton("Attach")
        self.detach_btn = QtWidgets.QPushButton("Detach")
        bar.addWidget(self.win_list)
        bar.addWidget(self.attach_btn)
        bar.addWidget(self.detach_btn)
        left_col.addLayout(bar)

        self.tabs = QtWidgets.QTabWidget()
        self.ocr_tab = QtWidgets.QWidget()
        self.inj_tab = QtWidgets.QWidget()
        self.tabs.addTab(self.ocr_tab, "OCR")
        self.tabs.addTab(self.inj_tab, "Injection")
        left_col.addWidget(self.tabs, 1)

        ocr_layout = QtWidgets.QVBoxLayout(self.ocr_tab)
        self.preview = PreviewWidget()
        ocr_layout.addWidget(self.preview, 1)

        ctrl = QtWidgets.QHBoxLayout()
        self.interval_spin = QtWidgets.QSpinBox()
        self.interval_spin.setRange(5, 2000)
        self.interval_spin.setValue(50)
        self.ocr_spin = QtWidgets.QSpinBox()
        self.ocr_spin.setRange(100, 5000)
        self.ocr_spin.setValue(300)

        self.manual_ocr_button = QtWidgets.QPushButton("Manual OCR")
        self.manual_ocr_button.clicked.connect(self.start_snip)
        self.preprocessing_settings_button = QtWidgets.QPushButton("Preprocessing Settings")
        self.preprocessing_settings_button.clicked.connect(self.open_preprocessing_settings)
        self.enable_preprocessing_checkbox = QtWidgets.QCheckBox("Enable preprocessing")
        self.enable_preprocessing_checkbox.stateChanged.connect(self.preprocessing_enable)
        
        ctrl.addWidget(QtWidgets.QLabel("Frame (ms)"))
        ctrl.addWidget(self.interval_spin)
        ctrl.addSpacing(20)
        ctrl.addWidget(QtWidgets.QLabel("OCR (ms)"))
        ctrl.addWidget(self.ocr_spin)
        ctrl.addStretch(1.5)
        ctrl.addWidget(self.enable_preprocessing_checkbox)
        ctrl.addWidget(self.preprocessing_settings_button)
        ctrl.addWidget(self.manual_ocr_button)

        snip_shortcut = QtGui.QShortcut(QtGui.QKeySequence("F1"), self)
        snip_shortcut.activated.connect(self.start_snip)
        
        ocr_layout.addLayout(ctrl)

        inj_layout = QtWidgets.QVBoxLayout(self.inj_tab)
        self.inject_log = QtWidgets.QPlainTextEdit()
        self.inject_log.setReadOnly(True)
        self.inject_log.setPlaceholderText(
            "Injection log. When attached, hooked text and translations will appear here."
        ) 
        mono_font = QtGui.QFontDatabase.systemFont(QtGui.QFontDatabase.FixedFont)
        self.inject_log.setFont(mono_font)
        inj_layout.addWidget(self.inject_log, 1)

        self.status = QtWidgets.QLabel("Ready.")
        left_col.addWidget(self.status)

        lang_row = QtWidgets.QHBoxLayout()
        self.src_combo = QtWidgets.QComboBox()
        self.dst_combo = QtWidgets.QComboBox()
        self.src_combo.addItem("Auto", userData="auto")
        for code, label in LANG_MAP.items():
            self.src_combo.addItem(label, userData=code)
        for code, label in LANG_MAP.items():
            self.dst_combo.addItem(label, userData=code)
        src_index = self.src_combo.findData("auto")
        if src_index >= 0:
            self.src_combo.setCurrentIndex(src_index)
        dst_index = self.dst_combo.findData("en")
        if dst_index >= 0:
            self.dst_combo.setCurrentIndex(dst_index)
        lang_row.addWidget(QtWidgets.QLabel("From"))
        lang_row.addWidget(self.src_combo)
        lang_row.addWidget(QtWidgets.QLabel("To"))
        lang_row.addWidget(self.dst_combo)
        self.text_color_btn = QtWidgets.QPushButton("Text Color")
        lang_row.addWidget(self.text_color_btn)
        right_col.addLayout(lang_row)

        self.table = QtWidgets.QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels([
            "Source Text",
            "Translate",
            "Translation",
            "BBox/Source",
        ])
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QtWidgets.QHeaderView.Stretch)
        header.setSectionResizeMode(1, QtWidgets.QHeaderView.ResizeToContents)
        header.setSectionResizeMode(2, QtWidgets.QHeaderView.Stretch)
        header.setSectionResizeMode(3, QtWidgets.QHeaderView.ResizeToContents)
        self.table.cellClicked.connect(self.on_row_selected)
        self.table.itemSelectionChanged.connect(self.on_select)
        right_col.addWidget(self.table, 1)

        self.edit = QtWidgets.QPlainTextEdit()
        right_col.addWidget(self.edit)

        btn_row = QtWidgets.QHBoxLayout()
        self.apply_btn = QtWidgets.QPushButton("Apply")
        self.save_btn = QtWidgets.QPushButton("Save")
        self.help_btn = QtWidgets.QPushButton("Help")
        btn_row.addWidget(self.apply_btn)
        btn_row.addWidget(self.save_btn)
        btn_row.addWidget(self.help_btn)
        right_col.addLayout(btn_row)

        self.win_list.popup_about_to_show.connect(self.refresh_windows)
        self.attach_btn.clicked.connect(self.attach_window)
        self.detach_btn.clicked.connect(self.detach_window)
        self.interval_spin.valueChanged.connect(self.on_interval_changed)
        self.ocr_spin.valueChanged.connect(self.on_interval_changed)
        self.apply_btn.clicked.connect(self.apply_translation)
        self.save_btn.clicked.connect(self.save_translations)
        self.help_btn.clicked.connect(self.show_help)
        self.text_color_btn.clicked.connect(self.choose_text_overlay_color)
        self.src_combo.currentIndexChanged.connect(self.on_src_lang_changed)

        self.table.itemChanged.connect(self.display_window_update)

        self.shortcut_thread = QtCore.QThread()
        self.shortcut_worker = ShortcutWorker()
        self.shortcut_worker.moveToThread(self.shortcut_thread)
        self.shortcut_worker.pressed.connect(self.start_snip)
        self.shortcut_thread.started.connect(self.shortcut_worker.run)
        self.shortcut_thread.start()

        self.refresh_windows()

    # ---------------------- Window and attach logic ----------------------
    def refresh_windows(self) -> None:
        previous_hwnd = self.current_hwnd()
        if previous_hwnd is None:
            previous_hwnd = self.attached_hwnd

        self.win_list.clear()
        try:
            wins = WindowLister.list_windows()
        except Exception as e:
            self.status.setText(f"Failed to list windows: {e}")
            return

        selected_index = -1
        for hwnd, title in wins:
            if not title:
                continue
            self.win_list.addItem(title, userData=hwnd)
            if previous_hwnd is not None and hwnd == previous_hwnd:
                selected_index = self.win_list.count() - 1

        if selected_index >= 0:
            self.win_list.setCurrentIndex(selected_index)
        self.status.setText(f"Found {self.win_list.count()} windows.")

    def current_hwnd(self) -> Optional[int]:
        idx = self.win_list.currentIndex()
        if idx < 0:
            return None
        return self.win_list.currentData()

    def attach_window(self) -> None:
        hwnd = self.current_hwnd()
        if hwnd is None:
            self.status.setText("No window selected.")
            return
        self.attached_hwnd = hwnd
        current_title = self.win_list.currentText()
        if self.tabs.currentIndex() == 0:
            self.start_capture()
            self.stop_hook()
            self.status.setText(f"Attached OCR to window: {current_title}")
        else:
            self.start_hook()
            self.stop_capture()
            self.status.setText(f"Attached hook to window: {current_title}")

    def detach_window(self) -> None:
        self.stop_capture()
        self.stop_hook()
        self.attached_hwnd = None
        self.reset_ui_on_detach()
        self.status.setText("Detached from window.")

    def reset_ui_on_detach(self) -> None:
        self.latest_ocr = []
        self.ocr_results = []
        self._active_text_signature = None
        self._last_text_switch_ts = 0.0
        self.pending_translation_keys.clear()
        self.selected_bbox = None
        self.preview.reset_view()
        self.table.setRowCount(0)
        self.edit.clear()
        self.inject_log.clear()
        self.tabs.setCurrentIndex(0)

    # ---------------------- Capture / OCR handling ----------------------
    def start_capture(self) -> None:
        if not self.attached_hwnd:
            return
        self.stop_capture()
        self.worker = CaptureWorker(
            self.attached_hwnd,
            interval_ms=self.interval_spin.value(),
        )
        self.worker.frame_ready.connect(self.on_frame_ready)
        self.worker.start()

        self.ocr_worker = OCRWorker(
            self.attached_hwnd,
            ocr_every_ms=self.ocr_spin.value(),
            prefer_lang=self.src_combo.currentData(),
        )
        self.ocr_worker.ocr_ready.connect(self.on_ocr_ready)
        # Enable preprocessing if related checkbox ticked
        self.ocr_worker.enable_preprocessing = self.enable_preprocessing_checkbox.isChecked()
        self.ocr_worker.start()

    def stop_capture(self) -> None:
        if self.worker:
            try:
                self.worker.stop()
            except Exception:
                pass
            self.worker = None
        if self.ocr_worker:
            try:
                self.ocr_worker.stop()
            except Exception:
                pass
            self.ocr_worker = None

    def on_frame_ready(self, frame_bgra) -> None:
        overlay = []
        src_lang = self.src_combo.currentData()
        dst_lang = self.dst_combo.currentData()
        for e in self.latest_ocr:
            txt = e.get("text") or ""
            key = f"{src_lang}|{dst_lang}|{txt}"
            trans = self.translation_cache.get(key)
            if txt.strip() and key not in self.translation_cache and key not in self.pending_translation_keys:
                self.translate_signal.emit(src_lang, dst_lang, txt)
            if trans:
                overlay.append({"text": txt, "bbox": e.get("bbox"), "translation": trans})
        self.preview.update_overlay(overlay)
        self.preview.update_frame(frame_bgra)

    def on_ocr_ready(self, entries: List[Dict[str, Any]]) -> None:
        normalized_entries: List[Dict[str, Any]] = []
        for e in entries:
            txt = (e.get("text") or "").strip()
            if not txt:
                continue
            normalized_entries.append({
                "text": txt,
                "bbox": e.get("bbox"),
                "lang": e.get("lang", "unknown"),
            })

        text_signature = tuple(e["text"] for e in normalized_entries)

        if text_signature == self._active_text_signature:
            return

        if self._active_text_signature is not None:
            current_text = "\n".join(self._active_text_signature)
            incoming_text = "\n".join(text_signature)
            similarity = SequenceMatcher(None, current_text, incoming_text).ratio()
            if similarity >= self._text_similarity_threshold:
                return

        now = time.time()
        if self._active_text_signature is not None:
            elapsed_ms = (now - self._last_text_switch_ts) * 1000.0
            if elapsed_ms < self._text_switch_lock_ms:
                return

        if not normalized_entries:
            return

        self._active_text_signature = text_signature
        self._last_text_switch_ts = now
        self.latest_ocr = normalized_entries

        self.ocr_results = []
        self.table.setRowCount(0)

        src_lang = self.src_combo.currentData()
        dst_lang = self.dst_combo.currentData()

        for row, e in enumerate(normalized_entries):
            src_text = e.get("text", "")
            self.table.insertRow(row)
            self.table.setItem(row, 0, QtWidgets.QTableWidgetItem(src_text))

            # Translate Button
            btn = QtWidgets.QPushButton("Translate")
            btn.clicked.connect(lambda checked=False, r=row: self.manual_translate_row(r))
            self.table.setCellWidget(row, 1, btn)

            self.table.setItem(row, 2, QtWidgets.QTableWidgetItem(""))  # Translation column
            bbox_str = str(e.get("bbox", ""))
            self.table.setItem(row, 3, QtWidgets.QTableWidgetItem(bbox_str))

            # Automatically request translation
            key = f"{src_lang}|{dst_lang}|{src_text}"
            if src_text.strip() and key not in self.translation_cache and key not in self.pending_translation_keys:
                self.pending_translation_keys.add(key)
                self.translator.translate_async(
                    src_lang,
                    dst_lang,
                    src_text,
                    tag={"type": "auto", "row": row}
                )

            self.ocr_results.append({
                "text": src_text,
                "bbox": e.get("bbox"),
                "lang": e.get("lang", "unknown"),
                "translation": "",
            })
    
    def start_snip(self):
        self.snipper= Snipper()
        self.snipper.image_captured.connect(self.on_snip)
        self.snipper.show()

    def on_snip(self, img):

        try:
            data, processed_img = ocr_image_data(img, self.src_combo.currentData(), self.enable_preprocessing_checkbox.isChecked())
            self.on_ocr_ready(data)
        # Case that OCR returns no data, but still want to see the processed image with current settings
        except:
            import numpy as np
            from PIL import Image
            temp_img = np.array(img)
            processed_img = removeBackground(temp_img)
            processed_img = Image.fromarray(processed_img)

        self.image_window = ImageWindow(img, processed_img)
        self.image_window.show()
    
    # Function for opening preprocessing settings for automatic OCR capture via debug_frame
    def open_preprocessing_settings(self):
        import cv2
        from PIL import Image
        temp_img = cv2.imread("logs/debug_frame.png")
        processed_img = removeBackground(temp_img)
        img = Image.fromarray(temp_img)
        processed_img = Image.fromarray(processed_img)
        self.image_window = ImageWindow(img, processed_img)
        self.image_window.show()

    # ---------------------- Hook handling ----------------------
    def start_hook(self) -> None:
        if not self.attached_hwnd:
            return
        self.stop_hook()
        if LunaHookWorker is None:
            self.status.setText("Luna hook backend unavailable.")
            self.inject_log.appendPlainText(
                "Luna hook backend unavailable. Ensure LunaTranslator_x64_win10 exists "
                "or set LUNA_TRANSLATOR_DIR."
            )
            return
        src_lang = (self.src_combo.currentData() or "auto").lower()
        codepage = HOOK_CODEPAGE_MAP.get(src_lang, 932)
        self.hook_worker = LunaHookWorker(self.attached_hwnd, codepage=codepage)
        self.hook_worker.text_ready.connect(self.on_hook_text)
        self.hook_worker.status.connect(self.on_hook_status)
        self.hook_worker.start()

    def stop_hook(self) -> None:
        if self.hook_worker:
            try:
                self.hook_worker.stop()
            except Exception:
                pass
            self.hook_worker = None

    def on_hook_text(self, text: str) -> None:
        src_lang = self.src_combo.currentData()
        dst_lang = self.dst_combo.currentData()

        timestamp = time.strftime("%H:%M:%S")

        row = self.table.rowCount()
        self.table.insertRow(row)
        self.table.setItem(row, 0, QtWidgets.QTableWidgetItem(text))
        self.table.setItem(row, 1, QtWidgets.QTableWidgetItem("hook"))
        self.table.setItem(row, 2, QtWidgets.QTableWidgetItem("(translating...)"))
        self.table.setItem(row, 3, QtWidgets.QTableWidgetItem("hook"))
        self.ocr_results.append({
            "text": text,
            "bbox": (0, 0, 0, 0),
            "lang": "hook",
            "translation": "",
        })

        self.translator.translate_async(
            src_lang,
            dst_lang,
            text,
            tag={"type": "hook", "row": row, "ts": timestamp},
        )

    def on_hook_status(self, message: str) -> None:
        self.status.setText(message)
        self.inject_log.appendPlainText(message)

    # ---------------------- Misc UI handlers ----------------------
    def on_interval_changed(self) -> None:
        if self.worker:
            self.start_capture()

    def on_row_selected(self, row: int, col: int) -> None:
        if row < 0 or row >= len(self.ocr_results):
            return
        bbox = self.ocr_results[row].get("bbox")
        self.selected_bbox = bbox
        self.preview.set_selected_bbox(bbox)

    def on_select(self) -> None:
        idxs = self.table.selectionModel().selectedRows()
        if not idxs:
            return
        r = idxs[0].row()
        if r >= len(self.ocr_results):
            return
        text = self.ocr_results[r].get("translation", "")
        self.edit.setPlainText(text)

    def manual_translate_row(self, row: int) -> None:
        item = self.table.item(row, 0)
        if not item:
            return
        src_text = item.text()
        src_lang = self.src_combo.currentData()
        dst_lang = self.dst_combo.currentData()

        self.table.setItem(row, 2, QtWidgets.QTableWidgetItem("(translating...)"))
        self.translator.translate_async(src_lang, dst_lang, src_text, tag={"type": "manual", "row": row})

    def translate_and_update(self, src: str, dst: str, text: str) -> None:
        if not text:
            return
        key = f"{src}|{dst}|{text}"
        if key in self.translation_cache or key in self.pending_translation_keys:
            return
        self.pending_translation_keys.add(key)
        self.translator.translate_async(src, dst, text, tag={"type": "auto"})

    def on_translation_ready(self, src: str, dst: str, text: str, trans: str, tag: Any) -> None:
        key = f"{src}|{dst}|{text}"
        self.pending_translation_keys.discard(key)
        self.translation_cache[key] = trans

        if tag:
            ttype = tag.get("type")
            if ttype == "hook":
                timestamp = tag.get("ts") or time.strftime("%H:%M:%S")
                self.inject_log.appendPlainText(f"[{timestamp}] {text}\n -> {trans}\n")
                row = tag.get("row")
                if row is not None and 0 <= row < self.table.rowCount():
                    self.table.setItem(row, 2, QtWidgets.QTableWidgetItem(trans))
                    if 0 <= row < len(self.ocr_results):
                        self.ocr_results[row]["translation"] = trans
            elif ttype == "manual":
                row = tag.get("row")
                if row is not None and 0 <= row < self.table.rowCount():
                    self.table.setItem(row, 2, QtWidgets.QTableWidgetItem(trans))
                    if 0 <= row < len(self.ocr_results):
                        self.ocr_results[row]["translation"] = trans

        try:
            overlay = []
            src_lang = self.src_combo.currentData()
            dst_lang = self.dst_combo.currentData()
            for e in self.latest_ocr:
                txt = e.get("text") or ""
                key = f"{src_lang}|{dst_lang}|{txt}"
                trans_text = self.translation_cache.get(key)
                if trans_text:
                    overlay.append({"text": txt, "bbox": e.get("bbox"), "translation": trans_text})
            self.preview.update_overlay(overlay)
        except Exception:
            pass

    def apply_translation(self) -> None:
        idxs = self.table.selectionModel().selectedRows()
        if not idxs:
            self.status.setText("No selection.")
            return
        r = idxs[0].row()
        text = self.edit.toPlainText().strip()
        if r < len(self.ocr_results):
            self.ocr_results[r]["translation"] = text
        self.table.setItem(r, 2, QtWidgets.QTableWidgetItem(text))
        self.status.setText("Applied translation to selected.")

    def save_translations(self) -> None:
        try:
            with open(TRANSLATION_FILE, "w", encoding="utf-8") as f:
                json.dump(self.ocr_results, f, ensure_ascii=False, indent=2)
            self.status.setText(f"Saved translations to {TRANSLATION_FILE}")
        except Exception as e:
            self.status.setText(f"Failed to save translations: {e}")

    def choose_text_overlay_color(self) -> None:
        color = QtWidgets.QColorDialog.getColor(
            self.preview.text_overlay_color, self, "Choose overlay text color"
        )
        if color.isValid():
            self.preview.setTextColor(color)

    def on_src_lang_changed(self) -> None:
        if self.ocr_worker:
            self.ocr_worker.prefer_lang = self.src_combo.currentData()

    def show_help(self) -> None:
        msg = (
            "<b>Game Translation Tool (OCR & Injection)</b><br><br>"
            "<b>Workflow:</b><br>"
            "1. Click the window dropdown to auto-refresh and list open windows.<br>"
            "2. Select a game window.<br>"
            "3. Choose either the OCR or Injection tab.<br>"
            "   - In OCR mode: Click <i>Attach</i> to start live capture, OCR and overlay translation.<br>"
            "   - In Injection mode: Click <i>Attach</i> to start the injection backend (if configured).<br>"
            "4. Click <i>Detach</i> to stop OCR/hook and release the current window.<br>"
            "5. Use the language selectors to choose source and target languages.<br>"
            "6. Click table rows to edit translations manually, then click <i>Apply</i> and"
            "   <i>Save</i> to persist them.<br>"
            "<br>"
            "Note: The injection mode depends on LunaHook from LunaTranslator. Ensure the"
            " LunaTranslator_x64_win10 folder exists, or set LUNA_TRANSLATOR_DIR. Luna Hook does not work with MacOS system."
        )
        QtWidgets.QMessageBox.information(self, "Help", msg)

    def display_window_update(self, item: QtWidgets.QTableWidgetItem) -> None:
        last_row = self.table.rowCount() - 1
        if last_row >= 0:
            trans_item = self.table.item(last_row, 2)
            if trans_item:
                self.display_signal.emit(trans_item.text())

    def update_last_row_translation(self, text: str) -> None:
        last_row = self.table.rowCount() - 1
        if last_row >= 0:
            # Ensure no accidental loop occurs (text edited -> table changed -> change text -> change table, etc.)
            self.table.blockSignals(True)
            self.table.setItem(last_row, 2, QtWidgets.QTableWidgetItem(text))
            self.table.blockSignals(False)

    # Enable preprocessing within current running ocr_worker, if applicable
    def preprocessing_enable(self, _):
        if self.ocr_worker:
            self.ocr_worker.enable_preprocessing = self.enable_preprocessing_checkbox.isChecked()
    
    def closeEvent(self, event: QtGui.QCloseEvent) -> None:
        self.display_window.close()
        self.stop_capture()
        self.stop_hook()
        try:
            self.translator.shutdown()
        except Exception:
            pass
        super().closeEvent(event)

class DisplayWindow(QtWidgets.QWidget):
    """Floating overlay window showing all OCR/translation entries."""
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Display Window")
        self.resize(400, 200)
        self.setWindowFlags(
            QtCore.Qt.WindowType.WindowStaysOnTopHint |
            QtCore.Qt.WindowType.FramelessWindowHint
        )
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setMouseTracking(True)

        self.drag_pos = None
        self.resizing = False
        self.resize_margin = 8

        self.size_grip = QtWidgets.QSizeGrip(self)
        self.size_grip.setFixedSize(16, 16)
        self.overlay_entries = []
        self.default_text = (
            "Latest translation text will be displayed here once extracted\n"
            "Drag box with left click\nResize box by dragging bottom-right corner\n"
            "Right-click to alter settings or close this window"
        )

        self.font_family = "Arial"
        self.font_size = 16
        self.bold = False
        self.italic = False
        self.text_color = QtGui.QColor("white")
        self.alignment = QtCore.Qt.AlignmentFlag.AlignLeft
        self.bg_color = QtGui.QColor(0, 0, 0)
        self.bg_alpha = 180

   
    def update_entries(self, entries) -> None:
        self.overlay_entries = entries
        self.update()

    def changed_text(self, text: str) -> None:
        if not text:
            self.overlay_entries = []
        else:
            self.overlay_entries = [{"translation": text, "bbox": (0, 0, self.width(), 20)}]
        self.update()

 
    def paintEvent(self, event) -> None:
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)

        # Overlay background
        painter.fillRect(self.rect(), QtGui.QColor(
            self.bg_color.red(), self.bg_color.green(), self.bg_color.blue(), self.bg_alpha
        ))

        font = QtGui.QFont(self.font_family, self.font_size)
        font.setBold(self.bold)
        font.setItalic(self.italic)
        painter.setFont(font)
        painter.setPen(self.text_color)

        metrics = QtGui.QFontMetrics(font)
        line_height = metrics.lineSpacing()
        overlay_x = 10
        overlay_width = self.width() - 20
        
        # If we have OCR entries, use them. Otherwise, show default instructions text.
        if self.overlay_entries:
            lines = []
            y_threshold = 15
            for e in self.overlay_entries:
                text = e.get('translation') or e.get('text')
                if not text:
                    continue
                x, y, w, h = e.get('bbox', (0, 0, 0, 0))
                placed = False
                for line in lines:
                    if abs(y - line['y']) <= y_threshold:
                        line['text'] += " " + text
                        line['y'] = min(line['y'], y)
                        line['h'] = max(line['h'], y + h - line['y'])
                        placed = True
                        break
                if not placed:
                    lines.append({'text': text, 'y': y, 'h': h})
            text_to_draw = [l['text'] for l in lines]
        else:
            
            text_to_draw = self.default_text.split("\n")

        
        for i, line in enumerate(text_to_draw):
            rect = QtCore.QRect(overlay_x, 10 + i * line_height, overlay_width, line_height)
            painter.drawText(rect, self.alignment, line)

        painter.end()

 
    def mousePressEvent(self, event) -> None:
        if event.button() == QtCore.Qt.MouseButton.LeftButton:
            self.drag_pos = event.globalPosition().toPoint()
        event.accept()

    def mouseMoveEvent(self, event) -> None:
        if self.drag_pos:
            delta = event.globalPosition().toPoint() - self.drag_pos
            self.move(self.pos() + delta)
            self.drag_pos = event.globalPosition().toPoint()
        event.accept()

    def mouseReleaseEvent(self, event) -> None:
        self.drag_pos = None
        self.resizing = False
        event.accept()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self.size_grip.move(self.width() - self.size_grip.width(), self.height() - self.size_grip.height())
        
    def contextMenuEvent(self, event) -> None:
        menu = QtWidgets.QMenu(self)
        settings_action = menu.addAction("Settings")
        minimize_action = menu.addAction("Minimize")
        exit_action = menu.addAction("Exit")

        action = menu.exec(event.globalPos())
        if action == settings_action:
            self.open_settings()
        elif action == minimize_action:
            self.showMinimized()
        elif action == exit_action:
            self.close()

    def open_settings(self) -> None:
        if hasattr(self, 'settings_window') and self.settings_window.isVisible():
            self.settings_window.raise_()
            return
        self.settings_window = SettingsWindow(self)
        self.settings_window.show()

class SettingsWindow(QtWidgets.QWidget):
    """Settings for DisplayWindow (works with overlay style)."""
    def __init__(self, target: 'DisplayWindow') -> None:
        super().__init__()
        self.target = target
        self.setWindowTitle("Settings")
        self.resize(350, 400)
        self.setWindowFlags(self.windowFlags() | QtCore.Qt.WindowType.WindowStaysOnTopHint)
        self.original_font_family = target.font_family
        self.original_font_size = target.font_size
        self.original_bold = target.bold
        self.original_italic = target.italic
        self.original_text_color = QtGui.QColor(target.text_color)
        self.original_bg_color = QtGui.QColor(target.bg_color)
        self.original_bg_alpha = target.bg_alpha
        self.original_alignment = target.alignment

        layout = QtWidgets.QVBoxLayout()

        font_label = QtWidgets.QLabel("Font:")
        self.font_combo = QtWidgets.QFontComboBox()
        self.font_combo.setCurrentFont(QtGui.QFont(target.font_family))
        self.font_combo.currentFontChanged.connect(self.font_changed)
        layout.addWidget(font_label)
        layout.addWidget(self.font_combo)

        size_label = QtWidgets.QLabel("Font Size:")
        self.size_spinner = QtWidgets.QSpinBox()
        self.size_spinner.setRange(6, 40)
        self.size_spinner.setValue(target.font_size)
        self.size_spinner.valueChanged.connect(self.size_changed)
        layout.addWidget(size_label)
        layout.addWidget(self.size_spinner)

        self.bold_checkbox = QtWidgets.QCheckBox("Bold?")
        self.bold_checkbox.setChecked(target.bold)
        self.bold_checkbox.stateChanged.connect(self.bold_changed)
        self.italic_checkbox = QtWidgets.QCheckBox("Italic?")
        self.italic_checkbox.setChecked(target.italic)
        self.italic_checkbox.stateChanged.connect(self.italic_changed)
        layout.addWidget(self.bold_checkbox)
        layout.addWidget(self.italic_checkbox)

        self.text_color_button = QtWidgets.QPushButton("Choose text color")
        self.text_color_button.clicked.connect(self.color_changed)
        layout.addWidget(self.text_color_button)

        self.bg_color_button = QtWidgets.QPushButton("Choose background color")
        self.bg_color_button.clicked.connect(self.background_changed)
        layout.addWidget(self.bg_color_button)

        opacity_label = QtWidgets.QLabel("Opacity:")
        self.opacity_slider = QtWidgets.QSlider(QtCore.Qt.Orientation.Horizontal)
        self.opacity_slider.setRange(1, 100)
        self.opacity_slider.setValue(int(target.bg_alpha/255*100))
        self.opacity_slider.valueChanged.connect(self.opacity_changed)
        layout.addWidget(opacity_label)
        layout.addWidget(self.opacity_slider)

        align_label = QtWidgets.QLabel("Alignment:")
        self.align_combo = QtWidgets.QComboBox()
        self.align_combo.addItems(["Left", "Center", "Right", "Top", "Bottom"])
        self.align_combo.setCurrentText("Left")
        self.align_combo.currentTextChanged.connect(self.alignment_changed)
        layout.addWidget(align_label)
        layout.addWidget(self.align_combo)

        button_layout = QtWidgets.QHBoxLayout()
        save_button = QtWidgets.QPushButton("Save")
        cancel_button = QtWidgets.QPushButton("Cancel")
        save_button.clicked.connect(self.on_save)
        cancel_button.clicked.connect(self.on_cancel)
        button_layout.addWidget(save_button)
        button_layout.addWidget(cancel_button)
        layout.addLayout(button_layout)

        layout.addStretch()
        self.setLayout(layout)

    # ---------------- Settings Functions ----------------
    def font_changed(self, font: QtGui.QFont):
        self.target.font_family = font.family()
        self.target.update()

    def size_changed(self, size: int):
        self.target.font_size = size
        self.target.update()

    def bold_changed(self, state: int):
        self.target.bold = state == QtCore.Qt.CheckState.Checked.value
        self.target.update()

    def italic_changed(self, state: int):
        self.target.italic = state == QtCore.Qt.CheckState.Checked.value
        self.target.update()

    def color_changed(self):
        dialog = QtWidgets.QColorDialog(self.target.text_color, self)
        if dialog.exec():
            color = dialog.selectedColor()
            if color.isValid():
                self.target.text_color = color
                self.target.update()

    def background_changed(self):
        dialog = QtWidgets.QColorDialog(self.target.bg_color, self)
        if dialog.exec():
            color = dialog.selectedColor()
            if color.isValid():
                self.target.bg_color = color
                self.target.update()

    def opacity_changed(self, value: int):
        self.target.bg_alpha = int((value/100)*255)
        self.target.update()

    def alignment_changed(self, text: str):
        alignments = {
            "Left": QtCore.Qt.AlignmentFlag.AlignLeft,
            "Center": QtCore.Qt.AlignmentFlag.AlignHCenter,
            "Right": QtCore.Qt.AlignmentFlag.AlignRight,
            "Top": QtCore.Qt.AlignmentFlag.AlignTop,
            "Bottom": QtCore.Qt.AlignmentFlag.AlignBottom
        }
        self.target.alignment = alignments.get(text, QtCore.Qt.AlignmentFlag.AlignLeft)
        self.target.update()

   
    def on_save(self):
        self.original_font_family = self.target.font_family
        self.original_font_size = self.target.font_size
        self.original_bold = self.target.bold
        self.original_italic = self.target.italic
        self.original_text_color = QtGui.QColor(self.target.text_color)
        self.original_bg_color = QtGui.QColor(self.target.bg_color)
        self.original_bg_alpha = self.target.bg_alpha
        self.original_alignment = self.target.alignment
        self.close()

    def on_cancel(self):
        self.target.font_family = self.original_font_family
        self.target.font_size = self.original_font_size
        self.target.bold = self.original_bold
        self.target.italic = self.original_italic
        self.target.text_color = self.original_text_color
        self.target.bg_color = self.original_bg_color
        self.target.bg_alpha = self.original_bg_alpha
        self.target.alignment = self.original_alignment
        self.target.update()
        self.close()

    def closeEvent(self, event) -> None:
        event.accept()


"""Window that appears after applying Manual OCR, or applying preprocessing to autOCR, for debug purposes"""
class ImageWindow(QtWidgets.QWidget):
    def __init__(self, img1, img2):
        import numpy as np
        super().__init__()
        self.setWindowTitle("Manual OCR")
                
        fin_layout = QtWidgets.QVBoxLayout()
        img_layout = QtWidgets.QHBoxLayout()

        self.original_image = np.array(img1)
        self.processed_image = img2
        qt_img1 = img1.toqpixmap()
        qt_img2 = self.processed_image.toqpixmap()
        
        label1 = QtWidgets.QLabel()
        label1.setPixmap(qt_img1)
        label1_name = QtWidgets.QLabel("Original")
        self.label2 = QtWidgets.QLabel()
        self.label2.setPixmap(qt_img2)
        label2_name = QtWidgets.QLabel("Processed")

        col1 = QtWidgets.QVBoxLayout()
        col1.addWidget(label1_name)
        col1.addWidget(label1)

        col2 = QtWidgets.QVBoxLayout()
        col2.addWidget(label2_name)
        col2.addWidget(self.label2)

        img_layout.addLayout(col1)
        img_layout.addLayout(col2)

        # Adding button to open preprocessing settings
        fin_layout.addLayout(img_layout)
        self.preprocess_btn = QtWidgets.QPushButton("Open Preprocessing Settings")
        self.preprocess_btn.clicked.connect(self.open_preprocessing_window)
        fin_layout.addWidget(self.preprocess_btn)
        self.setLayout(fin_layout)

        # Adding button to enable whether or not preprocessing is applied before sending to OCR model

        self.preprocessing_window = None

    def open_preprocessing_window(self):
        if self.preprocessing_window is None:
            self.preprocessing_window = PreprocessingWindow(self)
        self.preprocessing_window.show()

    def closeEvent(self, event):
        if self.preprocessing_window is not None:
            self.preprocessing_window.close()
        event.accept()

    def updateImage(self, h_min, s_min, v_min, h_max, s_max, v_max, binarize):
        from PIL import Image
        self.processed_image = removeBackground(self.original_image, h_min, s_min, v_min, h_max, s_max, v_max, binarize)
        new_display_image = Image.fromarray(self.processed_image)
        new_display_image = new_display_image.toqpixmap()
        self.label2.setPixmap(new_display_image)
    

class PreprocessingWindow(QtWidgets.QWidget):
    def __init__(self, image_window: ImageWindow):
        super().__init__()
        self.setWindowTitle("Image Preprocessing")
        self.setMinimumWidth(420)

        self.image_window = image_window
        layout = QtWidgets.QVBoxLayout()
        min_hue_row = QtWidgets.QHBoxLayout()
        min_saturation_row = QtWidgets.QHBoxLayout()
        min_brightness_row = QtWidgets.QHBoxLayout()
        max_hue_row = QtWidgets.QHBoxLayout()
        max_saturation_row = QtWidgets.QHBoxLayout()
        max_brightness_row = QtWidgets.QHBoxLayout()
        binarize_row = QtWidgets.QHBoxLayout()

        # Color min slider
        hue_min_label = QtWidgets.QLabel("Color Minimum:")
        hue_min_label.setFixedWidth(120)
        self.hue_min_slider = QtWidgets.QSlider(QtCore.Qt.Orientation.Horizontal)
        self.hue_min_slider.setRange(0, 179)
        self.h_min = QtWidgets.QLabel("0")
        min_hue_row.addWidget(hue_min_label)
        min_hue_row.addWidget(self.h_min)
        min_hue_row.addWidget(self.hue_min_slider)
        layout.addLayout(min_hue_row)
        
        # Saturation min slider
        saturation_min_label = QtWidgets.QLabel("Saturation Minimum:")
        saturation_min_label.setFixedWidth(120)
        self.saturation_min_slider = QtWidgets.QSlider(QtCore.Qt.Orientation.Horizontal)
        self.saturation_min_slider.setRange(0, 255)
        self.s_min = QtWidgets.QLabel("0")
        min_saturation_row.addWidget(saturation_min_label)
        min_saturation_row.addWidget(self.s_min)
        min_saturation_row.addWidget(self.saturation_min_slider)
        layout.addLayout(min_saturation_row)

        # Brightness min slider
        brightness_min_label = QtWidgets.QLabel("Brightness Minimum:")
        brightness_min_label.setFixedWidth(120)
        self.brightness_min_slider = QtWidgets.QSlider(QtCore.Qt.Orientation.Horizontal)
        self.brightness_min_slider.setRange(0, 255)
        self.v_min = QtWidgets.QLabel("0")
        min_brightness_row.addWidget(brightness_min_label)
        min_brightness_row.addWidget(self.v_min)
        min_brightness_row.addWidget(self.brightness_min_slider)
        layout.addLayout(min_brightness_row)

        # Color max slider
        hue_max_label = QtWidgets.QLabel("Color Maximum:")
        hue_max_label.setFixedWidth(120)
        self.hue_max_slider = QtWidgets.QSlider(QtCore.Qt.Orientation.Horizontal)
        self.hue_max_slider.setRange(0, 179)
        self.h_max = QtWidgets.QLabel("179")
        max_hue_row.addWidget(hue_max_label)
        max_hue_row.addWidget(self.h_max)
        max_hue_row.addWidget(self.hue_max_slider)
        layout.addLayout(max_hue_row)

        # Saturation max slider
        saturation_max_label = QtWidgets.QLabel("Saturation Maximum:")
        saturation_max_label.setFixedWidth(120)
        self.saturation_max_slider = QtWidgets.QSlider(QtCore.Qt.Orientation.Horizontal)
        self.saturation_max_slider.setRange(0, 255)
        self.s_max = QtWidgets.QLabel("255")
        max_saturation_row.addWidget(saturation_max_label)
        max_saturation_row.addWidget(self.s_max)
        max_saturation_row.addWidget(self.saturation_max_slider)
        layout.addLayout(max_saturation_row)

        # Brightness max slider
        brightness_max_label = QtWidgets.QLabel("Brightness Maximum:")
        brightness_max_label.setFixedWidth(120)
        self.brightness_max_slider = QtWidgets.QSlider(QtCore.Qt.Orientation.Horizontal)
        self.brightness_max_slider.setRange(0, 255)
        self.v_max = QtWidgets.QLabel("255")
        max_brightness_row.addWidget(brightness_max_label)
        max_brightness_row.addWidget(self.v_max)
        max_brightness_row.addWidget(self.brightness_max_slider)
        layout.addLayout(max_brightness_row)

        # Binarize slider
        binarize_label = QtWidgets.QLabel("Binarize:")
        binarize_label.setFixedWidth(120)
        self.binarize_slider = QtWidgets.QSlider(QtCore.Qt.Orientation.Horizontal)
        self.binarize_slider.setRange(0, 255)
        self.binarize = QtWidgets.QLabel("0")
        binarize_row.addWidget(binarize_label)
        binarize_row.addWidget(self.binarize)
        binarize_row.addWidget(self.binarize_slider)
        layout.addLayout(binarize_row)
        
        # Calls to change slider label based on current slider value
        self.hue_min_slider.valueChanged.connect(self.sliders_changed)
        self.saturation_min_slider.valueChanged.connect(self.sliders_changed)
        self.brightness_min_slider.valueChanged.connect(self.sliders_changed)
        self.hue_max_slider.valueChanged.connect(self.sliders_changed)
        self.saturation_max_slider.valueChanged.connect(self.sliders_changed)
        self.brightness_max_slider.valueChanged.connect(self.sliders_changed)
        self.binarize_slider.valueChanged.connect(self.sliders_changed)
        
        # Save button
        self.save_button = QtWidgets.QPushButton("Save preprocessing settings")
        self.save_button.clicked.connect(self.save_values)
        layout.addWidget(self.save_button)

        # Default button (returning sliders to default values
        self.default_button = QtWidgets.QPushButton("Reset to default")
        self.default_button.clicked.connect(self.reset_to_default)
        layout.addWidget(self.default_button)

        # Cancel button
        self.cancel_button = QtWidgets.QPushButton("Cancel")
        self.cancel_button.clicked.connect(self.cancel_preprocessing)
        layout.addWidget(self.cancel_button)
        
        # Instantiate sliders with preprocessing__settings if possible
        if os.path.exists("preprocessing_settings.json"):
            with open("preprocessing_settings.json", "r") as f:
                data = json.load(f)
            try:
                self.hue_min_slider.setValue(data["h_min"])
                self.saturation_min_slider.setValue(data["s_min"])
                self.brightness_min_slider.setValue(data["v_min"])
                self.hue_max_slider.setValue(data["h_max"])
                self.saturation_max_slider.setValue(data["s_max"])
                self.brightness_max_slider.setValue(data["v_max"])
                self.binarize_slider.setValue(data["binarization"])
            except KeyError:
                pass

        self.setLayout(layout)

    # Function to save current values of preprocessing sliders to preprocessing_settings.json
    def save_values(self):
        data = {
            "h_min": self.hue_min_slider.value(),
            "s_min": self.saturation_min_slider.value(),
            "v_min": self.brightness_min_slider.value(),
            "h_max": self.hue_max_slider.value(),
            "s_max": self.saturation_max_slider.value(),
            "v_max": self.brightness_max_slider.value(),
            "binarization": self.binarize_slider.value()
        }
        with open("preprocessing_settings.json", "w") as f:
            json.dump(data, f, indent=2)

    # Close with cancel button
    def cancel_preprocessing(self):
        self.close()

    # Reset preprocessing settings to default
    def reset_to_default(self):
        self.hue_min_slider.setValue(0)
        self.saturation_min_slider.setValue(0)
        self.brightness_min_slider.setValue(0)
        self.hue_max_slider.setValue(179)
        self.saturation_max_slider.setValue(255)
        self.brightness_max_slider.setValue(255)
        self.binarize_slider.setValue(0)

    # Changes image of image window and displayed slider value
    def sliders_changed(self):
        self.h_min.setText(str(self.hue_min_slider.value()))
        self.s_min.setText(str(self.saturation_min_slider.value()))
        self.v_min.setText(str(self.brightness_min_slider.value()))
        self.h_max.setText(str(self.hue_max_slider.value()))
        self.s_max.setText(str(self.saturation_max_slider.value()))
        self.v_max.setText(str(self.brightness_max_slider.value()))
        self.binarize.setText(str(self.binarize_slider.value()))
        self.image_window.updateImage(
            self.hue_min_slider.value(), self.saturation_min_slider.value(),
            self.brightness_min_slider.value(), self.hue_max_slider.value(),
            self.saturation_max_slider.value(), self.brightness_max_slider.value(),
            self.binarize_slider.value()
        )

def main() -> None:
    app = QtWidgets.QApplication(sys.argv)
    win2 = DisplayWindow()
    win = MainWindow(win2)
    win.display_signal.connect(win2.changed_text)
    win.show()
    win2.show()
    sys.exit(app.exec())

if __name__ == "__main__":
    main()
