import frida
from PySide6 import QtCore


class FridaWorker(QtCore.QThread):
    """Run a Frida script and emit captured text."""

    text_ready = QtCore.Signal(str)

    def __init__(self, pid: int, parent=None):
        super().__init__(parent)
        self.pid = pid
        self._session = None
        self._script = None
        self._running = False

    def run(self):
        self._running = True
        try:
            self._session = frida.attach(self.pid)
            js = """
            // Frida JavaScript code to hook text rendering functions
            // This is a placeholder; actual implementation depends on the target application
            rpc.exports = {
                // Exported functions can be defined here
            };
            """
            self._script = self._session.create_script(js)
            self._script.on("message", self.on_message)
            self._script.load()
        except Exception as e:
            print("[FRIDA] Failed to start:", e)
            return

        while self._running:
            QtCore.QThread.msleep(100)

    def on_message(self, message, data):
        if message.get("type") == "send":
            text = message.get("payload")
            if text:
                self.text_ready.emit(text)

    def stop(self):
        self._running = False
        try:
            if self._script:
                self._script.unload()
            if self._session:
                self._session.detach()
        except Exception:
            pass
        try:
            self.wait(1000)
        except Exception:
            pass
