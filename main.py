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
import re
from difflib import SequenceMatcher
from typing import List, Dict, Any, Optional, cast
from pynput import keyboard
from image_preprocessor import removeBackground

from PySide6 import QtWidgets, QtCore, QtGui

from capture import WindowLister, capture_window_bgra, capture_window_image
from ocr_backend import ocr_image_data
from translate_backend import LANG_MAP, translate_text
from translation_worker import Translator
from snipper import Snipper

try:
    from luna_worker import LunaHookWorker
except Exception:
    LunaHookWorker = None

try:
    from memory_patch_worker import ProcessMemoryPatchWorker
except Exception:
    ProcessMemoryPatchWorker = None


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
                    if isinstance(data, tuple):
                        data = data[0]
                    self.ocr_ready.emit(data)
            except Exception as e:
                msg = str(e)
                now = time.time()
                if msg != self._last_error_message or (now - self._last_error_ts) >= 2.0:
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
                        if not hasattr(frame_bgra, "convert"):
                            return
                        pil_img = cast(Any, frame_bgra).convert("RGBA")
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

        painter.fillRect(tgt_rect, QtGui.QColor(32, 34, 37))

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

        if self.qimage is not None and not self.qimage.isNull():
            painter.setFont(QtGui.QFont("Helvetica", 14))
            metrics = QtGui.QFontMetrics(painter.font())
            line_height = metrics.lineSpacing()

            bottom_y = max([line['y'] + line['h'] for line in lines])
            vertical_padding = 100  

            overlay_x = draw_rect.left() + 10
            overlay_y = draw_rect.top() + int(bottom_y * scale) + vertical_padding

            overlay_width = int(draw_rect.width() * 0.8)
            overlay_height = line_height * len(lines) + 8

            if overlay_y + overlay_height > draw_rect.bottom():
                overlay_y = draw_rect.bottom() - overlay_height - 5

            painter.setPen(QtCore.Qt.NoPen)
            painter.setBrush(QtGui.QColor(0, 0, 0, 180))
            painter.drawRect(overlay_x - 4, overlay_y - 4, overlay_width + 8, overlay_height + 8)

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
        self.pending_embed_by_key: Dict[str, List[str]] = {}

        self.worker: Optional[CaptureWorker] = None
        self.ocr_worker: Optional[OCRWorker] = None
        self.hook_worker: Optional[Any] = None
        self.attached_hwnd: Optional[int] = None
        self.ocr_results: List[Dict[str, Any]] = []
        self.latest_ocr: List[Dict[str, Any]] = []
        self._active_text_signature: Optional[tuple[str, ...]] = None
        self._last_text_switch_ts: float = 0.0
        self._text_switch_lock_ms: int = 220
        self._text_similarity_threshold: float = 0.7
        self._ocr_history: List[Dict[str, Any]] = []
        self._ocr_history_size: int = 5
        self._ocr_group_similarity_threshold: float = 0.72
        self._ocr_min_group_votes: int = 2
        self.selected_bbox: Optional[tuple[int, int, int, int]] = None
        self.memory_patch_worker = None
        self._recent_qlie_texts: Dict[str, float] = {}
        self._recent_qlie_ttl_sec: float = 25.0
        self._recent_embed_texts: Dict[str, float] = {}
        self._recent_embed_ttl_sec: float = 20.0
        self._recent_logged_pairs: Dict[str, float] = {}
        self._recent_logged_pair_ttl_sec: float = 8.0
        self._detected_engine: Optional[str] = None
        self._detected_hook_functions: set[str] = set()
        
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
        ctrl.addStretch(1)
        ctrl.addWidget(self.enable_preprocessing_checkbox)
        ctrl.addWidget(self.preprocessing_settings_button)
        ctrl.addWidget(self.manual_ocr_button)

        snip_shortcut = QtGui.QShortcut(QtGui.QKeySequence("F1"), self)
        snip_shortcut.activated.connect(self.start_snip)
        
        ocr_layout.addLayout(ctrl)

        inj_layout = QtWidgets.QVBoxLayout(self.inj_tab)
        embed_row = QtWidgets.QHBoxLayout()
        self.embed_toggle = QtWidgets.QCheckBox("Enable in-game subtitle replacement")
        self.embed_toggle.setChecked(False)
        self.embed_toggle.setToolTip("Use Luna embed callback to replace original subtitle text inside game.")
        embed_row.addWidget(self.embed_toggle)
        embed_row.addStretch(1)
        inj_layout.addLayout(embed_row)

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
        self.overlay_toggle = QtWidgets.QCheckBox("Show translation overlay")
        self.overlay_toggle.setChecked(False)
        lang_row.addWidget(self.overlay_toggle)
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
        self.overlay_toggle.toggled.connect(self.on_overlay_toggled)
        self.src_combo.currentIndexChanged.connect(self.on_src_lang_changed)

        self.table.itemChanged.connect(self.display_window_update)

        self.shortcut_thread = QtCore.QThread()
        self.shortcut_worker = ShortcutWorker()
        self.shortcut_worker.moveToThread(self.shortcut_thread)
        self.shortcut_worker.pressed.connect(self.start_snip)
        self.shortcut_thread.started.connect(self.shortcut_worker.run)
        self.shortcut_thread.start()

        self.refresh_windows()
        self.on_overlay_toggled(False)

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
        self._ocr_history = []
        self._active_text_signature = None
        self._last_text_switch_ts = 0.0
        self.pending_translation_keys.clear()
        self.pending_embed_by_key.clear()
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
        src_lang = self.src_combo.currentData()
        dst_lang = self.dst_combo.currentData()
        for e in self.latest_ocr:
            txt = e.get("text") or ""
            key = f"{src_lang}|{dst_lang}|{txt}"
            if txt.strip() and key not in self.translation_cache and key not in self.pending_translation_keys:
                self.translate_signal.emit(src_lang, dst_lang, txt)
        self.preview.update_frame(frame_bgra)
        self._refresh_preview_overlay()

    def _refresh_preview_overlay(self) -> None:
        try:
            overlay: List[Dict[str, Any]] = []
            src_lang = self.src_combo.currentData()
            dst_lang = self.dst_combo.currentData()
            for e in self.latest_ocr:
                txt = (e.get("text") or "").strip()
                if not txt:
                    continue
                key = f"{src_lang}|{dst_lang}|{txt}"
                trans_text = self.translation_cache.get(key)
                if trans_text:
                    overlay.append({"text": txt, "bbox": e.get("bbox"), "translation": trans_text})
            self.preview.update_overlay(overlay)
        except Exception:
            pass

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

        if not normalized_entries:
            return

        text_signature = tuple(e["text"] for e in normalized_entries)

        self._ocr_history.append({
            "signature": text_signature,
            "entries": normalized_entries,
            "ts": time.time(),
        })
        if len(self._ocr_history) > self._ocr_history_size:
            self._ocr_history = self._ocr_history[-self._ocr_history_size :]

        groups: List[Dict[str, Any]] = []
        for item in self._ocr_history:
            item_sig = cast(tuple[str, ...], item["signature"])
            item_text = "\n".join(item_sig)
            matched = None
            for group in groups:
                group_text = "\n".join(cast(tuple[str, ...], group["signature"]))
                score = SequenceMatcher(None, item_text, group_text).ratio()
                if score >= self._ocr_group_similarity_threshold:
                    matched = group
                    break
            if matched is None:
                groups.append({
                    "signature": item_sig,
                    "votes": 1,
                    "latest": item,
                })
            else:
                matched["votes"] = int(matched["votes"]) + 1
                matched["latest"] = item

        best_group = max(
            groups,
            key=lambda g: (int(g["votes"]), float(cast(Dict[str, Any], g["latest"])["ts"])),
        )
        best_votes = int(best_group["votes"])
        selected = cast(Dict[str, Any], best_group["latest"])
        selected_signature = cast(tuple[str, ...], selected["signature"])
        selected_entries = cast(List[Dict[str, Any]], selected["entries"])

        if best_votes < self._ocr_min_group_votes and len(self._ocr_history) > 1:
            latest = self._ocr_history[-1]
            selected_signature = cast(tuple[str, ...], latest["signature"])
            selected_entries = cast(List[Dict[str, Any]], latest["entries"])

        if selected_signature == self._active_text_signature:
            return

        if self._active_text_signature is not None:
            current_text = "\n".join(self._active_text_signature)
            incoming_text = "\n".join(selected_signature)
            similarity = SequenceMatcher(None, current_text, incoming_text).ratio()
            if similarity >= self._text_similarity_threshold:
                return

        now = time.time()
        if self._active_text_signature is not None:
            elapsed_ms = (now - self._last_text_switch_ts) * 1000.0
            if elapsed_ms < self._text_switch_lock_ms:
                return

        self._active_text_signature = selected_signature
        self._last_text_switch_ts = now
        self.latest_ocr = selected_entries

        self.ocr_results = []
        self.table.setRowCount(0)

        src_lang = self.src_combo.currentData()
        dst_lang = self.dst_combo.currentData()

        for row, e in enumerate(selected_entries):
            src_text = e.get("text", "")
            self.table.insertRow(row)
            self.table.setItem(row, 0, QtWidgets.QTableWidgetItem(src_text))

            btn = QtWidgets.QPushButton("Translate")
            btn.clicked.connect(lambda checked=False, r=row: self.manual_translate_row(r))
            self.table.setCellWidget(row, 1, btn)

            self.table.setItem(row, 2, QtWidgets.QTableWidgetItem(""))
            bbox_str = str(e.get("bbox", ""))
            self.table.setItem(row, 3, QtWidgets.QTableWidgetItem(bbox_str))

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
        self._refresh_preview_overlay()
    
    def start_snip(self):
        self.snipper= Snipper()
        self.snipper.image_captured.connect(self.on_snip)
        self.snipper.show()

    def on_snip(self, img):
        try:
            result = ocr_image_data(img, self.src_combo.currentData(), self.enable_preprocessing_checkbox.isChecked())
            if isinstance(result, tuple):
                data, processed_img = result
            else:
                data = result
                processed_img = img
            self.on_ocr_ready(data)
        except Exception:
            import numpy as np
            from PIL import Image
            temp_img = np.array(img)
            processed_img = removeBackground(temp_img)
            processed_img = Image.fromarray(processed_img)

        self.image_window = ImageWindow(img, processed_img, parent_window=self)
        self.image_window.show()
    
    def open_preprocessing_settings(self):
        import cv2
        from PIL import Image
        temp_img = cv2.imread("logs/debug_frame.png")
        processed_img = removeBackground(temp_img)
        img = Image.fromarray(temp_img)
        processed_img = Image.fromarray(processed_img)
        self.image_window = ImageWindow(img, processed_img, parent_window=self)
        self.image_window.show()

    # ---------------------- Hook handling ----------------------
    def _resolve_pid_from_hwnd(self) -> Optional[int]:
        if not self.attached_hwnd:
            return None
        try:
            import ctypes
            from ctypes import wintypes

            pid = wintypes.DWORD()
            ctypes.windll.user32.GetWindowThreadProcessId(wintypes.HWND(int(self.attached_hwnd)), ctypes.byref(pid))
            return int(pid.value) if pid.value else None
        except Exception:
            return None

    def _append_inject_log(self, message: str) -> None:
        msg = str(message or "")
        if not msg:
            return
        self.inject_log.appendPlainText(msg)

    def _should_show_status_message(self, message: str) -> bool:
        text = str(message or "").strip().lower()
        if not text:
            return False
        keywords = (
            "attached",
            "process connected",
            "process removed",
            "detected engine",
            "hook detected",
            "embed translation enabled",
            "error",
            "failed",
            "unavailable",
            "stopped",
        )
        return any(k in text for k in keywords)

    def _write_debug_event(self, event: str, **payload: Any) -> None:
        return

    def _analyze_status_line(self, line: str) -> None:
        text = str(line or "")
        low = text.lower()

        if text.startswith("[helper-debug] "):
            raw = text[len("[helper-debug] "):].strip()
            try:
                payload = json.loads(raw)
                event = str(payload.get("event") or "helper")
                if event == "output_text":
                    try:
                        if bool(payload.get("is_qlie_output")):
                            cleaned = str(payload.get("clean_text") or "").strip()
                            raw_txt = str(payload.get("raw_text") or "").strip()
                            now = time.time()
                            for t in (cleaned, raw_txt):
                                if t:
                                    self._recent_qlie_texts[t] = now
                    except Exception:
                        pass
                payload.pop("type", None)
                payload.pop("event", None)
                self._write_debug_event(f"helper.{event}", **payload)
            except Exception:
                self._write_debug_event("helper.raw", raw=text)
            return

        if "qlie" in low and self._detected_engine != "QLIE":
            self._detected_engine = "QLIE"
            self._append_inject_log("Detected engine: QLIE")
            self._write_debug_event("engine.detected", engine="QLIE", source_line=text)

        fn_match = re.search(r":\s*([A-Za-z_][A-Za-z0-9_]{2,})\s+[0-9A-Fa-f]{6,16}", text)
        if fn_match:
            fn_name = fn_match.group(1)
            if fn_name not in self._detected_hook_functions:
                self._detected_hook_functions.add(fn_name)
                self._append_inject_log(f"Hook function detected: {fn_name}")
                self._write_debug_event("hook.function_detected", function=fn_name, source_line=text)

        hook_insert = re.search(r"Embed/QLIE hook detected:\s*pid=(\d+)\s*addr=0x([0-9A-Fa-f]+)\s*code=(.*)$", text)
        if hook_insert:
            self._append_inject_log(
                f"Embed/QLIE hook detected: pid={hook_insert.group(1)} addr=0x{hook_insert.group(2)}"
            )
            self._write_debug_event(
                "hook.embed_detected",
                hook_pid=int(hook_insert.group(1)),
                hook_addr=f"0x{hook_insert.group(2)}",
                hook_code=hook_insert.group(3),
            )

        if "embed callback sent" in low:
            return

        if low.startswith("process connected:") or low.startswith("process removed:"):
            self._append_inject_log(text)
            self._write_debug_event("process.lifecycle", detail=text)

    def _is_recent_qlie_text(self, text: str) -> bool:
        t = str(text or "").strip()
        if not t:
            return False
        now = time.time()
        for k, ts in list(self._recent_qlie_texts.items()):
            if (now - ts) > self._recent_qlie_ttl_sec:
                self._recent_qlie_texts.pop(k, None)
        if t in self._recent_qlie_texts:
            return True
        compact = t.replace("\r", "").replace("\n", "").replace(" ", "")
        for k in self._recent_qlie_texts.keys():
            kc = k.replace("\r", "").replace("\n", "").replace(" ", "")
            if compact and kc and (compact == kc):
                return True
        return False

    def _remember_embed_text(self, text: str) -> None:
        t = str(text or "").strip()
        if not t:
            return
        now = time.time()
        self._recent_embed_texts[t] = now
        for key, ts in list(self._recent_embed_texts.items()):
            if (now - ts) > self._recent_embed_ttl_sec:
                self._recent_embed_texts.pop(key, None)

    def _is_recent_embed_text(self, text: str) -> bool:
        t = str(text or "").strip()
        if not t:
            return False
        now = time.time()
        for key, ts in list(self._recent_embed_texts.items()):
            if (now - ts) > self._recent_embed_ttl_sec:
                self._recent_embed_texts.pop(key, None)
        if t in self._recent_embed_texts:
            return True
        compact = t.replace("\r", "").replace("\n", "").replace(" ", "")
        for key in self._recent_embed_texts.keys():
            kc = key.replace("\r", "").replace("\n", "").replace(" ", "")
            if compact and kc and compact == kc:
                return True
        return False

    def _should_log_hook_translation(self, text: str) -> bool:
        t = str(text or "").strip()
        if not t:
            return False
        if self._is_recent_embed_text(t):
            return True

        low = t.lower()
        if any(k in low for k in ("file(&f)", "screen(&s)", "help(&h)", "ver1.", "progress control")):
            return False
        if any(k in t for k in ("ファイル(&F)", "画面(&S)", "進行制御(&M)", "ヘルプ(&H)")):
            return False
        if re.search(r"([一-龯ぁ-んァ-ン])\1\1", t):
            return False
        if t.count("「") >= 4 or t.count("」") >= 4:
            return False

        compact_len = len(t.replace("\r", "").replace("\n", "").replace(" ", ""))
        if compact_len > 90:
            return False
        if self._is_repetitive_noise_text(t):
            return False
        return True

    def _normalize_log_text(self, text: str) -> str:
        t = str(text or "").strip().lower()
        if not t:
            return ""
        t = t.replace("\r", "").replace("\n", "")
        t = re.sub(r"[\s\u3000]+", "", t)
        t = t.strip("\"'`\u201c\u201d\u2018\u2019\u300c\u300d\u300e\u300f\uff08\uff09()[]{}<>")
        return t

    def _is_repetitive_noise_text(self, text: str) -> bool:
        compact = self._normalize_log_text(text)
        if len(compact) < 8:
            return False

        max_unit = min(16, max(1, len(compact) // 3))
        for unit in range(1, max_unit + 1):
            if len(compact) % unit != 0:
                continue
            repeat_count = len(compact) // unit
            if repeat_count < 3:
                continue
            part = compact[:unit]
            if part * repeat_count == compact:
                return True

        stretched_runs = len(re.findall(r"([a-z\u4e00-\u9fff\u3041-\u3093\u30a1-\u30f3])\1{2,}", compact))
        if stretched_runs >= 3:
            return True
        return False

    def _should_emit_hook_log(self, source_text: str, translated_text: str) -> bool:
        if not self._should_log_hook_translation(source_text):
            return False

        src_norm = self._normalize_log_text(source_text)
        dst_norm = self._normalize_log_text(translated_text)
        if not src_norm or not dst_norm:
            return False
        if src_norm == dst_norm:
            return False
        if self._is_repetitive_noise_text(source_text) or self._is_repetitive_noise_text(translated_text):
            return False

        now = time.time()
        for key, ts in list(self._recent_logged_pairs.items()):
            if (now - ts) > self._recent_logged_pair_ttl_sec:
                self._recent_logged_pairs.pop(key, None)

        pair_key = f"{src_norm}=>{dst_norm}"
        if pair_key in self._recent_logged_pairs:
            return False
        self._recent_logged_pairs[pair_key] = now
        return True

    def start_hook(self) -> None:
        if not self.attached_hwnd:
            return
        self.stop_hook()
        if LunaHookWorker is None:
            self.status.setText("Luna hook backend unavailable.")
            self._append_inject_log(
                "Luna hook backend unavailable. Ensure LunaTranslator_x64_win10 exists "
                "or set LUNA_TRANSLATOR_DIR."
            )
            return
        src_lang = (self.src_combo.currentData() or "auto").lower()
        codepage = HOOK_CODEPAGE_MAP.get(src_lang, 932)
        embed_enabled = bool(self.embed_toggle.isChecked())
        effective_embed_enabled = bool(embed_enabled)
        self._write_debug_event(
            "hook.start",
            src_lang=src_lang,
            dst_lang=(self.dst_combo.currentData() or "en"),
            codepage=codepage,
            embed_enabled=embed_enabled,
            effective_embed_enabled=effective_embed_enabled,
        )
        worker = LunaHookWorker(
            self.attached_hwnd,
            codepage=codepage,
            enable_embed=effective_embed_enabled,
        )
        self.hook_worker = worker
        worker.text_ready.connect(self.on_hook_text)
        worker.status.connect(self.on_hook_status)
        worker.embed_text_requested.connect(self.on_embed_text_requested)
        worker.start()

        if ProcessMemoryPatchWorker and effective_embed_enabled:
            pid = self._resolve_pid_from_hwnd()
            if pid:
                try:
                    self.memory_patch_worker = ProcessMemoryPatchWorker(
                        pid,
                        status_cb=self._append_inject_log,
                        source_codepage=codepage,
                        debug_cb=self._on_memory_patch_debug,
                    )
                    self.memory_patch_worker.start()
                except Exception as e:
                    self._append_inject_log(f"Memory patch worker start failed: {e}")
            else:
                self._append_inject_log("Memory patch worker skipped: failed to resolve pid.")
                self._write_debug_event("memory_patch.skipped", reason="resolve_pid_failed")
        elif effective_embed_enabled and not ProcessMemoryPatchWorker:
            self._append_inject_log("Memory patch worker unavailable: import failed.")
            self._write_debug_event("memory_patch.skipped", reason="worker_unavailable")

    def _on_memory_patch_debug(self, payload: Dict[str, Any]) -> None:
        if not isinstance(payload, dict):
            return
        event_name = str(payload.get("event") or "memory_patch")
        data = dict(payload)
        data.pop("event", None)
        self._write_debug_event(f"memory_patch.{event_name}", **data)

    def stop_hook(self) -> None:
        if self.hook_worker:
            try:
                self.hook_worker.stop()
            except Exception:
                pass
            self.hook_worker = None
        self.pending_embed_by_key.clear()
        if self.memory_patch_worker:
            try:
                self.memory_patch_worker.stop()
            except Exception:
                pass
            self.memory_patch_worker = None

    def on_hook_text(self, text: str) -> None:
        src_lang = self.src_combo.currentData()
        dst_lang = self.dst_combo.currentData()
        self._write_debug_event("hook.text", src_lang=src_lang, dst_lang=dst_lang, text=text)

        if not self._should_log_hook_translation(text):
            return

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
        if self._should_show_status_message(message):
            self.status.setText(message)
        low_msg = str(message or "").lower()
        if "process removed:" in low_msg:
            if self.memory_patch_worker:
                try:
                    self.memory_patch_worker.stop()
                    self._append_inject_log("Memory patch worker stopped: process removed.")
                except Exception:
                    pass
                self.memory_patch_worker = None
        for line in str(message or "").splitlines() or [str(message or "")]:
            if line:
                self._analyze_status_line(line)

    def on_embed_text_requested(self, request_id: str, text: str) -> None:
        self._write_debug_event("embed.request", request_id=request_id, text=text, text_len=len(text or ""))
        self._remember_embed_text(text)
        src_lang = self.src_combo.currentData()
        dst_lang = self.dst_combo.currentData()
        key = f"{src_lang}|{dst_lang}|{text}"

        queue_for_key = self.pending_embed_by_key.setdefault(key, [])
        queue_for_key.append(request_id)

        cached = self.translation_cache.get(key)
        if cached is not None:
            self._resolve_embed_requests_for_key(key, cached)
            return
        try:
            trans = translate_text(src_lang, dst_lang, text)
        except Exception:
            trans = text
        self.translation_cache[key] = trans
        self._resolve_embed_requests_for_key(key, trans)

    def _resolve_embed_requests_for_key(self, key: str, translation: str) -> None:
        request_ids = self.pending_embed_by_key.pop(key, [])
        if not request_ids:
            return
        worker = self.hook_worker
        if worker is None:
            return
        for request_id in request_ids:
            try:
                worker.submit_embed_translation(request_id, translation)
                self._write_debug_event(
                    "embed.submit",
                    request_id=request_id,
                    translation=translation,
                    translation_len=len(translation or ""),
                )
            except Exception:
                pass

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
        self._resolve_embed_requests_for_key(key, trans)

        if tag:
            ttype = tag.get("type")
            if ttype == "hook":
                timestamp = tag.get("ts") or time.strftime("%H:%M:%S")
                self._write_debug_event(
                    "translation.hook_ready",
                    src=src,
                    dst=dst,
                    text=text,
                    translation=trans,
                    memory_worker_running=bool(self.memory_patch_worker),
                )
                if self.memory_patch_worker:
                    try:
                        if self._is_recent_qlie_text(text):
                            self.memory_patch_worker.update_mapping(text, trans)
                    except Exception:
                        pass
                if self._should_emit_hook_log(text, trans):
                    self._append_inject_log(f"[{timestamp}] {text}\n -> {trans}\n")
                self.display_signal.emit(trans)
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
                self.display_signal.emit(trans)
            elif ttype == "auto":
                row = tag.get("row")
                if row is not None and 0 <= row < self.table.rowCount():
                    self.table.setItem(row, 2, QtWidgets.QTableWidgetItem(trans))
                    if 0 <= row < len(self.ocr_results):
                        self.ocr_results[row]["translation"] = trans
                else:
                    for idx, item in enumerate(self.ocr_results):
                        if str(item.get("text") or "") == text:
                            if idx < self.table.rowCount():
                                self.table.setItem(idx, 2, QtWidgets.QTableWidgetItem(trans))
                            self.ocr_results[idx]["translation"] = trans
                            break
                self.display_signal.emit(trans)
        else:
            self.display_signal.emit(trans)

        self._refresh_preview_overlay()

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
        if text:
            self.display_signal.emit(text)
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

    def on_overlay_toggled(self, checked: bool) -> None:
        if checked:
            self.display_window.show()
            self.display_window.raise_()
            for row in range(self.table.rowCount() - 1, -1, -1):
                item = self.table.item(row, 2)
                if item:
                    text = (item.text() or "").strip()
                    if text and text != "(translating...)":
                        self.display_signal.emit(text)
                        break
        else:
            self.display_window.hide()

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
        if item is None:
            return
        if item.column() != 2:
            return
        text = (item.text() or "").strip()
        if not text or text == "(translating...)":
            return
        self.display_signal.emit(text)

    def update_last_row_translation(self, text: str) -> None:
        last_row = self.table.rowCount() - 1
        if last_row >= 0:
            self.table.blockSignals(True)
            self.table.setItem(last_row, 2, QtWidgets.QTableWidgetItem(text))
            self.table.blockSignals(False)

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

        if os.path.exists("text_overlay_display_settings.json"):
            with open("text_overlay_display_settings.json", "r") as f:
                data = json.load(f)
            try:
                self.font_family = data["font"]
                self.font_size = data["font_size"]
                self.bold = data.get("bold", False)
                self.italic = data.get("italic", False)
                self.text_color = QtGui.QColor(data["text_color"])
                self.bg_color = QtGui.QColor(data["background_color"])
                self.bg_alpha = data["opacity"]
                alignment_name = data.get("alignment", "Left")
                alignments = {
                    "Left": QtCore.Qt.AlignmentFlag.AlignLeft,
                    "Center": QtCore.Qt.AlignmentFlag.AlignHCenter,
                    "Right": QtCore.Qt.AlignmentFlag.AlignRight,
                    "Top": QtCore.Qt.AlignmentFlag.AlignTop,
                    "Bottom": QtCore.Qt.AlignmentFlag.AlignBottom,
                }
                self.alignment = alignments.get(alignment_name, QtCore.Qt.AlignmentFlag.AlignLeft)
            except KeyError:
                self._set_defaults()
        else:
            self._set_defaults()

    def _set_defaults(self) -> None:
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
        default_button = QtWidgets.QPushButton("Reset to default")
        save_button.clicked.connect(self.on_save)
        cancel_button.clicked.connect(self.on_cancel)
        default_button.clicked.connect(self.reset_to_default)
        button_layout.addWidget(save_button)
        button_layout.addWidget(default_button)
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

        alignment_map = {
            QtCore.Qt.AlignmentFlag.AlignLeft: "Left",
            QtCore.Qt.AlignmentFlag.AlignHCenter: "Center",
            QtCore.Qt.AlignmentFlag.AlignRight: "Right",
            QtCore.Qt.AlignmentFlag.AlignTop: "Top",
            QtCore.Qt.AlignmentFlag.AlignBottom: "Bottom",
        }
        data = {
            "text_color": self.original_text_color.name(),
            "background_color": self.original_bg_color.name(),
            "opacity": self.original_bg_alpha,
            "font": self.original_font_family,
            "font_size": self.original_font_size,
            "bold": self.original_bold,
            "italic": self.original_italic,
            "alignment": alignment_map.get(self.original_alignment, "Left"),
        }
        with open("text_overlay_display_settings.json", "w") as f:
            json.dump(data, f, indent=2)
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

    def reset_to_default(self):
        self.target.font_family = "Arial"
        self.target.font_size = 16
        self.target.bold = False
        self.target.italic = False
        self.target.text_color = QtGui.QColor("white")
        self.target.bg_color = QtGui.QColor(0, 0, 0)
        self.target.bg_alpha = 180
        self.target.alignment = QtCore.Qt.AlignmentFlag.AlignLeft
        self.font_combo.setCurrentFont(QtGui.QFont(self.target.font_family))
        self.size_spinner.setValue(self.target.font_size)
        self.bold_checkbox.setChecked(self.target.bold)
        self.italic_checkbox.setChecked(self.target.italic)
        self.opacity_slider.setValue(int(self.target.bg_alpha/255*100))
        self.align_combo.setCurrentText("Left")
        self.target.update()

    def closeEvent(self, event) -> None:
        event.accept()


"""Window that appears after applying Manual OCR, or applying preprocessing to autoOCR, for debug purposes"""
class ImageWindow(QtWidgets.QWidget):
    def __init__(self, img1, img2, parent_window):
        import numpy as np
        from PIL import Image
        super().__init__()
        self.setWindowTitle("Manual OCR")
        self.parent_window = parent_window

        fin_layout = QtWidgets.QVBoxLayout()
        img_layout = QtWidgets.QHBoxLayout()

        self.original_image = np.array(img1)
        self.processed_image = img2
        
        temp_img1 = img1
        temp_img2 = img2
        temp_img1.thumbnail((600, 600), Image.LANCZOS)
        temp_img2.thumbnail((600, 600), Image.LANCZOS)
        qt_img1 = temp_img1.toqpixmap()
        qt_img2 = temp_img2.toqpixmap()
        
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

        self.collapsible = QtWidgets.QWidget()
        preprocessing_label = QtWidgets.QLabel("Preprocessing Settings")
        preprocessing_layout = QtWidgets.QVBoxLayout()
        preprocessing_layout.addWidget(preprocessing_label)
        min_hue_row = QtWidgets.QHBoxLayout()
        min_saturation_row = QtWidgets.QHBoxLayout()
        min_brightness_row = QtWidgets.QHBoxLayout()
        max_hue_row = QtWidgets.QHBoxLayout()
        max_saturation_row = QtWidgets.QHBoxLayout()
        max_brightness_row = QtWidgets.QHBoxLayout()
        binarize_row = QtWidgets.QHBoxLayout()

        hue_min_label = QtWidgets.QLabel("Color Minimum:")
        hue_min_label.setFixedWidth(120)
        self.hue_min_slider = QtWidgets.QSlider(QtCore.Qt.Orientation.Horizontal)
        self.hue_min_slider.setRange(0, 179)
        self.h_min = QtWidgets.QLabel("0")
        min_hue_row.addWidget(hue_min_label)
        min_hue_row.addWidget(self.h_min)
        min_hue_row.addWidget(self.hue_min_slider)
        preprocessing_layout.addLayout(min_hue_row)
        
        saturation_min_label = QtWidgets.QLabel("Saturation Minimum:")
        saturation_min_label.setFixedWidth(120)
        self.saturation_min_slider = QtWidgets.QSlider(QtCore.Qt.Orientation.Horizontal)
        self.saturation_min_slider.setRange(0, 255)
        self.s_min = QtWidgets.QLabel("0")
        min_saturation_row.addWidget(saturation_min_label)
        min_saturation_row.addWidget(self.s_min)
        min_saturation_row.addWidget(self.saturation_min_slider)
        preprocessing_layout.addLayout(min_saturation_row)

        brightness_min_label = QtWidgets.QLabel("Brightness Minimum:")
        brightness_min_label.setFixedWidth(120)
        self.brightness_min_slider = QtWidgets.QSlider(QtCore.Qt.Orientation.Horizontal)
        self.brightness_min_slider.setRange(0, 255)
        self.v_min = QtWidgets.QLabel("0")
        min_brightness_row.addWidget(brightness_min_label)
        min_brightness_row.addWidget(self.v_min)
        min_brightness_row.addWidget(self.brightness_min_slider)
        preprocessing_layout.addLayout(min_brightness_row)

        hue_max_label = QtWidgets.QLabel("Color Maximum:")
        hue_max_label.setFixedWidth(120)
        self.hue_max_slider = QtWidgets.QSlider(QtCore.Qt.Orientation.Horizontal)
        self.hue_max_slider.setRange(0, 179)
        self.h_max = QtWidgets.QLabel("179")
        max_hue_row.addWidget(hue_max_label)
        max_hue_row.addWidget(self.h_max)
        max_hue_row.addWidget(self.hue_max_slider)
        preprocessing_layout.addLayout(max_hue_row)

        saturation_max_label = QtWidgets.QLabel("Saturation Maximum:")
        saturation_max_label.setFixedWidth(120)
        self.saturation_max_slider = QtWidgets.QSlider(QtCore.Qt.Orientation.Horizontal)
        self.saturation_max_slider.setRange(0, 255)
        self.s_max = QtWidgets.QLabel("255")
        max_saturation_row.addWidget(saturation_max_label)
        max_saturation_row.addWidget(self.s_max)
        max_saturation_row.addWidget(self.saturation_max_slider)
        preprocessing_layout.addLayout(max_saturation_row)

        brightness_max_label = QtWidgets.QLabel("Brightness Maximum:")
        brightness_max_label.setFixedWidth(120)
        self.brightness_max_slider = QtWidgets.QSlider(QtCore.Qt.Orientation.Horizontal)
        self.brightness_max_slider.setRange(0, 255)
        self.v_max = QtWidgets.QLabel("255")
        max_brightness_row.addWidget(brightness_max_label)
        max_brightness_row.addWidget(self.v_max)
        max_brightness_row.addWidget(self.brightness_max_slider)
        preprocessing_layout.addLayout(max_brightness_row)

        binarize_label = QtWidgets.QLabel("Binarize:")
        binarize_label.setFixedWidth(120)
        self.binarize_slider = QtWidgets.QSlider(QtCore.Qt.Orientation.Horizontal)
        self.binarize_slider.setRange(0, 255)
        self.binarize = QtWidgets.QLabel("0")
        binarize_row.addWidget(binarize_label)
        binarize_row.addWidget(self.binarize)
        binarize_row.addWidget(self.binarize_slider)
        preprocessing_layout.addLayout(binarize_row)
        
        self.hue_min_slider.valueChanged.connect(self.sliders_changed)
        self.saturation_min_slider.valueChanged.connect(self.sliders_changed)
        self.brightness_min_slider.valueChanged.connect(self.sliders_changed)
        self.hue_max_slider.valueChanged.connect(self.sliders_changed)
        self.saturation_max_slider.valueChanged.connect(self.sliders_changed)
        self.brightness_max_slider.valueChanged.connect(self.sliders_changed)
        self.binarize_slider.valueChanged.connect(self.sliders_changed)
        
        self.save_button = QtWidgets.QPushButton("Save preprocessing settings")
        self.save_button.clicked.connect(self.save_preprocessing_values)
        preprocessing_layout.addWidget(self.save_button)

        self.default_button = QtWidgets.QPushButton("Reset to default")
        self.default_button.clicked.connect(self.reset_to_default)
        preprocessing_layout.addWidget(self.default_button)

        self.reapply_button = QtWidgets.QPushButton("Reapply OCR")
        self.reapply_button.clicked.connect(self.reapply_OCR)
        preprocessing_layout.addWidget(self.reapply_button)

        self.cancel_button = QtWidgets.QPushButton("Cancel")
        self.cancel_button.clicked.connect(self.cancel_preprocessing)
        preprocessing_layout.addWidget(self.cancel_button)
        
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
        
        self.collapsible.setLayout(preprocessing_layout)
        self.collapsible.setVisible(False)

        self.show_preprocessing_settings_btn = QtWidgets.QPushButton("Show preprocessing settings \u25bc")
        self.show_preprocessing_settings_btn.clicked.connect(self.show_preprocesssing_settings)
        fin_layout.addLayout(img_layout)
        fin_layout.addWidget(self.show_preprocessing_settings_btn)
        fin_layout.addWidget(self.collapsible)
        self.setLayout(fin_layout)

    def save_preprocessing_values(self):
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

    def cancel_preprocessing(self):
        self.close()

    def reset_to_default(self):
        self.hue_min_slider.setValue(0)
        self.saturation_min_slider.setValue(0)
        self.brightness_min_slider.setValue(0)
        self.hue_max_slider.setValue(179)
        self.saturation_max_slider.setValue(255)
        self.brightness_max_slider.setValue(255)
        self.binarize_slider.setValue(0)

    def sliders_changed(self):
        self.h_min.setText(str(self.hue_min_slider.value()))
        self.s_min.setText(str(self.saturation_min_slider.value()))
        self.v_min.setText(str(self.brightness_min_slider.value()))
        self.h_max.setText(str(self.hue_max_slider.value()))
        self.s_max.setText(str(self.saturation_max_slider.value()))
        self.v_max.setText(str(self.brightness_max_slider.value()))
        self.binarize.setText(str(self.binarize_slider.value()))
        self.updateImage(
            self.hue_min_slider.value(), self.saturation_min_slider.value(),
            self.brightness_min_slider.value(), self.hue_max_slider.value(),
            self.saturation_max_slider.value(), self.brightness_max_slider.value(),
            self.binarize_slider.value()
        )

    def updateImage(self, h_min, s_min, v_min, h_max, s_max, v_max, binarize):
        from PIL import Image
        self.processed_image = removeBackground(self.original_image, h_min, s_min, v_min, h_max, s_max, v_max, binarize)
        new_display_image = Image.fromarray(self.processed_image)
        new_display_image.thumbnail((600, 600), Image.LANCZOS)
        new_display_image = new_display_image.toqpixmap()
        self.label2.setPixmap(new_display_image)

    def show_preprocesssing_settings(self):
        visible = self.collapsible.isVisible()
        self.collapsible.setVisible(not visible)
        self.show_preprocessing_settings_btn.setText("Collapse preprocessing settings \u25b2" if visible else "Show preprocessing settings \u25bc")
        self.adjustSize()

    def reapply_OCR(self):
        try:
            result = ocr_image_data(self.processed_image, self.parent_window.src_combo.currentData(), enable_preprocessing=False)
            if isinstance(result, tuple):
                data = result[0]
            else:
                data = result
            self.parent_window.on_ocr_ready(data)
        except Exception:
            pass

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
