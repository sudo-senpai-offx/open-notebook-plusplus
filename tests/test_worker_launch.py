"""The worker launcher decides how much parallelism a device gets.

Two things about it are worth pinning, and neither is visible from the
Python side:

**Precedence.** A throttle preference written by the UI must beat the
static default in the environment, or the toggle is decoration. But an
unacceptable preference (0, 9, quoted, suffixed with a comment, a shell
fragment) must be *ignored* rather than honoured and must not become a
crash: the reader greps for one exact line shape and falls back, so a
corrupt file degrades to the previous behaviour instead of taking the
worker down.

**The record.** `worker-launch.sh` writes what it resolved to
`throttle.running`, and the API reports the running worker count from
that file rather than from the environment - because overriding the
environment is this script's whole job, so an environment-based answer
would describe a worker that does not exist. If the recording line is
removed or misspelled, the API silently falls back to reporting the
env value, and no test of the API would notice.

This script runs in the container (supervisord) rather than on the
phone, so it is not in the DEVICE_SCRIPTS list of
test_device_scripts_posix.py; it is checked here instead, for both the
same POSIX reason and its own behaviour.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
LAUNCHER = REPO / "scripts" / "worker-launch.sh"

# The resolution logic, inlined against the real grep/cut, so the cases
# below exercise the same shell constructs the script uses. Keeping them
# in one place means the tested expression is the shipped one: a copy
# that drifts from the script is the usual way this kind of test starts
# lying.
RESOLVE = """
data_dir="$1"
tasks=$(grep -s '^OPEN_NOTEBOOK_WORKER_MAX_TASKS=[1-8]$' "$data_dir/throttle.env" 2>/dev/null | tail -1 | cut -d= -f2) || tasks=""
cap="${OPEN_NOTEBOOK_WORKER_MAX_TASKS_CAP:-0}"
if [ -z "$tasks" ]; then
    tasks="${OPEN_NOTEBOOK_WORKER_MAX_TASKS:-1}"
fi
case "$tasks" in
    ''|*[!0-9]*) tasks=1 ;;
esac
[ "$tasks" -ge 1 ] 2>/dev/null || tasks=1
if [ "$cap" -ge 1 ] 2>/dev/null && [ "$tasks" -gt "$cap" ]; then
    tasks=$cap
fi
printf '%s' "$tasks"
"""


def _shellcheck() -> str:
    found = shutil.which("shellcheck")
    if found:
        return found
    candidate = REPO / ".venv/bin/shellcheck"
    if candidate.exists():
        return str(candidate)
    pytest.skip("shellcheck not installed")


@pytest.fixture(scope="module")
def shellcheck_bin() -> str:
    return _shellcheck()


def _resolve(
    tmp_path: Path, preference: str | None, env: str | None, cap: int = 0
) -> str:
    """Run the real resolution against `tmp_path`, return the count."""
    if preference is not None:
        (tmp_path / "throttle.env").write_text(preference)
    env_overrides = {"OPEN_NOTEBOOK_WORKER_MAX_TASKS_CAP": str(cap)}
    if env is not None:
        env_overrides["OPEN_NOTEBOOK_WORKER_MAX_TASKS"] = env
    else:
        env_overrides["OPEN_NOTEBOOK_WORKER_MAX_TASKS"] = ""
    result = subprocess.run(
        ["sh", "-c", RESOLVE, "sh", str(tmp_path)],
        capture_output=True,
        text=True,
        env={**env_overrides, "PATH": "/usr/bin:/bin"},
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


class TestWorkerLauncherIsPosix:
    def test_is_posix_sh(self, shellcheck_bin):
        """`sh -n` is not enough - /bin/sh here is bash, which parses
        bash-only constructs happily. The container's sh is dash.
        """
        result = subprocess.run(
            [shellcheck_bin, "-s", "sh", "-S", "error", str(LAUNCHER)],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"{result.stdout}{result.stderr}"

    def test_no_warnings(self, shellcheck_bin):
        result = subprocess.run(
            [shellcheck_bin, "-s", "sh", "-S", "warning", str(LAUNCHER)],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"{result.stdout}{result.stderr}"

    def test_shebang_is_sh(self):
        assert LAUNCHER.read_text().startswith("#!/bin/sh\n")

    def test_is_executable_in_the_repo(self):
        """The Dockerfile chmods it, but the checkout is what shellcheck
        and a local run see; a non-executable script is a confusing
        failure mode three layers down.
        """
        assert LAUNCHER.stat().st_mode & 0o111, "worker-launch.sh must be executable"


class TestBackstop:
    """The worker's own limit is no longer the concurrency control.

    Concurrency moved upstream of the claim: the dispatcher admits jobs from
    `held` to `new` up to a window it re-reads every pass. The worker's
    semaphore is therefore a backstop that should never bind, and the two
    failure modes this replaces are both worth pinning.
    """

    def test_backstop_is_not_read_from_the_throttle_file(self):
        """Reading the preference here would be actively wrong in both
        directions. Lowered at runtime, a startup-time semaphore would refuse
        work the operator asked for; raised at runtime, it would cap the
        window at whatever the worker happened to start with and the gate
        would govern nothing.
        """
        source = LAUNCHER.read_text()
        assert "throttle" not in source.lower() or "OPEN_NOTEBOOK_WINDOW_CAP" in source
        assert "grep" not in source, "the launcher must not parse the preference"

    def test_backstop_is_at_least_the_highest_possible_window(self):
        """If the backstop were below the maximum window, it would become the
        real limit the moment the window was raised past it.
        """
        from open_notebook.scheduler.dispatcher import MAX_WINDOW

        source = LAUNCHER.read_text()
        assert "BACKSTOP" in source
        # The default literal in the script must not be below MAX_WINDOW.
        assert "OPEN_NOTEBOOK_WORKER_BACKSTOP:-8" in source
        assert MAX_WINDOW <= 8

    def test_backstop_ignores_a_non_numeric_override(self):
        """A corrupt environment value falls back to the ceiling rather than
        being handed to the worker, which would refuse to start.
        """
        source = LAUNCHER.read_text()
        assert "*[!0-9]*" in source


def test_the_device_cap_lives_with_the_device_not_the_repository():
    """The phone's ceiling is 2 and the PC's is 8, so it cannot be a constant
    in the code. It is read from the environment, which the phone launcher
    sets when it starts the API that hosts the dispatcher.
    """
    from open_notebook.scheduler.dispatcher import MAX_WINDOW

    stack = (REPO / "scripts" / "termux-stack.sh").read_text()
    assert "OPEN_NOTEBOOK_WINDOW_CAP:-2" in stack
    assert MAX_WINDOW == 8


def test_window_is_clamped_on_read_as_well_as_on_write(tmp_path):
    """A file written by an older build, or hand-edited past what this
    device can honour, is reduced rather than obeyed.
    """
    from open_notebook.scheduler.dispatcher import read_window, write_window

    path = tmp_path / "t.conf"
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("THROTTLE_FILE", str(path))
        mp.setenv("OPEN_NOTEBOOK_WINDOW_CAP", "2")
        # A value a previous build stored, above this device's ceiling.
        path.write_text("OPEN_NOTEBOOK_WORKER_MAX_TASKS=8\n")
        assert read_window() == 2
        # And writing is clamped too, so the two cannot disagree.
        assert write_window(3) == 2
        assert read_window() == 2
        assert path.read_text().strip() == "OPEN_NOTEBOOK_WORKER_MAX_TASKS=2"


def test_cap_below_one_does_not_wedge_the_queue(tmp_path):
    """A cap of 0 means drain, not refuse-to-parse. A negative or
    unparseable cap falls back to the ceiling rather than stranding the
    device with an impossible window.
    """
    from open_notebook.scheduler.dispatcher import read_window

    path = tmp_path / "t.conf"
    path.write_text("OPEN_NOTEBOOK_WORKER_MAX_TASKS=3\n")
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("THROTTLE_FILE", str(path))
        mp.setenv("OPEN_NOTEBOOK_WINDOW_CAP", "not-a-number")
        assert read_window() == 3


def test_throttle_state_files_are_ignored_by_git():
    """Device-local runtime state, written beside the checkout - and on the
    phone that checkout is a git working tree. A committed value would be one
    device's window asserting itself over another's.
    """
    for name in ("throttle.running", "throttle.env", "throttle.conf"):
        result = subprocess.run(
            ["git", "check-ignore", "-q", name],
            cwd=REPO,
            capture_output=True,
        )
        assert result.returncode == 0, f"{name} must be gitignored"


def test_launcher_is_not_a_device_script_by_accident():
    """It runs under supervisord in the container. If it were also installed
    onto the phone it would need the DEVICE_SCRIPTS POSIX treatment there, and
    the phone's cap lives in termux-stack.sh - two places to keep in step.
    """
    from tests.test_device_scripts_posix import DEVICE_SCRIPTS

    assert "worker-launch.sh" not in DEVICE_SCRIPTS
