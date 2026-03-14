import unittest
from PySide6 import QtWidgets, QtGui


'''Test to check that fonts can be correctly applied to text display'''
'''Useful for determining if a given font (currently Chiller) is valid for the implemented text display window'''
'''Could be extended to checking for different languages'''
class TestFont(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance()
        if cls.app is None:
            cls.app = QtWidgets.QApplication([])
    def setUp(self):
        from main import DisplayWindow, SettingsWindow
        self.DisplayWindow = DisplayWindow
        self.SettingsWindow = SettingsWindow
        self.display = DisplayWindow()
        self.settings = SettingsWindow(self.display)
    def tearDown(self):
        self.settings.close()
        self.display.close()
    def test_font(self):
        new_font = QtGui.QFont("Chiller")
        self.settings.font_combo.setCurrentFont(new_font)
        self.settings.on_save()
        self.assertEqual(self.display.label.font().family(), "Chiller")

if __name__ == "__main__":
    unittest.main()