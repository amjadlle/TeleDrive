"""Desktop management of shared Telegram-backed S3 access credentials."""
from __future__ import annotations

from datetime import datetime, timezone
import time
from typing import Callable

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QFormLayout, QHBoxLayout, QInputDialog,
    QLabel, QLineEdit, QMessageBox, QPushButton, QSpinBox, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
)

from teledrive.db import Database
from teledrive.s3.credentials import S3CredentialStore, PERMISSIONS, load_master_key


class S3ManagementPage(QWidget):
    """Shares DB/key storage with WebUI; never persists plaintext secrets."""

    def __init__(self, connect: Callable[[], Database], parent: QWidget | None = None):
        super().__init__(parent)
        self._connect = connect
        root = QVBoxLayout(self)
        title = QLabel("S3 Management")
        title.setStyleSheet("font-size:22px;font-weight:700;")
        root.addWidget(title)
        intro = QLabel(
            "Telegram is the only object backend. Manage encrypted S3 keys in "
            "the same SQL database as the WebUI. Provide the same master key "
            "through TELEDRIVE_S3_MASTER_KEY_FILE or TELEDRIVE_S3_MASTER_KEY. "
            "Keys are shown once and can be revoked immediately."
        )
        intro.setWordWrap(True)
        root.addWidget(intro)
        form = QFormLayout()
        self.name = QLineEdit()
        self.name.setPlaceholderText("AWS CLI backup")
        self.expiry_days = QSpinBox()
        self.expiry_days.setRange(0, 3650)
        self.expiry_days.setSpecialValueText("Never")
        self.expiry_days.setSuffix(" days")
        form.addRow("Credential name", self.name)
        form.addRow("Expires after", self.expiry_days)
        root.addLayout(form)
        root.addWidget(QLabel("S3 permissions"))
        self.permissions: dict[str, QCheckBox] = {}
        for name in sorted(PERMISSIONS):
            option = QCheckBox(name)
            option.setChecked(name in {"s3:ListAllMyBuckets", "s3:ListBucket", "s3:GetObject"})
            root.addWidget(option)
            self.permissions[name] = option
        toolbar = QHBoxLayout()
        self.create_button = QPushButton("Create S3 key")
        self.create_button.clicked.connect(self.create_key)
        self.refresh_button = QPushButton("Refresh")
        self.refresh_button.clicked.connect(self.refresh_keys)
        self.revoke_button = QPushButton("Revoke selected")
        self.revoke_button.clicked.connect(self.revoke_selected)
        for button in (self.create_button, self.refresh_button, self.revoke_button):
            toolbar.addWidget(button)
        root.addLayout(toolbar)
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(
            ["Name", "Access Key ID", "Permissions", "Created", "Status"]
        )
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setStretchLastSection(True)
        root.addWidget(self.table)

    @staticmethod
    def _timestamp(value: int | None) -> str:
        return datetime.fromtimestamp(value, timezone.utc).strftime("%Y-%m-%d UTC") if value else "-"

    def _error(self, error: Exception) -> None:
        QMessageBox.warning(self, "S3 Management", str(error))

    def refresh_keys(self) -> None:
        try:
            db = self._connect()
            try:
                rows = S3CredentialStore(db, load_master_key()).list()
            finally:
                db.close()
        except Exception as exc:
            self._error(exc)
            return
        self.table.setRowCount(len(rows))
        for index, record in enumerate(rows):
            state = "Revoked" if record["revoked"] else (
                "Expired" if record["expires_at"] and record["expires_at"] <= time.time()
                else "Active"
            )
            cells = [
                record["name"], record["access_key"],
                ", ".join(record["permissions"]),
                self._timestamp(record["created_at"]), state,
            ]
            for column, value in enumerate(cells):
                cell = QTableWidgetItem(value)
                if column == 0:
                    cell.setData(Qt.ItemDataRole.UserRole, record["id"])
                self.table.setItem(index, column, cell)

    def _selected_id(self) -> str | None:
        row = self.table.currentRow()
        cell = self.table.item(row, 0) if row >= 0 else None
        return cell.data(Qt.ItemDataRole.UserRole) if cell else None

    def _show_once(self, access: str, secret: str) -> None:
        # The dialog permits selecting/copying but never logs or auto-exports.
        QInputDialog.getMultiLineText(
            self, "S3 credentials — shown once",
            "Copy both values now. The secret cannot be recovered:",
            f"AWS_ACCESS_KEY_ID={access}\nAWS_SECRET_ACCESS_KEY={secret}",
        )

    def create_key(self) -> None:
        perms = [name for name, checkbox in self.permissions.items() if checkbox.isChecked()]
        expires = (int(time.time()) + self.expiry_days.value() * 86400
                   if self.expiry_days.value() else None)
        try:
            db = self._connect()
            try:
                value = S3CredentialStore(db, load_master_key()).create(
                    self.name.text(), perms, expires_at=expires
                )
            finally:
                db.close()
        except Exception as exc:
            self._error(exc)
            return
        self.name.clear()
        self.refresh_keys()
        self._show_once(value.access_key, value.secret_key)

    def revoke_selected(self) -> None:
        identifier = self._selected_id()
        if not identifier:
            return
        answer = QMessageBox.question(
            self, "Revoke S3 credentials",
            "Revoke the selected S3 access key immediately?",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            db = self._connect()
            try:
                if not S3CredentialStore(db, load_master_key()).revoke(identifier):
                    raise ValueError("Key already revoked or no longer exists")
            finally:
                db.close()
        except Exception as exc:
            self._error(exc)
            return
        self.refresh_keys()
