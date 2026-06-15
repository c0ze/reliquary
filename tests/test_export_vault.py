from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

import export_vault  # noqa: E402
from blobs import BlobStore  # noqa: E402
from compiled import PageRegistry  # noqa: E402


def _registry(tmp_path):
    blobs = BlobStore(blob_dir=str(tmp_path / "blobs"), signing_key=b"k", max_bytes=0)
    return PageRegistry(registry_dir=str(tmp_path / "compiled"), blobs=blobs)


def test_export_writes_one_md_per_page_under_domain(tmp_path):
    reg = _registry(tmp_path)
    reg.put_revision("brigid", "Forge goddess body.", {"title": "Brigid", "domain": "pagan"})
    reg.put_revision("loose-note", "No domain here.", {"title": "Loose"})

    out = tmp_path / "vault"
    written = export_vault.export_vault(reg, str(out))

    assert written == 2
    pagan_file = out / "pagan" / "brigid.md"
    loose_file = out / "_" / "loose-note.md"
    assert pagan_file.exists() and loose_file.exists()
    text = pagan_file.read_text(encoding="utf-8")
    assert text.startswith("---\n") and "Forge goddess body." in text  # frontmatter + body


def test_export_is_idempotent(tmp_path):
    reg = _registry(tmp_path)
    reg.put_revision("brigid", "v1", {"domain": "pagan"})
    out = tmp_path / "vault"
    assert export_vault.export_vault(reg, str(out)) == 1
    # Re-export overwrites cleanly (no .tmp left behind, no error).
    assert export_vault.export_vault(reg, str(out)) == 1
    assert (out / "pagan" / "brigid.md").exists()
    assert not list((out / "pagan").glob("*.tmp"))


def test_main_reports_and_exits_zero(tmp_path, capsys):
    reg = _registry(tmp_path)
    reg.put_revision("brigid", "body", {"domain": "pagan"})
    out = tmp_path / "vault"
    code = export_vault.main([
        "--out", str(out),
        "--compiled-dir", str(tmp_path / "compiled"),
        "--blob-dir", str(tmp_path / "blobs"),
    ])
    assert code == 0
    assert "Exported 1 page(s)" in capsys.readouterr().out
