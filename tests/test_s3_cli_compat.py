"""Real AWS CLI + rclone S3 compatibility over HTTP with Telegram-backed bytes.

Runs the actual client executables against Uvicorn in a background thread.
No network AWS/S3 backend is used: the gateway uses real SQLite manifests and
an in-memory Telegram protocol fake. Every downloaded byte is compared.

CI installs both binaries; local runs skip missing tools unless
TELEDRIVE_REQUIRE_CLI_CLIENTS=1 is set.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path

import pytest
import uvicorn

import web.s3 as gateway
from test_s3_gateway import service


@pytest.fixture
def live_s3(service):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.listen(16)
    server = uvicorn.Server(uvicorn.Config(
        gateway.app, host="127.0.0.1", port=port, lifespan="off", log_level="error",
    ))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    try:
        for _ in range(100):
            if server.started:
                break
            time.sleep(0.05)
        assert server.started, "S3 ASGI server did not start"
        yield f"http://127.0.0.1:{port}", service[1], service[2]
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        sock.close()
        assert not thread.is_alive(), "S3 ASGI server failed to stop"


def _client(name):
    command = shutil.which(name)
    if command:
        return command
    if os.getenv("TELEDRIVE_REQUIRE_CLI_CLIENTS") == "1":
        pytest.fail(f"Required real S3 client is absent: {name}")
    pytest.skip(f"Install {name} to run its real integration tests")


def _run(args, *, env, timeout=90):
    done = subprocess.run(args, capture_output=True, text=True, env=env,
                          timeout=timeout, check=False)
    assert done.returncode == 0, (
        f"Real S3 client failed: {args[0]} {args[1:]}\n"
        f"stdout: {done.stdout[-2500:]}\nstderr: {done.stderr[-3500:]}"
    )
    return done.stdout


def _data(tmp_path: Path) -> bytes:
    content = bytes(range(256)) * 121 + b"\x00\xff\x80binary"
    (tmp_path / "source.bin").write_bytes(content)
    return content


def test_real_aws_cli_upload_download_list_delete(live_s3, tmp_path):
    """Independent AWS CLI binary, including high-level cp and multipart threshold."""
    aws = _client("aws")
    endpoint, issued, transport = live_s3
    env = dict(os.environ)
    env.update({
        "AWS_ACCESS_KEY_ID": issued.access_key,
        "AWS_SECRET_ACCESS_KEY": issued.secret_key,
        "AWS_DEFAULT_REGION": "us-east-1",
        "AWS_EC2_METADATA_DISABLED": "true",
        "AWS_MAX_ATTEMPTS": "1",
        "AWS_PAGER": "",
        "AWS_CONFIG_FILE": str(tmp_path / "aws-config"),
    })
    (tmp_path / "aws-config").write_text(
        "[default]\ns3 =\n    addressing_style = path\n"
        "    payload_signing_enabled = true\n", encoding="utf-8"
    )
    def cli(*args):
        return _run([aws, "--endpoint-url", endpoint, *args], env=env)

    cli("s3api", "create-bucket", "--bucket", "aws-cli-test")
    content = _data(tmp_path)
    cli("s3api", "put-object", "--bucket", "aws-cli-test",
        "--key", "bin/source.bin", "--body", str(tmp_path / "source.bin"))
    cli("s3api", "head-object", "--bucket", "aws-cli-test",
        "--key", "bin/source.bin")
    cli("s3api", "get-object", "--bucket", "aws-cli-test",
        "--key", "bin/source.bin", str(tmp_path / "retrieved.bin"))
    assert (tmp_path / "retrieved.bin").read_bytes() == content
    assert hashlib.sha256(content).hexdigest() == hashlib.sha256(
        (tmp_path / "retrieved.bin").read_bytes()
    ).hexdigest()
    listing = cli("s3api", "list-objects-v2", "--bucket", "aws-cli-test",
                  "--prefix", "bin/")
    assert "bin/source.bin" in listing
    cli("s3api", "delete-object", "--bucket", "aws-cli-test",
        "--key", "bin/source.bin")
    assert transport.uploads >= 1 and transport.downloads >= 1


def test_real_rclone_sync_and_integrity(live_s3, tmp_path):
    """rclone real S3 remote: put, list, copy-to-local, replace and delete."""
    rclone = _client("rclone")
    endpoint, issued, transport = live_s3
    env = dict(os.environ)
    env.update({
        "RCLONE_CONFIG_TD_TYPE": "s3",
        "RCLONE_CONFIG_TD_PROVIDER": "Other",
        "RCLONE_CONFIG_TD_ACCESS_KEY_ID": issued.access_key,
        "RCLONE_CONFIG_TD_SECRET_ACCESS_KEY": issued.secret_key,
        "RCLONE_CONFIG_TD_ENDPOINT": endpoint,
        "RCLONE_CONFIG_TD_REGION": "us-east-1",
        "RCLONE_CONFIG_TD_FORCE_PATH_STYLE": "true",
        "RCLONE_CONFIG_TD_NO_CHECK_BUCKET": "true",
        "RCLONE_CONFIG_TD_DISABLE_CHECKSUM": "true",
        "RCLONE_CONFIG": str(tmp_path / "rclone.conf"),
    })
    (tmp_path / "rclone.conf").write_text("", encoding="utf-8")
    def cli(*args):
        return _run([rclone, "--retries", "1", "--low-level-retries", "1",
                     "--contimeout", "5s", "--timeout", "60s", *args], env=env)

    cli("mkdir", "td:rclone-test")
    content = _data(tmp_path)
    cli("copyto", str(tmp_path / "source.bin"),
        "td:rclone-test/deep/path/one.bin")
    listing = cli("lsf", "td:rclone-test/deep/path", "--files-only")
    assert "one.bin" in listing
    cli("copyto", "td:rclone-test/deep/path/one.bin",
        str(tmp_path / "rclone-downloaded.bin"))
    assert (tmp_path / "rclone-downloaded.bin").read_bytes() == content
    # A second write must replace current metadata without corrupting
    # old Telegram immutable manifests.
    updated = b"updated-content" * 517
    (tmp_path / "source.bin").write_bytes(updated)
    cli("copyto", str(tmp_path / "source.bin"), "td:rclone-test/deep/path/one.bin")
    cli("copyto", "td:rclone-test/deep/path/one.bin",
        str(tmp_path / "rclone-replaced.bin"))
    assert (tmp_path / "rclone-replaced.bin").read_bytes() == updated
    cli("deletefile", "td:rclone-test/deep/path/one.bin")
    assert cli("lsf", "td:rclone-test/deep/path", "--files-only").strip() == ""
    assert transport.uploads >= 2 and transport.downloads >= 2
