APP_STYLESHEET = """
QWidget {
    font-family: "Microsoft YaHei UI", "Segoe UI";
    font-size: 13px;
    color: #243044;
}
QMainWindow, QDialog {
    background: #f5f7fb;
}
QFrame#TopHeader {
    background: #ffffff;
    border: none;
    border-bottom: 1px solid #e5ebf4;
}
QPushButton#TopCategory {
    background: transparent;
    border: none;
    border-radius: 9px;
    color: #526078;
    font-size: 13px;
    font-weight: 600;
    min-height: 36px;
    padding: 0 9px;
}
QPushButton#TopCategory:hover {
    background: #eef3fb;
    color: #315fbd;
}
QPushButton#TopCategory:checked {
    background: #eaf0ff;
    color: #2859bd;
}
QLabel#PageTitle {
    color: #1f2d43;
    font-size: 22px;
    font-weight: 700;
}
QLabel#PageSubtitle {
    color: #6a788e;
    font-size: 12px;
}
QLabel#TopBrandTitle {
    color: #1f2d43;
    font-size: 19px;
    font-weight: 700;
}
QLabel#TopBrandSubtitle, QLabel#TopUserLabel {
    color: #7a879b;
    font-size: 11px;
}
QLabel#SectionTitle {
    color: #263750;
    font-size: 16px;
    font-weight: 700;
}
QLabel#FieldLabel {
    color: #526078;
    font-size: 12px;
    font-weight: 600;
}
QFrame#PageActionBar {
    background: #ffffff;
    border: 1px solid #e1e8f2;
    border-radius: 12px;
}
QFrame#FilterBar {
    background: #ffffff;
    border: 1px solid #e3e9f2;
    border-radius: 12px;
}
QLabel#EmptyState {
    color: #8a96a9;
    padding: 28px;
}
QLabel#StatusBadge {
    border-radius: 9px;
    padding: 2px 8px;
    font-size: 11px;
    font-weight: 600;
    background: #eef2f8;
    color: #68768d;
}
QLabel#StatusBadge[tone="success"] {
    background: #e9f7ef;
    color: #227348;
}
QLabel#StatusBadge[tone="warning"] {
    background: #fff4e5;
    color: #9a5b22;
}
QLabel#StatusBadge[tone="danger"] {
    background: #fff0f2;
    color: #b72f3c;
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
QFrame#Card, QGroupBox#Card {
    background: #ffffff;
    border: 1px solid #e3e9f3;
    border-radius: 14px;
}
QWidget#CardContent {
    background: #ffffff;
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
    border: 1px solid #d5deeb;
    border-radius: 9px;
    padding: 8px 10px;
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
    background: #f0f4fa;
    border: 1px solid #d8e1ee;
    border-radius: 9px;
    padding: 7px 14px;
    min-height: 20px;
}
QPushButton:hover {
    background: #e2eaf5;
}
QPushButton:pressed {
    background: #d6e1ef;
}
QPushButton#PrimaryButton {
    background: #5276e6;
    color: white;
    border: none;
    font-weight: 700;
    padding: 9px 18px;
}
QPushButton#PrimaryButton:hover {
    background: #4266d5;
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
    border: 1px solid #e1e8f2;
    border-radius: 12px;
    gridline-color: #eef2f7;
    selection-background-color: #eaf0ff;
    selection-color: #243044;
}
QHeaderView::section {
    background: #f6f8fc;
    border: none;
    border-bottom: 1px solid #dce4ef;
    padding: 9px 8px;
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
    background: #ffffff;
    border-top: 1px solid #e7edf5;
    color: #637086;
}
QToolTip {
    background: #1e2c43;
    color: #ffffff;
    border: none;
    padding: 5px;
}
"""
