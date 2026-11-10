"""Tiny atomic JSON-file store for small bits of persistent server state."""

from __future__ import annotations

import json
import os
import secrets
import tempfile
from typing import Any


def fsync_dir(directory: str) -> None:
    """Make a rename or create in ``directory`` durable. Best effort: some
    platforms and filesystems refuse to fsync a directory."""
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def atomic_write(path: str, data: bytes, *, sync_dir: bool = True) -> None:
    """Replace ``path`` with ``data`` so a crash or power loss leaves the old or
    the new content, never a truncated file: write a uniquely named temp file
    in the same directory (so another process writing the same path cannot
    interleave with it), fsync it, rename it over ``path``, then fsync the
    directory (callers writing several files to one directory may defer that
    to the last write)."""
    directory = os.path.dirname(path) or "."
    tmp = os.path.join(directory, f".{secrets.token_hex(8)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise
    if sync_dir:
        fsync_dir(directory)


class JsonFileStore:
    """Load/save a JSON object atomically. Missing/corrupt file -> empty dict."""

    def __init__(self, path: str) -> None:
        self.path = path

    def load(self) -> dict[str, Any]:
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (FileNotFoundError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def save(self, data: dict[str, Any]) -> None:
        directory = os.path.dirname(self.path) or "."
        os.makedirs(directory, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass
            raise
        fsync_dir(directory)
