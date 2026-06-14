from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))


def test_proxy_builds_compiled_layer(proxy):
    assert proxy.pages is not None
    assert proxy.compiled_memory is not None


def test_compiled_layer_disabled_when_no_collection(make_proxy):
    p = make_proxy(compiled_collection="")
    assert p.pages is None
    assert p.compiled_memory is None
