"""onb-update: the device update path, exercised without a device.

This script is the one piece of the sync story that runs *on the
phone*, against a repo inside a proot guest, reached through a git
bundle the operator pushes over adb. Every part of that is untestable
on a development box as written - which is why it is stubbed here rather
than left to be discovered on a device at 1am.

What is pinned:

  * already up to date exits 0 and says so. Nothing to do is success.
    A launcher that treats "no update" as an error teaches you to
    ignore the exit code, which costs you the errors that matter.
  * a real update exits 0 and reports both commits.
  * it refuses while the API answers, because merging underneath a live
    server leaves a half-old process tree.
  * `--check` does NOT refuse while the stack is up. It changes nothing,
    and "is there an update?" is precisely the question you want to ask
    before deciding to restart.
  * `--restart` proceeds instead of refusing.
  * a missing bundle is an error with the adb command in the message,
    because that is the failure a first-time user will hit.
  * a divergent guest repo is refused rather than reset: the guest may
    have local edits, and a silent `reset --hard` throws them away.
"""

import os
import stat
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "termux-update.sh"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


@pytest.fixture
def device(tmp_path: Path):
    """A fake Termux + proot guest with a real git repo in it.

    `proot-distro` and `curl` are stubbed on PATH so the script runs
    unchanged: the same file that ships to the phone is the file under
    test, not a copy of its logic.
    """
    fake_prefix = tmp_path / "termux" / "files" / "usr"
    rootfs = fake_prefix / "var/lib/proot-distro/containers/ubuntu/rootfs"
    guest_repo = rootfs / "root/app/open-notebook-plusplus"
    bin_dir = tmp_path / "bin"
    # guest_repo is deliberately NOT pre-created: copytree requires the
    # destination to be absent.
    for d in (guest_repo.parent, bin_dir, rootfs / "tmp", fake_prefix):
        d.mkdir(parents=True, exist_ok=True)

    # Upstream repo, and a guest clone that starts behind it.
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    _git(upstream, "init", "-q", "-b", "main")
    _git(upstream, "config", "user.email", "t@t")
    _git(upstream, "config", "user.name", "t")
    (upstream / "f").write_text("one\n")
    _git(upstream, "add", "-A")
    _git(upstream, "commit", "-qm", "base")

    # A real clone, not a copy of the directory. `git fetch <bundle>`
    # checks that the bundle's objects match this repository's history,
    # and a copytree gives it a repository that merely contains the same
    # files - so git refuses the bundle, correctly, and the test would be
    # measuring the fixture rather than the script.
    subprocess.run(["git", "clone", "-q", str(upstream), str(guest_repo)], check=True)
    # A clone does not inherit the origin repo's local config, and the
    # divergence test needs to commit in the guest.
    _git(guest_repo, "config", "user.email", "t@t")
    _git(guest_repo, "config", "user.name", "t")

    bundle = tmp_path / "plusplus.bundle"

    # proot-distro stub: rewrite guest-absolute paths into the fake rootfs.
    proot = bin_dir / "proot-distro"
    proot.write_text(
        """#!/bin/sh
# Test stub. Invoked as `proot-distro login ubuntu -- <cmd...>`.
# Drops the `login ubuntu --` prefix, rewrites guest-absolute paths into
# the fake rootfs, and execs the rest.
#
# The prefix matters: without stripping it the stub execs `login`, which
# does not exist, and every git call fails as "fetch rejected" for a
# reason that has nothing to do with git.
[ -n "${FAKE_ROOTFS:-}" ] || exit 1
[ "$1" = "login" ] || exit 2
[ "$2" = "ubuntu" ] || exit 2
shift 3
[ "$1" = "--" ] && shift
out=""
for a in "$@"; do
  case "$a" in
    /root/*|/tmp/*|/usr/*) a="$FAKE_ROOTFS$a" ;;
  esac
  out="$out '$a'"
done
eval "set -- $out"
exec "$@"
"""
    )
    proot.chmod(proot.stat().st_mode | stat.S_IEXEC)

    # curl stub: answers "is the API up" however the test says.
    # Reports "up" for the first two probes and then goes quiet. Those two
    # are the main guard and the pre-stop guard; every probe after that
    # is `stop_stack` polling, and a stack that never goes down would make
    # --restart burn its full poll and fail - correct behaviour, but not
    # what that test is about.
    curl = bin_dir / "curl"
    curl.write_text(
        "#!/bin/sh\n"
        '[ "${FAKE_API_UP:-0}" = "1" ] || exit 7\n'
        'n=$(cat "${FAKE_API_CALLS:-/dev/null}" 2>/dev/null || echo 0)\n'
        "n=$((n + 1))\n"
        'echo "$n" > "${FAKE_API_CALLS:-/dev/null}"\n'
        '[ "$n" -le 2 ] && exit 0\n'
        "exit 7\n"
    )
    curl.chmod(curl.stat().st_mode | stat.S_IEXEC)

    def build_bundle() -> None:
        if bundle.exists():
            bundle.unlink()
        subprocess.run(
            ["git", "-C", str(upstream), "bundle", "create", "-q", str(bundle), "main"],
            check=True,
        )

    # A bundle exists from the start, pinned to the commit the guest is
    # already on. That is the ordinary state: the operator pushed
    # something, applied it, and now runs the updater again.
    build_bundle()

    def advance() -> None:
        """Commit upstream and rebuild the bundle, as a later push would."""
        (upstream / "f").write_text("two\n")
        _git(upstream, "add", "-A")
        _git(upstream, "commit", "-qm", "feat: something new")
        build_bundle()

    def run(*args: str, api_up: bool = False):
        env = dict(os.environ)
        env["PATH"] = f"{bin_dir}:{env['PATH']}"
        env["FAKE_ROOTFS"] = str(rootfs)
        env["FAKE_API_UP"] = "1" if api_up else "0"
        env["PREFIX"] = str(fake_prefix)
        env["ONB_API_PORT"] = "59999"  # nothing listens here
        env["ONB_UPDATE_STATE"] = str(tmp_path / "state")
        env["ONB_UPDATE_SRC"] = str(bundle)
        env["HOME"] = str(tmp_path)
        env["FAKE_API_CALLS"] = str(tmp_path / "api-calls")
        return subprocess.run(
            ["sh", str(SCRIPT), *args], capture_output=True, text=True, env=env
        )

    return {
        "run": run,
        "advance": advance,
        "bundle": bundle,
        "guest": guest_repo,
        "upstream": upstream,
        "tmp": tmp_path,
    }


def test_up_to_date_is_success(device):
    """Exit 0 and one line. Nothing to do is not a failure."""
    result = device["run"]("--check")

    assert result.returncode == 0, result.stdout + result.stderr
    assert "up to date" in result.stdout


def test_an_available_update_is_reported(device):
    device["advance"]()

    result = device["run"]("--check")

    assert result.returncode == 0
    assert "update available" in result.stdout
    assert "something new" in result.stdout


def test_it_refuses_to_update_while_the_stack_is_up(device):
    """The whole reason this script exists."""
    device["advance"]()

    result = device["run"](api_up=True)

    assert result.returncode == 1
    assert "running" in result.stdout
    assert "--restart" in result.stdout
    # And it really did not touch the repo.
    assert len(_git(device["guest"], "log", "--oneline").splitlines()) == 1


def test_check_still_works_while_the_stack_is_up(device):
    """Read-only, so it may run against a live stack.

    "Is there an update?" is exactly the question you want to ask before
    deciding whether to restart anything.
    """
    device["advance"]()

    result = device["run"]("--check", api_up=True)

    assert result.returncode == 0
    assert "update available" in result.stdout


def test_restart_proceeds_against_a_running_stack(device):
    device["advance"]()

    result = device["run"]("--restart", api_up=True)

    assert "ERROR: the stack is running" not in result.stdout
    assert "updated to" in result.stdout
    # The merge is what matters: a fast-forward landed in the guest.
    assert len(_git(device["guest"], "log", "--oneline").splitlines()) == 2


def test_a_missing_bundle_explains_how_to_push_one(device):
    device["bundle"].unlink()

    result = device["run"]("--check")

    assert result.returncode != 0
    assert "adb push" in result.stdout


def test_a_divergent_guest_repo_is_refused_not_reset(device):
    """Silent `reset --hard` would throw away device-local edits."""
    device["advance"]()
    guest = device["guest"]
    # On main itself. A side branch that is checked out again leaves
    # main fast-forwardable, which is a perfectly good update rather than
    # a divergence - the branch test would pass for the wrong reason.
    (guest / "local").write_text("work in progress\n")
    _git(guest, "add", "-A")
    _git(guest, "commit", "-qm", "wip")

    result = device["run"]()

    assert result.returncode == 2
    assert "diverged" in result.stdout
    # Still there, and still first: a refusal must not have reset it.
    assert "wip" in _git(guest, "log", "--oneline").splitlines()[0]


def test_help_exits_zero(device):
    assert device["run"]("--help").returncode == 0


def test_an_unknown_option_is_rejected(device):
    assert device["run"]("--nope").returncode == 2


def test_install_and_launch_reference_the_script():
    """A wrapper that is committed but never installed drifts - that is
    how onb-embed fell out of step with termux-embed.sh in the first
    place."""
    installer = (REPO / "scripts" / "install-bin.sh").read_text()
    launcher = (REPO / "scripts" / "termux-launch.sh").read_text()

    assert "termux-update.sh onb-update" in installer
    assert "--launch-check" in launcher


def test_the_launch_hook_never_fails_a_launch():
    """A hook that can block startup will, eventually, block startup."""
    launcher = (REPO / "scripts" / "termux-launch.sh").read_text()

    hook = launcher[
        launcher.index("--launch-check") - 400 : launcher.index("--launch-check") + 200
    ]
    assert "|| true" in hook, "the hook must not be able to fail the launch"
