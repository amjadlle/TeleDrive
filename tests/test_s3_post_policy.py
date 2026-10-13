"""Real multipart browser POSTs with bounded, checksum-pinned SigV4 policies."""
import base64
import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone

import pytest

from test_s3_gateway import service, signed
from teledrive.s3.credentials import S3CredentialStore


def make_form(service, data, *, key="browser/upload.bin", seconds=300,
              missing=None, clauses=None, issued=None):
    credential = issued or service[1]
    current = datetime.now(timezone.utc).replace(microsecond=0)
    day = current.strftime("%Y%m%d")
    form = {
        "key": key,
        "x-amz-algorithm": "AWS4-HMAC-SHA256",
        "x-amz-credential": f"{credential.access_key}/{day}/us-east-1/s3/aws4_request",
        "x-amz-date": current.strftime("%Y%m%dT%H%M%SZ"),
        "x-amz-checksum-sha256": base64.b64encode(hashlib.sha256(data).digest()).decode(),
    }
    conditions = [{"bucket": "backup-bucket"}]
    conditions.extend({k: v} for k, v in form.items())
    conditions.append(["content-length-range", 0, 8 * 1024 * 1024])
    if missing:
        conditions = [c for c in conditions if not isinstance(c, dict) or missing not in c]
    if clauses is not None:
        conditions = clauses
    document = {"expiration": (current + timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "conditions": conditions}
    encoded = base64.b64encode(json.dumps(document).encode()).decode()
    form["policy"] = encoded
    secret = hmac.new(("AWS4" + credential.secret_key).encode(), day.encode(), hashlib.sha256).digest()
    for label in ("us-east-1", "s3", "aws4_request"):
        secret = hmac.new(secret, label.encode(), hashlib.sha256).digest()
    form["x-amz-signature"] = hmac.new(secret, encoded.encode(), hashlib.sha256).hexdigest()
    return form


def post(service, data, fields, *, bucket="backup-bucket"):
    return service[0].post(
        f"/{bucket}", data=fields,
        files={"file": ("blob.bin", data, "application/octet-stream")},
    )


@pytest.mark.parametrize("size", [0, 1, 2048, 8193, 65537, 1048579])
def test_browser_post_signed_random_bytes_sha_and_readback(service, size):
    assert signed(service, "PUT", "/backup-bucket").status_code == 200
    payload = hashlib.shake_256(str(size).encode()).digest(size)
    key = f"browser/{size}.bin"
    upload = post(service, payload, make_form(service, payload, key=key))
    assert upload.status_code == 204, upload.text
    assert upload.headers["etag"] == '"' + hashlib.sha256(payload).hexdigest() + '"'
    assert service[2].uploads == (0 if size == 0 else 1)
    result = signed(service, "GET", f"/backup-bucket/{key}")
    assert result.status_code == 200 and result.content == payload


def test_browser_post_rejects_mutation_without_telegram_upload(service):
    assert signed(service, "PUT", "/backup-bucket").status_code == 200
    body = b"the-verified-upload"
    form = make_form(service, body)
    mutations = [
        (body+b"x", form), (body, {**form, "key": "changed.bin"}),
        (body, {**form, "x-amz-signature": "0"*64}),
        (body, {**form, "x-amz-checksum-sha256": base64.b64encode(b"0"*32).decode()}),
        (body, {**form, "policy": base64.b64encode(b"{}").decode()}),
    ]
    for payload, values in mutations:
        response = post(service, payload, values)
        assert response.status_code in (400, 403), response.text
    assert service[2].uploads == 0


def test_browser_post_requires_exact_policy_conditions(service):
    assert signed(service, "PUT", "/backup-bucket").status_code == 200
    body = b"fixed-policy"
    for field in ("bucket", "key", "x-amz-algorithm", "x-amz-credential",
                  "x-amz-date", "x-amz-checksum-sha256"):
        assert post(service, body, make_form(service, body, missing=field)).status_code == 403
    assert post(service, body, make_form(service, body), bucket="another-bucket").status_code == 403
    form = make_form(service, body)
    constraints = json.loads(base64.b64decode(form["policy"]))["conditions"]
    constraints[-1] = ["content-length-range", 0, len(body)-1]
    assert post(service, body, make_form(service, body, clauses=constraints)).status_code == 403
    constraints[-1] = ["starts-with", "$key", "browser/"]
    assert post(service, body, make_form(service, body, clauses=constraints)).status_code == 403
    assert service[2].uploads == 0


def test_browser_post_rejects_expired_or_revoked_signers(service):
    assert signed(service, "PUT", "/backup-bucket").status_code == 200
    body = b"secret"
    assert post(service, body, make_form(service, body, seconds=-1)).status_code == 403
    assert post(service, body, make_form(service, body, seconds=960)).status_code == 403
    conn = service[3]()
    try:
        readonly = S3CredentialStore(conn, service[4]).create("readonly", ["s3:GetObject"])
        assert post(service, body, make_form(service, body, issued=readonly)).status_code == 403
        assert S3CredentialStore(conn, service[4]).revoke(service[1].id)
    finally:
        conn.close()
    assert post(service, body, make_form(service, body)).status_code == 403
    assert service[2].uploads == 0


def test_browser_post_rejects_unsigned_extra_and_duplicate_fields(service):
    assert signed(service, "PUT", "/backup-bucket").status_code == 200
    body = b"not-writable"
    form = make_form(service, body)
    assert post(service, body, {**form, "x-amz-meta-hijack": "yes"}).status_code == 400
    assert post(service, body, {**form, "key": "prefix/"+"$"+"{filename}"}).status_code == 400
    duplicate = list(form.items()) + [("key", form["key"])]
    response = service[0].post("/backup-bucket", data=duplicate,
                               files={"file": ("payload.bin", body)})
    assert response.status_code == 400
    assert service[2].uploads == 0


def test_browser_post_mime_upload_limit_blocks_before_remote_send(service, monkeypatch):
    assert signed(service, "PUT", "/backup-bucket").status_code == 200
    monkeypatch.setenv("TELEDRIVE_REST_MAX_UPLOAD_BYTES", "4096")
    body = b"X" * 8192
    assert post(service, body, make_form(service, body)).status_code == 413
    assert service[2].uploads == 0
