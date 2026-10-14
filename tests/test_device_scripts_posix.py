"""The device scripts must be POSIX, not bash.

`onb-sync` died on the phone with:

    onb-sync: 149: Syntax error: "(" unexpected (expecting "}")

because it used `auth=()`, a bash array. The script runs under Termux's
`sh`, which rejects that outright - and it is a *parse* error, so it took
the entire script down rather than one branch. Every other command,
including the ones that worked minutes earlier, stopped working.

`sh -n` did not catch it, because `/bin/sh` on the dev box is bash, which
happily parses arrays. Only ShellCheck with `-s sh` sees what the phone
actually runs.

So: the scripts that get installed onto the device are checked here as
POSIX. Dev-box scripts are excluded - they run under bash, where `local`
and `[[ ]]` are perfectly fine.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "scripts"

# Installed into ~/bin and run on the phone under Termux's sh.
DEVICE_SCRIPTS = [
    "termux-launch.sh",
    "termux-stack.sh",
    "termux-model.sh",
    "termux-embed.sh",
    "termux-sync.sh",
    "install-bin.sh",
]


def _shellcheck() -> str | None:
    """Locate shellcheck: PATH first, then the project venv."""
    found = shutil.which("shellcheck")
    if found:
        return found
    candidate = REPO / ".venv/bin/shellcheck"
    return str(candidate) if candidate.exists() else None


SHELLCHECK = _shellcheck()


@pytest.mark.skipif(SHELLCHECK is None, reason="shellcheck not installed")
class TestDeviceScriptsArePosix:
    @pytest.mark.parametrize("name", DEVICE_SCRIPTS)
    def test_script_exists(self, name):
        assert (SCRIPTS / name).is_file(), f"{name} is installed onto the device"

    @pytest.mark.parametrize("name", DEVICE_SCRIPTS)
    def test_parses_as_posix_sh(self, name):
        """`sh -n` is not enough - /bin/sh here is bash.

        Bash parses bash-only constructs happily, so this check has to be
        told the dialect or it agrees with the bug.
        """
        result = subprocess.run(
            [SHELLCHECK, "-s", "sh", "-S", "error", str(SCRIPTS / name)],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, (
            f"{name} is not POSIX sh:\n{result.stdout}{result.stderr}"
        )

    @pytest.mark.parametrize("name", DEVICE_SCRIPTS)
    def test_no_warnings(self, name):
        result = subprocess.run(
            [SHELLCHECK, "-s", "sh", "-S", "warning", str(SCRIPTS / name)],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, (
            f"{name} has POSIX warnings:\n{result.stdout}{result.stderr}"
        )

    def test_the_shebang_matches_the_dialect_checked(self):
        """A script claiming one dialect while being checked as another
        is how the two drift apart."""
        for name in DEVICE_SCRIPTS:
            first = (SCRIPTS / name).read_text().splitlines()[0]
            assert first.startswith("#!"), f"{name} has no shebang"
            assert "/sh" in first or "/bash" in first, (
                f"{name}: unexpected interpreter {first!r}"
            )

    def test_no_bash_arrays_in_device_scripts(self):
        """A direct guard on the construct that actually broke it.

        ShellCheck covers this today, but the failure mode is severe and
        silent in CI terms - the script is fine everywhere it is run
        except on the one machine it ships to. An explicit assertion
        documents why.
        """
        for name in DEVICE_SCRIPTS:
            text = (SCRIPTS / name).read_text()
            for lineno, line in enumerate(text.splitlines(), 1):
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue
                # `name=(`, `name=( )`, and the `("${arr[@]}"` expansion.
                if (
                    "=()" in stripped
                    or "=(" in stripped.replace(" ", "")
                    and "(" in stripped
                ):
                    assert "=()" not in stripped, (
                        f"{name}:{lineno} assigns an array, which Termux's sh "
                        f"rejects as a parse error: {stripped!r}"
                    )
                assert "[@]" not in stripped, (
                    f"{name}:{lineno} expands an array, which Termux's sh "
                    f"rejects as a parse error: {stripped!r}"
                )
