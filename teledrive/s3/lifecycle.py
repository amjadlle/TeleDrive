"""S3 lifecycle subset: abort expired incomplete multipart sessions in SQL only.

Never deletes Telegram messages, object manifests, or any completed version.
"""
from __future__ import annotations
import json
from xml.etree import ElementTree as ET
from defusedxml import ElementTree as SafeET
from defusedxml.common import DefusedXmlException
from sqlalchemy import Column, ForeignKey, String, Table, Text, select, insert, update, delete
from sqlalchemy.exc import IntegrityError
from teledrive.storage.catalog import BucketCatalog, catalog_metadata

NS = "http://s3.amazonaws.com/doc/2006-03-01/"
MAX_XML = 32768
rules_table = Table("storage_bucket_lifecycle", catalog_metadata,
    Column("bucket", String(63), ForeignKey("storage_buckets.name"), primary_key=True),
    Column("rules_json", Text, nullable=False))


class LifecycleError(ValueError):
    def __init__(self, code, message, status=400):
        super().__init__(message)
        self.code, self.status = code, status


def fields(node, names):
    result = {}
    for el in node:
        k = el.tag.rsplit("}", 1)[-1]
        if (el.tag not in {k, f"{{{NS}}}{k}"} or el.attrib
                or k not in names or k in result):
            raise LifecycleError("NotImplemented", "Unsupported lifecycle rule", 501)
        result[k] = el
    return result


def scalar(el):
    if el is None or len(el):
        raise LifecycleError("MalformedXML", "Invalid lifecycle element")
    return el.text or ""


def parse_lifecycle_xml(body: bytes):
    if len(body) > MAX_XML:
        raise LifecycleError("MalformedXML", "Lifecycle XML exceeds 32 KiB")
    try:
        root = SafeET.fromstring(body)
    except (ET.ParseError, DefusedXmlException) as exc:
        raise LifecycleError("MalformedXML", "Malformed lifecycle XML") from exc
    if (root.tag not in {"LifecycleConfiguration", f"{{{NS}}}LifecycleConfiguration"}
            or root.attrib or not 1 <= len(root) <= 50):
        raise LifecycleError("MalformedXML", "Requires 1..50 lifecycle rules")
    result, ids = [], set()
    for el in root:
        if el.tag not in {"Rule", f"{{{NS}}}Rule"} or el.attrib:
            raise LifecycleError("NotImplemented", "Unsupported lifecycle node", 501)
        rule = fields(el, {"ID", "Status", "Filter", "AbortIncompleteMultipartUpload"})
        if "Status" not in rule or "AbortIncompleteMultipartUpload" not in rule:
            raise LifecycleError("InvalidArgument", "Missing lifecycle abort action")
        status = scalar(rule["Status"])
        if status not in {"Enabled", "Disabled"}:
            raise LifecycleError("InvalidArgument", "Invalid rule status")
        ident = scalar(rule["ID"]) if "ID" in rule else str(len(result) + 1)
        if not 1 <= len(ident.encode("utf-8")) <= 255 or ident in ids:
            raise LifecycleError("InvalidArgument", "Invalid duplicate rule ID")
        ids.add(ident)
        prefix = ""
        if "Filter" in rule:
            fil = fields(rule["Filter"], {"Prefix"})
            if set(fil) != {"Prefix"}:
                raise LifecycleError("NotImplemented", "Only Prefix filter supported", 501)
            prefix = scalar(fil["Prefix"])
        if len(prefix.encode("utf-8")) > 1024:
            raise LifecycleError("InvalidArgument", "Lifecycle prefix too long")
        abort = fields(rule["AbortIncompleteMultipartUpload"], {"DaysAfterInitiation"})
        if set(abort) != {"DaysAfterInitiation"}:
            raise LifecycleError("InvalidArgument", "Missing abort age")
        value = scalar(abort["DaysAfterInitiation"])
        if not value.isascii() or not value.isdigit() or value.startswith("0") or len(value) > 4:
            raise LifecycleError("InvalidArgument", "Invalid abort age")
        days = int(value)
        if not 1 <= days <= 3650:
            raise LifecycleError("InvalidArgument", "Abort age outside 1..3650 days")
        result.append({"id": ident, "status": status, "prefix": prefix, "days": days})
    return result


def lifecycle_xml(rules):
    root = ET.Element("LifecycleConfiguration", xmlns=NS)
    for rule in rules:
        el = ET.SubElement(root, "Rule")
        ET.SubElement(el, "ID").text = rule["id"]
        ET.SubElement(el, "Status").text = rule["status"]
        fil = ET.SubElement(el, "Filter")
        ET.SubElement(fil, "Prefix").text = rule["prefix"]
        abort = ET.SubElement(el, "AbortIncompleteMultipartUpload")
        ET.SubElement(abort, "DaysAfterInitiation").text = str(rule["days"])
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


class BucketLifecycleStore:
    def __init__(self, catalog: BucketCatalog):
        self.catalog, self.db = catalog, catalog.db
        catalog_metadata.create_all(self.db.engine, tables=[rules_table])

    def get(self, bucket):
        if bucket not in self.catalog.list_buckets():
            raise FileNotFoundError("No such bucket")
        raw = self.db.connection.execute(select(rules_table.c.rules_json).where(
            rules_table.c.bucket == bucket)).scalar_one_or_none()
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except (TypeError, ValueError) as exc:
            raise RuntimeError("Corrupted lifecycle policy") from exc

    def replace(self, bucket, rules):
        if bucket not in self.catalog.list_buckets():
            raise FileNotFoundError("No such bucket")
        try:
            if rules is None:
                self.db.connection.execute(delete(rules_table).where(
                    rules_table.c.bucket == bucket))
            else:
                data = json.dumps(rules, separators=(",", ":"), sort_keys=True)
                changed = self.db.connection.execute(update(rules_table).where(
                    rules_table.c.bucket == bucket).values(rules_json=data))
                if changed.rowcount == 0:
                    self.db.connection.execute(insert(rules_table).values(
                        bucket=bucket, rules_json=data))
            self.db.commit()
        except IntegrityError as exc:
            self.db.connection.rollback()
            raise LifecycleError("OperationAborted", "Concurrent policy edit", 409) from exc
        except BaseException:
            self.db.connection.rollback()
            raise

    def apply_due(self, *, now: int, limit=100):
        """Bounded active-state CAS; no remote or SQL journal deletion."""
        if type(now) is not int or now < 0 or type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("Invalid lifecycle run bounds")
        from teledrive.s3.multipart import uploads, _meta
        _meta.create_all(self.db.engine, tables=[uploads])
        entries = self.db.connection.execute(select(
            rules_table.c.bucket, rules_table.c.rules_json)).all()
        count = 0
        try:
            for bucket, data in entries:
                try:
                    rules = json.loads(data)
                    if not isinstance(rules, list):
                        raise ValueError()
                except (TypeError, ValueError) as exc:
                    raise RuntimeError("Corrupted lifecycle policy") from exc
                for rule in rules:
                    if (not isinstance(rule, dict) or set(rule) != {"id", "status", "prefix", "days"}
                            or rule["status"] not in {"Enabled", "Disabled"}
                            or not isinstance(rule["prefix"], str)
                            or type(rule["days"]) is not int or not 1 <= rule["days"] <= 3650):
                        raise RuntimeError("Corrupted lifecycle policy")
                    if rule["status"] != "Enabled":
                        continue
                    remaining = limit - count
                    if remaining <= 0:
                        self.db.commit()
                        return count
                    select_ids = select(uploads.c.id).where(
                        uploads.c.bucket == bucket, uploads.c.state == "active",
                        uploads.c.created_at <= now - rule["days"] * 86400,
                    )
                    if rule["prefix"]:
                        select_ids = select_ids.where(uploads.c.object_key.startswith(
                            rule["prefix"], autoescape=True))
                    ids = self.db.connection.execute(
                        select_ids.order_by(uploads.c.created_at, uploads.c.id)
                        .limit(remaining)).scalars()
                    for ident in list(ids):
                        changed = self.db.connection.execute(update(uploads).where(
                            uploads.c.id == ident, uploads.c.state == "active"
                        ).values(state="aborted"))
                        count += int(changed.rowcount)
            self.db.commit()
            return count
        except BaseException:
            self.db.connection.rollback()
            raise
