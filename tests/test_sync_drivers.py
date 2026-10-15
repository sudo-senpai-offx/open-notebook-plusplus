"""The two sync drivers, and the offline route between them.

`pc-sync.sh` and `termux-sync.sh` drive the same three API endpoints so
there is only ever one notion of what the data is - a driver that read
the database itself would agree with the API today and diverge the first
time an export rule changed.

What is tested here is the part with no network: a git bundle carried as
one file. It exists because the phone cannot open a TCP connection to
the PC on the LAN, so "just fetch it from the remote" is not on offer
without it, and the fallback has to work with no internet at either end.

The divergence guard gets the most attention. It once detected a
rewritten history correctly and then told the operator to go and look,
which on a phone with no interactive git means guessing. It must refuse,
name the exact command, and leave HEAD where it was.
"""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
PC_SYNC = REPO / "scripts" / "pc-sync.sh"
TERMUX_SYNC = REPO / "scripts" / "termux-sync.sh"


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def _init(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    _git("init", "-q", "-b", "main", cwd=path)
    _git("config", "user.email", "test@localhost", cwd=path)
    _git("config", "user.name", "test", cwd=path)


def _commit(path: Path, name: str, content: str) -> None:
    target = path / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)
    _git("add", "-A", cwd=path)
    _git("commit", "-qm", f"sync: {name}", cwd=path)


def _drive(script: Path, sync_dir: Path, *args: str) -> subprocess.CompletedProcess:
    """Run a driver against a sync dir, with the API deliberately dead.

    These commands must not need the app: everything here is git. If one
    reaches for the API it will fail against a closed port, which is the
    point - it proves the offline route touches no network at all.
    """
    return subprocess.run(
        ["sh", str(script), *args],
        capture_output=True,
        text=True,
        env={
            "PATH": "/usr/bin:/bin:/usr/local/bin",
            "HOME": str(sync_dir.parent),
            "SYNC_DIR": str(sync_dir),
            "SYNC_API": "http://127.0.0.1:1",  # nothing listens here
            "TOKEN_FILE": str(sync_dir.parent / "no-such-env"),
        },
    )


pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")


class TestTheDrivers:
    def test_both_expose_the_same_verbs(self):
        """A verb on one side and not the other is a silent asymmetry.

        Parsed from the dispatch block rather than the whole file: a
        looser scan also picks up shell assignments like
        `hdr=$(auth_header)`, which merely happen to end in a bracket.
        """

        def verbs(path: Path) -> set[str]:
            text = path.read_text()
            block = text[text.index('case "${1:-') :]
            return {
                m.group(1)
                for m in re.finditer(r"^  ([a-z][a-z-]*)\)", block, re.MULTILINE)
            }

        assert verbs(PC_SYNC) == verbs(TERMUX_SYNC)

    def test_both_offline_verbs_are_present(self):
        """The route that needs no internet has to be reachable from both ends."""

        for script in (PC_SYNC, TERMUX_SYNC):
            text = script.read_text()
            assert "cmd_bundle()" in text, f"{script.name} cannot create a bundle"
            assert "cmd_fetch_bundle()" in text, f"{script.name} cannot read one"

    def test_init_needs_no_remote(self, tmp_path):
        """The offline route has no URL, so requiring one made it unusable.

        `init` is the command that exists to prepare a repository for
        syncing. Demanding a remote meant the one case with no internet
        could not be set up at all.
        """
        target = tmp_path / "sync"
        result = _drive(PC_SYNC, target, "init")

        assert result.returncode == 0, result.stderr
        assert (target / ".git").is_dir()
        assert "offline via bundle" in result.stdout

    def test_a_missing_bundle_names_the_copy_step(self, tmp_path):
        """The path that failed is not the one the phone has.

        Termux cannot read /data/data from adb, so a bundle has to be
        copied into the guest first. Saying only "no bundle at <path>"
        leaves the operator guessing which directory was meant.
        """
        sync_dir = tmp_path / "sync"
        _drive(PC_SYNC, sync_dir, "init")
        result = _drive(
            PC_SYNC, sync_dir, "fetch-bundle", str(tmp_path / "absent.bundle")
        )

        assert result.returncode != 0
        assert "no bundle at" in result.stderr

    def test_the_phone_driver_names_the_guest_path(self, tmp_path):
        """Same, and specifically the proot path it has to be copied to."""
        sync_dir = tmp_path / "sync"
        _drive(TERMUX_SYNC, sync_dir, "init")
        result = _drive(
            TERMUX_SYNC, sync_dir, "fetch-bundle", str(tmp_path / "absent.bundle")
        )

        assert result.returncode != 0
        assert "proot-distro" in result.stderr, (
            "the recovery step has to name where Termux has to copy the file"
        )


class TestOfflineRoundTrip:
    def test_a_bundle_carries_state_with_no_network(self, tmp_path):
        a, b = tmp_path / "a", tmp_path / "b"
        bundle = tmp_path / "one.bundle"

        _drive(PC_SYNC, a, "init")
        _commit(a, "notes/note-1.json", '{"title":"from the PC"}')
        assert _drive(PC_SYNC, a, "bundle", str(bundle)).returncode == 0
        assert bundle.exists()

        _init(b)
        result = _drive(PC_SYNC, b, "fetch-bundle", str(bundle))

        assert result.returncode == 0, result.stderr
        assert (b / "notes" / "note-1.json").read_text() == '{"title":"from the PC"}'

    def test_state_flows_back_the_other_way(self, tmp_path):
        """One direction is not a sync."""
        a, b = tmp_path / "a", tmp_path / "b"
        _drive(PC_SYNC, a, "init")
        _commit(a, "notes/pc.json", "pc")
        _drive(PC_SYNC, a, "bundle", str(tmp_path / "ab.bundle"))

        _init(b)
        _drive(PC_SYNC, b, "fetch-bundle", str(tmp_path / "ab.bundle"))
        _commit(b, "notes/phone.json", "phone")
        _drive(PC_SYNC, b, "bundle", str(tmp_path / "ba.bundle"))

        result = _drive(PC_SYNC, a, "fetch-bundle", str(tmp_path / "ba.bundle"))

        assert result.returncode == 0, result.stderr
        assert sorted(p.name for p in (a / "notes").iterdir()) == [
            "pc.json",
            "phone.json",
        ]

    def test_fetching_the_same_bundle_twice_is_harmless(self, tmp_path):
        """Idempotence, so a retry after an interrupted sync is safe."""
        a, b = tmp_path / "a", tmp_path / "b"
        _drive(PC_SYNC, a, "init")
        _commit(a, "notes/x.json", "x")
        _drive(PC_SYNC, a, "bundle", str(tmp_path / "x.bundle"))

        _init(b)
        assert (
            _drive(PC_SYNC, b, "fetch-bundle", str(tmp_path / "x.bundle")).returncode
            == 0
        )
        second = _drive(PC_SYNC, b, "fetch-bundle", str(tmp_path / "x.bundle"))

        assert second.returncode == 0, second.stderr


def _divergent(tmp_path: Path) -> tuple[Path, Path]:
    """Two histories sharing a root and neither containing the other.

    This is the exact shape of the incident that broke a deploy: a
    commit was amended after it had shipped, so the two sides became
    siblings and no fast-forward existed between them.
    """
    x, y = tmp_path / "x", tmp_path / "y"
    _init(x)
    _init(y)

    _commit(x, "root.txt", "root")
    _git("fetch", "-q", str(x), "main", cwd=y)
    _git("reset", "-q", "--hard", "FETCH_HEAD", cwd=y)

    _commit(x, "x.txt", "x")
    _commit(y, "y.txt", "y")
    return x, y


def _unrelated(tmp_path: Path) -> tuple[Path, Path]:
    """Two histories sharing no commit at all.

    The shape of the first sync: the phone held its October history and
    the remote held a fresh two-commit history from a bare `git init`.
    A fast-forward can never exist between them, and calling that a
    conflict sends the operator to resolve an edit collision that never
    happened.
    """
    x, y = tmp_path / "x", tmp_path / "y"
    _init(x)
    _init(y)

    _commit(x, "x.txt", "x")
    _commit(y, "y.txt", "y")
    return x, y


class TestTheDivergenceGuard:
    @pytest.mark.parametrize("script", [PC_SYNC, TERMUX_SYNC], ids=["pc", "phone"])
    def test_divergence_is_refused_and_HEAD_is_untouched(self, tmp_path, script):
        x, y = _divergent(tmp_path)
        bundle = tmp_path / "y.bundle"
        _drive(script, y, "bundle", str(bundle))
        before = _git("rev-parse", "HEAD", cwd=x)

        result = _drive(script, x, "fetch-bundle", str(bundle))

        assert result.returncode != 0, "a rewrite must not be silently accepted"
        assert _git("rev-parse", "HEAD", cwd=x) == before, (
            "HEAD must not move on refusal"
        )

    @pytest.mark.parametrize("script", [PC_SYNC, TERMUX_SYNC], ids=["pc", "phone"])
    def test_the_refusal_names_the_command_to_recover(self, tmp_path, script):
        """It detected the problem correctly and made the operator diagnose it.

        There is no interactive git on a phone, so "inspect and decide"
        is not an instruction - it is a dead end unless the command is
        spelled out.
        """
        x, y = _divergent(tmp_path)
        bundle = tmp_path / "y.bundle"
        _drive(script, y, "bundle", str(bundle))

        result = _drive(script, x, "fetch-bundle", str(bundle))
        out = result.stdout + result.stderr

        assert "reset --hard FETCH_HEAD" in out, f"no recovery command given:\n{out}"
        assert "log --oneline" in out, f"no way to inspect:\n{out}"

    def test_a_stale_local_commit_is_listed_by_hash(self, tmp_path):
        """So the operator can tell a rewrite from real work."""
        x, y = _divergent(tmp_path)
        bundle = tmp_path / "y.bundle"
        _drive(PC_SYNC, y, "bundle", str(bundle))

        result = _drive(PC_SYNC, x, "fetch-bundle", str(bundle))
        local = _git("rev-parse", "--short", "HEAD", cwd=x)

        assert local in result.stdout + result.stderr


class TestUnrelatedHistoriesAreNotConflicts:
    """The first sync, verbatim: a fresh remote against an old device.

    The phone held its October history; the remote held a fresh
    two-commit history from a bare `git init`. No fast-forward can ever
    exist, and the old message called it "someone edited the same
    record" - sending the operator to resolve an edit collision that
    never happened, on a branch that shared nothing with the remote.
    """

    @pytest.mark.parametrize("script", [PC_SYNC, TERMUX_SYNC], ids=["pc", "phone"])
    def test_pull_names_unrelated_histories(self, tmp_path, script):
        x, y = _unrelated(tmp_path)
        _git("remote", "add", "origin", str(y), cwd=x)

        result = _drive(script, x, "pull")

        out = result.stdout + result.stderr
        assert result.returncode != 0
        assert "unrelated histories" in out, f"misdiagnosed:\n{out}"
        assert "edited the same record" not in out, f"misdiagnosed:\n{out}"
        assert "reset --hard FETCH_HEAD" in out, f"no recovery command:\n{out}"

    @pytest.mark.parametrize("script", [PC_SYNC, TERMUX_SYNC], ids=["pc", "phone"])
    def test_a_refused_pull_changes_nothing(self, tmp_path, script):
        x, y = _unrelated(tmp_path)
        _git("remote", "add", "origin", str(y), cwd=x)
        before = _git("rev-parse", "HEAD", cwd=x)

        _drive(script, x, "pull")

        assert _git("rev-parse", "HEAD", cwd=x) == before
        assert _git("branch", "--show-current", cwd=x) == "main"


class TestAPullFailureIsNotReportedAsAConflict:
    """A guard that names the wrong reason sends the operator to fix one
    that does not exist.

    `pull` used to discard stderr and report every failure - including a
    network drop or a refused credential - as "someone edited the same
    record". On a phone with no interactive git, the operator then tried
    to resolve a conflict on a branch that had never been committed to.
    The output has to say which problem it actually is.
    """

    @pytest.mark.parametrize("script", [PC_SYNC, TERMUX_SYNC], ids=["pc", "phone"])
    def test_an_unreachable_remote_is_not_a_conflict(self, tmp_path, script):
        sync_dir = tmp_path / "sync"
        _drive(script, sync_dir, "init", "https://does-not-exist.invalid/none.git")

        result = _drive(script, sync_dir, "pull")

        out = result.stdout + result.stderr
        assert "edited the same record" not in out, "misdiagnosed: " + out
        assert "network or the credential" in out, "cause not named: " + out

    @pytest.mark.parametrize("script", [PC_SYNC, TERMUX_SYNC], ids=["pc", "phone"])
    def test_a_failed_pull_changes_nothing_locally(self, tmp_path, script):
        sync_dir = tmp_path / "sync"
        _drive(script, sync_dir, "init", "https://does-not-exist.invalid/none.git")

        result = _drive(script, sync_dir, "pull")

        assert result.returncode != 0
        before = list(sync_dir.glob("*"))
        assert before == [] or all(p.name == ".git" for p in sync_dir.iterdir()), (
            "a failed pull must not leave files behind"
        )


class TestNoCourseContentInTheRepo:
    def test_the_sync_dir_is_not_committed_to_the_app_repo(self):
        """The manifest has its own repository and its own remote.

        Left un-ignored it would also try to become a gitlink here, and
        a transcript would end up in this repository's history - the one
        thing the design exists to prevent.
        """
        ignored = subprocess.run(
            ["git", "-C", str(REPO), "check-ignore", "-q", "sync/"],
            capture_output=True,
        )
        assert ignored.returncode == 0, "sync/ is not gitignored in the app repository"

    def test_the_sync_dir_has_its_own_remote(self, tmp_path):
        """Otherwise the content lands wherever the app repo points."""
        sync_dir = REPO / "sync"
        if not (sync_dir / ".git").is_dir():
            pytest.skip("sync repo not initialised on this machine")

        remotes = _git("remote", "-v", cwd=sync_dir)
        assert remotes, "the sync repo has no remote of its own"

    def test_the_api_can_see_the_manifest_directory(self):
        """A container that does not mount it writes the manifest away.

        The export succeeds, git finds nothing to commit, and the whole
        thing looks like it worked.
        """
        compose = (REPO / "docker-compose.yml").read_text()
        assert "./sync:/app/sync" in compose, (
            "the sync directory is not mounted into the container, so the "
            "manifest the API writes is invisible to git on the host"
        )
