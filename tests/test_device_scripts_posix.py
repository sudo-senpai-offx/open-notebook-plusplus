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

import re
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
    "termux-update.sh",
    "termux-config.sh",
    "install-bin.sh",
]


class TestEveryDeviceScriptReachesThePhone:
    """A script that lives only in the repo is a script the phone cannot run.

    `install-bin.sh` copies `scripts/termux-*` into `~/bin` under a shorter
    `onb-*` name. A new script that nobody adds to that list fails at the moment
    an operator reaches for it, on a phone, with a "No such file or directory"
    that says nothing about the real cause — which is exactly what happened with
    `termux-fetch-models.sh`: it was written, tested, committed and bundled, and
    then not runnable because the install list was never updated.

    So the list is checked against the directory rather than remembered.
    """

    # Scripts that legitimately do not belong in ~/bin, each with its reason.
    NOT_ON_DEVICE = {
        "termux-chat-setup.sh": (
            "runs INSIDE the proot guest as /root/chat-setup.sh, not from Termux"
        ),
    }

    def test_no_script_is_missing_from_the_installer(self) -> None:
        installer = (SCRIPTS / "install-bin.sh").read_text()
        missing = []
        for path in sorted(SCRIPTS.glob("termux-*.sh")):
            name = path.name
            if name in self.NOT_ON_DEVICE:
                continue
            if name not in installer:
                missing.append(name)
        assert not missing, (
            "these scripts exist but install-bin.sh never installs them, so they "
            "cannot be run on the phone: " + ", ".join(missing)
        )

    def test_the_exceptions_are_still_reasons_and_not_a_scrap_heap(self) -> None:
        """An allowlist with no stated reason becomes a place things go to die."""
        for name, reason in self.NOT_ON_DEVICE.items():
            assert (SCRIPTS / name).exists(), f"{name} is excepted but no longer exists"
            assert len(reason) > 20, f"{name} needs a real reason, not a placeholder"


class TestUpdateSurvivesTheOperator:
    """`onb-update --restart` used to leave the device with nothing running.

    Two separate defects, both reported as "it freezes" and "the update rarely
    takes, I have to force-close Termux".

    **The detached start was not detached.** `nohup ... &` ignores SIGHUP and
    nothing else. In a non-interactive shell job control is off, so the launcher
    stayed in the caller's process group — the same group as the operator's
    terminal, where Ctrl+C reaches it. `onb` then spends its life blocked on a
    foreground `proot-distro login`, and proot runs with `--kill-on-exit`, so
    killing the launcher reaped every process inside the guest. Observed live
    while building this: signalling the child's group also killed the harness,
    which is the same property with the signaller on the receiving end.

    **The guest commands were unbounded.** `proot-distro login` hangs when the
    session it wants to attach to is wedged — the updater's own teardown says so
    — and an unbounded hang presents as a frozen script with no indication of
    where it is stuck.
    """

    def test_the_restarted_launcher_is_put_in_its_own_session(self):
        text = (SCRIPTS / "termux-update.sh").read_text()
        assert "setsid" in text, "nohup alone leaves the launcher in the caller's group"
        # The start must go through setsid, not merely mention it.
        assert re.search(r"setsid\s+nohup\s+sh\s+\"\$HOME/bin/onb\"", text), (
            "the restarted launcher must be started via `setsid nohup sh ~/bin/onb`"
        )

    def test_the_detached_start_does_not_hold_the_terminals_stdin(self):
        """A background process holding stdin makes the next interactive command
        read from the wrong place — which looks like the shell hanging."""
        text = (SCRIPTS / "termux-update.sh").read_text()
        for match in re.finditer(r"sh \"\$HOME/bin/onb\"[^\n]*", text):
            assert "< /dev/null" in match.group(0), (
                f"detached start does not redirect stdin: {match.group(0)!r}"
            )

    def test_it_warns_when_setsid_is_unavailable(self):
        """Silently falling back to nohup would report a detach that did not
        happen, which is the original bug wearing a different hat."""
        text = (SCRIPTS / "termux-update.sh").read_text()
        assert "setsid not found" in text

    def test_every_guest_login_is_bounded(self):
        text = (SCRIPTS / "termux-update.sh").read_text()
        body = text[text.index("in_guest()") :]
        body = body[: body.index("\n  }")]
        assert "timeout" in body, "in_guest can hang on a wedged proot session"
        assert "ONB_GUEST_TIMEOUT" in body, (
            "the bound must be adjustable and documented"
        )

    def test_the_bound_actually_terminates(self):
        """`timeout N cmd` is not a bound when the child ignores SIGTERM.

        Measured on the device: proot has SigIgn bit 15 set, so it discards
        SIGTERM outright. `timeout` sends TERM at N and then *waits forever*; it
        only escalates to SIGKILL when given `-k`. A `timeout 120` wrapper was
        observed still running at 134 seconds with its proot child alive — the
        opposite of the bound it appears to provide.
        """
        text = (SCRIPTS / "termux-update.sh").read_text()
        body = text[text.index("in_guest()") :]
        body = body[: body.index("\n  }")]
        timeout_lines = [
            l
            for l in body.splitlines()
            if "timeout" in l and "ONB_GUEST_TIMEOUT:-0" not in l
        ]
        assert timeout_lines, "no timeout in in_guest"
        for line in timeout_lines:
            if line.strip().startswith("timeout"):
                assert "-k" in line, (
                    "timeout without -k does not terminate a SIGTERM-ignoring "
                    f"child: {line.strip()!r}"
                )

    def test_restart_is_honoured_when_already_up_to_date(self):
        """The bug that made `--restart` a silent no-op, and a frequent one.

        The up-to-date arm used to `exit 0` unconditionally, so
        `onb-update --restart` did nothing at all whenever the repo was already
        current — printing a success line and leaving the old process tree
        running the old code. It is also the documented recovery for a wedged
        stack, so it failed exactly when it was needed most. Reported as "the
        update rarely takes".
        """
        text = (SCRIPTS / "termux-update.sh").read_text()
        lines = text.split("\n")
        start = next(n for n, l in enumerate(lines) if l.startswith('case "$rc" in'))
        end = next(n for n in range(start, len(lines)) if lines[n].strip() == "esac")
        arm = "\n".join(lines[start : end + 1])
        assert '[ "$RESTART" -eq 1 ] || exit 0' in arm, (
            "the up-to-date arm must honour --restart rather than always exiting"
        )
        after = "\n".join(lines[end : end + 20])
        for step in ("stop_stack", "do_apply", 'if [ "$RESTART" -eq 1 ]'):
            assert step in after, f"{step} unreachable after the up-to-date arm"

    def test_raw_proot_logins_outside_the_wrapper_are_named(self):
        """`in_guest` is the wrapper; a bare login beside it is an unbounded one
        that the wrapper cannot help with."""
        text = (SCRIPTS / "termux-update.sh").read_text()
        body = text[text.index("in_guest()") + 10 :]
        stray = [
            line.strip()
            for line in body.splitlines()
            if "proot-distro login" in line
            and not line.strip().startswith("#")
            # `say "Inspect with: proot-distro login ..."` is an instruction
            # printed for the operator to run by hand, not a command this script
            # executes, so it is not an unbounded call.
            and not line.strip().startswith("say ")
            and 'proot-distro login ubuntu -- "$@"' not in line
        ]
        for line in stray:
            assert "timeout" in line or "nohup" in line or "setsid" in line, (
                f"unbounded proot-distro login: {line}"
            )


class TestInstalledScriptsAreRunCorrectly:
    """A Python entry point must be invoked by its shebang, never through `sh`.

    `sh ~/bin/onb-speech-suite` bypasses the shebang entirely: the shell parses
    the module docstring as commands and emits `import: not found` once per line
    of it. Observed on the device, and the failure is actively misleading —
    the output looks like a broken script rather than a wrong invocation, and
    the suite's own exit code is never reached.

    Two guards: the shebang must point at a Termux python (so direct invocation
    works at all), and the usage text must not tell anyone to use `sh`.
    """

    def _installed(self) -> list[tuple[str, str]]:
        """(installed name, repo file) for every non-shell entry point."""
        text = (SCRIPTS / "install-bin.sh").read_text()
        found = []
        for line in text.splitlines():
            line = line.strip()
            if not line.startswith("install_one "):
                continue
            parts = line.split()
            if len(parts) >= 2 and parts[1].endswith((".py", ".sh")):
                found.append((parts[2], parts[1]))
        return found

    def test_the_installer_does_ship_a_python_suite(self):
        """A guard that has nothing to guard is not a guard."""
        names = {name for name, _ in self._installed()}
        assert "onb-speech-suite" in names, "the suite is not installed at all"

    def test_python_entry_points_have_a_termux_python_shebang(self):
        for name, source in self._installed():
            path = SCRIPTS / source
            if not path.exists():
                continue
            first = path.read_text().splitlines()[0] if path.read_text() else ""
            if first.startswith("#!") and "python" in first:
                assert "/data/data/com.termux/files/usr/bin/python" in first, (
                    f"{name}: shebang must point at Termux's python so the script "
                    f"can be run directly; got {first!r}"
                )
                assert path.stat().st_size > 0

    def test_the_suite_is_never_documented_as_a_shell_script(self):
        """`sh script.py` is the mistake; the usage text must not teach it."""
        suite = (SCRIPTS / "termux-speech-suite.py").read_text()
        for line in suite.splitlines():
            stripped = line.strip()
            if stripped.startswith("sh ~/bin/"):
                assert ".py" not in stripped, (
                    f"usage text teaches `sh` on a .py: {stripped!r}"
                )
        # And it must be executable by its shebang, which install_one guarantees
        # by chmod, so direct invocation is the documented path.
        assert suite.startswith("#!/data/data/com.termux/files/usr/bin/python")


def _shellcheck() -> str | None:
    """Locate shellcheck: PATH first, then the project venv."""
    found = shutil.which("shellcheck")
    if found:
        return found
    candidate = REPO / ".venv/bin/shellcheck"
    return str(candidate) if candidate.exists() else None


@pytest.fixture(scope="module")
def shellcheck() -> str:
    """The shellcheck binary, or skip.

    A fixture rather than a module constant paired with `skipif`: mypy
    cannot narrow a module-level `str | None` through a skipif, so the
    constant leaked Optional into the subprocess argument list.
    """
    found = _shellcheck()
    if found is None:
        pytest.skip("shellcheck not installed")
    return found


class TestDeviceScriptsArePosix:
    @pytest.mark.parametrize("name", DEVICE_SCRIPTS)
    def test_script_exists(self, name):
        assert (SCRIPTS / name).is_file(), f"{name} is installed onto the device"

    @pytest.mark.parametrize("name", DEVICE_SCRIPTS)
    def test_parses_as_posix_sh(self, name, shellcheck):
        """`sh -n` is not enough - /bin/sh here is bash.

        Bash parses bash-only constructs happily, so this check has to be
        told the dialect or it agrees with the bug.
        """
        result = subprocess.run(
            [shellcheck, "-s", "sh", "-S", "error", str(SCRIPTS / name)],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, (
            f"{name} is not POSIX sh:\n{result.stdout}{result.stderr}"
        )

    @pytest.mark.parametrize("name", DEVICE_SCRIPTS)
    def test_no_warnings(self, name, shellcheck):
        result = subprocess.run(
            [shellcheck, "-s", "sh", "-S", "warning", str(SCRIPTS / name)],
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
