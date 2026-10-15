"""Fail-closed S3 query routing contract.

The actual S3 handler still authenticates every operation with SigV4. This
module prevents *unrecognized* AWS control-plane subresources and ambiguous
query combinations from degrading into a normal bucket/object request.
This is a parameter-level guard, not a claim that all 116 S3 APIs exist.
"""
from __future__ import annotations

from collections.abc import Mapping

# Explicit allowlist of implemented selectors and their concrete parameters.
# SDK ?x-id=OperationName is a non-authoritative hint, not a selector; the
# signed HTTP method and URL are always authoritative for routing/auth.
_ROOT_BUCKET_LIST = frozenset({"max-buckets", "prefix", "bucket-region", "continuation-token"})
_BUCKET_GET_V1 = frozenset({
    "prefix", "delimiter", "marker", "max-keys", "encoding-type",
})
_BUCKET_GET_V2 = frozenset({
    "list-type", "prefix", "delimiter", "continuation-token",
    "start-after", "max-keys", "encoding-type",
})
_BUCKET_GET_VERSIONS = frozenset({
    "versions", "prefix", "max-keys", "key-marker", "version-id-marker",
})
_BUCKET_GET_MULTIPART = frozenset({
    "uploads", "prefix", "key-marker", "upload-id-marker", "max-uploads",
})
_BUCKET_CONTROL = frozenset({
    "location", "cors", "tagging", "versioning", "publicAccessBlock", "lifecycle",
    "policyStatus",
})
_OBJECT_CONTROL = frozenset({"tagging", "attributes", "uploads", "uploadId", "versionId"})
_EMPTY_FLAG = frozenset({
    "location", "cors", "tagging", "versioning", "publicAccessBlock",
    "lifecycle", "policyStatus", "uploads", "versions", "delete", "attributes",
})
# No upper/lower-case folding for S3 query fields: AWS subresources are
# case-sensitive. A misspelled control may not be silently ignored.


class S3QueryError(ValueError):
    def __init__(self, code: str, message: str, status: int):
        super().__init__(message)
        self.code, self.status = code, status


def validate_s3_query(method: str, bucket: str, object_path: bool, params) -> None:
    """Reject unknown controls before SigV4 resolver/DB/media side effects.

    The only exceptions to the enumerated S3 params are AWS SigV4 X-Amz-*
    query fields, verified downstream, and the SDK's non-authoritative x-id.
    """
    keys = set(params)
    if len(keys) > 64:
        raise S3QueryError("InvalidArgument", "Too many query parameters", 400)
    real = {key for key in keys if not key.startswith("X-Amz-") and key != "x-id"}
    if "x-id" in keys:
        hint = params.getlist("x-id")
        if len(hint) != 1 or len(hint[0]) > 128:
            raise S3QueryError("InvalidArgument", "Invalid SDK operation hint", 400)

    if not bucket:
        allowed = _ROOT_BUCKET_LIST if method == "GET" else frozenset()
    elif not object_path:
        control = real & _BUCKET_CONTROL
        if control:
            if len(control) != 1:
                raise S3QueryError("InvalidArgument", "Conflicting bucket controls", 400)
            selector = next(iter(control))
            methods = {
                "location": {"GET"}, "cors": {"GET", "PUT", "DELETE"},
                "tagging": {"GET", "PUT", "DELETE"},
                "versioning": {"GET", "PUT"},
                "publicAccessBlock": {"GET", "PUT"},
                "lifecycle": {"GET", "PUT", "DELETE"},
                "policyStatus": {"GET"},
            }
            allowed = frozenset({selector}) if method in methods[selector] else frozenset()
        elif method == "GET":
            if "versions" in real:
                allowed = _BUCKET_GET_VERSIONS
            elif "uploads" in real:
                allowed = _BUCKET_GET_MULTIPART
            else:
                allowed = _BUCKET_GET_V2 if "list-type" in real else _BUCKET_GET_V1
        elif method == "POST":
            allowed = frozenset({"delete"}) if "delete" in real else frozenset()
        else:
            # HEAD/PUT/DELETE cannot treat ?policy/?acl/?prefix as regular
            # bucket mutations. An unadorned request remains supported.
            allowed = frozenset()
    else:
        if "uploadId" in real:
            allowed = {
                "PUT": frozenset({"uploadId", "partNumber"}),
                "GET": frozenset({"uploadId", "part-number-marker", "max-parts"}),
                "DELETE": frozenset({"uploadId"}),
                "POST": frozenset({"uploadId"}),
            }.get(method, frozenset())
        elif "uploads" in real:
            allowed = frozenset({"uploads"}) if method == "POST" else frozenset()
        elif "tagging" in real:
            allowed = (frozenset({"tagging", "versionId"})
                       if method in {"GET", "PUT", "DELETE"} else frozenset())
        elif "attributes" in real:
            # GetObjectAttributes may address a published historical VersionId.
            # Other object controls still fail closed, even if correctly signed.
            allowed = (frozenset({"attributes", "versionId"})
                       if method == "GET" else frozenset())
        elif "versionId" in real:
            allowed = (frozenset({"versionId"})
                       if method in {"GET", "HEAD", "DELETE"} else frozenset())
        else:
            allowed = frozenset()

    # A recognized ListObjects cursor in the wrong API dialect is an
    # InvalidArgument (400), not an unsupported AWS extension (501).
    # This preserves AWS CLI/boto3 V1/V2 cursor error contracts.
    if (bucket and not object_path and method == "GET"
            and not (real & (_BUCKET_CONTROL | {"versions", "uploads"}))):
        if ("list-type" not in real
                and real & {"continuation-token", "start-after"}):
            raise S3QueryError(
                "InvalidArgument", "V2 cursor requires list-type=2", 400,
            )
        if "list-type" in real and "marker" in real:
            raise S3QueryError(
                "InvalidArgument", "V1 marker cannot be used with V2 listing", 400,
            )

    forbidden = real - allowed
    if forbidden:
        # Fail closed: no unexpected list, create, overwrite, delete or
        # anonymous body staging even for a valid signed request.
        raise S3QueryError(
            "NotImplemented", "Unsupported S3 query parameter or selector", 501,
        )

    for name in real:
        values = params.getlist(name)
        if len(values) != 1:
            raise S3QueryError("InvalidArgument", "Duplicate S3 query parameter", 400)
        if name in _EMPTY_FLAG and values[0] != "":
            raise S3QueryError("InvalidArgument", "Unexpected selector value", 400)

    if "uploadId" in real and not params["uploadId"]:
        raise S3QueryError("InvalidArgument", "Missing multipart UploadId", 400)
    if "list-type" in real and params["list-type"] != "2":
        raise S3QueryError("InvalidArgument", "Unsupported bucket listing mode", 400)

    # Not all whitelisted parameter combinations are valid: multipart UploadPart
    # needs BOTH parameters; list cursors may not mix V1 and V2.
    if object_path and method == "PUT" and "uploadId" in real and "partNumber" not in real:
        raise S3QueryError("NotImplemented", "Multipart partNumber required", 501)
