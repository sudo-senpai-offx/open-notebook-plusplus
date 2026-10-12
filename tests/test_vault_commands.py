"""Vault ingest command: sources from new markdown, vault untouched."""

from unittest.mock import AsyncMock, patch

import pytest

from commands.vault_commands import VaultIngestInput, ingest_vault_command


def _input(**over):
    args = {"notebook_ids": [], "embed": False}
    args.update(over)
    return VaultIngestInput(**args)


@pytest.mark.asyncio
async def test_ingest_submits_one_job_per_file(tmp_path, monkeypatch):

    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "note.md").write_text("# Note")
    monkeypatch.setenv("OPEN_NOTEBOOK_VAULT_DIR", str(vault))
    monkeypatch.setenv("OPEN_NOTEBOOK_DATA_DIR", str(tmp_path / "data"))

    saved_sources = []

    class FakeSource:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)
            self.id = f"source:{len(saved_sources)}"

        async def save(self):
            saved_sources.append(self)

        async def add_to_notebook(self, _nid):
            pass

    submitted = []

    async def fake_submit(app, name, args):
        submitted.append((app, name, args))
        return "command:xyz"

    with (
        patch("commands.vault_commands.Source", FakeSource),
        patch(
            "api.command_service.CommandService.submit_command_job",
            new=AsyncMock(side_effect=fake_submit),
        ),
    ):
        out = await ingest_vault_command(_input())
    assert out.success is True
    assert out.ingested == 1
    assert submitted[0][0] == "open_notebook"
    assert submitted[0][1] == "process_source"
    assert submitted[0][2]["content_state"]["delete_source"] is False
    assert submitted[0][2]["content_state"]["file_path"].endswith("note.md")


@pytest.mark.asyncio
async def test_second_scan_is_quiet(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "note.md").write_text("# Note")
    monkeypatch.setenv("OPEN_NOTEBOOK_VAULT_DIR", str(vault))
    monkeypatch.setenv("OPEN_NOTEBOOK_DATA_DIR", str(tmp_path / "data"))

    submitted = []

    class FakeSource:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)
            self.id = "source:x"

        async def save(self):
            pass

        async def add_to_notebook(self, _nid):
            pass

    async def fake_submit(app, name, args):
        submitted.append(args)
        return "command:xyz"

    with (
        patch("commands.vault_commands.Source", FakeSource),
        patch(
            "api.command_service.CommandService.submit_command_job",
            new=AsyncMock(side_effect=fake_submit),
        ),
    ):
        first = await ingest_vault_command(_input())
        second = await ingest_vault_command(_input())
    assert first.ingested == 1
    assert second.ingested == 0
    assert len(submitted) == 1


@pytest.mark.asyncio
async def test_missing_vault_dir_is_empty_success(tmp_path, monkeypatch):
    monkeypatch.setenv("OPEN_NOTEBOOK_VAULT_DIR", str(tmp_path / "nope"))
    monkeypatch.setenv("OPEN_NOTEBOOK_DATA_DIR", str(tmp_path / "data"))
    out = await ingest_vault_command(_input())
    assert out.success is True
    assert out.ingested == 0
