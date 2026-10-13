"""Real multipart browser POSTs with bounded, checksum-pinned SigV4 policies."""
import base64
import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone
from urllib.parse import quote, urlsplit, parse_qs
from xml.etree import ElementTree as ET

import pytest

from test_s3_gateway import service, signed
from teledrive.s3.credentials import S3CredentialStore


def make_form(service, data, *, key="browser/upload.bin", seconds=300,
              missing=None, clauses=None, issued=None, key_prefix=None,
              status=None, redirect=None, content_type=None, content_type_prefix=None,
              extra_fields=None):
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
    if status is not None:
        form["success_action_status"] = status
    if redirect is not None:
        form["success_action_redirect"] = redirect
    if content_type is not None:
        form["Content-Type"] = content_type
    form.update(extra_fields or {})
    conditions = [{"bucket": "backup-bucket"}]
    conditions.extend(
        ["starts-with", "$key", key_prefix] if k == "key" and key_prefix is not None
        else ["starts-with", "$Content-Type", content_type_prefix]
        if k == "Content-Type" and content_type_prefix is not None
        else {k: v}
        for k, v in form.items()
    )
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
        follow_redirects=False,
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
    constraints[-1] = ["starts-with", "$key", "unrelated/"]
    assert post(service, body, make_form(service, body, clauses=constraints)).status_code == 403
    assert service[2].uploads == 0


def test_browser_post_prefix_policy_binds_bucket_checksum_and_destination(service):
    assert signed(service, "PUT", "/backup-bucket").status_code == 200
    data = hashlib.shake_256(b"browser-prefix").digest(65537)
    form = make_form(
        service, data, key="browser/prefix/first.bin", key_prefix="browser/prefix/",
    )
    # The signed policy authorizes a prefix, rather than the form's original key.
    authorized = {**form, "key": "browser/prefix/nested/İstanbul.bin"}
    assert post(service, data, authorized).status_code == 204
    # Also verify independent SigV4 GET readback through an ASCII HTTP path.
    # Raw non-ASCII URL normalization is covered by the dedicated S3 path tests.
    assert post(
        service, data, {**form, "key": "browser/prefix/nested/check.bin"},
    ).status_code == 204
    assert signed(
        service, "GET", "/backup-bucket/browser/prefix/nested/check.bin",
    ).content == data
    for forbidden in ("browser/outside.bin", "browser/prefix-escape.bin"):
        assert post(service, data, {**form, "key": forbidden}).status_code == 403
    assert post(service, data+b"x", authorized).status_code in (400, 403)
    assert post(service, data, authorized, bucket="other-bucket").status_code == 403
    assert service[2].uploads == 2


@pytest.mark.parametrize("bad_condition", [
    ["starts-with", "$x-amz-checksum-sha256", ""],
    ["starts-with", "$bucket", "backup"],
    ["starts-with", "$key", 42],
    ["starts-with", "key", "browser/"],
])
def test_browser_post_rejects_unapproved_wildcards(service, bad_condition):
    assert signed(service, "PUT", "/backup-bucket").status_code == 200
    data = b"unchanged"
    valid = make_form(service, data)
    constraints = json.loads(base64.b64decode(valid["policy"]))["conditions"]
    constraints[1] = bad_condition
    assert post(service, data, make_form(service, data, clauses=constraints)).status_code == 403
    assert service[2].uploads == 0


def test_browser_post_real_boto3_filename_prefix_policy(service):
    import boto3
    from botocore.config import Config

    assert signed(service, "PUT", "/backup-bucket").status_code == 200
    payload = hashlib.shake_256(b"sdk-prefix").digest(8193)
    checksum = base64.b64encode(hashlib.sha256(payload).digest()).decode()
    creds = service[1]
    client = boto3.client(
        "s3", endpoint_url="http://localhost:9000",
        aws_access_key_id=creds.access_key,
        aws_secret_access_key=creds.secret_key,
        region_name="us-east-1", config=Config(
            signature_version="s3v4", s3={"addressing_style": "path"},
        ),
    )
    signed_form = client.generate_presigned_post(
        Bucket="backup-bucket", Key="sdk/prefix/${filename}",
        Fields={"x-amz-checksum-sha256": checksum},
        Conditions=[
            {"x-amz-checksum-sha256": checksum},
            ["content-length-range", 0, 8 * 1024 * 1024],
        ],
        ExpiresIn=120,
    )
    assert "${filename}" in signed_form["fields"]["key"]
    expanded = {**signed_form["fields"], "key": "sdk/prefix/nested.bin"}
    assert post(service, payload, expanded).status_code == 204
    assert signed(service, "GET", "/backup-bucket/sdk/prefix/nested.bin").content == payload
    assert post(service, payload, {**expanded, "key": "sdk/outside.bin"}).status_code == 403
    assert service[2].uploads == 1



@pytest.mark.parametrize("status", ["200", "201", "204"])
@pytest.mark.parametrize("size", [0, 1, 2049, 65537])
def test_browser_post_exact_signed_success_response_modes(service, status, size):
    """Each supported status responds only after checksum and SQL publication."""
    assert signed(service, "PUT", "/backup-bucket").status_code == 200
    payload = hashlib.shake_256(f"{status}:{size}".encode()).digest(size)
    key = f"browser/status/{status}/{size}.bin"
    response = post(service, payload, make_form(
        service, payload, key=key, status=status,
    ))
    assert response.status_code == int(status), response.text
    etag = '"' + hashlib.sha256(payload).hexdigest() + '"'
    assert response.headers["etag"] == etag
    assert response.headers["x-amz-version-id"]
    if status == "201":
        root = ET.fromstring(response.content)
        assert root.tag.endswith("PostResponse")
        assert root.findtext("{*}Bucket") == "backup-bucket"
        assert root.findtext("{*}Key") == key
        assert root.findtext("{*}ETag") == etag
        assert root.findtext("{*}Location") == response.headers["location"]
        assert response.headers["location"].endswith("/backup-bucket/" + key)
    else:
        assert response.content == b""
        assert "location" not in response.headers
    assert signed(service, "GET", "/backup-bucket/" + key).content == payload
    assert service[2].uploads == (0 if size == 0 else 1)


def test_browser_post_201_response_escapes_key_and_url(service):
    assert signed(service, "PUT", "/backup-bucket").status_code == 200
    payload = b"correctly-escaped-xml" * 32
    key = "browser/201/İstanbul & validation?.bin"
    result = post(service, payload, make_form(service, payload, key=key, status="201"))
    assert result.status_code == 201, result.text
    root = ET.fromstring(result.content)
    assert root.findtext("{*}Key") == key
    assert "&amp;" in result.text
    assert root.findtext("{*}Location") == result.headers["location"]
    assert result.headers["location"].endswith(
        "/backup-bucket/" + quote(key, safe="/")
    )
    assert service[2].uploads == 1


@pytest.mark.parametrize("invalid", [
    "", "0", "202", "302", "-1", "0201", "201 ", "True", "201\\nLocation:evil",
])
def test_browser_post_invalid_success_status_is_denied_before_send(service, invalid):
    assert signed(service, "PUT", "/backup-bucket").status_code == 200
    payload = b"status-validation"
    response = post(service, payload, make_form(service, payload, status=invalid))
    assert response.status_code in (400, 403)
    assert service[2].uploads == 0


def test_browser_post_unsigned_or_changed_status_and_redirect_rejected(service):
    assert signed(service, "PUT", "/backup-bucket").status_code == 200
    body = b"immutable-status"
    original = make_form(service, body, status="201")
    valid_without_status = make_form(service, body)
    mutations = [
        {**original, "success_action_status": "200"},
        {**valid_without_status, "success_action_status": "201"},
        {k: v for k, v in original.items() if k != "success_action_status"},
        {**original, "success_action_redirect": "https://example.com/"},
        {**original, "x-amz-meta-injected": "not-signed"},
    ]
    for fields in mutations:
        response = post(service, body, fields)
        assert response.status_code in (400, 403), response.text
    assert service[2].uploads == 0


def test_browser_post_signed_status_policy_rejects_unsupported_wildcard(service):
    assert signed(service, "PUT", "/backup-bucket").status_code == 200
    payload = b"bound-201"
    base = make_form(service, payload, status="201")
    clauses = json.loads(base64.b64decode(base["policy"]))["conditions"]
    clauses[-2] = ["starts-with", "$success_action_status", "20"]
    assert post(service, payload, make_form(
        service, payload, status="201", clauses=clauses,
    )).status_code == 403
    assert service[2].uploads == 0


def test_real_boto3_presigned_post_status_201_xml(service):
    """SDK-generated exact signed status must survive independent verification."""
    import boto3
    from botocore.config import Config

    assert signed(service, "PUT", "/backup-bucket").status_code == 200
    data = hashlib.shake_256(b"boto3-status-201").digest(262157)
    checksum = base64.b64encode(hashlib.sha256(data).digest()).decode()
    creds = service[1]
    client = boto3.client(
        "s3", endpoint_url="http://localhost:9000",
        aws_access_key_id=creds.access_key, aws_secret_access_key=creds.secret_key,
        region_name="us-east-1", config=Config(
            signature_version="s3v4", s3={"addressing_style": "path"},
        ),
    )
    signed_form = client.generate_presigned_post(
        Bucket="backup-bucket", Key="sdk/status-201.bin",
        Fields={"x-amz-checksum-sha256": checksum, "success_action_status": "201"},
        Conditions=[
            {"x-amz-checksum-sha256": checksum},
            {"success_action_status": "201"},
            ["content-length-range", 0, 8 * 1024 * 1024],
        ],
        ExpiresIn=120,
    )
    response = post(service, data, signed_form["fields"])
    assert response.status_code == 201, response.text
    assert ET.fromstring(response.content).findtext("{*}Key") == "sdk/status-201.bin"
    assert signed(service, "GET", "/backup-bucket/sdk/status-201.bin").content == data
    assert service[2].uploads == 1



@pytest.mark.parametrize("size", [0, 1, 65537])
def test_browser_post_exact_signed_https_redirect_persists_before_303(service, size):
    assert signed(service, "PUT", "/backup-bucket").status_code == 200
    data = hashlib.shake_256(str(size).encode()).digest(size)
    key = f"browser/callback/{size}.bin"
    callback = "https://callbacks.example.test/upload/done?tracking=abc%2F123"
    response = post(service, data, make_form(
        service, data, key=key, redirect=callback,
    ))
    assert response.status_code == 303, response.text
    assert response.content == b""
    etag = '"' + hashlib.sha256(data).hexdigest() + '"'
    assert response.headers["etag"] == etag
    url = urlsplit(response.headers["location"])
    assert url.scheme == "https" and url.hostname == "callbacks.example.test"
    assert url.path == "/upload/done"
    assert parse_qs(url.query) == {
        "tracking": ["abc/123"], "bucket": ["backup-bucket"],
        "key": [key], "etag": [etag],
    }
    assert signed(service, "GET", "/backup-bucket/" + key).content == data
    assert service[2].uploads == (0 if size == 0 else 1)


def test_browser_post_https_callback_percent_encodes_non_ascii_key(service):
    assert signed(service, "PUT", "/backup-bucket").status_code == 200
    data = b"redirect-unicode" * 313
    key = "browser/callback/İstanbul & 東京?.bin"
    response = post(service, data, make_form(
        service, data, key=key, redirect="https://callbacks.example.test/complete",
    ))
    assert response.status_code == 303
    assert parse_qs(urlsplit(response.headers["location"]).query)["key"] == [key]
    assert "%C4%B0stanbul" in response.headers["location"]
    assert "%26" in response.headers["location"]
    assert service[2].uploads == 1


@pytest.mark.parametrize("unsafe_url", [
    "", "http://callbacks.example.test/done",
    "//callbacks.example.test/done",
    "javascript:alert(1)",
    "https://user:secret@callbacks.example.test/done",
    "https://callbacks.example.test/done#fragment",
    "https://callbacks.example.test:badport/done",
    "https://callbacks.example.test/done\\evil",
    "https://callbacks.example.test/done\\r\\nSet-Cookie:bad=1",
    "https://",
])
def test_browser_post_unsafe_signed_redirect_cannot_upload(service, unsafe_url):
    assert signed(service, "PUT", "/backup-bucket").status_code == 200
    data = b"reject-unsafe-redirect"
    response = post(service, data, make_form(
        service, data, redirect=unsafe_url,
    ))
    assert response.status_code in (400, 403), response.text
    assert service[2].uploads == 0


def test_browser_post_callback_requires_exact_signed_url_not_status_override(service):
    assert signed(service, "PUT", "/backup-bucket").status_code == 200
    data = b"authenticated-callback"
    base = "https://callbacks.example.test/complete"
    correct = make_form(service, data, redirect=base)
    other = make_form(service, data)
    altered = [
        {**correct, "success_action_redirect": base + "?changed=1"},
        {**other, "success_action_redirect": base},
        {k: v for k, v in correct.items() if k != "success_action_redirect"},
        make_form(service, data, redirect=base, status="201"),
        {**correct, "success_action_status": "200"},
    ]
    for fields in altered:
        response = post(service, data, fields)
        assert response.status_code in (400, 403), response.text
    assert service[2].uploads == 0


def test_real_boto3_presigned_post_https_callback_and_payload_integrity(service):
    import boto3
    from botocore.config import Config

    assert signed(service, "PUT", "/backup-bucket").status_code == 200
    payload = hashlib.shake_256(b"boto3-redirect").digest(128 * 1024 + 7)
    checksum = base64.b64encode(hashlib.sha256(payload).digest()).decode()
    callback = "https://callbacks.example.test/complete?request=43"
    creds = service[1]
    client = boto3.client(
        "s3", endpoint_url="http://localhost:9000",
        aws_access_key_id=creds.access_key, aws_secret_access_key=creds.secret_key,
        region_name="us-east-1", config=Config(
            signature_version="s3v4", s3={"addressing_style": "path"},
        ),
    )
    signed_form = client.generate_presigned_post(
        Bucket="backup-bucket", Key="sdk/callback.bin",
        Fields={"x-amz-checksum-sha256": checksum, "success_action_redirect": callback},
        Conditions=[
            {"x-amz-checksum-sha256": checksum},
            {"success_action_redirect": callback},
            ["content-length-range", 0, 8 * 1024 * 1024],
        ],
        ExpiresIn=120,
    )
    result = post(service, payload, signed_form["fields"])
    assert result.status_code == 303, result.text
    assert parse_qs(urlsplit(result.headers["location"]).query)["key"] == ["sdk/callback.bin"]
    assert signed(service, "GET", "/backup-bucket/sdk/callback.bin").content == payload
    assert service[2].uploads == 1




@pytest.mark.parametrize("mime", [
    "application/pdf", "image/png", "text/plain; charset=utf-8",
    "application/octet-stream",
])
def test_browser_post_content_type_is_signed_persisted_and_retrievable(service, mime):
    assert signed(service, "PUT", "/backup-bucket").status_code == 200
    payload = hashlib.shake_256(mime.encode()).digest(65537)
    key = "browser/metadata/" + hashlib.sha256(mime.encode()).hexdigest()[:12]
    result = post(service, payload, make_form(
        service, payload, key=key, content_type=mime,
    ))
    assert result.status_code == 204, result.text
    read = signed(service, "GET", "/backup-bucket/" + key)
    assert read.status_code == 200 and read.content == payload
    assert read.headers["content-type"] == mime
    head = signed(service, "HEAD", "/backup-bucket/" + key)
    assert head.status_code == 200 and head.headers["content-type"] == mime
    assert service[2].uploads == 1


def test_browser_post_content_type_signed_prefix_accepts_only_authorized_mimes(service):
    assert signed(service, "PUT", "/backup-bucket").status_code == 200
    payload = b"verified-image" * 327
    policy = make_form(
        service, payload, key="browser/image-policy.bin",
        content_type="image/png", content_type_prefix="image/",
    )
    form = {**policy, "Content-Type": "image/webp"}
    assert post(service, payload, form).status_code == 204
    got = signed(service, "HEAD", "/backup-bucket/browser/image-policy.bin")
    assert got.headers["content-type"] == "image/webp"
    assert post(service, payload, {**policy, "Content-Type": "text/html"}).status_code == 403
    assert service[2].uploads == 1


@pytest.mark.parametrize("override", [
    "text/html", "application/javascript", "application/pdf ",
    "image/png\\nSet-Cookie: evil=1",
])
def test_browser_post_exact_mime_cannot_be_changed_or_injected(service, override):
    assert signed(service, "PUT", "/backup-bucket").status_code == 200
    body = b"mime-policy-enforced"
    form = make_form(service, body, content_type="application/pdf")
    response = post(service, body, {**form, "Content-Type": override})
    assert response.status_code in (400, 403)
    assert service[2].uploads == 0


def test_browser_post_unsigned_mime_and_malformed_mime_denied_before_send(service):
    assert signed(service, "PUT", "/backup-bucket").status_code == 200
    body = b"metadata-must-be-bound"
    unsigned = make_form(service, body)
    assert post(service, body, {**unsigned, "Content-Type": "text/plain"}).status_code == 403
    form = make_form(service, body, content_type="text/plain")
    conditions = json.loads(base64.b64decode(form["policy"]))["conditions"]
    constraints = [
        entry for entry in conditions if entry != {"Content-Type": "text/plain"}
    ]
    assert post(service, body, make_form(
        service, body, content_type="text/plain", clauses=constraints,
    )).status_code == 403
    malformed = make_form(service, body, content_type="text/" + chr(1))
    assert post(service, body, malformed).status_code == 400
    assert service[2].uploads == 0


def test_real_boto3_presigned_browser_post_content_type_is_persisted(service):
    import boto3
    from botocore.config import Config

    assert signed(service, "PUT", "/backup-bucket").status_code == 200
    payload = hashlib.shake_256(b"sdk-mime").digest(131089)
    checksum = base64.b64encode(hashlib.sha256(payload).digest()).decode()
    creds = service[1]
    client = boto3.client(
        "s3", endpoint_url="http://localhost:9000",
        aws_access_key_id=creds.access_key,
        aws_secret_access_key=creds.secret_key,
        region_name="us-east-1", config=Config(
            signature_version="s3v4", s3={"addressing_style": "path"},
        ),
    )
    signed_form = client.generate_presigned_post(
        Bucket="backup-bucket", Key="sdk/mime.pdf",
        Fields={
            "Content-Type": "application/pdf",
            "x-amz-checksum-sha256": checksum,
        },
        Conditions=[
            {"Content-Type": "application/pdf"},
            {"x-amz-checksum-sha256": checksum},
            ["content-length-range", 0, 8 * 1024 * 1024],
        ],
        ExpiresIn=120,
    )
    assert post(service, payload, signed_form["fields"]).status_code == 204
    response = signed(service, "GET", "/backup-bucket/sdk/mime.pdf")
    assert response.status_code == 200 and response.content == payload
    assert response.headers["content-type"] == "application/pdf"
    assert service[2].uploads == 1



@pytest.mark.parametrize("size", [0, 1, 65537])
def test_browser_post_signed_custom_metadata_and_cache_headers_roundtrip(service, size):
    assert signed(service, "PUT", "/backup-bucket").status_code == 200
    data = hashlib.shake_256(f"browser-metadata:{size}".encode()).digest(size)
    metadata = {
        "x-amz-meta-owner": "ci-operator",
        "x-amz-meta-şehir": "not-allowed",
    }
    # The metadata field name is strictly ASCII and must be signed.
    invalid = make_form(service, data, extra_fields=metadata)
    assert post(service, data, invalid).status_code == 400
    extra = {
        "x-amz-meta-owner": "ci-operator",
        "x-amz-meta-i18n": "Istanbul & Tokyo",
        "Cache-Control": "private, max-age=120",
        "Content-Disposition": "attachment; filename=remote.bin",
        "Content-Language": "tr",
    }
    key = f"browser/custom-metadata/{size}.bin"
    result = post(service, data, make_form(
        service, data, key=key, content_type="application/octet-stream",
        extra_fields=extra,
    ))
    assert result.status_code == 204, result.text
    get = signed(service, "GET", f"/backup-bucket/{key}")
    head = signed(service, "HEAD", f"/backup-bucket/{key}")
    assert get.status_code == 200 and get.content == data
    assert head.status_code == 200
    for response in (get, head):
        assert response.headers["x-amz-meta-owner"] == extra["x-amz-meta-owner"]
        assert response.headers["x-amz-meta-i18n"] == extra["x-amz-meta-i18n"]
        assert response.headers["cache-control"] == extra["Cache-Control"]
        assert response.headers["content-disposition"] == extra["Content-Disposition"]
        assert response.headers["content-language"] == "tr"
        assert response.headers["content-type"] == "application/octet-stream"
    assert service[2].uploads == (0 if size == 0 else 1)


@pytest.mark.parametrize("field", [
    "x-amz-meta-owner", "Cache-Control", "Content-Disposition",
    "Content-Language",
])
def test_browser_post_metadata_mutation_or_missing_policy_denied_before_send(service, field):
    assert signed(service, "PUT", "/backup-bucket").status_code == 200
    body = b"metadata-must-be-exact"
    initial = {field: "signed-value"}
    signed_form = make_form(service, body, extra_fields=initial)
    changes = [
        {**signed_form, field: "changed"},
        {k: v for k, v in signed_form.items() if k != field},
        {**make_form(service, body), field: "unsigned"},
    ]
    for mutated in changes:
        response = post(service, body, mutated)
        assert response.status_code in (400, 403), response.text
    assert service[2].uploads == 0


def test_browser_post_metadata_must_be_bounded_before_telegram_send(service):
    assert signed(service, "PUT", "/backup-bucket").status_code == 200
    body = b"guard-aggregate-metadata"
    large = {
        "x-amz-meta-one": "a" * 1024,
        "x-amz-meta-two": "b" * 1024,
        "x-amz-meta-three": "c" * 1024,
    }
    form = make_form(service, body, extra_fields=large)
    assert post(service, body, form).status_code == 400
    too_many = {f"x-amz-meta-field-{n}": str(n) for n in range(13)}
    assert post(service, body, make_form(
        service, body, extra_fields=too_many,
    )).status_code == 400
    malformed = {"x-amz-meta-shell": "valid\r\nInjected: unsafe"}
    assert post(service, body, make_form(
        service, body, extra_fields=malformed,
    )).status_code == 400
    assert service[2].uploads == 0


def test_real_boto3_presigned_post_custom_metadata_cache_and_binary_readback(service):
    import boto3
    from botocore.config import Config

    assert signed(service, "PUT", "/backup-bucket").status_code == 200
    payload = hashlib.shake_256(b"boto3-meta-and-cache").digest(262149)
    checksum = base64.b64encode(hashlib.sha256(payload).digest()).decode()
    fields = {
        "Content-Type": "application/pdf",
        "Cache-Control": "private, max-age=60",
        "x-amz-meta-ci-origin": "SDK-generated",
        "x-amz-meta-ci-variant": "binary",
        "x-amz-checksum-sha256": checksum,
    }
    creds = service[1]
    client = boto3.client(
        "s3", endpoint_url="http://localhost:9000",
        aws_access_key_id=creds.access_key, aws_secret_access_key=creds.secret_key,
        region_name="us-east-1", config=Config(
            signature_version="s3v4", s3={"addressing_style": "path"},
        ),
    )
    generated = client.generate_presigned_post(
        Bucket="backup-bucket", Key="sdk/custom-meta.pdf",
        Fields=fields, Conditions=[
            {name: value} for name, value in fields.items()
        ] + [["content-length-range", 0, 8 * 1024 * 1024]],
        ExpiresIn=120,
    )
    result = post(service, payload, generated["fields"])
    assert result.status_code == 204, result.text
    response = signed(service, "GET", "/backup-bucket/sdk/custom-meta.pdf")
    assert response.status_code == 200 and response.content == payload
    assert response.headers["x-amz-meta-ci-origin"] == "SDK-generated"
    assert response.headers["x-amz-meta-ci-variant"] == "binary"
    assert response.headers["cache-control"] == "private, max-age=60"
    assert response.headers["content-type"] == "application/pdf"
    assert service[2].uploads == 1



def test_browser_post_metadata_non_latin1_cannot_publish_unreadable_object(service):
    """HTTP server cannot emit U+6771 directly in response header values."""
    assert signed(service, "PUT", "/backup-bucket").status_code == 200
    data = b"metadata-header-encoding-regression" * 47
    fields = make_form(service, data, extra_fields={
        "x-amz-meta-unsafe": "東京",
    })
    response = post(service, data, fields)
    assert response.status_code == 400, response.text
    assert service[2].uploads == 0


@pytest.mark.parametrize("field,content", [
    ("x-amz-meta-bad@name", "value"),
    ("x-amz-meta-", "empty-name"),
    ("x-amz-meta-double.dot", "unsupported-name"),
    ("Content-MD5", "unbound-unsupported-checksum"),
    ("x-amz-server-side-encryption", "AES256"),
])
def test_browser_post_rejects_unsupported_signed_fields_before_upload(service, field, content):
    assert signed(service, "PUT", "/backup-bucket").status_code == 200
    body = b"do-not-write-insecure-metadata"
    response = post(service, body, make_form(
        service, body, extra_fields={field: content},
    ))
    assert response.status_code == 400, response.text
    assert service[2].uploads == 0


def test_browser_post_metadata_duplicate_constraints_fail_closed(service):
    assert signed(service, "PUT", "/backup-bucket").status_code == 200
    body = b"duplicate-user-meta-must-fail"
    base = make_form(service, body, extra_fields={
        "x-amz-meta-ci": "signed",
    })
    constraints = json.loads(base64.b64decode(base["policy"]))["conditions"]
    constraints[-2] = {"x-amz-meta-ci": "signed"}
    response = post(service, body, make_form(
        service, body, extra_fields={"x-amz-meta-ci": "signed"},
        clauses=constraints,
    ))
    assert response.status_code == 403, response.text
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
    duplicate = [(name, (None, value)) for name, value in form.items()]
    duplicate += [
        ("key", (None, form["key"])),
        ("file", ("payload.bin", body, "application/octet-stream")),
    ]
    # HTTPX only accepts multi-valued *files* for duplicate multipart keys;
    # passing a list as data creates an invalid stream, not an HTTP request.
    response = service[0].post("/backup-bucket", files=duplicate)
    assert response.status_code == 400
    assert service[2].uploads == 0


def test_browser_post_mime_upload_limit_blocks_before_remote_send(service, monkeypatch):
    assert signed(service, "PUT", "/backup-bucket").status_code == 200
    monkeypatch.setenv("TELEDRIVE_REST_MAX_UPLOAD_BYTES", "4096")
    body = b"X" * 8192
    assert post(service, body, make_form(service, body)).status_code == 413
    assert service[2].uploads == 0


def test_real_boto3_generated_sigv4_browser_post_policy(service):
    """Interoperate with the SDK's real POST presigner, not a homegrown signer."""
    import boto3
    from botocore.config import Config

    assert signed(service, "PUT", "/backup-bucket").status_code == 200
    creds = service[1]
    payload = b"sdk-authenticated-browser-upload" * 41
    checksum = base64.b64encode(hashlib.sha256(payload).digest()).decode()
    client = boto3.client(
        "s3", endpoint_url="http://localhost:9000",
        aws_access_key_id=creds.access_key,
        aws_secret_access_key=creds.secret_key,
        region_name="us-east-1", config=Config(
            signature_version="s3v4", s3={"addressing_style": "path"},
        ),
    )
    signed_form = client.generate_presigned_post(
        Bucket="backup-bucket", Key="sdk/form.bin",
        Fields={"x-amz-checksum-sha256": checksum},
        Conditions=[
            {"x-amz-checksum-sha256": checksum},
            ["content-length-range", 0, 8 * 1024 * 1024],
        ],
        ExpiresIn=120,
    )
    response = post(service, payload, signed_form["fields"])
    assert response.status_code == 204, response.text
    assert service[2].uploads == 1
    assert signed(service, "GET", "/backup-bucket/sdk/form.bin").content == payload


def test_browser_post_cannot_claim_unimplemented_encryption(service):
    assert signed(service, "PUT", "/backup-bucket").status_code == 200
    blob = b"unencrypted-payload"
    fields = make_form(service, blob)
    response = service[0].post(
        "/backup-bucket", data=fields,
        files={"file": ("sensitive.bin", blob)},
        headers={"x-amz-server-side-encryption": "AES256"},
    )
    assert response.status_code == 501
    assert service[2].uploads == 0
