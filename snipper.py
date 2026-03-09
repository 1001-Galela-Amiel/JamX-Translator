"""File for creating an in-program "snipper" function, used for manual OCR via QT (probably better ways, but seemed most convenient with current structure)"""
import mss
from PySide6 import QtWidgets, QtCore
from PIL import Image
class Snipper(QtWidgets.QWidget):
    image_captured = QtCore.Signal(object)
    def __init__(self):

        super().__init__()
        self.setWindowFlags(QtCore.Qt.WindowType.FramelessWindowHint 
                            | QtCore.Qt.WindowType.WindowStaysOnTopHint)
        self.setWindowState(QtCore.Qt.WindowState.WindowFullScreen)
        self.setWindowOpacity(0.3)
        self.setCursor(QtCore.Qt.CursorShape.CrossCursor)
        self.rubberband = QtWidgets.QRubberBand(QtWidgets.QRubberBand.Shape.Rectangle, self)
        self.origin = QtCore.QPoint()

    def mousePressEvent(self, event):
        self.origin = event.pos()
        self.rubberband.setGeometry(QtCore.QRect(self.origin.x(), self.origin.y(), 0, 0))
        self.rubberband.show()

    def mouseMoveEvent(self, event):
        self.rubberband.setGeometry(QtCore.QRect(self.origin, event.pos()).normalized())

    def mouseReleaseEvent(self, event):
        self.rubberband.hide()
        rect = QtCore.QRect(self.origin, event.pos()).normalized()
        self.close()
        self.capture(rect)

    def capture(self, rect):
        with mss.mss() as sct:
            monitor = {"top": rect.top(), "left": rect.left(),
                       "width": rect.width(), "height": rect.height()}
            screenshot = sct.grab(monitor)
            img = Image.frombytes("RGB", screenshot.size, screenshot.bgra, "raw", "BGRX")
            img.save("snip.png")
            self.image_captured.emit(img)
