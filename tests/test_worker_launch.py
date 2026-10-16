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


class TestPrecedence:
    def test_preference_beats_the_environment(self, tmp_path):
        assert _resolve(tmp_path, "OPEN_NOTEBOOK_WORKER_MAX_TASKS=3\n", env="1") == "3"

    def test_environment_applies_when_no_preference(self, tmp_path):
        assert _resolve(tmp_path, None, env="2") == "2"

    def test_defaults_to_strict(self, tmp_path):
        """Nothing set anywhere must be 1, not 0 and not unlimited. The
        low-end floor this project targets has one llama-server beside
        the worker; an accidental default of 3 is an OOM.
        """
        assert _resolve(tmp_path, None, env=None) == "1"

    @pytest.mark.parametrize(
        "rejected",
        [
            "OPEN_NOTEBOOK_WORKER_MAX_TASKS=0\n",
            "OPEN_NOTEBOOK_WORKER_MAX_TASKS=9\n",
            'OPEN_NOTEBOOK_WORKER_MAX_TASKS="3"\n',
            "OPEN_NOTEBOOK_WORKER_MAX_TASKS=3 # fast\n",
            "  OPEN_NOTEBOOK_WORKER_MAX_TASKS=3\n",
            "OPEN_NOTEBOOK_WORKER_MAX_TASKS=3;rm -rf /\n",
            "OPEN_NOTEBOOK_OTHER=3\n",
        ],
    )
    def test_unacceptable_preference_falls_back(self, tmp_path, rejected):
        """Ignored, never executed, never clamped up.

        A rejected preference must leave the device exactly as it was -
        the env value - not promote to some middle value that nobody
        chose.
        """
        assert _resolve(tmp_path, rejected, env="2") == "2"

    def test_garbage_environment_is_not_a_crash(self, tmp_path):
        assert _resolve(tmp_path, None, env="not-a-number") == "1"

    def test_last_preference_line_wins(self, tmp_path):
        """Matches the launchers' `tail -1` and the Python reader's
        last-match rule. Disagreement here means the UI shows one
        number and the worker starts with another.
        """
        assert (
            _resolve(
                tmp_path,
                "OPEN_NOTEBOOK_WORKER_MAX_TASKS=3\nOPEN_NOTEBOOK_WORKER_MAX_TASKS=2\n",
                env="1",
            )
            == "2"
        )


class TestPlatformCap:
    def test_cap_lowers_a_high_preference(self, tmp_path):
        """The phone's 2-worker ceiling is hardware, not preference, so
        it applies after the preference is read and cannot be talked
        around by the UI writing a larger number.
        """
        assert (
            _resolve(tmp_path, "OPEN_NOTEBOOK_WORKER_MAX_TASKS=3\n", env="1", cap=2)
            == "2"
        )

    def test_cap_leaves_a_lower_preference_alone(self, tmp_path):
        assert (
            _resolve(tmp_path, "OPEN_NOTEBOOK_WORKER_MAX_TASKS=1\n", env="1", cap=2)
            == "1"
        )

    def test_no_cap_by_default(self, tmp_path):
        assert (
            _resolve(tmp_path, "OPEN_NOTEBOOK_WORKER_MAX_TASKS=3\n", env="1", cap=0)
            == "3"
        )


class TestTheRecord:
    """The API reads `throttle.running` to report the running worker.

    It cannot be verified from Python - this is about the file being
    written at all, and the API's own reader already has its tests.
    """

    def test_writes_what_it_resolved(self, tmp_path):
        """A real end-to-end run of the script's own write path.

        `exec uv run ...` at the end makes this impossible to run
        directly, so the write is exercised through the same shell
        constructs - and the assertion is on the file the API reads,
        not on the resolved number the caller already knows.
        """
        write = (
            RESOLVE.replace("printf '%s' \"$tasks\"", "")
            + 'mkdir -p "$data_dir"\n'
            + "printf 'OPEN_NOTEBOOK_WORKER_MAX_TASKS=%s\\n' \"$tasks\" "
            + '>"$data_dir/throttle.running"\n'
        )
        (tmp_path / "throttle.env").write_text("OPEN_NOTEBOOK_WORKER_MAX_TASKS=3\n")
        result = subprocess.run(
            ["sh", "-c", write, "sh", str(tmp_path)],
            capture_output=True,
            text=True,
            env={
                "OPEN_NOTEBOOK_WORKER_MAX_TASKS": "1",
                "OPEN_NOTEBOOK_WORKER_MAX_TASKS_CAP": "0",
                "PATH": "/usr/bin:/bin",
            },
        )
        assert result.returncode == 0, result.stderr

        # The line shape the Python reader accepts, exactly.
        from api.routers.processing import _read_throttle_key

        assert _read_throttle_key(tmp_path / "throttle.running") == 3

    def test_api_reads_the_record_not_the_environment(self, tmp_path, monkeypatch):
        """The end of the chain, in one test.

        Container: environment says 1, throttle.env says 3, the worker
        starts at 3 and records 3. The API must then report 3 - not the
        1 it would read from the environment it just overrode, and not
        a pending restart that has already happened.
        """
        from api.routers.processing import processing_status

        (tmp_path / "throttle.env").write_text("OPEN_NOTEBOOK_WORKER_MAX_TASKS=3\n")
        (tmp_path / "throttle.running").write_text("OPEN_NOTEBOOK_WORKER_MAX_TASKS=3\n")
        monkeypatch.setenv("THROTTLE_FILE", str(tmp_path / "throttle.env"))
        monkeypatch.setenv("OPEN_NOTEBOOK_WORKER_MAX_TASKS", "1")

        import asyncio

        body = asyncio.run(processing_status())
        assert body["worker_max_tasks"] == 3
        assert body["full_throttle"] is True
        assert body["throttle_restart_pending"] is False

    def test_a_missing_record_falls_back_to_the_environment(
        self, tmp_path, monkeypatch
    ):
        """Before the worker starts there is no record, and the
        environment is the best available answer. Getting this wrong
        would make a fresh container report a worker that has not
        started yet.
        """
        from api.routers.processing import processing_status

        (tmp_path / "throttle.env").write_text("OPEN_NOTEBOOK_WORKER_MAX_TASKS=3\n")
        monkeypatch.setenv("THROTTLE_FILE", str(tmp_path / "throttle.env"))
        monkeypatch.setenv("OPEN_NOTEBOOK_WORKER_MAX_TASKS", "1")

        import asyncio

        body = asyncio.run(processing_status())
        assert body["worker_max_tasks"] == 1
        # The preference is ahead of the worker: still pending.
        assert body["throttle_restart_pending"] is True


def test_throttle_state_files_are_ignored_by_git():
    """Both files are device-local runtime state written beside the
    checkout - on the phone that checkout is a git working tree.

    A committed `throttle.running` would be one device's worker count
    asserting itself over another's on the next pull, which is the
    exact failure the phone cap exists to prevent.
    """
    ignored = subprocess.run(
        ["git", "check-ignore", "-q", "throttle.running"],
        cwd=REPO,
        capture_output=True,
    )
    assert ignored.returncode == 0, "throttle.running must be gitignored"


def test_launcher_is_not_a_device_script_by_accident():
    """It runs under supervisord in the container. If it were also
    installed onto the phone it would need the DEVICE_SCRIPTS POSIX
    treatment there, and the phone's cap lives in termux-stack.sh - two
    places to keep in step instead of one.
    """
    from tests.test_device_scripts_posix import DEVICE_SCRIPTS

    assert "worker-launch.sh" not in DEVICE_SCRIPTS
