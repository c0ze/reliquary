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
        resolve_public_url(url)
    except ValueError as exc:
        return str(exc)
    return None


def resolve_public_url(url: str) -> str:
    """Resolve and validate once, returning a public IP to connect to directly.

    Fetching the original hostname after validation would resolve DNS again,
    allowing a rebinding host to switch to a private address between checks.
    """
    try:
        parts = urlsplit(url)
        host = parts.hostname
        port = parts.port or (443 if parts.scheme == "https" else 80)
    except ValueError as exc:
        raise ValueError("unparseable URL or invalid port") from exc
    if parts.scheme not in ("http", "https"):
        raise ValueError("only http/https URLs are allowed")
    if not host:
        raise ValueError("missing host")
    if parts.username is not None or parts.password is not None:
        raise ValueError("URL credentials are not allowed")
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except (OSError, UnicodeError) as exc:
        raise ValueError("host does not resolve") from exc
    if not infos:
        raise ValueError("host does not resolve")
    for info in infos:
        ip_text = info[4][0]
        try:
            addr = ipaddress.ip_address(ip_text)
        except ValueError:
            raise ValueError(f"invalid resolved address {ip_text!r}")
        # Classify IPv4-mapped IPv6 (::ffff:a.b.c.d) by its embedded IPv4
        # address — older ipaddress versions mark mapped internals as global.
        if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
            addr = addr.ipv4_mapped
        if (
            not addr.is_global
            or addr.is_private
            or addr.is_loopback
            or addr.is_link_local
            or addr.is_reserved
            or addr.is_multicast
        ):
            raise ValueError(f"host resolves to a non-public address ({ip_text})")
    # Prefer IPv4 on hosts without IPv6 egress; IPv6-only destinations still work.
    infos.sort(key=lambda info: info[0] != socket.AF_INET)
    return infos[0][4][0]
