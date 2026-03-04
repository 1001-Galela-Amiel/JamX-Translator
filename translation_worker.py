from PySide6 import QtCore
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Optional
from translate_backend import translate_text


class Translator(QtCore.QObject):
    """Runs translations in a thread pool and emits results back to the UI.

    Signals:
        translation_ready(src, dst, text, translation, tag)
    """

    translation_ready = QtCore.Signal(str, str, str, str, object)

    def __init__(self, max_workers: int = 4, parent: Optional[QtCore.QObject] = None) -> None:
        super().__init__(parent)
        self.executor = ThreadPoolExecutor(max_workers=max_workers)
        self._futures = set()

    def translate_async(self, src: str, dst: str, text: str, tag: Any = None) -> None:
        if not text:
            # Emit empty immediately
            self.translation_ready.emit(src, dst, text, "", tag)
            return

        future = self.executor.submit(self._translate, src, dst, text)
        self._futures.add(future)

        def _done(fut):
            self._futures.discard(fut)
            try:
                res = fut.result()
            except Exception:
                res = text
            # Emit the result (Qt will handle thread crossing)
            self.translation_ready.emit(src, dst, text, res, tag)

        future.add_done_callback(_done)

    @staticmethod
    def _translate(src: str, dst: str, text: str) -> str:
        return translate_text(src, dst, text)

    def shutdown(self) -> None:
        for f in list(self._futures):
            try:
                f.cancel()
            except Exception:
                pass
        self.executor.shutdown(wait=False)
