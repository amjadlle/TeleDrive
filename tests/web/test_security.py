from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path

import yaml

from teledrive import db
from teledrive import uploader
import web.app as webapp
from fastapi import HTTPException
from starlette.requests import Request


class SecurityTests(unittest.TestCase):
    def test_python_sources_live_in_packages(self):
        root = Path(__file__).resolve().parents[2]
        self.assertEqual([], sorted(p.name for p in root.glob("*.py")))
        self.assertTrue((root / "teledrive" / "__init__.py").is_file())
        self.assertTrue((root / "teledrive" / "uploader.py").is_file())
        self.assertTrue((root / "teledrive" / "db.py").is_file())

    def test_web_config_redacts_and_restores_secrets(self):
        raw = {
            "telegram": {
                "api_hash": "topsecret",
                "accounts": [{"name": "primary", "api_hash": "accountsecret"}],
            },
            "state": {
                "database_url": "postgresql+psycopg://alice:dbsecret@db/teledrive?token=querysecret"
            },
        }
        redacted = webapp._redacted_config(raw)
        rendered = str(redacted)
        self.assertNotIn("topsecret", rendered)
        self.assertNotIn("accountsecret", rendered)
        self.assertNotIn("dbsecret", rendered)
        self.assertNotIn("querysecret", rendered)

        restored = webapp._restore_secrets(redacted, raw)
        self.assertEqual(restored["telegram"]["api_hash"], "topsecret")
        self.assertEqual(restored["telegram"]["accounts"][0]["api_hash"], "accountsecret")
        self.assertEqual(restored["state"]["database_url"], raw["state"]["database_url"])

    def test_state_changes_require_csrf_header_and_same_origin(self):
        def request(headers):
            return Request({
                "type": "http",
                "method": "POST",
                "path": "/api/process/start",
                "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
                "client": ("127.0.0.1", 12345),
                "server": ("127.0.0.1", 8080),
                "scheme": "http",
                "query_string": b"",
            })

        with self.assertRaises(HTTPException):
            webapp._same_origin(request({"host": "localhost:8080", "origin": "http://localhost:8080"}))
        with self.assertRaises(HTTPException):
            webapp._same_origin(request({
                "host": "localhost:8080",
                "origin": "http://evil.example",
                "x-teledrive-csrf": "1",
            }))
        webapp._same_origin(request({
            "host": "localhost:8080",
            "origin": "http://localhost:8080",
            "x-teledrive-csrf": "1",
        }))

    def test_uploader_log_redaction(self):
        secret = "postgresql+psycopg://alice:supersecret@db.example/teledrive"
        scrubbed = uploader._scrub_log_text(secret)
        self.assertNotIn("supersecret", scrubbed)
        self.assertIn("alice:***@db.example", scrubbed)

    def test_database_url_is_vendor_neutral_and_redacted(self):
        url = "oracle+oracledb://alice:supersecret@db.example:1521/?service_name=FREEPDB1"
        self.assertEqual(db.normalize_database_url(url), url)
        rendered = db.redact_database_url(url)
        self.assertNotIn("supersecret", rendered)
        self.assertIn("***", rendered)
        query_url = "oracle+oracledb://alice:pw@db.example/x?token=querysecret&mode=safe"
        query_rendered = db.redact_database_url(query_url)
        self.assertNotIn("querysecret", query_rendered)
        self.assertIn("mode=safe", query_rendered)

    def test_atomic_file_claim_prevents_second_worker(self):
        conn = db.connect_database("sqlite:///:memory:")
        try:
            now = int(time.time())
            conn.insert_pending("/tmp/example.bin", 10, 1.0, now)
            conn.commit()
            self.assertTrue(conn.claim_file("/tmp/example.bin", "worker-a", now, 3600))
            self.assertFalse(conn.claim_file("/tmp/example.bin", "worker-b", now, 3600))
            conn.mark_uploaded("/tmp/example.bin", 123, "primary", now)
            conn.commit()
        finally:
            conn.close()

    def test_distributed_lease_is_exclusive(self):
        with tempfile.TemporaryDirectory() as td:
            url = f"sqlite:///{Path(td) / 'state.db'}"
            a = db.connect_database(url)
            b = db.connect_database(url)
            try:
                now = int(time.time())
                self.assertTrue(a.acquire_lease("uploader", "a", now, 60))
                self.assertFalse(b.acquire_lease("uploader", "b", now, 60))
                a.release_lease("uploader", "a")
                self.assertTrue(b.acquire_lease("uploader", "b", now + 1, 60))
            finally:
                a.close()
                b.close()

    def test_symlink_is_not_scanned(self):
        with tempfile.TemporaryDirectory() as source, tempfile.TemporaryDirectory() as outside:
            good = Path(source) / "good.txt"
            good.write_text("ok", encoding="utf-8")
            secret = Path(outside) / "secret.txt"
            secret.write_text("secret", encoding="utf-8")
            link = Path(source) / "link.txt"
            link.symlink_to(secret)
            files = set(uploader.iter_source_files(source))
            self.assertIn(str(good.resolve()), files)
            self.assertNotIn(str(link.absolute()), files)

    def test_caption_template_rejects_attribute_traversal(self):
        with self.assertRaises(ValueError):
            uploader.build_caption("example.txt", "{name.__class__}")
        self.assertEqual(uploader.build_caption("example.txt", "{stem}{ext}"), "example.txt")

    def test_non_regular_file_is_not_scanned(self):
        if not hasattr(os, "mkfifo"):
            self.skipTest("mkfifo unavailable")
        with tempfile.TemporaryDirectory() as source:
            fifo = Path(source) / "pipe"
            os.mkfifo(fifo)
            self.assertNotIn(str(fifo), set(uploader.iter_source_files(source)))
            # iter_source_files enumerates paths; scan-level regular-file enforcement is separate.

    def test_account_name_rejects_control_characters(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = root / "uploads"
            source.mkdir()
            raw = {
                "telegram": {
                    "strategy": "single",
                    "api_id": 1,
                    "api_hash": "hash",
                    "target": "me",
                    "accounts": [{
                        "name": "bad\nname",
                        "phone": "",
                        "session_path": str(root / "sessions" / "bad"),
                    }],
                },
                "upload": {
                    "source_dir": str(source),
                    "allowed_extensions": [],
                    "max_file_size_mb": 0,
                    "sleep_min_seconds": 1,
                    "sleep_max_seconds": 1,
                    "max_files_per_run": 1,
                    "max_files_per_day": 1,
                    "retry_attempts": 1,
                    "backoff_base_seconds": 0,
                    "floodwait_buffer_seconds": 0,
                    "caption_template": "{name}",
                    "send_mode": "document",
                },
                "state": {"database_url": "sqlite:///:memory:"},
                "logging": {"log_path": str(root / "app.log"), "level": "INFO"},
            }
            config = root / "config.yaml"
            config.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
            cfg = uploader.load_config(str(config))
            with self.assertRaises(ValueError):
                uploader.validate_config(cfg)

    def test_allowed_source_root_is_enforced(self):
        old = os.environ.get("TELEDRIVE_ALLOWED_SOURCE_ROOTS")
        try:
            with tempfile.TemporaryDirectory() as allowed, tempfile.TemporaryDirectory() as outside:
                os.environ["TELEDRIVE_ALLOWED_SOURCE_ROOTS"] = allowed
                with self.assertRaises(ValueError):
                    uploader._validate_allowed_roots(outside, "TELEDRIVE_ALLOWED_SOURCE_ROOTS")
        finally:
            if old is None:
                os.environ.pop("TELEDRIVE_ALLOWED_SOURCE_ROOTS", None)
            else:
                os.environ["TELEDRIVE_ALLOWED_SOURCE_ROOTS"] = old


if __name__ == "__main__":
    unittest.main()
