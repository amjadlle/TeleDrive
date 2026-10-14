"""Safe, immutable S3 bucket-versioning compatibility.

TeleDrive versions are always-on: every published object keeps its original
VersionId and prior data. "Suspended" is NOT supported because acknowledging
it without implementing null-version semantics would risk client data loss.
"""
from __future__ import annotations

from xml.etree import ElementTree as ET

from defusedxml import ElementTree as SafeET
from defusedxml.common import DefusedXmlException

MAX_VERSIONING_XML_BYTES = 16 * 1024
NAMESPACE = "http://s3.amazonaws.com/doc/2006-03-01/"


class VersioningError(ValueError):
    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code, self.status = code, status


def _tag(element) -> str:
    return element.tag.rsplit("}", 1)[-1]


def parse_versioning_xml(payload: bytes) -> None:
    """Validate a supported idempotent Enabled request without side effects."""
    if len(payload) > MAX_VERSIONING_XML_BYTES:
        raise VersioningError("MalformedXML", "Bucket versioning XML exceeds 16 KiB")
    try:
        root = SafeET.fromstring(payload)
    except (ET.ParseError, DefusedXmlException) as exc:
        raise VersioningError("MalformedXML", "Invalid bucket versioning XML") from exc
    if (_tag(root) != "VersioningConfiguration" or root.attrib
            or root.tag not in {"VersioningConfiguration",
                                f"{{{NAMESPACE}}}VersioningConfiguration"}):
        raise VersioningError("MalformedXML", "Unexpected versioning document")
    values = {}
    for element in root:
        field = _tag(element)
        if (field not in {"Status", "MFADelete"} or element.attrib
                or len(element) or field in values
                or element.tag not in {field, f"{{{NAMESPACE}}}{field}"}):
            raise VersioningError("MalformedXML", "Unexpected versioning field")
        values[field] = element.text or ""
    if values.get("MFADelete") is not None:
        raise VersioningError("NotImplemented", "MFADelete is not implemented", 501)
    if values.get("Status") == "Suspended":
        raise VersioningError(
            "NotImplemented", "Versioning suspension is unsupported", 501,
        )
    if values != {"Status": "Enabled"}:
        raise VersioningError("InvalidArgument", "Only Enabled bucket versioning is supported")


def versioning_xml() -> bytes:
    root = ET.Element("VersioningConfiguration", xmlns=NAMESPACE)
    ET.SubElement(root, "Status").text = "Enabled"
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)
