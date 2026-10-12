"""P7 vault scan: pure find-new-markdown + watermark (no database)."""

import os

from open_notebook.vault.ingest import load_watermark, save_watermark, scan_vault


def test_scan_finds_new_markdown(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "a.md").write_text("# A")
    (vault / "b.md").write_text("# B")
    (vault / "img.png").write_bytes(b"\x00")
    found, watermark = scan_vault(str(vault), {})
    assert sorted(p.name for p in found) == ["a.md", "b.md"]
    assert set(watermark) == {"a.md", "b.md"}


def test_scan_skips_unchanged(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "a.md").write_text("# A")
    _, watermark = scan_vault(str(vault), {})
    found, _ = scan_vault(str(vault), watermark)
    assert found == []


def test_scan_picks_up_modified(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    target = vault / "a.md"
    target.write_text("# A")
    _, watermark = scan_vault(str(vault), {})
    os.utime(target, (watermark["a.md"] + 10, watermark["a.md"] + 10))
    found, _ = scan_vault(str(vault), watermark)
    assert [p.name for p in found] == ["a.md"]


def test_watermark_round_trip(tmp_path):
    path = str(tmp_path / "wm.json")
    assert load_watermark(path) == {}
    save_watermark(path, {"a.md": 123.0})
    assert load_watermark(path) == {"a.md": 123.0}
    assert load_watermark("/nonexistent/wm.json") == {}
