"""Atomic S3 write/delete precondition evaluation.

The condition is checked against the current object before uploading Telegram
bytes, then the exact generation AND immutable object ID are compare-and-set
at publication. Object-ID comparison prevents delete/recreate ABA races.
"""
from __future__ import annotations

from dataclasses import dataclass

from teledrive.s3.object_metadata import signed_header_names
from teledrive.s3.multipart import MultipartStore
from teledrive.storage.catalog import BucketCatalog


class WritePreconditionError(ValueError):
    def __init__(self, code: str, message: str, status: int):
        super().__init__(message)
        self.code = code
        self.status = status


@dataclass(frozen=True)
class ExpectedHead:
    generation: int
    object_id: str | None


def check_write_precondition(request, catalog: BucketCatalog, bucket: str,
                             key: str, *, delete: bool = False) -> ExpectedHead | None:
    """Return a CAS snapshot or None if no write condition was requested.

    Conditions MUST be included in the SigV4 signed headers, otherwise an
    intermediary could add/strip If-Match while reusing a valid signature.
    """
    if_match = request.headers.get("if-match")
    if_none_match = request.headers.get("if-none-match")
    if if_match is None and if_none_match is None:
        return None
    if if_match is not None and if_none_match is not None:
        raise WritePreconditionError("InvalidRequest", "Conflicting write preconditions", 400)
    if delete and if_none_match is not None:
        raise WritePreconditionError("NotImplemented", "Conditional DELETE If-None-Match is unsupported", 501)

    name = "if-match" if if_match is not None else "if-none-match"
    if name not in signed_header_names(request):
        raise WritePreconditionError("AccessDenied", "Write precondition must be signed", 403)

    try:
        head = catalog.get(bucket, key)
    except FileNotFoundError:
        head = None

    if if_none_match is not None:
        if if_none_match.strip() != "*":
            raise WritePreconditionError("NotImplemented", "PUT If-None-Match supports only *", 501)
        if head is not None:
            raise WritePreconditionError("PreconditionFailed", "Object already exists", 412)
        return ExpectedHead(generation=0, object_id=None)

    if not if_match or (if_match != "*" and
                        not (len(if_match) >= 3 and if_match.startswith('"')
                             and if_match.endswith('"') and '"' not in if_match[1:-1])):
        raise WritePreconditionError("InvalidArgument", "Invalid If-Match ETag", 400)
    if head is None:
        raise WritePreconditionError("PreconditionFailed", "Object does not exist", 412)
    current_etag = MultipartStore(catalog).object_etag(head.item.id, head.item.sha256)
    if if_match not in ("*", '"' + current_etag + '"'):
        raise WritePreconditionError("PreconditionFailed", "ETag no longer matches", 412)
    return ExpectedHead(generation=head.generation, object_id=head.item.id)
