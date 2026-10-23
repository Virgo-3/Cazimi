"""Bounded, explicit source adapters for Cazimi research observations.

Feeds supply evidence; these adapters do not infer financial metrics from prose.
Credentials are names of environment variables in source configuration and are
resolved only while making a request.  Network errors never include response
bodies, request headers, URL query strings, or underlying exception messages.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import math
import os
import re
import socket
import ssl
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from http.client import HTTPException
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit
from urllib.request import (
    HTTPRedirectHandler,
    HTTPSHandler,
    ProxyHandler,
    Request,
    build_opener,
)
from xml.etree import ElementTree


MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_OBSERVATIONS = 256
SOURCE_CATEGORIES = frozenset(
    {
        "company_filings_ir",
        "market_data_estimates",
        "earnings_transcripts_events",
        "internal_research",
        "portfolio_models_trackers",
    }
)
SOURCE_QUALITIES = frozenset({"primary", "secondary", "user"})
_SOURCE_KEYS = frozenset(
    {"id", "kind", "location", "category", "quality", "ticker", "timeout", "request_cost", "header_env"}
)
_SOURCE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z")
_TICKER = re.compile(r"[A-Za-z0-9^][A-Za-z0-9.\-:/^=]{0,31}\Z")
_HEADER_NAME = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+\Z")
_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_FORBIDDEN_HEADERS = frozenset(
    {"host", "content-length", "transfer-encoding", "connection", "proxy-authorization", "proxy-connection"}
)


def _text(value: object, field: str, maximum: int, *, empty: bool = False) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be text")
    value = value.strip()
    if (not empty and not value) or len(value) > maximum:
        raise ValueError(f"{field} is empty or too long")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError(f"{field} contains control characters")
    return value


def _number(value: object, field: str, *, minimum: float | None = None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} must be a finite number")
    try:
        number = float(value)
    except (OverflowError, ValueError):
        raise ValueError(f"{field} must be a finite number") from None
    if not math.isfinite(number) or (minimum is not None and number < minimum):
        raise ValueError(f"{field} must be a finite number within the allowed range")
    return number


def _ticker(value: object) -> str:
    value = _text(value, "ticker", 32).upper()
    if not _TICKER.fullmatch(value):
        raise ValueError("ticker has an unsupported format")
    return value


def _is_remote(location: str) -> bool:
    return "://" in location


def _network_location(location: str) -> str:
    """Validate transport without printing a possibly secret URL."""
    try:
        parts = urlsplit(location)
        port = parts.port
        hostname = parts.hostname
        if (
            parts.scheme.lower() not in {"https", "http"}
            or not hostname
            or parts.username is not None
            or parts.password is not None
            or parts.fragment
            or any(char.isspace() for char in location)
            or "\\" in location
            or (port is not None and port == 0)
        ):
            raise ValueError
        if parts.scheme.lower() == "http":
            try:
                loopback = ipaddress.ip_address(hostname).is_loopback
            except ValueError:
                loopback = hostname.lower() == "localhost"
            if not loopback:
                raise ValueError
    except (ValueError, UnicodeError):
        raise ValueError("source URL must use HTTPS (HTTP is allowed only for loopback), without credentials or fragments") from None
    return location


def validate_source(raw: dict, base_dir: Path | None = None) -> dict:
    """Return a normalized copy of a configured source, without reading it.

    Relative filenames resolve against ``base_dir`` (or the current directory).
    URL query strings are preserved in configuration; use ``header_env`` for
    secrets because configurations and citations are persisted in audit records.
    """
    if not isinstance(raw, dict):
        raise ValueError("source must be an object")
    if set(raw) - _SOURCE_KEYS:
        raise ValueError("source contains unsupported fields")
    source_id = _text(raw.get("id"), "source id", 64)
    if not _SOURCE_ID.fullmatch(source_id):
        raise ValueError("source id must use letters, digits, dots, underscores, or hyphens")
    kind = raw.get("kind")
    if kind not in ("json", "rss"):
        raise ValueError("source kind must be json or rss")
    category = raw.get("category")
    if not isinstance(category, str) or category not in SOURCE_CATEGORIES:
        raise ValueError("source category is unsupported")
    quality = raw.get("quality")
    if not isinstance(quality, str) or quality not in SOURCE_QUALITIES:
        raise ValueError("source quality must be primary, secondary, or user")
    location = _text(raw.get("location"), "source location", 4096)
    if _is_remote(location):
        location = _network_location(location)
    else:
        # Explicit URL schemes without // are errors, except Windows drive paths.
        if re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", location) and not re.match(r"^[A-Za-z]:[\\/]", location):
            raise ValueError("source location must be a filesystem path or an HTTPS URL")
        try:
            path = Path(location).expanduser()
            if not path.is_absolute():
                path = (base_dir or Path.cwd()) / path
            location = str(path.resolve())
        except (OSError, ValueError, RuntimeError):
            raise ValueError("source filesystem path is invalid") from None
    timeout = _number(raw.get("timeout", 15), "source timeout", minimum=0.001)
    if timeout > 60:
        raise ValueError("source timeout must not exceed 60 seconds")
    request_cost = _number(raw.get("request_cost", 0), "source request cost", minimum=0)
    headers = raw.get("header_env", {})
    if not isinstance(headers, dict) or len(headers) > 32:
        raise ValueError("header_env must map at most 32 header names to environment variable names")
    normalized_headers = {}
    for name, env_name in headers.items():
        if (
            not isinstance(name, str)
            or len(name) > 128
            or not _HEADER_NAME.fullmatch(name)
            or name.lower() in _FORBIDDEN_HEADERS
            or name.lower() in {header.lower() for header in normalized_headers}
            or not isinstance(env_name, str)
            or len(env_name) > 128
            or not _ENV_NAME.fullmatch(env_name)
        ):
            raise ValueError("header_env contains an invalid header or environment variable name")
        normalized_headers[name] = env_name
    if normalized_headers and not _is_remote(location):
        raise ValueError("local sources cannot use request headers")
    source = {
        "id": source_id,
        "kind": kind,
        "location": location,
        "category": category,
        "quality": quality,
        "timeout": timeout,
        "request_cost": request_cost,
        "header_env": normalized_headers,
    }
    if "ticker" in raw:
        source["ticker"] = _ticker(raw["ticker"])
    elif kind == "rss":
        raise ValueError("RSS and Atom sources require a ticker")
    return source


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _read_source(source: dict) -> bytes:
    location = source["location"]
    if not _is_remote(location):
        try:
            with Path(location).open("rb") as stream:
                payload = stream.read(MAX_RESPONSE_BYTES + 1)
        except (OSError, ValueError):
            raise ValueError("source file could not be read") from None
    else:
        headers = {"User-Agent": "Cazimi/0.1", "Accept": "application/json" if source["kind"] == "json" else "application/rss+xml, application/atom+xml, application/xml"}
        for name, env_name in source["header_env"].items():
            value = os.environ.get(env_name)
            if not value or len(value) > 8192 or any(ord(char) < 32 or ord(char) == 127 for char in value):
                raise ValueError("a source credential environment variable is missing or invalid")
            headers[name] = value
        try:
            # Disable both redirects and implicit system/environment proxies.
            opener = build_opener(ProxyHandler({}), _NoRedirect(), HTTPSHandler(context=ssl.create_default_context()))
            request = Request(location, headers=headers, method="GET")
            with opener.open(request, timeout=source["timeout"]) as response:
                payload = response.read(MAX_RESPONSE_BYTES + 1)
        except HTTPError as error:
            code = error.code
            error.close()
            if 300 <= code < 400:
                raise ValueError("source redirect refused; configure the final HTTPS URL explicitly") from None
            raise ValueError(f"source request failed with HTTP status {code}") from None
        except (URLError, OSError, ValueError, UnicodeError, socket.timeout, HTTPException):
            raise ValueError("source request failed") from None
    if len(payload) > MAX_RESPONSE_BYTES:
        raise ValueError("source response exceeds the 2 MiB size limit")
    return payload


def _as_of(value: object, now: datetime, *, xml: bool = False) -> str:
    value = _text(value, "observation as_of", 128)
    try:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            if not xml:
                raise
            parsed = parsedate_to_datetime(value)
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError
        parsed = parsed.astimezone(timezone.utc)
        if parsed > now:
            raise ValueError("future")
    except (ValueError, TypeError, OverflowError, AttributeError):
        raise ValueError("observation as_of must be a valid timezone-aware date at or before the run time") from None
    return parsed.isoformat()


def _provenance(source: dict) -> dict:
    return {"source_id": source["id"], "source_quality": source["quality"], "source_category": source["category"]}


def _observation(raw: object, source: dict, now: datetime) -> dict:
    if not isinstance(raw, dict):
        raise ValueError("every observation must be an object")
    identifier = _text(raw.get("id"), "observation id", 128)
    ticker = _ticker(raw.get("ticker", source.get("ticker")))
    if source.get("ticker") and ticker != source["ticker"]:
        raise ValueError("observation ticker does not match the registered source ticker")
    kind = raw.get("kind")
    if kind not in ("metric", "price", "document"):
        raise ValueError("observation kind must be metric, price, or document")
    result = {
        "id": f"{source['id']}:{identifier}",
        "ticker": ticker,
        "kind": kind,
        "as_of": _as_of(raw.get("as_of"), now),
        "metric": "",
        "value": None,
        "unit": "",
        "period": "",
        "citation": _text(raw.get("citation"), "observation citation", 2048),
        "title": _text(raw.get("title", ""), "observation title", 1024, empty=True),
        **_provenance(source),
    }
    if kind in {"metric", "price"}:
        result["value"] = _number(raw.get("value"), "observation value")
        result["unit"] = _text(raw.get("unit"), "observation unit", 64)
        if kind == "price":
            if result["value"] <= 0:
                raise ValueError("price observations must have a positive value")
            if not re.fullmatch(r"[A-Za-z]{3}", result["unit"]):
                raise ValueError("price observations require a three-letter currency unit")
            result["unit"] = result["unit"].upper()
            result["metric"] = "price"
            result["period"] = "spot"
        else:
            result["metric"] = _text(raw.get("metric"), "observation metric", 128)
            result["period"] = _text(raw.get("period"), "observation period", 128)
    elif raw.get("value") is not None:
        raise ValueError("document observations cannot contain numeric values")
    return result


def _json_observations(payload: bytes, source: dict, now: datetime) -> list[dict]:
    def reject_constant(value):
        raise ValueError("nonfinite JSON number")

    try:
        envelope = json.loads(payload.decode("utf-8-sig"), parse_constant=reject_constant)
    except (ValueError, UnicodeError, RecursionError):
        raise ValueError("source response must be valid UTF-8 JSON without nonfinite numbers") from None
    if not isinstance(envelope, dict) or not isinstance(envelope.get("observations"), list):
        raise ValueError("JSON source requires an object with an observations array")
    if len(envelope["observations"]) > MAX_OBSERVATIONS:
        raise ValueError("source exceeds the 256 observation limit")
    return [_observation(raw, source, now) for raw in envelope["observations"]]


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _child_text(element, *names) -> str:
    for name in names:
        for child in element:
            if _local_name(child.tag) == name:
                return " ".join("".join(child.itertext()).split())
    return ""


def _rss_observations(payload: bytes, source: dict, now: datetime) -> list[dict]:
    try:
        xml_text = payload.decode("utf-8-sig")
    except UnicodeError:
        raise ValueError("document feed must be UTF-8 XML") from None
    if "\x00" in xml_text or re.search(r"<!\s*(?:DOCTYPE|ENTITY)\b", xml_text, re.IGNORECASE):
        raise ValueError("document feed DTD and entity declarations are forbidden")
    try:
        root = ElementTree.fromstring(xml_text)
    except (ElementTree.ParseError, ValueError, RecursionError):
        raise ValueError("document feed must be valid RSS or Atom XML") from None
    root_name = _local_name(root.tag)
    if root_name == "feed":
        entries = [child for child in root if _local_name(child.tag) == "entry"]
        atom = True
    elif root_name == "rss":
        entries = [entry for channel in root if _local_name(channel.tag) == "channel" for entry in channel if _local_name(entry.tag) == "item"]
        atom = False
    else:
        raise ValueError("document feed root must be RSS or Atom")
    if len(entries) > MAX_OBSERVATIONS:
        raise ValueError("source exceeds the 256 observation limit")
    results = []
    for entry in entries:
        date = _child_text(entry, "updated", "published", "pubDate", "date")
        if not date:
            # No observation date can be inferred from the collection time.
            continue
        as_of = _as_of(date, now, xml=True)
        link = ""
        if atom:
            for child in entry:
                if _local_name(child.tag) == "link" and child.attrib.get("rel", "alternate") == "alternate":
                    link = child.attrib.get("href", "")
                    if link:
                        break
        else:
            link = _child_text(entry, "link")
        if link:
            link = urljoin(source["location"], link) if _is_remote(source["location"]) else link
        citation = _text(link or source["location"], "document citation", 2048)
        title = _child_text(entry, "title")
        title = _text(title, "document title", 1024, empty=True)
        identifier = _child_text(entry, "id" if atom else "guid")
        if not identifier:
            identifier = hashlib.sha256(f"{citation}\n{as_of}\n{title}".encode("utf-8")).hexdigest()
        # Feed IDs may be long URLs.  Preserve short IDs; hash long ones stably.
        if len(identifier) > 128:
            identifier = hashlib.sha256(identifier.encode("utf-8")).hexdigest()
        identifier = _text(identifier, "document id", 128)
        results.append(
            {
                "id": f"{source['id']}:{identifier}",
                "ticker": source["ticker"],
                "kind": "document",
                "as_of": as_of,
                "metric": "",
                "value": None,
                "unit": "",
                "period": "",
                "citation": citation,
                "title": title,
                **_provenance(source),
            }
        )
    return results


def fetch_source(source: dict, now: datetime) -> list[dict]:
    """Fetch one configured source and return normalized, source-bound evidence.

    A malformed JSON record fails the whole source, so a run never silently
    accepts a selectively valid subset. Undated RSS entries are skipped because
    collection time is not an evidence date. Duplicate upstream IDs are errors.
    """
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("run time must be a timezone-aware datetime")
    now = now.astimezone(timezone.utc)
    source = validate_source(source)
    payload = _read_source(source)
    observations = _json_observations(payload, source, now) if source["kind"] == "json" else _rss_observations(payload, source, now)
    ids = [observation["id"] for observation in observations]
    if len(ids) != len(set(ids)):
        raise ValueError("source contains duplicate observation ids")
    return observations
