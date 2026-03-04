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

from PySide6 import QtWidgets, QtCore, QtGui

from capture import WindowLister, capture_window_image, capture_window_bgra
from ocr_backend import ocr_image_data
from translate_backend import LANG_MAP
from translation_worker import Translator

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

    def run(self) -> None:
        self._running = True
        while self._running:
            start = time.time()
            try:
                pil_img = capture_window_image(self.hwnd)
                if pil_img is not None:
                    data = ocr_image_data(pil_img, self.prefer_lang)
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

        if self.qimage is not None and not self.qimage.isNull():
            src_w, src_h = self.qimage.width(), self.qimage.height()
            if src_w > 0 and src_h > 0:
                scale = min(tgt_rect.width() / src_w, tgt_rect.height() / src_h)
                draw_w = int(src_w * scale)
                draw_h = int(src_h * scale)
                offset_x = (tgt_rect.width() - draw_w) // 2
                offset_y = (tgt_rect.height() - draw_h) // 2
                self._draw_rect = QtCore.QRect(offset_x, offset_y, draw_w, draw_h)
            else:
                self._draw_rect = tgt_rect
            painter.drawImage(self._draw_rect, self.qimage, self.qimage.rect())
        else:
            self._draw_rect = tgt_rect
            painter.fillRect(self.rect(), QtGui.QColor(32, 34, 37))

        if self.qimage is None or self.qimage.isNull():
            painter.end()
            return

        draw_rect = getattr(self, "_draw_rect", tgt_rect)
        src_w, src_h = self.qimage.width(), self.qimage.height()
        scale_x = draw_rect.width() / max(1, src_w)
        scale_y = draw_rect.height() / max(1, src_h)
        off_x = draw_rect.left()
        off_y = draw_rect.top()

        if self.selected_bbox:
            x, y, w, h = self.selected_bbox
            tx = int(x * scale_x) + off_x
            ty = int(y * scale_y) + off_y
            tw = int(w * scale_x)
            th = int(h * scale_y)
            painter.setPen(QtGui.QPen(QtGui.QColor(0, 255, 0), 2))
            painter.drawRect(tx, ty, tw, th)

        if not self.overlay_entries:
            painter.end()
            return

        painter.setPen(self.text_overlay_color)
        font = painter.font()
        font.setPointSize(max(font.pointSize(), 10))
        painter.setFont(font)
        metrics = QtGui.QFontMetrics(font)

        max_w = int(draw_rect.width() * 0.6)

        for e in self.overlay_entries:
            bbox = e.get("bbox")
            text = e.get("translation") or e.get("text") or ""
            if not bbox or not text:
                continue
            x, y, w, h = bbox
            tx = int(x * scale_x) + off_x
            ty = int(y * scale_y) + off_y
            rect = metrics.boundingRect(0, 0, max_w, 1000, QtCore.Qt.AlignLeft | QtCore.Qt.TextWordWrap, text)
            box_w = rect.width() + 12
            box_h = rect.height() + 12
            painter.setPen(QtCore.Qt.NoPen)
            painter.setBrush(QtGui.QColor(0, 0, 0, 160))
            painter.drawRect(QtCore.QRect(tx, ty, box_w, box_h))
            painter.setPen(self.text_overlay_color)
            painter.drawText(
                QtCore.QRect(tx + 6, ty + 6, rect.width(), rect.height()),
                QtCore.Qt.AlignLeft | QtCore.Qt.AlignTop | QtCore.Qt.TextWordWrap,
                text,
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

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Game Translation Tool")
        self.resize(1200, 700)

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
        ctrl.addWidget(QtWidgets.QLabel("Frame (ms)"))
        ctrl.addWidget(self.interval_spin)
        ctrl.addSpacing(20)
        ctrl.addWidget(QtWidgets.QLabel("OCR (ms)"))
        ctrl.addWidget(self.ocr_spin)
        ctrl.addStretch(1)
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
        for e in normalized_entries:
            src_text = e.get("text", "")
            row = self.table.rowCount()
            self.table.insertRow(row)
            self.table.setItem(row, 0, QtWidgets.QTableWidgetItem(src_text))
            btn = QtWidgets.QPushButton("Translate")
            btn.clicked.connect(lambda checked=False, r=row: self.manual_translate_row(r))
            self.table.setCellWidget(row, 1, btn)
            self.table.setItem(row, 2, QtWidgets.QTableWidgetItem(""))
            bbox_str = str(e.get("bbox", ""))
            self.table.setItem(row, 3, QtWidgets.QTableWidgetItem(bbox_str))
            self.ocr_results.append({
                "text": src_text,
                "bbox": e.get("bbox"),
                "lang": e.get("lang", "unknown"),
                "translation": "",
            })

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
            " LunaTranslator_x64_win10 folder exists, or set LUNA_TRANSLATOR_DIR."
        )
        QtWidgets.QMessageBox.information(self, "Help", msg)

    def closeEvent(self, event: QtGui.QCloseEvent) -> None:
        self.stop_capture()
        self.stop_hook()
        try:
            self.translator.shutdown()
        except Exception:
            pass
        super().closeEvent(event)


def main() -> None:
    app = QtWidgets.QApplication(sys.argv)
    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
