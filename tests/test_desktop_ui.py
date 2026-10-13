"""Real Qt integration tests (QT_QPA_PLATFORM=offscreen in CI)."""
from __future__ import annotations

import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
import yaml
from PySide6.QtWidgets import QApplication, QInputDialog, QLineEdit, QMessageBox
from PySide6.QtCore import QProcess, QCoreApplication, QEvent, Qt
import shiboken6

from app import window as desktop
from app.paths import DEFAULT_CONFIG
from teledrive import db


@pytest.fixture(scope="module")
def qt_app():
    app = QApplication.instance() or QApplication([])
    app.setQuitOnLastWindowClosed(False)
    yield app
    app.closeAllWindows()
    app.processEvents()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    app.processEvents()


@pytest.fixture
def ui(tmp_path, monkeypatch, qt_app):
    config = tmp_path / "config.yaml"
    config.write_text(DEFAULT_CONFIG, encoding="utf-8")
    monkeypatch.setattr(desktop, "config_path", lambda: config)
    monkeypatch.setattr(desktop, "app_data_dir", lambda: tmp_path)
    monkeypatch.setattr(desktop.MainWindow, "_check_for_updates", lambda self: None)
    monkeypatch.setattr(QMessageBox, "information", lambda *args, **kwargs: QMessageBox.StandardButton.Ok)
    warnings = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *args, **kwargs: warnings.append(args[2]) or QMessageBox.StandardButton.Ok)
    win = desktop.MainWindow()
    win.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
    win.show()
    qt_app.processEvents()
    yield win, config, warnings
    win.loop_timer.stop()
    win.refresh_timer.stop()
    win.update_timer.stop()
    if win.process.state() != QProcess.ProcessState.NotRunning:
        win.process.kill()
        win.process.waitForFinished(5000)
    win.close()
    qt_app.processEvents()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    qt_app.processEvents()
    if shiboken6.isValid(win):
        shiboken6.delete(win)


def test_desktop_launches_and_has_account_controls(ui):
    win, _, _ = ui
    assert win.strategy.count() == 4
    assert win.database_url is not None
    assert len(win._account_widgets) == 1
    assert win._account_widgets[0]["name"].text() == "default"


def test_desktop_can_persist_multiple_accounts_and_strategy(ui, tmp_path):
    win, config, warnings = ui
    source = tmp_path / "files"
    source.mkdir()
    win.source.setText(str(source))
    win.api_id.setText("12345")
    win.api_hash.setText("globalhash")
    win.strategy.setCurrentIndex(win.strategy.findData("parallel"))
    win.database_url.setText(f"sqlite:///{tmp_path / 'state.db'}")
    account = win._account_widgets[0]
    account["name"].setText("primary")
    account["phone"].setText("+901111111111")
    account["session"].setText("data/sessions/primary")
    win._add_account_editor({
        "name": "secondary",
        "enabled": True,
        "phone": "+902222222222",
        "session_path": "data/sessions/secondary",
        "target": "me",
    })
    win._save_settings()
    assert not warnings
    raw = yaml.safe_load(config.read_text(encoding="utf-8"))
    assert raw["telegram"]["strategy"] == "parallel"
    assert [a["name"] for a in raw["telegram"]["accounts"]] == ["primary", "secondary"]
    assert raw["state"]["database_url"] == f"sqlite:///{tmp_path / 'state.db'}"
    win._load_settings()
    assert len(win._account_widgets) == 2


def test_desktop_login_prompts_write_account_scoped_files(ui, monkeypatch):
    win, _, _ = ui
    responses = iter([("123456", True), ("secret-2fa", True)])
    monkeypatch.setattr(QInputDialog, "getText", lambda *args, **kwargs: next(responses))
    win._answer_login_prompt("Please enter the code for account 'primary':")
    assert (win.auth_dir / "primary_code.txt").read_text() == "123456"
    win._answer_login_prompt("Please enter the password for account 'secondary':")
    assert (win.auth_dir / "secondary_password.txt").read_text() == "secret-2fa"
    assert not (win.auth_dir / "code.txt").exists()
    assert not (win.auth_dir / "password.txt").exists()


def test_desktop_uses_database_abstraction_for_status(ui, tmp_path):
    win, config, warnings = ui
    database_path = tmp_path / "desktop.db"
    raw = yaml.safe_load(config.read_text(encoding="utf-8"))
    raw["state"] = {"database_url": f"sqlite:///{database_path}"}
    config.write_text(yaml.safe_dump(raw), encoding="utf-8")
    conn = db.connect_database(str(database_path))
    conn.insert_pending(str(tmp_path / "test.bin"), 40, 1.0, 100)
    conn.commit()
    conn.mark_uploaded(str(tmp_path / "test.bin"), 5, "primary", 200)
    conn.set_account_state("primary", "ready", last_error=None,
                           cooldown_until=None, last_success_ts=200, now=200)
    conn.commit()
    conn.close()
    win._refresh_all()
    assert "1 uploaded" in win.stats_label.text()
    assert "primary" in win.account_status_label.text()
    win._refresh_table(win.history_table, "status = 'uploaded'")
    assert win.history_table.rowCount() == 1
    assert win.history_table.item(0, 1).text() == "uploaded"
    assert not warnings


def test_desktop_rejects_duplicate_accounts_before_saving(ui, tmp_path):
    win, config, warnings = ui
    source = tmp_path / "uploads"
    source.mkdir()
    win.source.setText(str(source))
    win.api_id.setText("123")
    win.api_hash.setText("hash")
    win._add_account_editor({"name": "default", "session_path": "data/other"})
    original = config.read_text()
    win._save_settings()
    assert warnings
    assert config.read_text() == original
