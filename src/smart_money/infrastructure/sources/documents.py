"""Offline document extraction and reuse of the existing immutable evidence archive."""

from __future__ import annotations

import base64
import fcntl
import hashlib
import json
import os
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

from lxml import etree, html  # type: ignore[import-untyped]
from trafilatura import extract

from smart_money.infrastructure.sources.direct_http import MAX_CANDIDATES, SourcePolicyError
from smart_money.infrastructure.wallet_storage import Archive, atomic_bytes, encoded


def archive() -> Archive:
    return Archive(Path(os.environ.get("SMART_MONEY_EVIDENCE_DIR", "data/evidence")))


def cached(key: str, *, value: dict[str, Any] | None = None, ttl: int = 300) -> dict[str, Any] | None:
    """Small URL/config-keyed metadata only; expiry never deletes cited content blocks."""
    path = archive().root / "cache" / (hashlib.sha256(key.encode()).hexdigest() + ".json")
    if value is not None:
        atomic_bytes(path, encoded({"stored_at": datetime.now(timezone.utc).isoformat(), "value": value}))
        return value
    if not path.exists():
        return None
    entry = json.loads(path.read_bytes())
    age = (datetime.now(timezone.utc) - datetime.fromisoformat(entry["stored_at"])).total_seconds()
    return dict(entry["value"]) if 0 <= age <= ttl else None


def first_seen(source_id: str, url: str, content_hash: str, retrieved: datetime) -> datetime:
    """Keep first acquisition of this exact origin/version after the URL cache expires."""
    key = hashlib.sha256(f"{source_id}|{url}|{content_hash}".encode()).hexdigest()
    path = archive().root / "acquisitions" / (key + ".json")
    path.parent.mkdir(parents=True, exist_ok=True)
    with (path.parent / ".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if path.exists():
            return datetime.fromisoformat(json.loads(path.read_bytes())["first_seen_at"])
        atomic_bytes(
            path,
            encoded(
                {
                    "source_id": source_id,
                    "url": url,
                    "content_hash": content_hash,
                    "first_seen_at": retrieved.isoformat(),
                }
            ),
        )
        return retrieved


def hydrate_document(row: dict[str, Any]) -> dict[str, Any]:
    reference = (row.get("source_metadata") or {}).get("document_ref")
    if not reference:
        return row
    body = archive().get(reference)
    for key, expected in {"raw_text": body["text"], "raw_data": body["metadata"]}.items():
        if key in row and row[key] != expected:
            raise ValueError("DOCUMENT_REFERENCE_CONTENT_MISMATCH")
    return {**row, "raw_text": body["text"], "raw_data": body["metadata"]}


def compact_document(row: dict[str, Any]) -> dict[str, Any]:
    if not (row.get("source_metadata") or {}).get("document_ref"):
        return row
    hydrate_document(row)
    return {k: v for k, v in row.items() if k not in {"raw_text", "raw_data"}}


def timestamp(value: str, source: str) -> dict[str, Any]:
    """Do not promote an undated/date-only/local-time statement to a UTC instant."""
    result: dict[str, Any] = {"raw": value, "source": source, "precision": "unknown", "at": None}
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value.strip()):
        result["precision"] = "date"
        return result
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = parsedate_to_datetime(value)
        except (ValueError, TypeError, OverflowError):
            return result
    result["precision"] = "datetime" if parsed.tzinfo else "local_datetime"
    if parsed.tzinfo:
        result["at"] = parsed.astimezone(timezone.utc).isoformat()
    return result


def extract_document(content: bytes, url: str, media_type: str) -> dict[str, Any]:
    if media_type not in {"text/html", "application/xhtml+xml"}:
        raise SourcePolicyError("UNSUPPORTED_DOCUMENT_MEDIA:" + media_type)
    tree = html.fromstring(content, parser=html.HTMLParser(no_network=True))
    title = " ".join(tree.xpath("//title/text()"))
    if tree.xpath('//input[@type="password"]') or re.search(
        r"access denied|just a moment|verify (?:you are|you're) human|captcha|sign in to continue", title, re.I
    ):
        raise SourcePolicyError("SOURCE_ACCESS_CHALLENGE")
    article_nodes = tree.xpath("//article")
    prose = tree.xpath("//p[not(ancestor::nav or ancestor::header or ancestor::footer)]/text()")
    if not article_nodes and not any(t.strip() for t in prose):
        raise SourcePolicyError("DOCUMENT_BODY_UNAVAILABLE")
    text = extract(
        content, url=url, output_format="markdown", include_tables=True, include_comments=False, favor_recall=True
    )
    if not text:
        # A short official notice is valid even below the extractor's scoring threshold.
        articles = tree.xpath("//article | //main")
        if articles:
            clean = html.fromstring(html.tostring(articles[0]))
            for node in clean.xpath(".//script | .//style | .//nav | .//form | .//footer"):
                node.drop_tree()
            text = "\n\n".join(t.strip() for t in clean.itertext() if t.strip())
    if not text or not text.strip():
        raise SourcePolicyError("DOCUMENT_BODY_UNAVAILABLE")
    if re.fullmatch(
        r"\W*(?:please )?(?:enable javascript|sign in to continue|subscribe to continue)[\W\s]*", text, re.I
    ):
        raise SourcePolicyError("SOURCE_ACCESS_CHALLENGE")
    metadata: dict[str, Any] = {"title": title, "language": tree.get("lang"), "times": {}}
    for kind, names in {
        "published": ("article:published_time", "datepublished", "date", "dc.date", "dc.date.issued"),
        "modified": ("article:modified_time", "datemodified", "last-modified"),
    }.items():
        entries = [
            timestamp(node.get("content"), "meta:" + (node.get("property") or node.get("name")))
            for node in tree.xpath("//meta[@content]")
            if (node.get("property") or node.get("name") or "").lower() in names
        ]
        if entries:
            metadata["times"][kind] = entries
    # Preserve machine-readable originals for review; do not infer event dates from article dates.
    metadata["time_elements"] = [
        {"datetime": n.get("datetime"), "text": " ".join(n.itertext())} for n in tree.xpath("//time")[:20]
    ]
    return {"html_base64": base64.b64encode(content).decode("ascii"), "text": text, "metadata": metadata}


def discover_documents(content: bytes, url: str, kind: str) -> list[dict[str, Any]]:
    """Parse downloaded bytes only. Sitemap indexes do not recursively crawl other maps."""
    rows: list[dict[str, Any]] = []
    if kind == "listing":
        tree = html.fromstring(content, parser=html.HTMLParser(no_network=True))
        for node in tree.xpath("//nav | //header | //footer | //script | //style"):
            node.drop_tree()
        for node in tree.xpath("//a[@href]"):
            title = " ".join(node.itertext()).strip()
            if title:
                rows.append({"url": urljoin(url, node.get("href")), "title": title, "discovery_kind": kind})
    else:
        if b"<!DOCTYPE" in content.upper() or b"<!ENTITY" in content.upper():
            raise SourcePolicyError("XML_ENTITY_FORBIDDEN")
        root = etree.fromstring(content, parser=etree.XMLParser(resolve_entities=False, no_network=True))
        nodes = (
            root.xpath("//*[local-name()='item' or local-name()='entry']")
            if kind == "feed"
            else root.xpath("/*[local-name()='urlset']/*[local-name()='url']")
        )
        for node in nodes:
            parts = {etree.QName(c).localname: c for c in node if isinstance(c.tag, str)}
            links = node.xpath("./*[local-name()='link' and (not(@rel) or @rel='alternate')]")
            link = (links[0] if links else None) if kind == "feed" else parts.get("loc")
            if link is None:
                continue
            location = link.get("href") or link.text or ""
            if not location:
                continue
            row: dict[str, Any] = {
                "url": urljoin(url, location.strip()),
                "title": "".join(parts["title"].itertext()).strip() if "title" in parts else location.strip(),
                "discovery_kind": kind,
            }
            for key in ("pubDate", "published", "updated", "lastmod"):
                if key in parts and parts[key].text:
                    row[key] = timestamp(parts[key].text.strip(), kind + ":" + key)
            rows.append(row)
    return list({r["url"]: r for r in rows}.values())[:MAX_CANDIDATES]
