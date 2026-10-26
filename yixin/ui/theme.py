APP_STYLESHEET = """
QWidget {
    font-family: "Microsoft YaHei UI", "Segoe UI";
    font-size: 13px;
    color: #243044;
}
QMainWindow, QDialog {
    background: #f3f6fb;
}
QFrame#Sidebar {
    background: #152238;
    border: none;
}
QLabel#BrandTitle {
    color: #ffffff;
    font-size: 21px;
    font-weight: 700;
}
QLabel#BrandSubtitle {
    color: #9fb0ca;
    font-size: 11px;
}
QPushButton#NavButton {
    background: transparent;
    border: none;
    border-radius: 9px;
    color: #d8e2f0;
    padding: 0 16px;
    text-align: left;
    font-size: 14px;
}
QPushButton#NavButton:hover {
    background: #233552;
}
QPushButton#NavButton:checked {
    background: #2e5fb7;
    color: #ffffff;
    font-weight: 600;
}
QFrame#Card, QGroupBox#Card {
    background: #ffffff;
    border: 1px solid #e3e9f2;
    border-radius: 12px;
}
QGroupBox {
    font-weight: 600;
    margin-top: 10px;
    padding-top: 12px;
}
QGroupBox::title {
    subcontrol-origin: margin;
    left: 12px;
    padding: 0 5px;
    color: #44526b;
}
QLineEdit, QTextEdit, QPlainTextEdit, QComboBox, QSpinBox {
    background: #fbfcff;
    border: 1px solid #ccd6e4;
    border-radius: 7px;
    padding: 7px 9px;
    selection-background-color: #3f73cb;
}
QLineEdit:focus, QTextEdit:focus, QPlainTextEdit:focus, QComboBox:focus, QSpinBox:focus {
    border: 1px solid #3f73cb;
}
QComboBox::drop-down {
    border: none;
    width: 24px;
}
QPushButton {
    background: #edf2f9;
    border: 1px solid #d2dcea;
    border-radius: 7px;
    padding: 7px 13px;
}
QPushButton:hover {
    background: #e2eaf5;
}
QPushButton:pressed {
    background: #d6e1ef;
}
QPushButton#PrimaryButton {
    background: #2e64c4;
    color: white;
    border: none;
    font-weight: 700;
    padding: 10px 16px;
}
QPushButton#PrimaryButton:hover {
    background: #2557ad;
}
QPushButton#DangerButton {
    color: #b72f3c;
    border-color: #e9b6bb;
    background: #fff5f6;
}
QPushButton#GhostButton {
    background: transparent;
    border: 1px solid #637089;
    color: white;
}
QListWidget#ImageStrip {
    background: #f8fafd;
    border: 1px solid #e3e9f2;
    border-radius: 8px;
    outline: none;
}
QListWidget#ImageStrip::item {
    border: 2px solid transparent;
    border-radius: 7px;
    margin: 3px;
}
QListWidget#ImageStrip::item:selected {
    border: 2px solid #3f73cb;
    background: #eaf1fd;
}
QListWidget#WorkList {
    background: #ffffff;
    border: 1px solid #e3e9f2;
    border-radius: 10px;
    outline: none;
}
QListWidget#WorkList::item {
    border: 2px solid transparent;
    border-radius: 8px;
    padding: 4px;
}
QListWidget#WorkList::item:selected {
    border: 2px solid #3f73cb;
    background: #eaf1fd;
}
QTableWidget {
    background: #ffffff;
    border: 1px solid #e3e9f2;
    border-radius: 8px;
    gridline-color: #edf1f6;
    selection-background-color: #e6eefe;
    selection-color: #243044;
}
QHeaderView::section {
    background: #f3f6fb;
    border: none;
    border-bottom: 1px solid #dce4ef;
    padding: 7px;
    font-weight: 600;
}
QTabWidget::pane {
    border: 1px solid #e3e9f2;
    background: white;
    border-radius: 8px;
}
QTabBar::tab {
    padding: 8px 15px;
    background: #eef2f8;
    margin-right: 3px;
    border-top-left-radius: 7px;
    border-top-right-radius: 7px;
}
QTabBar::tab:selected {
    background: white;
    color: #245ab8;
}
QScrollArea {
    border: none;
    background: transparent;
}
QStatusBar {
    background: #eef2f8;
    color: #526078;
}
QToolTip {
    background: #1e2c43;
    color: #ffffff;
    border: none;
    padding: 5px;
}
"""
