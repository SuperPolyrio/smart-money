"""Bounded public HTTPS: validate every DNS answer and pin the actual TLS connection."""

from __future__ import annotations

import ipaddress
import os
import socket
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

import pycurl  # type: ignore[import-untyped]

from smart_money.infrastructure.budget import ResearchBudget

MAX_BYTES = 2 * 1024 * 1024
MAX_REQUESTS = 24
MAX_DOCUMENTS = 6
MAX_CANDIDATES = 60
MAX_SOURCES = 4
TOTAL_SECONDS = 90.0
REQUEST_SECONDS = 20.0
MAX_REDIRECTS = 3
# A stalled system resolver cannot create unbounded threads or dispatch a late HTTP request.
_DNS = ThreadPoolExecutor(max_workers=2, thread_name_prefix="evidence-dns")


class SourcePolicyError(ValueError):
    """The request is outside the registered read-only source boundary."""


def public_host(url: str) -> str:
    if not url or "\\" in url or any(ord(c) <= 32 or ord(c) >= 127 for c in url):
        raise SourcePolicyError("AMBIGUOUS_SOURCE_URL")
    parts = urlsplit(url)
    host = parts.hostname or ""
    if parts.scheme != "https" or not host:
        raise SourcePolicyError("PUBLIC_HTTPS_REQUIRED")
    if parts.username is not None or parts.password is not None:
        raise SourcePolicyError("URL_CREDENTIALS_FORBIDDEN")
    if parts.netloc.lower() not in {host, host + ":443"}:
        raise SourcePolicyError("NON_STANDARD_AUTHORITY_FORBIDDEN")
    if host.endswith((".", ".local", ".internal", ".localhost")) or "." not in host or "%" in host:
        raise SourcePolicyError("BLOCKED_HOST:" + host)
    try:
        ipaddress.ip_address(host)
    except ValueError:
        try:
            # libcurl also understands abbreviated, octal and hexadecimal IPv4 literals.
            socket.inet_aton(host)
        except OSError:
            return host
    raise SourcePolicyError("IP_LITERAL_FORBIDDEN")


def resolve_public(url: str, timeout: float) -> list[str]:
    host = public_host(url)
    future = _DNS.submit(socket.getaddrinfo, host, 443, type=socket.SOCK_STREAM)
    try:
        answers = future.result(timeout=max(0.001, timeout))
    except (OSError, TimeoutError) as exc:
        future.cancel()
        raise SourcePolicyError("SOURCE_DNS_UNRESOLVED:" + host) from exc
    addresses = list(dict.fromkeys(str(answer[4][0]) for answer in answers))
    if not addresses:
        raise SourcePolicyError("SOURCE_DNS_UNRESOLVED:" + host)
    for value in addresses:
        address = ipaddress.ip_address(value)
        deny = [v.strip() for v in os.environ.get("SMART_MONEY_EVIDENCE_DENY_CIDRS", "").split(",") if v.strip()]
        local_data_host = os.environ.get("xue_lab_ip", "")
        if local_data_host:
            try:
                deny.append(str(ipaddress.ip_address(local_data_host)))
            except ValueError:
                if host == local_data_host.lower():
                    raise SourcePolicyError("SOURCE_PRODUCTION_HOST_FORBIDDEN") from None
        if any(address in ipaddress.ip_network(network, strict=False) for network in deny):
            raise SourcePolicyError("SOURCE_PRODUCTION_NETWORK_FORBIDDEN")
        if not address.is_global or address.is_multicast or address.is_reserved:
            raise SourcePolicyError("SOURCE_DNS_NOT_PUBLIC:" + host)
        # Do not admit translation/tunnel addresses that conceal another destination.
        if isinstance(address, ipaddress.IPv6Address) and (
            address.ipv4_mapped
            or address.sixtofour
            or address.teredo
            or address in ipaddress.ip_network("64:ff9b::/96")
        ):
            raise SourcePolicyError("SOURCE_DNS_NOT_PUBLIC:" + host)
    return addresses


@dataclass
class RetrievalBudget:
    """One research's initial collection and supplement share these limits."""

    requests: int = 0
    documents: set[str] = field(default_factory=set)
    sources: set[str] = field(default_factory=set)
    candidates: int = 0
    seconds_used: float = 0
    attempts: list[dict[str, Any]] = field(default_factory=list)

    def remaining(self) -> float:
        remaining = TOTAL_SECONDS - self.seconds_used
        if remaining <= 0:
            raise SourcePolicyError("RETRIEVAL_DEADLINE")
        return remaining

    def reserve(self, url: str, shared: ResearchBudget | None) -> float:
        if self.requests >= MAX_REQUESTS:
            raise SourcePolicyError("RETRIEVAL_REQUEST_LIMIT")
        timeout = min(REQUEST_SECONDS, self.remaining())
        if shared:
            timeout = shared.reserve("public-evidence", timeout)
        self.requests += 1
        return timeout


@dataclass
class HttpDocument:
    status: int
    headers: dict[str, str]
    body: bytes
    url: str
    wire_bytes: int


def fetch_public(url: str, *, timeout: float, max_bytes: int) -> HttpDocument:
    """No environment proxy, cookies, automatic redirects, or unchecked DNS re-resolution."""
    started = time.monotonic()
    host = public_host(url)
    addresses = resolve_public(url, timeout)
    remaining = timeout - (time.monotonic() - started)
    if remaining <= 0:
        raise SourcePolicyError("SOURCE_REQUEST_DEADLINE")
    body = bytearray()
    headers: dict[str, str] = {}
    header_bytes = 0
    failure = ""

    def write(chunk: bytes) -> int:
        nonlocal failure
        if len(body) + len(chunk) > max_bytes:
            failure = "SOURCE_RESPONSE_TOO_LARGE"
            return 0
        body.extend(chunk)
        return len(chunk)

    def header(line: bytes) -> int:
        nonlocal header_bytes, failure
        header_bytes += len(line)
        if header_bytes > 64 * 1024:
            failure = "SOURCE_HEADERS_TOO_LARGE"
            return 0
        if b":" in line:
            key, value = line.decode("iso-8859-1").split(":", 1)
            headers[key.lower().strip()] = value.strip()
        return len(line)

    def progress(_total: float, downloaded: float, _upload: float, _uploaded: float) -> int:
        nonlocal failure
        if downloaded > max_bytes:
            failure = "SOURCE_COMPRESSED_RESPONSE_TOO_LARGE"
        return int(bool(failure))

    curl = pycurl.Curl()
    try:
        curl.setopt(pycurl.URL, url)
        pinned = ",".join(f"[{a}]" if ":" in a else a for a in addresses)
        curl.setopt(pycurl.RESOLVE, [f"{host}:443:{pinned}"])
        curl.setopt(pycurl.PROXY, "")
        curl.setopt(pycurl.NOPROXY, "*")
        curl.setopt(pycurl.NETRC, pycurl.NETRC_IGNORED)
        curl.setopt(pycurl.FOLLOWLOCATION, False)
        curl.setopt(pycurl.PROTOCOLS, pycurl.PROTO_HTTPS)
        curl.setopt(pycurl.SSL_VERIFYPEER, True)
        curl.setopt(pycurl.SSL_VERIFYHOST, 2)
        curl.setopt(pycurl.CONNECTTIMEOUT_MS, max(1, int(min(10, remaining) * 1000)))
        curl.setopt(pycurl.TIMEOUT_MS, max(1, int(remaining * 1000)))
        curl.setopt(pycurl.NOSIGNAL, True)
        curl.setopt(pycurl.USERAGENT, "SmartMoneyEvidence/1.0")
        curl.setopt(pycurl.ACCEPT_ENCODING, "gzip,deflate")
        curl.setopt(pycurl.MAXFILESIZE_LARGE, max_bytes)
        curl.setopt(pycurl.WRITEFUNCTION, write)
        curl.setopt(pycurl.HEADERFUNCTION, header)
        curl.setopt(pycurl.NOPROGRESS, False)
        curl.setopt(pycurl.XFERINFOFUNCTION, progress)
        curl.perform()
        peer = str(curl.getinfo(pycurl.PRIMARY_IP))
        if ipaddress.ip_address(peer) not in [ipaddress.ip_address(a) for a in addresses]:
            raise SourcePolicyError("SOURCE_PEER_NOT_PINNED")
        return HttpDocument(
            int(curl.getinfo(pycurl.RESPONSE_CODE)),
            headers,
            bytes(body),
            url,
            int(curl.getinfo(pycurl.SIZE_DOWNLOAD_T)),
        )
    except pycurl.error as exc:
        raise SourcePolicyError(failure or f"SOURCE_TRANSPORT_FAILED:{exc.args[0]}") from exc
    finally:
        curl.close()
