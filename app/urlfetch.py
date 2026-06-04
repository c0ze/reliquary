"""SSRF-guarding URL validation for server-side image ingest (stdlib only)."""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlsplit


def validate_public_url(url: str) -> str | None:
    """Return None if the URL is safe to fetch, else a human-readable reason.

    Rejects non-http(s) schemes and any host that resolves to a private,
    loopback, link-local, reserved, or multicast address (SSRF guard)."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return "unparseable URL"
    if parts.scheme not in ("http", "https"):
        return "only http/https URLs are allowed"
    host = parts.hostname
    if not host:
        return "missing host"
    port = parts.port or (443 if parts.scheme == "https" else 80)
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        return "host does not resolve"
    if not infos:
        return "host does not resolve"
    for info in infos:
        ip_text = info[4][0]
        try:
            addr = ipaddress.ip_address(ip_text)
        except ValueError:
            return f"invalid resolved address {ip_text!r}"
        if (
            not addr.is_global
            or addr.is_private
            or addr.is_loopback
            or addr.is_link_local
            or addr.is_reserved
            or addr.is_multicast
        ):
            return f"host resolves to a non-public address ({ip_text})"
    return None
