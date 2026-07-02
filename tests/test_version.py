"""Drift guard: pyproject.toml's version must match the MCP server's
serverInfo version (MCP_SERVER_VERSION), which is what connectors actually
see during the MCP handshake."""

from __future__ import annotations

import sys
import tomllib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from server import MCP_SERVER_VERSION  # noqa: E402

PYPROJECT_PATH = Path(__file__).resolve().parents[1] / "pyproject.toml"


def _pyproject_version() -> str:
    with PYPROJECT_PATH.open("rb") as f:
        data = tomllib.load(f)
    return data["project"]["version"]


def test_server_version_matches_pyproject():
    assert MCP_SERVER_VERSION == _pyproject_version(), (
        f"MCP_SERVER_VERSION ({MCP_SERVER_VERSION!r}) in app/server.py has drifted "
        f"from pyproject.toml's version ({_pyproject_version()!r}); keep them in sync."
    )
