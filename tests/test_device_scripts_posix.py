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

import os
import re
import shutil
import signal
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
    "termux-tts.sh",
    "termux-tts-ja-install.sh",
    "termux-fetch-models.sh",
    "termux-stt.sh",
    "termux-stt-benchmark.sh",
    "termux-cpu.sh",
    "termux-build-piper.sh",
    "termux-chat-setup.sh",
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
            line
            for line in body.splitlines()
            if "timeout" in line and "ONB_GUEST_TIMEOUT:-0" not in line
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
        start = next(
            index
            for index, line in enumerate(lines)
            if line.startswith('case "$rc" in')
        )
        end = next(
            index
            for index in range(start, len(lines))
            if lines[index].strip() == "esac"
        )
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


class TestLauncherDoesNotKillItsOwnSubshells:
    """`onb stop` reported its own subshells as having survived SIGKILL.

    A forked subshell keeps its parent's cmdline. `launcher_pids` matches on
    `$HOME/bin/onb `, which is in the script's *own* command line, so the
    command-substitution subshell that does the scanning looks exactly like a
    launcher. `kill_stragglers` then SIGKILLed it, and the re-check found the
    dying subshell still present and reported:

        WARNING: launcher(s) survived SIGKILL (pid 26251 26252)
        reboot, or: kill -9 26251 26252

    Three things wrong with that: SIGKILL cannot be ignored, so "survived
    SIGKILL" is a contradiction; "reboot" is drastic advice for a stray
    subshell; and by the time the operator ran the suggested kill, the
    processes were gone, so the warning described something that had already
    resolved itself.

    `$$` does not exclude a subshell — it is the *parent* shell's pid. The
    first fix excluded by parent, and the cold-boot drill showed that to be
    one level too shallow: a freshly rebooted phone with nothing running
    printed

        [onb] stopping launcher(s) (pid 13430 13431 )
        [onb] WARNING: launcher(s) survived SIGKILL (pid 13597 13598 )
                 reboot, or: kill -9 13597 13598

    Two fresh, adjacent pairs seconds apart, both fictional — the scanner's
    own grandchildren. The pipeline forks a child *inside* the command
    substitution, and the `{ tr ...; }` reading each cmdline forks inside
    that child; neither is a direct child of $$, so the one-level check saw
    every scan as launchers that had survived. The exclusion has to walk the
    whole tree in both directions — which is exactly what `_is_ours`,
    already inside `already_running` for this same reason, now does for both
    scans.

    The first test below pins the shape; the second runs the real extracted
    functions through the real production call instead of trusting prose.
    """

    def test_the_launcher_scan_excludes_our_whole_tree(self):
        """One level of parent is the bug, not the fix.

        The direct-child exclusion let the pipeline fork and the
        `{ tr ...; }` fork through, and both inherit the script's own
        cmdline — so each scan reported the pids it had just created. Assert
        the tree walk is in place, and that the shallow check stays gone.
        """
        text = (SCRIPTS / "termux-launch.sh").read_text()
        assert re.search(r"^_is_ours\(\) \{", text, re.M), (
            "_is_ours must be defined at the top level so launcher_pids and "
            "already_running share the one walk"
        )
        body = text[text.index("launcher_pids()") :]
        body = body[: body.index("\n}")]
        assert '_is_ours "${p#/proc/}" && continue' in body, (
            "launcher_pids must exclude by the whole process tree; the "
            "scanner's own subshells are grandchildren, not children"
        )
        assert '[ "$_ppid" = "$$" ] && continue' not in body, (
            "the one-level parent check is what the cold-boot drill caught; "
            "it must not come back"
        )

    def test_the_real_scan_reports_only_real_launchers(self, tmp_path):
        """Run the extracted functions through the real production call.

        The harness is planted at `$HOME/bin/onb`, so its command-substitution
        and pipeline subshells inherit a cmdline matching the very pattern
        `launcher_pids` scans for — reproducing the production self-match
        instead of describing it. Both cases were observed on the phone:

        nothing running → no output (the drill got two phantom pids);
        one parked launcher → exactly that pid, nothing else.
        """
        text = (SCRIPTS / "termux-launch.sh").read_text()

        # The harness replicates this call verbatim; if the call site changes
        # shape, the harness has to change with it or it tests a path the
        # phone never runs.
        assert 'pids=$(launcher_pids | grep -v "^$$\\$" || true)' in text

        extracted = []
        for name in ("_is_ours", "launcher_pids"):
            m = re.search(rf"^{name}\(\) \{{", text, re.M)
            assert m, f"{name} must be defined at column 0 to be extractable"
            extracted.append(text[m.start() : text.index("\n}", m.start()) + 2])
        functions = "\n".join(extracted)
        assert '_is_ours "${p#/proc/}"' in functions, (
            "extraction pulled the wrong text; the scan must call the walk"
        )

        home = tmp_path / "home"
        (home / "bin").mkdir(parents=True)
        onb = home / "bin" / "onb"
        onb.write_text(
            "#!/bin/sh\n" + functions + '\ncase "${1:-scan}" in\n'
            # `; :` after the sleep: with no following command some shells
            # exec-replace themselves with sleep, and the process would then
            # carry `sleep 60` instead of the cmdline this test matches on.
            "  park) sleep 60; : ;;\n"
            '  *) pids=$(launcher_pids | grep -v "^$$\\$" || true)\n'
            '     printf "%s\\n" "$pids" ;;\n'
            "esac\n"
        )
        onb.chmod(0o755)
        env = {**os.environ, "HOME": str(home)}

        def scan() -> list[str]:
            done = subprocess.run(
                ["sh", str(onb)],
                env=env,
                capture_output=True,
                text=True,
                timeout=30,
            )
            assert "not found" not in done.stderr, (
                "the harness calls a function it does not define: " + done.stderr
            )
            assert done.returncode == 0, done.stderr
            return done.stdout.split()

        # The drill's case: a freshly rebooted phone with nothing running,
        # where the scan still produced two fresh pids to report.
        assert scan() == [], "the scan invented launchers on an idle machine"

        park = subprocess.Popen([str(onb), "park"], env=env, start_new_session=True)
        try:
            found = scan()
        finally:
            # One signal takes the park shell and its sleep, so the suite
            # leaves nothing running behind it.
            try:
                os.killpg(park.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            park.wait(timeout=10)
        assert found == [str(park.pid)], (
            f"expected exactly the parked launcher ({park.pid}), got {found} "
            "- any other pid means the scan is still counting itself"
        )

    def test_a_subshell_really_does_inherit_its_parents_cmdline(self):
        """The premise, checked rather than assumed.

        If a subshell did *not* inherit the parent's cmdline, this whole class
        of bug could not happen, and the exclusion would be cargo cult.
        """
        import subprocess

        done = subprocess.run(
            [
                "/bin/sh",
                "-c",
                'echo "parent: $$"; (echo "child: $$"; tr "\\0" " " < /proc/$$/cmdline)',
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        out = done.stdout
        assert "parent:" in out and "child:" in out
        # `$$` is the same in both: the parent's pid. That is precisely why it
        # cannot be used to exclude a subshell.
        parent = out.split("parent:")[1].split()[0]
        child = out.split("child:")[1].split()[0]
        assert parent == child, (
            "`$$` must be identical in a subshell to be useless here"
        )


class TestTheSupervisorNeitherBlocksNorLingers:
    """The residency supervisor is a helper: `stop` must free it, and the
    launch guard must not count it.

    Neither was true. `stop_all` - the path `onb-stack up` runs for its
    clean slate - stopped llama, the embedder, the guest and the proot
    sessions, but not the supervisor; only the Ctrl+C trap did. So a warm
    `down` left `python3 ~/bin/onb-residency` alive on :8099, and the next
    `up` met it with a guard whose loose `*bin/onb*` matches every helper
    named `onb-*`:

        [onb] already running (pid 25925) - not starting a second launcher.

    Observed twice in one drill with the stack down both times: the whole
    down->up cycle could not come back up, refused against a process that
    starts nothing and races nothing.

    Two independent defenses, each exercised below: `stop_all` now stops
    the supervisor with the rest (and says which pid), and the guard's
    pattern now requires `onb ` - a space - which keeps every `onb-*`
    helper out while a parked `sh ~/bin/onb` still matches, because
    /proc/pid/cmdline ends with a NUL that the scan's `tr` turns into that
    space.
    """

    @staticmethod
    def _extract(*names: str) -> str:
        """The named definitions whole, comments included - nothing more."""
        text = (SCRIPTS / "termux-launch.sh").read_text()
        parts = []
        for name in names:
            m = re.search(rf"^{name}\(\) \{{", text, re.M)
            assert m, f"{name} must be defined at column 0 to be extractable"
            parts.append(text[m.start() : text.index("\n}", m.start()) + 2])
        return "\n".join(parts)

    @staticmethod
    def _supervisor(home: Path) -> Path:
        """The helper where production lays it out: `$HOME/bin/onb-residency`.

        Production runs it via python3; what both scans discriminate on is
        the path text in the cmdline, identical under sh.
        """
        (home / "bin").mkdir(parents=True, exist_ok=True)
        sup = home / "bin" / "onb-residency"
        sup.write_text("#!/bin/sh\nsleep 60; :\n")
        sup.chmod(0o755)
        return sup

    def test_the_guard_counts_launchers_not_helpers(self, tmp_path):
        """The refusal itself, reproduced: a live supervisor must not look
        like a second launcher, and a real one must still be seen.

        The middle assertion is the regression - it fails against the loose
        pattern, reporting the supervisor's pid exactly as the phone did.
        """
        text = (SCRIPTS / "termux-launch.sh").read_text()
        # The production call site the harness below replicates.
        assert "if other=$(already_running); then" in text
        functions = self._extract("_is_ours", "already_running")
        assert '_is_ours "$_pid"' in functions, (
            "extraction pulled the wrong text; the guard must call the walk"
        )

        home = tmp_path / "home"
        (home / "bin").mkdir(parents=True)
        onb = home / "bin" / "onb"
        onb.write_text(
            "#!/bin/sh\n" + functions + "\n"
            'case "${1:-scan}" in\n'
            # `; :` after the sleep: without a following command some shells
            # exec-replace themselves with sleep, losing this cmdline.
            "  park) sleep 60; : ;;\n"
            "  *) if other=$(already_running); then"
            ' printf "%s\\n" "$other"; else exit 1; fi ;;\n'
            "esac\n"
        )
        onb.chmod(0o755)
        env = {**os.environ, "HOME": str(home)}
        sup = self._supervisor(home)

        def scan() -> tuple[int, list[str]]:
            done = subprocess.run(
                ["sh", str(onb)],
                env=env,
                capture_output=True,
                text=True,
                timeout=30,
            )
            assert "not found" not in done.stderr, (
                "the harness calls a function it does not define: " + done.stderr
            )
            return done.returncode, done.stdout.split()

        # Idle: nothing to report - in particular not its own
        # command-substitution subshell, which carries the identical cmdline.
        rc, found = scan()
        assert (rc, found) == (1, []), "the guard refused on an idle machine"

        helper = subprocess.Popen(["sh", str(sup)], env=env, start_new_session=True)
        park = None
        try:
            rc, found = scan()
            assert (rc, found) == (1, []), (
                f"the guard counted the residency supervisor ({helper.pid}) "
                "as a second launcher - the 25925 refusal"
            )
            park = subprocess.Popen([str(onb), "park"], env=env, start_new_session=True)
            rc, found = scan()
            assert rc == 0, (
                "the guard no longer refuses a real second launcher: the "
                "tightened pattern must not have turned it off"
            )
            assert found == [str(park.pid)], (
                f"expected exactly the parked launcher ({park.pid}), got {found}"
            )
        finally:
            for proc in (park, helper):
                if proc is None:
                    continue
                try:
                    os.killpg(proc.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                proc.wait(timeout=10)

    def test_stop_frees_the_supervisor_and_names_the_pid(self, tmp_path):
        """`stop_residency` through the real production call: the supervisor
        dies, and the stop says which pid went - an absent line could
        equally mean the scan missed it.
        """
        home = tmp_path / "home"
        sup = self._supervisor(home)
        env = {**os.environ, "HOME": str(home)}
        runner = tmp_path / "stop.sh"
        runner.write_text(
            "#!/bin/sh\n" + self._extract("stop_residency") + "\nstop_residency\n"
        )

        helper = subprocess.Popen(["sh", str(sup)], env=env, start_new_session=True)
        try:
            done = subprocess.run(
                ["sh", str(runner)],
                env=env,
                capture_output=True,
                text=True,
                timeout=30,
            )
            assert "not found" not in done.stderr, done.stderr
            assert done.returncode == 0, done.stderr
            assert f"stopping residency supervisor (pid {helper.pid})" in (
                done.stdout
            ), (
                "the stop reported nothing, so the operator cannot tell a "
                "killed supervisor from a missed scan: " + done.stdout
            )
            assert helper.wait(timeout=10) == -signal.SIGTERM, (
                "the supervisor outlived stop_residency"
            )
        finally:
            try:
                os.killpg(helper.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            helper.wait(timeout=10)


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

    def test_installs_are_atomic(self):
        """An installer must not rewrite the file it is running from.

        A shell reads a script incrementally through an open fd and resumes at
        its current byte offset after every command. `cp "$src" "$dst"`
        truncates and rewrites the *same inode*, so a script that installs over
        itself - which `onb-update` does, since it ships its own replacement -
        finds the contents shifted underneath it and carries on parsing whatever
        now sits at that offset. Observed on the device:

            onb-update: 518: Syntax error: ";;" unexpected (expecting "fi")

        from a file that `dash` parses cleanly, because `onb-update` had
        installed a newer version over itself while the running copy was still
        executing.

        Writing beside the target and renaming over it replaces the directory
        entry instead: the running process keeps its fd on the old inode and is
        untouched, and a reader never observes a partial file.
        """
        text = (SCRIPTS / "install-bin.sh").read_text()
        lines = text.split("\n")
        start = next(
            index
            for index, line in enumerate(lines)
            if line.startswith("install_one()")
        )
        # Closed by a brace at column 0. Sliced by line rather than by an
        # indented `  }`, which this function does not have.
        end = next(n for n in range(start + 1, len(lines)) if lines[n] == "}")
        body = "\n".join(lines[start : end + 1])
        # Code, not prose: the function's own comment quotes `cp "$src" "$dst"`
        # in order to explain why it must not appear, so a whole-body search
        # finds the warning instead of the defect.
        code = "\n".join(
            line for line in body.split("\n") if not line.lstrip().startswith("#")
        )
        assert 'cp "$src" "$dst"' not in code, (
            "cp into the target rewrites the inode a running shell has open"
        )
        assert "mv " in body, (
            "the install must rename over the target, not write into it"
        )
        assert 'rm -f "$tmp"' in body, (
            "a failed install must not leave a stray executable in ~/bin"
        )

    def test_the_temporary_name_is_unique(self):
        """`$$`, so two concurrent installs cannot share one temp path and
        clobber each other's partial write."""
        assert '"$dst.new.$$"' in (SCRIPTS / "install-bin.sh").read_text()


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
    def test_no_heredoc_leaves_its_handler_on_the_body_line(self) -> None:
        """`|| handler` must share the line with `<<'EOF'`.

        The first device run of the config validation printed:

            File "<stdin>", line 1
              die "config rejected by piper-plus"
            IndentationError: unexpected indent

        because the `||` sat at the end of the heredoc's opening line
        and the shell swallowed the NEXT physical line as heredoc
        body: python read `die ...` as its stdin, the handler never
        armed, and the check it guarded ran gated by nothing while
        looking (to a skimmer) like it had fired.
        """
        dangling = []
        for name in DEVICE_SCRIPTS:
            for lineno, line in enumerate((SCRIPTS / name).read_text().splitlines(), 1):
                stripped = line.rstrip()
                if "<<'" in stripped and stripped.endswith("||"):
                    dangling.append(f"{name}:{lineno}")
        assert not dangling, (
            "a `||` at the end of a heredoc's opening line makes the next "
            f"line its body, not its handler: {dangling}"
        )

    def test_a_dangling_or_really_does_swallow_the_next_line(self) -> None:
        """The premise, checked rather than assumed.

        The rule above is only worth enforcing if the shell really
        behaves this way - so reproduce it: the handler line becomes
        python's stdin (IndentationError) and execution continues past
        it as if the guard had passed.
        """
        import subprocess
        import sys

        snippet = "\n".join(
            [
                f"{sys.executable} - <<'PY' ||",
                "  echo DIED-HANDLER",
                'print("body")',
                "PY",
                "echo continued",
                "",
            ]
        )
        done = subprocess.run(
            ["/bin/sh", "-c", snippet],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert "IndentationError" in done.stderr, (
            "the handler line should have arrived as python's stdin"
        )
        assert "DIED-HANDLER" not in done.stdout, "the handler never runs"
        assert "continued" in done.stdout, "execution sails past the guard"

    def test_the_list_covers_the_directory(self) -> None:
        """Checked against the directory rather than remembered.

        The same failure as the installer roster, one level down: the
        tts scripts were written, bundled and run on the phone while
        this list still held the original eight, so shellcheck - the
        only check that sees what Termux's `sh` actually parses - never
        ran on them through pytest. The manual battery covered them by
        hand, once, which is exactly the kind of coverage that lapses.
        """
        on_disk = {path.name for path in SCRIPTS.glob("termux-*.sh")}
        on_disk.add("install-bin.sh")
        assert set(DEVICE_SCRIPTS) == on_disk, (
            "DEVICE_SCRIPTS and scripts/ disagree; every device script is "
            "POSIX-checked here or excluded with a reason: "
            f"unlisted={sorted(on_disk - set(DEVICE_SCRIPTS))} "
            f"stale={sorted(set(DEVICE_SCRIPTS) - on_disk)}"
        )

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


class TestEveryProcReadIsGuarded:
    """A pid can exit between the glob and the read.

    POSIX applies redirections left to right, so `< "$p/cmdline"` is attempted
    *before* `2>/dev/null` takes effect, and the shell's own redirection error
    lands on the terminal. Five of them appeared on every update:

        onb-update: 175: cannot open /proc/13546/cmdline: No such file

    The fix is braces — `{ tr ... < "$p/cmdline"; } 2>/dev/null` — which put the
    failing redirection inside the redirected command. The launcher, the speech
    scripts and the stack script were fixed for this; the updater was not, and
    nobody noticed because the message is easy to mistake for a harmless warning
    from a teardown.

    Checked across the whole family, because the fix is one line each and the
    bug is invisible in review.
    """

    UNGUARDED = re.compile(
        r"\$\(\s*tr\b[^|]*<\"?\$[a-zA-Z_]+/(?:cmdline|environ)\"?\s+2>/dev/null"
    )

    def test_no_script_redirects_a_proc_path_before_stderr(self):
        offenders = []
        for path in sorted(SCRIPTS.glob("*.sh")):
            for number, line in enumerate(
                path.read_text(encoding="utf-8").splitlines(), 1
            ):
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue
                if self.UNGUARDED.search(stripped):
                    offenders.append(f"{path.name}:{number}")
        assert not offenders, (
            "these read /proc without braces, so a pid that exits mid-scan "
            "prints the shell's redirection error: " + ", ".join(offenders)
        )


class TestStackFromTermux:
    """`onb-stack` from Termux used to start nothing and say nothing.

    Every path in the script is under /root, which does not exist in
    Termux: one attempt sailed into `cd "$APP"` and exited there, after the
    steps before it had already printed along the way — so the stack stayed
    down while the output looked like a run. `status` from the same place
    was a different lie: a table read against Termux's /proc, where the
    guest's venv paths are invisible, so tts-ja reported "not installed"
    against a device that had it.

    The Termux side now delegates. status/down go through a *bounded*
    login running the guest copy; `up` cannot be a login at all — a login
    exits with its command and proot's --kill-on-exit reaps every service
    it just started, a green table and then nothing — so it goes through
    the launcher, which is the keep-alive session.
    """

    def test_outside_termux_and_outside_the_guest_it_refuses(self):
        """The premise of the whole block: no context, no answer.

        Pre-fix, this exact invocation printed a status table read from the
        dev box's /proc — plausible, and about nothing on the phone.
        """
        result = subprocess.run(
            ["sh", str(SCRIPTS / "termux-stack.sh"), "status"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 1
        assert "not in Termux" in result.stderr
        assert "surreal" not in result.stdout, (
            "a status table printed where the guest paths do not exist is "
            "a table about the wrong machine"
        )

    def test_every_login_goes_through_the_bounded_wrapper(self):
        text = (SCRIPTS / "termux-stack.sh").read_text()
        for number, line in enumerate(text.splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            if "proot-distro login" in stripped:
                assert 'proot-distro login ubuntu -- "$@"' in stripped, (
                    f"termux-stack.sh:{number} bypasses the bounded wrapper: {stripped}"
                )
        block = text[text.index('if [ ! -d "$APP" ]') :]
        assert "timeout -k" in block, (
            "proot discards SIGTERM (SigIgn 15) - timeout without -k waits "
            "forever and is not a bound"
        )
        assert "ONB_GUEST_TIMEOUT" in block, "the bound must be adjustable"
        # The two verbs that run in the guest, explicitly - and naming the
        # script file, since $APP is the repository directory: `sh <dir>`
        # reads nothing and exits 0.
        assert 'in_guest sh "$APP/scripts/termux-stack.sh" status' in text
        assert 'in_guest sh "$APP/scripts/termux-stack.sh" down' in text

    def test_delegation_never_hands_sh_the_repository_directory(self):
        """`sh <directory>` reads nothing and exits 0 - a silent no-op.

        Observed on the device: `onb-stack status` returned at rc 0 with
        an empty terminal while the stack was up, twice in a row. The
        transport was fine - an `echo` through the same bounded wrapper
        printed - so what failed was the arm: it passed `$APP`, the
        repository directory, to sh, which ran no dispatch and reported
        nothing. `down` from the same place would have stopped nothing
        while claiming success, which is why the path is asserted here
        rather than left to the eye.
        """
        text = (SCRIPTS / "termux-stack.sh").read_text()
        termux = text[text.index('if [ ! -d "$APP" ]') :]
        calls = re.findall(r'in_guest sh "([^"]+)" (\S+)', termux)
        assert calls, "the Termux branch must delegate its guest verbs"
        for path, verb in calls:
            assert path.endswith("/scripts/termux-stack.sh"), (
                f"in_guest sh {path} {verb}: $APP is the directory, and "
                "`sh <dir>` exits 0 having run nothing"
            )
        assert [verb for _, verb in calls] == ["status", "down", "status"], (
            "status is delegated by its own arm and by up's short-circuit, "
            "down by its own"
        )

    def test_termux_up_never_logs_in_and_goes_through_the_launcher(self):
        text = (SCRIPTS / "termux-stack.sh").read_text()
        termux = text[text.index('if [ ! -d "$APP" ]') :]
        dispatch = termux.index('case "${1:-up}" in')
        up_arm = termux[termux.index("    up)", dispatch) :]
        up_arm = up_arm[: up_arm.index("    *)")]
        up_arm_code = "\n".join(
            line for line in up_arm.splitlines() if not line.strip().startswith("#")
        )

        assert "proot-distro login" not in up_arm_code, (
            "a login for `up` exits with its command, and --kill-on-exit "
            "reaps every service it started: a green table, then nothing"
        )
        # A healthy stack is reported, never restarted.
        assert "if api_up; then" in up_arm_code
        # The canonical down -> up cycle leaves a parked launcher whose
        # already_running guard would refuse a second start; the clean
        # slate is what makes that cycle work.
        assert 'sh "$HOME/bin/onb" stop' in up_arm_code
        # setsid's refusal must come BEFORE anything is stopped: after the
        # clean slate it would leave the device with everything down and
        # no way back up in the same command.
        assert up_arm_code.index("command -v setsid") < up_arm_code.index(
            'sh "$HOME/bin/onb" stop'
        )
        # The detached form onb-update uses, with stdin released and a
        # setsid guard — nohup alone leaves the launcher in this shell's
        # process group, where Ctrl+C reaches it and takes the stack down.
        match = re.search(r'setsid nohup sh "\$HOME/bin/onb"[^\n]*', up_arm_code)
        assert match, "the start must be the detached setsid form"
        assert "</dev/null" in match.group(0), (
            "a background process holding stdin makes the next interactive "
            "command read from the wrong place"
        )

    def test_the_ready_line_only_prints_when_surreal_is_ready(self):
        """The call site echoed `surreal  ready` unconditionally, so a real
        timeout printed the WARNING and then "ready" — two contradicting
        lines from one check, and the false one was the one that got read.
        """
        lines = (SCRIPTS / "termux-stack.sh").read_text().splitlines()
        ready = [
            i
            for i, line in enumerate(lines)
            if 'echo "surreal  ready"' in line and not line.strip().startswith("#")
        ]
        assert len(ready) == 1, "exactly one ready line"
        definition = next(
            i
            for i, line in enumerate(lines)
            if line.strip().endswith("wait_for_surreal() {")
        )
        call = next(
            i for i, line in enumerate(lines) if line.strip() == "wait_for_surreal"
        )
        assert definition < ready[0] < call, (
            "the ready line must sit inside wait_for_surreal's success "
            "branch, not at the call site where it printed regardless"
        )
