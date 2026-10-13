"""Local desktop API key administration; same SQL model as the Web UI."""
from __future__ import annotations

from datetime import datetime, timezone
import time
from typing import Callable

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView, QFormLayout, QHBoxLayout, QLabel, QLineEdit,
    QInputDialog, QMessageBox, QPushButton, QSpinBox, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
)

from teledrive.api_keys import ApiKeyStore
from teledrive.db import Database


class ApiManagementPage(QWidget):
    def __init__(self, connect: Callable[[], Database], parent: QWidget | None = None):
        super().__init__(parent)
        self._connect = connect
        layout = QVBoxLayout(self)
        title = QLabel("API Management")
        title.setStyleSheet("font-size:22px;font-weight:700;")
        layout.addWidget(title)
        hint = QLabel(
            "Keys are shared with the Docker API when connected to the same database. "
            "Only the key hash is saved; the secret appears once at creation/rotation."
        )
        hint.setWordWrap(True)
        layout.addWidget(hint)
        form = QFormLayout()
        self.name = QLineEdit()
        self.name.setPlaceholderText("Backup service")
        self.scopes = QLineEdit("objects:read")
        self.scopes.setPlaceholderText("Comma-separated scopes, e.g. buckets:read,objects:read")
        self.expiry_days = QSpinBox()
        self.expiry_days.setRange(0, 3650)
        self.expiry_days.setSpecialValueText("Never")
        self.expiry_days.setSuffix(" days")
        form.addRow("Key name", self.name)
        form.addRow("Permissions (comma-separated)", self.scopes)
        form.addRow("Expires after", self.expiry_days)
        layout.addLayout(form)
        toolbar = QHBoxLayout()
        self.create_button = QPushButton("Create key")
        self.create_button.clicked.connect(self.create_key)
        self.refresh_button = QPushButton("Refresh")
        self.refresh_button.clicked.connect(self.refresh_keys)
        self.rotate_button = QPushButton("Rotate selected")
        self.rotate_button.clicked.connect(self.rotate_selected)
        self.revoke_button = QPushButton("Revoke selected")
        self.revoke_button.clicked.connect(self.revoke_selected)
        for btn in (self.create_button, self.refresh_button, self.rotate_button, self.revoke_button):
            toolbar.addWidget(btn)
        layout.addLayout(toolbar)
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["Name", "Prefix", "Permissions", "Created", "Status"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self.table)

    def _selected_id(self) -> str | None:
        row = self.table.currentRow()
        if row < 0:
            return None
        first = self.table.item(row, 0)
        return first.data(Qt.ItemDataRole.UserRole) if first else None

    @staticmethod
    def _format_time(value: int | None) -> str:
        return datetime.fromtimestamp(value, timezone.utc).strftime("%Y-%m-%d UTC") if value else "-"

    def _warning(self, error: Exception) -> None:
        QMessageBox.warning(self, "API Management", str(error))

    def refresh_keys(self) -> None:
        try:
            conn = self._connect()
            try:
                records = ApiKeyStore(conn).list()
            finally:
                conn.close()
        except Exception as exc:
            self._warning(exc)
            return
        self.table.setRowCount(len(records))
        for row, info in enumerate(records):
            values = [
                info["name"], info["prefix"] + "…",
                ", ".join(info["scopes"]), self._format_time(info["created_at"]),
                "Revoked" if info["revoked"] else (
                    "Expired" if info["expires_at"] and info["expires_at"] <= time.time()
                    else "Active"
                ),
            ]
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                if column == 0:
                    item.setData(Qt.ItemDataRole.UserRole, info["id"])
                self.table.setItem(row, column, item)

    def _show_once(self, value: str) -> None:
        # Does not persist in UI state, logs or clipboard unless user copies it.
        QInputDialog.getText(
            self, "New API key — shown only once",
            "Copy this secret now. It cannot be retrieved later:",
            QLineEdit.EchoMode.Normal, value,
        )

    def create_key(self) -> None:
        try:
            scopes = [x.strip() for x in self.scopes.text().split(",") if x.strip()]
            expires = (int(time.time()) + self.expiry_days.value() * 86400
                       if self.expiry_days.value() else None)
            conn = self._connect()
            try:
                issued = ApiKeyStore(conn).create(self.name.text(), scopes, expires_at=expires)
            finally:
                conn.close()
        except Exception as exc:
            self._warning(exc)
            return
        self.name.clear()
        self.refresh_keys()
        self._show_once(issued.value)

    def rotate_selected(self) -> None:
        identifier = self._selected_id()
        if identifier is None:
            return
        answer = QMessageBox.question(
            self, "Rotate API key",
            "Immediately invalidate the selected key and generate a replacement?",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            conn = self._connect()
            try:
                issued = ApiKeyStore(conn).rotate(identifier)
            finally:
                conn.close()
        except Exception as exc:
            self._warning(exc)
            return
        self.refresh_keys()
        self._show_once(issued.value)

    def revoke_selected(self) -> None:
        identifier = self._selected_id()
        if identifier is None:
            return
        answer = QMessageBox.question(
            self, "Revoke API key",
            "Revoke the selected API key? Clients lose access immediately.",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            conn = self._connect()
            try:
                if not ApiKeyStore(conn).revoke(identifier):
                    raise ValueError("Key is already revoked or no longer exists")
            finally:
                conn.close()
        except Exception as exc:
            self._warning(exc)
            return
        self.refresh_keys()
