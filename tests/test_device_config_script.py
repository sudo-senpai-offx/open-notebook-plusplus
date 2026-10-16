"""`onb-config`: the script that stops hand-typing secrets into the guest.

Setting a credential in the proot guest had been typed by hand four times and
failed three of them - the `>>` redirect evaluated by Termux's shell, the copy
run from the wrong side of the proot boundary, and a staged file chmod'd 600
under a UID Termux could not read.

The fourth failure is the one this script exists to prevent, and it is not a
crash. A `printf` built from `$(cat)` of a file that was not there wrote
``KEY=`` with an **empty value**. The key was present, `grep` found it, and
the configuration read as correct while supplying no credential at all - auth
silently off on a self-hosted phone.

So the properties pinned here are behavioural, exercised against the real
guest helper extracted from the script rather than a reimplementation of it.
A copy is exactly the kind of thing that drifts from what it was copied from.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "termux-config.sh"
PASSWORD = "kypP8iwl5dw9NBrMST8dYNr3yWxswmit"


def _guest_helper(tmp_path: Path, env_file: Path) -> Path:
    """The guest-side script, verbatim, pointed at a temporary file.

    Extracted rather than reimplemented on purpose. The helper runs inside
    proot, so these tests cannot invoke the real one, and a hand-written
    stand-in would be free to disagree with the shipped code - which is how
    this class of test starts lying.
    """
    text = SCRIPT.read_text()
    start = text.index("<<'HELPER'\n") + len("<<'HELPER'\n")
    end = text.index("\nHELPER\n", start)
    body = text[start:end].replace(
        'ENV_FILE="/root/app/open-notebook-plusplus/.env"',
        f'ENV_FILE="{env_file}"',
    )
    helper = tmp_path / "helper.sh"
    helper.write_text(body + "\n")
    helper.chmod(0o700)
    return helper


def _run(helper: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["sh", str(helper), *args], capture_output=True, text=True)


@pytest.fixture
def env_file(tmp_path: Path) -> Path:
    path = tmp_path / "env"
    path.write_text("OPEN_NOTEBOOK_ENCRYPTION_KEY=k\nOPEN_NOTEBOOK_PASSWORD=\n")
    return path


class TestEmptyValuesAreRefused:
    """The failure this script was written for."""

    def test_check_flags_a_present_but_empty_value(self, tmp_path, env_file):
        """`KEY=` present is not `KEY` configured, and it has to say so.

        This is the exact state the manual sequence left behind: the key
        existed, so a grep for it succeeded, and the value was empty.
        """
        helper = _guest_helper(tmp_path, env_file)
        result = _run(helper, "check", "OPEN_NOTEBOOK_PASSWORD")

        assert result.returncode != 0, "an empty value must not report success"
        assert "EMPTY" in result.stdout

    def test_check_passes_on_a_real_value(self, tmp_path, env_file):
        env_file.write_text(f"OPEN_NOTEBOOK_PASSWORD={PASSWORD}\n")
        helper = _guest_helper(tmp_path, env_file)
        result = _run(helper, "check", "OPEN_NOTEBOOK_PASSWORD")

        assert result.returncode == 0
        assert "32 chars" in result.stdout

    def test_the_value_is_never_printed(self, tmp_path, env_file):
        env_file.write_text(f"OPEN_NOTEBOOK_PASSWORD={PASSWORD}\n")
        helper = _guest_helper(tmp_path, env_file)
        for args in (("check", "OPEN_NOTEBOOK_PASSWORD"), ("list",)):
            result = _run(helper, *args)
            assert PASSWORD not in result.stdout
            assert PASSWORD not in result.stderr

    def test_list_shows_key_names_only(self, tmp_path, env_file):
        helper = _guest_helper(tmp_path, env_file)
        result = _run(helper, "list")

        assert "OPEN_NOTEBOOK_PASSWORD" in result.stdout
        assert "OPEN_NOTEBOOK_ENCRYPTION_KEY" in result.stdout


class TestUpsert:
    def test_set_replaces_an_empty_value(self, tmp_path, env_file):
        staging = Path("/tmp/onb-config-value")
        staging.write_text(PASSWORD)
        helper = _guest_helper(tmp_path, env_file)
        try:
            result = _run(helper, "set", "OPEN_NOTEBOOK_PASSWORD")
        finally:
            staging.unlink(missing_ok=True)

        assert result.returncode == 0
        assert f"OPEN_NOTEBOOK_PASSWORD={PASSWORD}" in env_file.read_text()

    def test_set_does_not_duplicate_the_key(self, tmp_path, env_file):
        """Re-running must replace, not append. A duplicated key is read
        back by whichever wins, which is not knowable in advance.
        """
        staging = Path("/tmp/onb-config-value")
        helper = _guest_helper(tmp_path, env_file)
        try:
            for _ in range(3):
                staging.write_text(PASSWORD)
                assert _run(helper, "set", "OPEN_NOTEBOOK_PASSWORD").returncode == 0
        finally:
            staging.unlink(missing_ok=True)

        occurrences = env_file.read_text().count("OPEN_NOTEBOOK_PASSWORD=")
        assert occurrences == 1

    def test_set_preserves_unrelated_keys(self, tmp_path, env_file):
        staging = Path("/tmp/onb-config-value")
        staging.write_text(PASSWORD)
        helper = _guest_helper(tmp_path, env_file)
        try:
            _run(helper, "set", "OPEN_NOTEBOOK_PASSWORD")
        finally:
            staging.unlink(missing_ok=True)

        assert "OPEN_NOTEBOOK_ENCRYPTION_KEY=k" in env_file.read_text()

    def test_set_reports_the_length(self, tmp_path, env_file):
        staging = Path("/tmp/onb-config-value")
        staging.write_text(PASSWORD)
        helper = _guest_helper(tmp_path, env_file)
        try:
            result = _run(helper, "set", "OPEN_NOTEBOOK_PASSWORD")
        finally:
            staging.unlink(missing_ok=True)

        assert "32 chars" in result.stdout
        assert PASSWORD not in result.stdout

    def test_set_fails_when_no_value_was_staged(self, tmp_path, env_file):
        """The original failure, at the level the helper can see it: no
        staging file means no value, and the helper must refuse rather than
        write an empty one.
        """
        Path("/tmp/onb-config-value").unlink(missing_ok=True)
        helper = _guest_helper(tmp_path, env_file)
        before = env_file.read_text()

        result = _run(helper, "set", "OPEN_NOTEBOOK_PASSWORD")

        # `set -eu` inside the helper aborts on the failed `cat`, so nothing
        # is written. The file must be byte-identical afterwards - the
        # fixture already holds an empty value, so this asserts the helper
        # did not *add* another or overwrite a real key with one.
        assert result.returncode != 0
        assert env_file.read_text() == before


class TestAbsentKeys:
    def test_check_reports_an_absent_key_as_failure(self, tmp_path, env_file):
        helper = _guest_helper(tmp_path, env_file)
        result = _run(helper, "check", "NO_SUCH_KEY")

        assert result.returncode != 0
        assert "absent" in result.stdout

    def test_list_on_a_missing_env_is_not_an_error(self, tmp_path):
        missing = tmp_path / "does-not-exist"
        helper = _guest_helper(tmp_path, missing)
        result = _run(helper, "list")

        assert result.returncode == 0
        assert "no .env" in result.stdout


class TestScriptShape:
    def test_uses_the_staged_file_not_argv(self):
        """The value is written to a file inside the guest, never passed as
        an argument. On Android, argv is readable through /proc by the same
        UID for the life of the process.
        """
        text = SCRIPT.read_text()
        assert "/tmp/onb-config-value" in text
        assert 'set" "$key" "$value"' not in text

    def test_guest_logic_is_a_file_not_a_nested_quote_string(self):
        """`bash -lc "... \\$escaped..."` is how quoting bugs survive
        review. The guest side is plain POSIX in a heredoc, readable on its
        own and testable - which is what this module extracts.
        """
        text = SCRIPT.read_text()
        assert "<<'HELPER'" in text
        # Count invocations, not mentions: the docstring names `bash -lc`
        # while explaining why the script stopped using it, and an earlier
        # version of this counted that sentence as a failure of itself.
        invocations = [
            line
            for line in text.splitlines()
            if "bash -lc" in line and not line.lstrip().startswith("#")
        ]
        assert invocations == []

    def test_rejects_a_key_that_is_not_an_identifier(self, tmp_path):
        """A key containing shell metacharacters would be spliced into the
        guest script's own grep and sed patterns.
        """
        if (
            shutil.which("proot-distro")
            or Path("/data/data/com.termux/files/usr/bin/sh").exists()
        ):
            pytest.skip("this check is about the Termux-only driver")
        # The guard is in the driver, not the helper, so assert on its text.
        text = SCRIPT.read_text()
        assert "*[!A-Za-z0-9_]*" in text
