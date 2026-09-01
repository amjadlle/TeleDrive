from __future__ import annotations

from pathlib import Path
import json
import sqlite3
import sys
from urllib.request import Request, urlopen

import yaml
from PySide6.QtCore import QProcess, QTimer, Qt, QPoint, QThread, Signal, QUrl
from PySide6.QtGui import QFont, QColor, QBrush
from PySide6.QtWidgets import (
    QAbstractItemView, QFileDialog, QFormLayout, QFrame, QHBoxLayout, QLabel,
    QLineEdit, QMainWindow, QMessageBox, QPlainTextEdit, QProgressBar,
    QPushButton, QSpinBox, QStackedWidget, QTableWidget, QTableWidgetItem,
    QVBoxLayout, QWidget, QInputDialog, QComboBox, QScrollArea, QHeaderView, QGridLayout,
)
from PySide6.QtGui import QDesktopServices

from .paths import app_data_dir, config_path, state_path

STYLE = """
QMainWindow { background: transparent; }
QWidget { background: #eef3f8; color: #172235; }
#appShell { background: #eef3f8; border-radius: 22px; }
QLabel { color: #202126; background: transparent; }
QFrame#sidebar, QFrame#card { background: #f9fbfe; border: 1px solid #d9e3ef; border-radius: 22px; }
QLabel#brand { color: #202126; font-size: 22px; font-weight: 700; }
QLabel#eyebrow { color: #77736d; font-size: 12px; font-weight: 600; }
QLabel#sectionTitle { color: #41688f; font-size: 13px; font-weight: 700; padding-top: 10px; padding-bottom: 2px; }
QLabel#fieldHint { color: #77736d; font-size: 11px; }
QLabel#statusBadge { color: #327454; background: #e7f4ec; border: 1px solid #cbe7d5; border-radius: 10px; padding: 5px 9px; font-size: 11px; font-weight: 700; }
QPushButton { background: #22252b; color: white; border: 0; border-radius: 9px; padding: 10px 16px; font-weight: 600; }
QPushButton:hover { background: #3a3f48; }
QPushButton#secondary { background: #e7eef6; color: #20344d; border: 1px solid #d5e0ec; }
QPushButton#secondary:hover { background: #dce8f4; }
QPushButton#coffeeButton { background: #ffdd00; color: #172235; border: 1px solid #e5c700; font-weight: 700; border-radius: 9px; padding: 8px 12px; }
QPushButton#coffeeButton:hover { background: #ffe633; border-color: #ffd000; }
QPushButton#danger { background: #b63838; }
QPushButton#danger:hover { background: #982d2d; }
QLabel#legend { color: #77736d; background: #f4f7fb; border: 1px solid #d9e3ef; border-radius: 10px; padding: 9px 12px; font-size: 11px; }
QLineEdit, QSpinBox, QComboBox { background: #ffffff; color: #202126; selection-background-color: #a9d8ff; selection-color: #172235; border: 1px solid #d2deeb; border-radius: 10px; padding: 8px 10px; min-height: 18px; }
QLineEdit:focus, QSpinBox:focus, QComboBox:focus { border: 1px solid #6ca9d7; background: #fbfdff; }
QSpinBox { padding-right: 32px; }
QSpinBox::up-button, QSpinBox::down-button { subcontrol-origin: border; width: 24px; border: 0; border-left: 1px solid #c4d3e3; background: #e8f0f8; }
QSpinBox::up-button { subcontrol-position: top right; border-top-right-radius: 9px; }
QSpinBox::down-button { subcontrol-position: bottom right; border-bottom-right-radius: 9px; }
QSpinBox::up-button:hover, QSpinBox::down-button:hover { background: #d5e5f3; }
QComboBox QAbstractItemView { background: white; color: #202126; border: 1px solid #dcd8d0; selection-background-color: #eeeae2; }
QScrollArea { border: 0; background: transparent; }
QScrollBar:vertical { background: transparent; width: 10px; margin: 4px 0; }
QScrollBar::handle:vertical { background: #d6d1c8; border-radius: 5px; min-height: 30px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QTableWidget, QTableWidget QTableCornerButton::section { background: white; color: #202126; border: 1px solid #e8e5de; border-radius: 14px; gridline-color: #efede7; }
QTableWidget { alternate-background-color: #fbfaf7; selection-background-color: #f1e4d0; selection-color: #202126; }
QTableWidget::item { color: #202126; padding: 3px 8px; border-bottom: 1px solid #f0eee9; }
QHeaderView::section { background: #f3f1eb; color: #514d46; border: 0; padding: 10px 8px; font-size: 12px; font-weight: 700; }
QProgressBar { background: #ede9e1; border: 0; border-radius: 6px; height: 12px; text-align: center; }
QProgressBar { color: #202126; }
QProgressBar::chunk { background: #ffad43; border-radius: 6px; }
QPlainTextEdit { background: #171a1f; color: #d9e3ee; border: 0; border-radius: 12px; padding: 12px; font-family: Consolas; }
QPlainTextEdit#activity { padding: 18px; }
QFrame#titleBar { background: #ffffff; border: 1px solid #e8e5de; border-radius: 16px; }
QPushButton#windowButton, QPushButton#maximizeButton, QPushButton#closeButton { background: transparent; color: #25252a; border: 0; border-radius: 8px; padding: 2px 0 0 0; min-width: 36px; max-width: 36px; min-height: 36px; max-height: 36px; font-family: "Segoe UI"; font-size: 18px; font-weight: 400; }
QPushButton#closeButton { font-size: 21px; }
QPushButton#windowButton:hover, QPushButton#maximizeButton:hover { background: #f0eee9; color: #25252a; }
QPushButton#closeButton:hover { background: #f4dede; color: #a52d2d; }
QPushButton#menuButton, QPushButton#maximizeButton, QPushButton#closeButton { background: transparent; color: #25252a; border: 0; }
QPushButton#menuButton { width: 42px; font-size: 22px; font-weight: 500; }
QPushButton#menuButton:hover { background: transparent; color: #25252a; }
"""

TABLE_ROW_LIMIT = 100
APP_VERSION = "1.0.6"
RELEASES_API_URL = "https://api.github.com/repos/amjadlle/TeleDrive/releases/latest"
UPDATE_CHECK_INTERVAL_MS = 24 * 60 * 60 * 1000


class UpdateCheckWorker(QThread):
    update_available = Signal(str, str)

    def run(self) -> None:
        try:
            request = Request(RELEASES_API_URL, headers={"Accept": "application/vnd.github+json", "User-Agent": "TeleDrive"})
            with urlopen(request, timeout=5) as response:
                release = json.load(response)
            latest = str(release.get("tag_name", "")).strip().lstrip("v")
            release_url = str(release.get("html_url", "")).strip()
            if latest and release_url and self._version_key(latest) > self._version_key(APP_VERSION):
                self.update_available.emit(latest, release_url)
        except Exception:
            # Update checks are optional and must never affect app startup.
            return

    @staticmethod
    def _version_key(version: str) -> tuple[int, ...]:
        try:
            return tuple(int(part) for part in version.split("."))
        except ValueError:
            return ()


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.Window)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setWindowTitle(f"TeleDrive v{APP_VERSION}")
        self.resize(1220, 780)
        self.setMinimumSize(980, 650)
        self.setStyleSheet(STYLE)
        self.process = QProcess(self)
        self.process.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        self.process.readyReadStandardOutput.connect(self._read_process_output)
        self.process.finished.connect(self._process_finished)
        self.loop_timer = QTimer(self)
        self.loop_timer.timeout.connect(lambda: self._start_uploader("--run-once"))
        self.awaiting_login_prompt = False
        self.pending_login_mode: str | None = None
        self.login_code: str | None = None
        self.auth_dir = app_data_dir() / "auth"
        self.auth_dir.mkdir(parents=True, exist_ok=True)
        self._build_ui()
        self._load_settings()
        self._refresh_all()
        self._update_worker: UpdateCheckWorker | None = None
        QTimer.singleShot(2000, self._check_for_updates)
        self.refresh_timer = QTimer(self)
        self.refresh_timer.timeout.connect(self._refresh_all)
        self.refresh_timer.start(3000)
        self.update_timer = QTimer(self)
        self.update_timer.timeout.connect(self._check_for_updates)
        self.update_timer.start(UPDATE_CHECK_INTERVAL_MS)

    def _build_ui(self) -> None:
        root = QWidget(objectName="appShell")
        outer = QVBoxLayout(root); outer.setContentsMargins(12, 12, 12, 12); outer.setSpacing(10)
        self._outer_layout = outer
        outer.addWidget(self._build_title_bar())
        layout = QHBoxLayout(); layout.setContentsMargins(0, 0, 0, 0); layout.setSpacing(18)
        self._sidebar = self._build_sidebar(); layout.addWidget(self._sidebar)
        self.pages = QStackedWidget()
        self.pages.addWidget(self._build_dashboard())
        self.pages.addWidget(self._build_queue_page())
        self.pages.addWidget(self._build_history_page())
        self.pages.addWidget(self._build_settings_page())
        self.pages.addWidget(self._build_logs_page())
        layout.addWidget(self.pages, 1); outer.addLayout(layout, 1)
        self.setCentralWidget(root)

    def _build_title_bar(self) -> QFrame:
        bar = QFrame(objectName="titleBar"); bar.setFixedHeight(52)
        layout = QHBoxLayout(bar); layout.setContentsMargins(12, 8, 8, 8); layout.setSpacing(0)
        menu = QPushButton("≡", objectName="menuButton"); menu.setToolTip("Toggle sidebar")
        menu.clicked.connect(lambda: self._toggle_sidebar())
        layout.addWidget(menu)
        title = QLabel(f"TeleDrive v{APP_VERSION}"); title.setStyleSheet("font-weight: 700; color: #25252a;")
        layout.addStretch(); layout.addWidget(title); layout.addStretch()
        minimize = QPushButton("-", objectName="windowButton"); minimize.clicked.connect(self.showMinimized); layout.addWidget(minimize)
        maximize = QPushButton("□", objectName="maximizeButton"); maximize.clicked.connect(self._toggle_maximized); layout.addWidget(maximize)
        close = QPushButton("×", objectName="windowButton"); close.setObjectName("closeButton"); close.clicked.connect(self.close); layout.addWidget(close)
        for button in (menu, minimize, maximize, close):
            button.setCursor(Qt.CursorShape.PointingHandCursor)
        bar.mousePressEvent = self._title_press; bar.mouseMoveEvent = self._title_move
        self._title_bar = bar; self._drag_position = QPoint()
        return bar

    def _toggle_sidebar(self) -> None:
        self._sidebar.setVisible(not self._sidebar.isVisible())

    def _toggle_maximized(self) -> None:
        if self.isMaximized():
            self.showNormal()
            self._set_floating_shell(True)
        else:
            self.showMaximized()
            self._set_floating_shell(False)

    def _set_floating_shell(self, floating: bool) -> None:
        self._outer_layout.setContentsMargins(12, 12, 12, 12) if floating else self._outer_layout.setContentsMargins(0, 0, 0, 0)
        self._outer_layout.setSpacing(10 if floating else 0)
        self.centralWidget().setStyleSheet("#appShell { border-radius: 22px; }" if floating else "#appShell { border-radius: 0px; }")
        self._title_bar.setStyleSheet("QFrame#titleBar { background: #ffffff; border: 1px solid #e8e5de; border-radius: 16px; }" if floating else "QFrame#titleBar { background: #ffffff; border: 1px solid #e8e5de; border-radius: 0px; }")

    def _title_press(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_position = event.globalPosition().toPoint() - self.frameGeometry().topLeft()

    def _title_move(self, event) -> None:
        if event.buttons() & Qt.MouseButton.LeftButton and not self.isMaximized():
            self.move(event.globalPosition().toPoint() - self._drag_position)

    def _build_sidebar(self) -> QFrame:
        sidebar = QFrame(objectName="sidebar")
        sidebar.setFixedWidth(224)
        layout = QVBoxLayout(sidebar)
        layout.setContentsMargins(18, 20, 18, 18)
        brand = QLabel("TeleDrive")
        brand.setObjectName("brand")
        layout.addWidget(brand)
        layout.addWidget(QLabel("PERSONAL TELEGRAM CLOUD", objectName="eyebrow"))
        layout.addSpacing(18)
        self.nav_buttons: list[QPushButton] = []
        for index, label in enumerate(("Dashboard", "Upload Queue", "History", "Settings", "Activity Log")):
            button = QPushButton(label)
            button.setObjectName("secondary")
            button.clicked.connect(lambda _=False, i=index: self._show_page(i))
            layout.addWidget(button)
            self.nav_buttons.append(button)
        layout.addStretch()
        coffee_btn = QPushButton("☕ Buy Me a Coffee")
        coffee_btn.setObjectName("coffeeButton")
        coffee_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        coffee_btn.setToolTip("Support TeleDrive development")
        coffee_btn.clicked.connect(lambda: QDesktopServices.openUrl(QUrl("https://buymeacoffee.com/amjadlle")))
        layout.addWidget(coffee_btn)
        layout.addSpacing(6)
        self.status_badge = QLabel("Ready", objectName="statusBadge")
        layout.addWidget(self.status_badge)
        layout.addWidget(QLabel("Private · Resumable · Local", objectName="eyebrow"))
        return sidebar

    def _title(self, title: str, subtitle: str) -> QVBoxLayout:
        layout = QVBoxLayout()
        heading = QLabel(title)
        heading.setFont(QFont("Segoe UI", 25, QFont.Weight.Bold))
        layout.addWidget(heading)
        layout.addWidget(QLabel(subtitle, objectName="eyebrow"))
        return layout

    def _card(self) -> tuple[QFrame, QVBoxLayout]:
        card = QFrame(objectName="card")
        box = QVBoxLayout(card)
        box.setContentsMargins(20, 18, 20, 18)
        return card, box

    def _build_dashboard(self) -> QWidget:
        page = QWidget(); layout = QVBoxLayout(page); layout.setSpacing(16)
        layout.addLayout(self._title("Welcome back", "Turn Telegram into your personal unlimited cloud storage."))
        card, box = self._card()
        self.stats_label = QLabel("No uploads yet")
        self.stats_label.setFont(QFont("Segoe UI", 18, QFont.Weight.DemiBold))
        self.stats_detail = QLabel("Set up your account and source folder to get started.", objectName="eyebrow")
        self.progress = QProgressBar()
        box.addWidget(self.stats_label); box.addWidget(self.stats_detail); box.addSpacing(6); box.addWidget(self.progress)
        layout.addWidget(card)
        actions = QHBoxLayout()
        self.run_button = QPushButton("Run upload batch")
        self.run_button.clicked.connect(lambda: self._start_uploader("--run-once"))
        scan = QPushButton("Scan folder"); scan.setObjectName("secondary"); scan.clicked.connect(lambda: self._start_uploader("--scan-only"))
        loop = QPushButton("Start automatic loop"); loop.setObjectName("secondary"); loop.clicked.connect(self._start_loop)
        self.stop_loop_button = QPushButton("Stop loop"); self.stop_loop_button.setObjectName("secondary"); self.stop_loop_button.clicked.connect(self._stop_loop)
        self.stop_button = QPushButton("Stop"); self.stop_button.setObjectName("danger"); self.stop_button.clicked.connect(self._stop_uploader)
        self.stop_loop_button.setEnabled(False)
        self.stop_button.setEnabled(False)
        clear = QPushButton("Clear activity"); clear.setObjectName("secondary"); clear.clicked.connect(self._clear_activity)
        reset = QPushButton("Reset all"); reset.setObjectName("danger"); reset.clicked.connect(self._reset_all)
        for button, tip in ((self.run_button, "Upload queued files now."), (scan, "Scan the source folder and update the queue."), (loop, "Run uploads automatically at the selected interval."), (self.stop_loop_button, "Turn off the automatic loop."), (self.stop_button, "Stop the current upload process."), (clear, "Clear only this activity panel."), (reset, "Delete all tracked file history and start fresh.")):
            button.setToolTip(tip)
        actions.addWidget(self.run_button); actions.addWidget(scan); actions.addWidget(loop); actions.addWidget(self.stop_loop_button); actions.addWidget(self.stop_button); actions.addWidget(clear); actions.addWidget(reset); actions.addStretch()
        layout.addLayout(actions)
        layout.addWidget(QLabel("Live activity", objectName="eyebrow"))
        self.activity = QPlainTextEdit(); self.activity.setObjectName("activity"); self.activity.setReadOnly(True)
        self.activity.setPlaceholderText("No activity yet\n\nRun a batch or scan your source folder to see live progress here.")
        self.activity.setCenterOnScroll(False)
        layout.addWidget(self.activity, 1)
        layout.addWidget(QLabel("Legend: Run batch = upload queued files  •  Scan = find new files  •  Loop = repeat automatically  •  Stop = cancel current work  •  Clear activity = clear messages  •  Reset all = remove tracked history", objectName="legend"))
        return page

    def _table_page(self, title: str, subtitle: str) -> tuple[QWidget, QTableWidget]:
        page = QWidget(); layout = QVBoxLayout(page); layout.setSpacing(16)
        layout.addLayout(self._title(title, subtitle))
        table = QTableWidget(0, 4)
        table.setHorizontalHeaderLabels(("File", "Status", "Attempts", "Updated"))
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.setAlternatingRowColors(True)
        table.setShowGrid(False)
        table.setWordWrap(False)
        table.verticalHeader().setVisible(False)
        table.verticalHeader().setDefaultSectionSize(42)
        header = table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        header.setMinimumSectionSize(90)
        layout.addWidget(table, 1)
        return page, table

    def _build_queue_page(self) -> QWidget:
        page, self.queue_table = self._table_page("Upload Queue", "Pending and retryable items")
        return page

    def _build_history_page(self) -> QWidget:
        page, self.history_table = self._table_page("History", "Uploaded files and items that need attention")
        return page

    def _build_settings_page(self) -> QWidget:
        page = QWidget(); layout = QVBoxLayout(page); layout.setSpacing(16)
        layout.addLayout(self._title("Settings", "Stored privately in your Windows app-data folder"))
        scroll = QScrollArea(); scroll.setWidgetResizable(True); scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        content = QWidget(); content_layout = QVBoxLayout(content); content_layout.setContentsMargins(0, 0, 4, 0)
        card, form_box = self._card(); form = QFormLayout(); form.setHorizontalSpacing(18); form.setVerticalSpacing(10)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.DontWrapRows)
        self.api_id = QLineEdit(); self.api_hash = QLineEdit(); self.api_hash.setEchoMode(QLineEdit.EchoMode.Password)
        self.phone = QLineEdit(); self.phone.setPlaceholderText("+country code and number")
        self.target = QLineEdit(); self.target.setPlaceholderText("me, @channel, or channel ID")
        self.source = QLineEdit(); self.source.setPlaceholderText("Select the folder containing files to upload")
        for text_field in (self.api_id, self.api_hash, self.phone, self.target):
            text_field.setFixedWidth(420)
        self.source.setFixedWidth(300)
        choose = QPushButton("Choose folder"); choose.setObjectName("secondary"); choose.setFixedWidth(112); choose.clicked.connect(self._choose_folder)
        source_row = QHBoxLayout(); source_row.setSpacing(8); source_row.addWidget(self.source); source_row.addWidget(choose); source_row.addStretch()
        self.per_run = QSpinBox(); self.per_run.setRange(1, 10000)
        self.per_day = QSpinBox(); self.per_day.setRange(1, 50000)
        self.sleep_min = QSpinBox(); self.sleep_min.setRange(1, 86400); self.sleep_min.setSuffix(" sec")
        self.sleep_max = QSpinBox(); self.sleep_max.setRange(1, 86400); self.sleep_max.setSuffix(" sec")
        self.run_interval = QSpinBox(); self.run_interval.setRange(1, 10080); self.run_interval.setSuffix(" min")
        self.retry_attempts = QSpinBox(); self.retry_attempts.setRange(1, 50)
        self.backoff = QSpinBox(); self.backoff.setRange(0, 3600); self.backoff.setSuffix(" sec")
        self.flood_buffer = QSpinBox(); self.flood_buffer.setRange(0, 3600); self.flood_buffer.setSuffix(" sec")
        for numeric in (self.per_run, self.per_day, self.sleep_min, self.sleep_max, self.run_interval, self.retry_attempts, self.backoff, self.flood_buffer):
            numeric.setFixedWidth(150)
        self.send_mode = QComboBox(); self.send_mode.setFixedWidth(300); self.send_mode.addItem("Document (file attachment)", "document"); self.send_mode.addItem("Media (photo/video)", "media"); self.send_mode.addItem("Auto (Telegram default)", "auto")
        form.addRow(QLabel("Telegram connection", objectName="sectionTitle"))
        form.addRow("Telegram API ID", self.api_id); form.addRow("Telegram API hash", self.api_hash)
        form.addRow("Phone number", self.phone); form.addRow("Target channel / Saved Messages", self.target)
        form.addRow(QLabel("Upload source", objectName="sectionTitle"))
        form.addRow("Source folder", source_row)
        upload_limits = QGridLayout(); upload_limits.setHorizontalSpacing(18); upload_limits.setVerticalSpacing(10)
        upload_limits.setColumnStretch(1, 1); upload_limits.setColumnStretch(3, 1)
        upload_limits.addWidget(QLabel("Files per run"), 0, 0); upload_limits.addWidget(self.per_run, 0, 1)
        upload_limits.addWidget(QLabel("Files per day"), 0, 2); upload_limits.addWidget(self.per_day, 0, 3)
        for column in (0, 2):
            upload_limits.itemAtPosition(0, column).widget().setMinimumWidth(0)
        form.addRow(upload_limits)
        form.addRow(QLabel("Timing & reliability", objectName="sectionTitle"))
        timing = QGridLayout(); timing.setHorizontalSpacing(18); timing.setVerticalSpacing(10)
        timing.setColumnStretch(1, 1); timing.setColumnStretch(3, 1)
        timing_rows = (("Delay between uploads (minimum)", self.sleep_min, "Delay between uploads (maximum)", self.sleep_max), ("Delay between automatic runs", self.run_interval, "Retry attempts per file", self.retry_attempts), ("Retry backoff base", self.backoff, "Flood-wait buffer", self.flood_buffer))
        for row, (left_label, left_widget, right_label, right_widget) in enumerate(timing_rows):
            left = QLabel(left_label); left.setWordWrap(True); left.setMaximumWidth(210)
            right = QLabel(right_label); right.setWordWrap(True); right.setMaximumWidth(210)
            timing.addWidget(left, row, 0); timing.addWidget(left_widget, row, 1)
            timing.addWidget(right, row, 2); timing.addWidget(right_widget, row, 3)
        for column in (0, 2):
            timing.setColumnMinimumWidth(column, 120)
        form.addRow(timing)
        form.addRow(QLabel("File handling", objectName="sectionTitle"))
        form.addRow("Send files as", self.send_mode)
        form_box.addLayout(form)
        quick_help = QLabel("Quick help: API ID/hash identify your Telegram account. Source folder is scanned for files. Files per run/day control limits. Timing values control delays. Retry settings control recovery after failures. Save changes before starting an upload.", objectName="legend")
        quick_help.setMaximumWidth(420); quick_help.setWordWrap(True)
        form_box.addWidget(quick_help)
        save = QPushButton("Save settings"); save.setFixedWidth(420); save.setMinimumHeight(42); save.clicked.connect(self._save_settings); form_box.addWidget(save)
        for widget, tip in ((self.api_id, "Telegram API ID from my.telegram.org."), (self.api_hash, "Telegram API hash from my.telegram.org."), (self.source, "Folder that TeleDrive scans for files."), (self.run_interval, "How often the automatic loop starts a new run."), (self.retry_attempts, "How many times a failed upload is retried."), (self.send_mode, "Choose whether files are sent as documents or media.")):
            widget.setToolTip(tip)
        content_layout.addWidget(card)
        config_note = QLabel(f"Config location: {config_path()}", objectName="fieldHint"); config_note.setMaximumWidth(420)
        content_layout.addWidget(config_note)
        help_note = QLabel("Help is also shown here so it is always visible, even when a tooltip disappears.", objectName="legend")
        help_note.setMaximumWidth(420); help_note.setWordWrap(True)
        content_layout.addWidget(help_note)
        content_layout.addStretch()
        scroll.setWidget(content)
        layout.addWidget(scroll, 1)
        return page

    def _build_logs_page(self) -> QWidget:
        page = QWidget(); layout = QVBoxLayout(page); layout.setSpacing(16)
        layout.addLayout(self._title("Activity Log", "Recent uploader output"))
        self.log_view = QPlainTextEdit(); self.log_view.setReadOnly(True); layout.addWidget(self.log_view, 1)
        return page

    def _load_settings(self) -> None:
        raw = yaml.safe_load(config_path().read_text(encoding="utf-8")) or {}
        telegram, upload = raw.get("telegram", {}), raw.get("upload", {})
        api_id = int(telegram.get("api_id", 0) or 0)
        self.api_id.setText(str(api_id) if api_id else "")
        self.api_hash.setText(str(telegram.get("api_hash", "")))
        self.phone.setText(str(telegram.get("phone", "")))
        self.target.setText(str(telegram.get("target", "me")))
        self.source.setText(str(upload.get("source_dir", "")))
        self.per_run.setValue(int(upload.get("max_files_per_run", 3)))
        self.per_day.setValue(int(upload.get("max_files_per_day", 100)))
        self.sleep_min.setValue(int(upload.get("sleep_min_seconds", 60)))
        self.sleep_max.setValue(int(upload.get("sleep_max_seconds", 90)))
        self.run_interval.setValue(int(raw.get("app", {}).get("run_interval_minutes", 60)))
        self.retry_attempts.setValue(int(upload.get("retry_attempts", 5)))
        self.backoff.setValue(int(upload.get("backoff_base_seconds", 10)))
        self.flood_buffer.setValue(int(upload.get("floodwait_buffer_seconds", 5)))
        mode = str(upload.get("send_mode", "document")).lower()
        self.send_mode.setCurrentIndex(max(0, self.send_mode.findData(mode)))

    def _save_settings(self) -> None:
        try:
            api_id = int(self.api_id.text().strip())
            if api_id <= 0 or not self.api_hash.text().strip():
                raise ValueError("Enter your valid Telegram API ID and API hash.")
            source = Path(self.source.text().strip())
            if not source.is_dir():
                raise ValueError("Choose an existing source folder.")
            if self.sleep_min.value() > self.sleep_max.value():
                raise ValueError("Minimum upload delay cannot exceed maximum delay.")
        except ValueError as exc:
            QMessageBox.warning(self, "Settings", str(exc)); return
        raw = yaml.safe_load(config_path().read_text(encoding="utf-8")) or {}
        raw.setdefault("telegram", {}).update({"api_id": api_id, "api_hash": self.api_hash.text().strip(), "phone": self.phone.text().strip(), "target": self.target.text().strip() or "me"})
        raw.setdefault("upload", {}).update({"source_dir": str(source), "max_files_per_run": self.per_run.value(), "max_files_per_day": self.per_day.value(), "sleep_min_seconds": self.sleep_min.value(), "sleep_max_seconds": self.sleep_max.value(), "retry_attempts": self.retry_attempts.value(), "backoff_base_seconds": self.backoff.value(), "floodwait_buffer_seconds": self.flood_buffer.value(), "send_mode": self.send_mode.currentData()})
        raw.setdefault("app", {})["run_interval_minutes"] = self.run_interval.value()
        temp = config_path().with_suffix(".yaml.tmp")
        temp.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
        temp.replace(config_path())
        self.activity.appendPlainText("Settings saved.")
        QMessageBox.information(self, "Settings", "Settings saved. On your first upload, Telegram will ask for a login code in the terminal setup flow.")

    def _choose_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Choose source folder", self.source.text())
        if folder: self.source.setText(folder)

    def _show_page(self, index: int) -> None:
        self.pages.setCurrentIndex(index)
        if index == 1:
            self._refresh_table(self.queue_table, "status IN ('pending', 'failed')")
        elif index == 2:
            self._refresh_table(self.history_table, "status = 'uploaded'")

    def _check_for_updates(self) -> None:
        if self._update_worker is not None and self._update_worker.isRunning():
            return
        self._update_worker = UpdateCheckWorker(self)
        self._update_worker.update_available.connect(self._show_update_available)
        self._update_worker.finished.connect(self._update_worker.deleteLater)
        self._update_worker.start()

    def _show_update_available(self, version: str, release_url: str) -> None:
        answer = QMessageBox.information(
            self,
            "TeleDrive update available",
            f"TeleDrive v{version} is available. You are using v{APP_VERSION}.",
            QMessageBox.StandardButton.Open | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Open,
        )
        if answer == QMessageBox.StandardButton.Open:
            QDesktopServices.openUrl(QUrl(release_url))

    def closeEvent(self, event) -> None:
        if self._update_worker is not None and self._update_worker.isRunning():
            self._update_worker.requestInterruption()
            self._update_worker.wait(6000)
        super().closeEvent(event)

    def _command(self, mode: str) -> list[str]:
        if getattr(sys, "frozen", False):
            return [sys.executable, "--cli", "--config", str(config_path()), "--auth-dir", str(self.auth_dir), mode]
        return [sys.executable, str(Path(__file__).parents[1] / "uploader.py"), "--config", str(config_path()), "--auth-dir", str(self.auth_dir), mode]

    def _start_uploader(self, mode: str) -> None:
        if self.process.state() != QProcess.ProcessState.NotRunning:
            QMessageBox.information(self, "Uploader", "An upload process is already running."); return
        if not self.source.text().strip() or not self.api_hash.text().strip():
            self.pages.setCurrentIndex(3)
            QMessageBox.information(self, "Setup required", "Complete and save the Settings page before running uploads."); return
        command = self._command(mode)
        for auth_file in (self.auth_dir / "code.txt", self.auth_dir / "password.txt"):
            auth_file.unlink(missing_ok=True)
        self.activity.appendPlainText("$ " + " ".join(command))
        self.status_badge.setText("Working")
        self.run_button.setEnabled(False)
        self.stop_button.setEnabled(True)
        self.process.start(command[0], command[1:])

    def _start_loop(self) -> None:
        if self.run_interval.value() < 1:
            return
        self._start_uploader("--run-once")
        self.loop_timer.start(self.run_interval.value() * 60 * 1000)
        self.status_badge.setText("Automatic loop enabled")
        self.stop_loop_button.setEnabled(True)

    def _stop_loop(self) -> None:
        self.loop_timer.stop()
        self.status_badge.setText("Ready")
        self.activity.appendPlainText("Automatic loop stopped.")
        self.stop_loop_button.setEnabled(False)

    def _stop_uploader(self) -> None:
        if self.process.state() != QProcess.ProcessState.NotRunning:
            self.process.terminate()
            if not self.process.waitForFinished(5000): self.process.kill()
            self.activity.appendPlainText("Upload process stopped by user.")
            self.status_badge.setText("Stopped")
            self.stop_button.setEnabled(False)
        else:
            self.activity.appendPlainText("No upload process is running.")

    def _clear_activity(self) -> None:
        self.activity.clear()
        self.activity.appendPlainText("Activity cleared.")

    def _reset_all(self) -> None:
        if self.process.state() != QProcess.ProcessState.NotRunning or self.loop_timer.isActive():
            QMessageBox.information(self, "Stop first", "Stop the upload process and automatic loop before resetting.")
            return
        answer = QMessageBox.warning(self, "Reset all tracked files", "This removes all queue and upload history from TeleDrive. Your files and settings will not be deleted. Continue?", QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
        if answer != QMessageBox.StandardButton.Yes:
            return
        conn = self._connection()
        if conn is not None:
            try:
                conn.execute("DELETE FROM files")
                conn.commit()
            finally:
                conn.close()
        self.activity.appendPlainText("All tracked files and history were reset.")
        self.status_badge.setText("Ready")
        self._refresh_all()

    def _read_process_output(self) -> None:
        text = bytes(self.process.readAllStandardOutput()).decode("utf-8", errors="replace").rstrip()
        if text:
            self.activity.appendPlainText(text)
            self.log_view.appendPlainText(text)
            self._answer_login_prompt(text)

    def _answer_login_prompt(self, text: str) -> None:
        if self.awaiting_login_prompt:
            return
        prompt = None
        secret = False
        if "Please enter the code" in text:
            prompt = "Enter the Telegram code sent to your phone."
            self.pending_login_mode = "code"
        elif "Please enter your password" in text or "Please enter the password" in text:
            prompt = "Enter your Telegram two-step verification password."
            secret = True
            self.pending_login_mode = "password"
        if prompt is None:
            return
        self.awaiting_login_prompt = True
        answer, ok = QInputDialog.getText(self, "Telegram sign in", prompt, QLineEdit.EchoMode.Password if secret else QLineEdit.EchoMode.Normal)
        if ok and answer.strip() and self.pending_login_mode:
            value = answer.strip()
            if self.pending_login_mode == "code":
                self.login_code = value
            response = self.auth_dir / f"{self.pending_login_mode}.txt"
            response.write_text(value, encoding="utf-8")
        else:
            self._stop_uploader()
        self.awaiting_login_prompt = False


    def _process_finished(self, exit_code: int, _status: QProcess.ExitStatus) -> None:
        self.run_button.setEnabled(True)
        self.stop_button.setEnabled(False)
        self.awaiting_login_prompt = False
        self.status_badge.setText("Ready" if exit_code == 0 else "Needs attention")
        self.activity.appendPlainText(f"Uploader finished with exit code {exit_code}.")
        self._refresh_all()

    def _refresh_all(self) -> None:
        self._refresh_stats()
        current_page = self.pages.currentIndex()
        if current_page == 1:
            self._refresh_table(self.queue_table, "status IN ('pending', 'failed')")
        elif current_page == 2:
            self._refresh_table(self.history_table, "status = 'uploaded'")

    def _connection(self) -> sqlite3.Connection | None:
        if not state_path().exists(): return None
        conn = sqlite3.connect(state_path(), timeout=2); conn.execute("PRAGMA busy_timeout=2000"); return conn

    def _refresh_stats(self) -> None:
        conn = self._connection()
        if conn is None:
            self.stats_label.setText("No uploads yet"); self.stats_detail.setText("Save settings, then scan the source folder."); self.progress.setValue(0); return
        try:
            total = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
            uploaded = conn.execute("SELECT COUNT(*) FROM files WHERE status='uploaded'").fetchone()[0]
            failed = conn.execute("SELECT COUNT(*) FROM files WHERE status='failed'").fetchone()[0]
        except sqlite3.Error:
            return
        finally:
            conn.close()
        self.stats_label.setText(f"{uploaded:,} uploaded  •  {max(total-uploaded, 0):,} remaining")
        self.stats_detail.setText(f"{total:,} files tracked  •  {failed:,} need attention")
        self.progress.setValue(round(uploaded * 100 / total) if total else 0)

    def _refresh_table(self, table: QTableWidget, condition: str) -> None:
        conn = self._connection()
        if conn is None: table.setRowCount(0); return
        try:
            rows = conn.execute(f"SELECT path, status, attempts, datetime(last_update_ts, 'unixepoch', 'localtime') FROM files WHERE {condition} ORDER BY last_update_ts DESC LIMIT ?", (TABLE_ROW_LIMIT,)).fetchall()
        except sqlite3.Error:
            return
        finally:
            conn.close()
        table.setUpdatesEnabled(False)
        table.blockSignals(True)
        try:
            table.clearContents()
            table.setRowCount(len(rows))
            status_colors = {"pending": "#9a661d", "failed": "#b63838", "uploaded": "#327454"}
            for row_index, row in enumerate(rows):
                for column, value in enumerate(row):
                    text = str(value or "")
                    item = QTableWidgetItem(text)
                    if column == 0:
                        item.setToolTip(text)
                    elif column == 1:
                        item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                        color = status_colors.get(text.lower())
                        if color:
                            item.setForeground(QBrush(QColor(color)))
                    table.setItem(row_index, column, item)
        finally:
            table.blockSignals(False)
            table.setUpdatesEnabled(True)
