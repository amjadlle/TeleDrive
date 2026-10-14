"""Private-only Public Access Block S3 controls.

TeleDrive denies anonymous/public object access and public ACLs globally.
The only truthful configuration is all four fields enabled. It is enforced
even before a bucket exists and cannot be relaxed with an AWS-style API call.
"""
from __future__ import annotations

from xml.etree import ElementTree as ET
from defusedxml import ElementTree as SafeET
from defusedxml.common import DefusedXmlException

MAX_PUBLIC_ACCESS_XML_BYTES = 16 * 1024
NAMESPACE = "http://s3.amazonaws.com/doc/2006-03-01/"
FIELDS = (
    "BlockPublicAcls", "IgnorePublicAcls",
    "BlockPublicPolicy", "RestrictPublicBuckets",
)


class PublicAccessError(ValueError):
    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code, self.status = code, status


def parse_public_access_xml(payload: bytes) -> None:
    if len(payload) > MAX_PUBLIC_ACCESS_XML_BYTES:
        raise PublicAccessError("MalformedXML", "Public access XML exceeds 16 KiB")
    try:
        root = SafeET.fromstring(payload)
    except (ET.ParseError, DefusedXmlException) as exc:
        raise PublicAccessError("MalformedXML", "Invalid public access XML") from exc
    if (root.tag not in {"PublicAccessBlockConfiguration",
                        f"{{{NAMESPACE}}}PublicAccessBlockConfiguration"}
            or root.attrib or len(root) != 4):
        raise PublicAccessError("MalformedXML", "Unexpected public access document")
    seen = set()
    for element in root:
        field = element.tag.rsplit("}", 1)[-1]
        if (field not in FIELDS or field in seen
                or element.tag not in {field, f"{{{NAMESPACE}}}{field}"}
                or element.attrib or len(element)):
            raise PublicAccessError("MalformedXML", "Unknown or duplicate public access field")
        seen.add(field)
        if element.text != "true":
            if element.text == "false":
                raise PublicAccessError(
                    "NotImplemented", "Disabling public access protection is unsupported", 501,
                )
            raise PublicAccessError("InvalidArgument", "Public access fields must be true")
    if seen != set(FIELDS):
        raise PublicAccessError("MalformedXML", "Incomplete public access document")


def public_access_xml() -> bytes:
    root = ET.Element("PublicAccessBlockConfiguration", xmlns=NAMESPACE)
    for field in FIELDS:
        ET.SubElement(root, field).text = "true"
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)
