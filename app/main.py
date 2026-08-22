import sys
from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QPalette, QColor
from .window import MainWindow

def main() -> int:
    if "--cli" in sys.argv:
        sys.argv.remove("--cli")
        from uploader import main as uploader_main
        return uploader_main()
    app = QApplication(sys.argv)
    app.setApplicationName("TeleDrive")
    app.setApplicationDisplayName("TeleDrive")
    app.setOrganizationName("TeleDrive")
    app.setStyle("Fusion")
    palette = app.palette()
    palette.setColor(QPalette.ColorRole.Window, QColor("#f7f6f2"))
    palette.setColor(QPalette.ColorRole.Base, QColor("#ffffff"))
    palette.setColor(QPalette.ColorRole.AlternateBase, QColor("#f3f1eb"))
    palette.setColor(QPalette.ColorRole.Text, QColor("#202126"))
    palette.setColor(QPalette.ColorRole.WindowText, QColor("#202126"))
    palette.setColor(QPalette.ColorRole.ButtonText, QColor("#202126"))
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor("#202126"))
    app.setPalette(palette)
    window = MainWindow()
    window.show()
    return app.exec()

if __name__ == "__main__":
    raise SystemExit(main())
