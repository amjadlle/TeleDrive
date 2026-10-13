"""Bounded S3 bucket CORS policy storage and conservative origin matching.

CORS is browser policy metadata, *never* S3 authentication or authorization.
Only a real signature can authorize an object operation. OPTIONS preflights
are public and disclose only permitted cross-origin request headers.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from urllib.parse import urlsplit
from xml.etree import ElementTree as ET

from defusedxml import ElementTree as SafeET
from defusedxml.common import DefusedXmlException
from sqlalchemy import Column, ForeignKey, String, Table, Text, delete, insert, select, update

from teledrive.storage.catalog import BucketCatalog, buckets, catalog_metadata

MAX_CORS_XML_BYTES = 64 * 1024
MAX_CORS_RULES = 100
METHODS = frozenset({"GET", "PUT", "HEAD", "POST", "DELETE"})
cors_config = Table(
    "storage_bucket_cors", catalog_metadata,
    Column("bucket", String(63), ForeignKey("storage_buckets.name"), primary_key=True),
    Column("rules", Text, nullable=False),
)


class CORSError(ValueError):
    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code, self.status = code, status


def _name(node) -> str:
    return node.tag.rsplit("}", 1)[-1]


def _origin_pattern(value: str) -> str:
    if value == "*":
        return value
    if not isinstance(value, str) or len(value) > 256:
        raise CORSError("InvalidRequest", "CORS origin is invalid")
    parsed = urlsplit(value)
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname
            or parsed.username or parsed.password or parsed.path or parsed.query
            or parsed.fragment or parsed.netloc.endswith(":")):
        raise CORSError("InvalidRequest", "CORS origins must be HTTP(S) origins")
    host = parsed.hostname
    if "*" in host and (host.count("*") != 1 or not host.startswith("*.")):
        raise CORSError("InvalidRequest", "Unsupported wildcard origin pattern")
    if re.search(r"[^a-zA-Z0-9.*-]", host) and ":" not in host:
        raise CORSError("InvalidRequest", "Invalid CORS host")
    if parsed.port is not None and not (1 <= parsed.port <= 65535):
        raise CORSError("InvalidRequest", "Invalid CORS port")
    return value


def _header_rule(value: str) -> str:
    if value == "*":
        return value
    if not re.fullmatch(r"[A-Za-z0-9!#$%&'*+.^_`|~-]{1,128}", value):
        raise CORSError("InvalidRequest", "Invalid CORS header name")
    return value


def parse_cors_xml(payload: bytes) -> list[dict]:
    if len(payload) > MAX_CORS_XML_BYTES:
        raise CORSError("MalformedXML", "CORS configuration exceeds 64 KiB")
    try:
        root = SafeET.fromstring(payload)
    except (ET.ParseError, DefusedXmlException) as exc:
        raise CORSError("MalformedXML", "Invalid CORS XML") from exc
    if _name(root) != "CORSConfiguration" or root.attrib:
        raise CORSError("MalformedXML", "Unexpected CORS document")
    if not 1 <= len(root) <= MAX_CORS_RULES:
        raise CORSError("InvalidRequest", "CORS requires 1..100 rules")
    result = []
    for node in root:
        if _name(node) != "CORSRule" or node.attrib:
            raise CORSError("MalformedXML", "Unexpected CORS rule element")
        fields = {"AllowedOrigin": [], "AllowedMethod": [],
                  "AllowedHeader": [], "ExposeHeader": [],
                  "MaxAgeSeconds": [], "ID": []}
        for element in node:
            label = _name(element)
            if label not in fields or element.attrib or len(element):
                raise CORSError("MalformedXML", "Unsupported CORS rule field")
            fields[label].append(element.text or "")
        origins = fields["AllowedOrigin"]
        methods = fields["AllowedMethod"]
        if not origins or not methods:
            raise CORSError("InvalidRequest", "CORS rule requires origins and methods")
        if len(fields["MaxAgeSeconds"]) > 1 or len(fields["ID"]) > 1:
            raise CORSError("InvalidRequest", "Repeated CORS rule field")
        if any(method not in METHODS for method in methods):
            raise CORSError("InvalidRequest", "Unsupported CORS method")
        if len(set(methods)) != len(methods) or len(set(origins)) != len(origins):
            raise CORSError("InvalidRequest", "Duplicate CORS method or origin")
        for raw in origins:
            _origin_pattern(raw)
        for raw in fields["AllowedHeader"] + fields["ExposeHeader"]:
            _header_rule(raw)
        try:
            maximum = int(fields["MaxAgeSeconds"][0]) if fields["MaxAgeSeconds"] else 0
        except ValueError as exc:
            raise CORSError("InvalidRequest", "Invalid MaxAgeSeconds") from exc
        if not 0 <= maximum <= 86400:
            raise CORSError("InvalidRequest", "MaxAgeSeconds exceeds 24 hours")
        rule_id = fields["ID"][0] if fields["ID"] else None
        if rule_id is not None and len(rule_id.encode("utf-8")) > 255:
            raise CORSError("InvalidRequest", "CORS rule ID too long")
        result.append({
            "origins": origins,
            "methods": methods,
            "allowed_headers": fields["AllowedHeader"],
            "expose_headers": fields["ExposeHeader"],
            "max_age": maximum,
            "id": rule_id,
        })
    return result


def cors_xml(rules: list[dict]) -> bytes:
    root = ET.Element("CORSConfiguration", xmlns="http://s3.amazonaws.com/doc/2006-03-01/")
    for rule in rules:
        child = ET.SubElement(root, "CORSRule")
        if rule["id"] is not None:
            ET.SubElement(child, "ID").text = rule["id"]
        for field, values in (
            ("AllowedOrigin", rule["origins"]), ("AllowedMethod", rule["methods"]),
            ("AllowedHeader", rule["allowed_headers"]),
            ("ExposeHeader", rule["expose_headers"]),
        ):
            for value in values:
                ET.SubElement(child, field).text = value
        if rule["max_age"]:
            ET.SubElement(child, "MaxAgeSeconds").text = str(rule["max_age"])
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def _origin_matches(pattern: str, origin: str) -> bool:
    if not origin or len(origin) > 256 or any(ord(c) < 33 for c in origin):
        return False
    parsed = urlsplit(origin)
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname or
            parsed.path or parsed.query or parsed.fragment or parsed.username or parsed.password):
        return False
    if pattern == "*":
        return True
    if "*" not in pattern:
        return origin == pattern
    # Match a single label at *.example.org; never echo untrusted wildcard
    # patterns as literal Access-Control-Allow-Origin values.
    regex = "^" + re.escape(pattern).replace(r"\*", r"[^./:]+") + "$"
    return bool(re.fullmatch(regex, origin))


def matching_rule(rules: list[dict], origin: str, method: str,
                  requested_headers: list[str] | None = None) -> dict | None:
    requested_headers = requested_headers or []
    for rule in rules:
        if method not in rule["methods"] or not any(
            _origin_matches(pattern, origin) for pattern in rule["origins"]
        ):
            continue
        allowed = {header.lower() for header in rule["allowed_headers"]}
        if not all("*" in allowed or name.lower() in allowed
                   for name in requested_headers):
            continue
        return rule
    return None


def response_cors_headers(rule: dict, origin: str, *, preflight: bool,
                          requested_headers: list[str] | None = None) -> dict[str, str]:
    result = {
        "Access-Control-Allow-Origin": origin,
        "Vary": "Origin, Access-Control-Request-Method, Access-Control-Request-Headers",
    }
    if preflight:
        result["Access-Control-Allow-Methods"] = ", ".join(rule["methods"])
        if requested_headers:
            result["Access-Control-Allow-Headers"] = ", ".join(requested_headers)
        result["Access-Control-Max-Age"] = str(rule["max_age"])
    elif rule["expose_headers"]:
        result["Access-Control-Expose-Headers"] = ", ".join(rule["expose_headers"])
    return result


class BucketCORSStore:
    def __init__(self, catalog: BucketCatalog):
        self.catalog, self.db = catalog, catalog.db
        catalog_metadata.create_all(self.db.engine, tables=[cors_config])

    def _bucket_exists(self, name: str) -> None:
        if self.db.connection.execute(
            select(buckets.c.name).where(buckets.c.name == name)
        ).first() is None:
            raise FileNotFoundError("Bucket not found")

    def get(self, name: str) -> list[dict] | None:
        self._bucket_exists(name)
        data = self.db.connection.execute(select(cors_config.c.rules).where(
            cors_config.c.bucket == name
        )).scalar_one_or_none()
        return json.loads(data) if data is not None else None

    def replace(self, name: str, rules: list[dict]) -> None:
        """Fence concurrent bucket deletion with a conditional SQL UPDATE."""
        try:
            guard = self.db.connection.execute(update(buckets).where(
                buckets.c.name == name
            ).values(created_at=buckets.c.created_at))
            if guard.rowcount != 1:
                raise FileNotFoundError("Bucket not found")
            self.db.connection.execute(delete(cors_config).where(cors_config.c.bucket == name))
            if rules:
                self.db.connection.execute(insert(cors_config).values(
                    bucket=name, rules=json.dumps(rules, ensure_ascii=False, sort_keys=True)
                ))
            self.db.commit()
        except BaseException:
            self.db.connection.rollback()
            raise
