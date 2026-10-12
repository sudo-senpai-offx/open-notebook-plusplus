"""Note export: stable filenames, frontmatter, on-demand only."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from open_notebook.exceptions import NotFoundError


def _client():
    from api.main import app

    return TestClient(app)


def _note(**over):
    base = {
        "id": "note:abc123",
        "title": "My Study Note!",
        "note_type": "human",
        "content": "# Hello\nBody text.",
    }
    base.update(over)
    return SimpleNamespace(**base)


def test_export_writes_frontmatter_file(tmp_path, monkeypatch):
    monkeypatch.setenv("OPEN_NOTEBOOK_EXPORT_DIR", str(tmp_path))
    with patch("api.routers.notes.Note.get", new=AsyncMock(return_value=_note())):
        response = _client().post("/api/notes/note:abc123/export")
    assert response.status_code == 200
    body = response.json()
    assert body["filename"].startswith("my-study-note--note-abc123")
    assert body["filename"].endswith(".md")
    text = (tmp_path / body["filename"]).read_text()
    assert text.startswith("---\n")
    assert "title: My Study Note!" in text
    assert "open_notebook_id: 'note:abc123'" in text
    assert "# Hello" in text


def test_export_missing_is_404():
    with patch(
        "api.routers.notes.Note.get",
        new=AsyncMock(side_effect=NotFoundError("gone")),
    ):
        response = _client().post("/api/notes/note:nope/export")
    assert response.status_code == 404


def test_export_idempotent_filename(tmp_path, monkeypatch):
    monkeypatch.setenv("OPEN_NOTEBOOK_EXPORT_DIR", str(tmp_path))
    with patch("api.routers.notes.Note.get", new=AsyncMock(return_value=_note())):
        first = _client().post("/api/notes/note:abc123/export").json()
        second = _client().post("/api/notes/note:abc123/export").json()
    assert first["filename"] == second["filename"]
