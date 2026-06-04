import socket
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from urlfetch import validate_public_url  # noqa: E402


def _patch_dns(monkeypatch, ip):
    monkeypatch.setattr(socket, "getaddrinfo",
        lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (ip, a[1]))])


def test_rejects_non_http():
    assert validate_public_url("ftp://example.com/x.png") is not None
    assert validate_public_url("file:///etc/passwd") is not None


def test_rejects_private_and_loopback(monkeypatch):
    for ip in ("127.0.0.1", "10.0.0.5", "192.168.1.10", "169.254.1.1", "172.16.0.1"):
        _patch_dns(monkeypatch, ip)
        assert validate_public_url("http://internal.example/x") is not None


def test_allows_public(monkeypatch):
    _patch_dns(monkeypatch, "93.184.216.34")  # example.com
    assert validate_public_url("https://example.com/cat.png") is None


def test_missing_host():
    assert validate_public_url("http:///x") is not None
