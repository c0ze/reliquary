from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

import lint  # noqa: E402
from blobs import BlobStore  # noqa: E402
from compiled import PageRegistry  # noqa: E402


def _registry(tmp_path):
    blobs = BlobStore(blob_dir=str(tmp_path / "blobs"), signing_key=b"k", max_bytes=0)
    return PageRegistry(registry_dir=str(tmp_path / "compiled"), blobs=blobs)


def _dirs(tmp_path):
    return str(tmp_path / "compiled"), str(tmp_path / "blobs")


def test_build_report_finds_stale_page(tmp_path):
    reg = _registry(tmp_path)
    reg.put_revision("fresh", "current body", {"status": "current"})
    reg.put_revision("old", "stale body", {"status": "stale"})

    compiled_dir, blob_dir = _dirs(tmp_path)
    report = lint.build_report(compiled_dir=compiled_dir, blob_dir=blob_dir,
                               dataset_path=None, min_count=8)
    stale_slugs = {item["slug"] for item in report["stale_pages"]}
    assert stale_slugs == {"old"}
    # The four categories are always present.
    assert set(report.keys()) == {"stale_pages", "coverage_gaps", "supersession", "orphans"}


def test_main_strict_exit_code_nonzero_when_proposals(tmp_path):
    reg = _registry(tmp_path)
    reg.put_revision("old", "stale body", {"status": "stale"})

    compiled_dir, blob_dir = _dirs(tmp_path)
    code = lint.main(["--strict", "--compiled-dir", compiled_dir, "--blob-dir", blob_dir])
    assert code == 1  # a stale page is a proposal


def test_main_strict_exit_code_zero_when_clean(tmp_path):
    reg = _registry(tmp_path)
    reg.put_revision("fresh", "current body", {"status": "current"})

    compiled_dir, blob_dir = _dirs(tmp_path)
    code = lint.main(["--strict", "--compiled-dir", compiled_dir, "--blob-dir", blob_dir])
    assert code == 0  # no proposals => clean exit


def test_main_without_strict_always_zero(tmp_path):
    reg = _registry(tmp_path)
    reg.put_revision("old", "stale body", {"status": "stale"})

    compiled_dir, blob_dir = _dirs(tmp_path)
    code = lint.main(["--compiled-dir", compiled_dir, "--blob-dir", blob_dir])
    assert code == 0  # without --strict, proposals do not fail the run


def test_main_json_output(tmp_path, capsys):
    reg = _registry(tmp_path)
    reg.put_revision("old", "stale body", {"status": "stale"})

    compiled_dir, blob_dir = _dirs(tmp_path)
    lint.main(["--json", "--compiled-dir", compiled_dir, "--blob-dir", blob_dir])
    out = capsys.readouterr().out
    import json
    parsed = json.loads(out)
    assert {item["slug"] for item in parsed["stale_pages"]} == {"old"}
