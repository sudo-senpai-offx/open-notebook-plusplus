"""One source of truth for the phone launcher.

The loopback binding was implemented, shipped, deployed, reinstalled, and
restarted - and the phone was still listening on `0.0.0.0` for hours. Every
signal said it had landed:

    grep -c api_host ~/bin/onb-stack        -> 3
    git log --oneline -1                    -> the commit that added it
    onb-update --restart                    -> clean run
    kill <pid>; sh ~/bin/onb                -> fresh process

and `adb shell netstat` still showed `0.0.0.0:5055`.

The cause: `termux-launch.sh` executes `sh /root/termux-stack.sh up` - a copy
at the guest root that **nothing in the repository installs or updates**. So
there were two launchers, one refreshed by `install-bin.sh` and one that was
actually run, frozen at setup time. Every fix reached the file that was not
being executed.

This is the same failure the codebase already documents once, for
`onb-embed`: an installed copy that drifts because nobody keeps it current.
A second instance of it is a structural bug, not a coincidence, so the test
below pins the *absence* of a second unmaintained copy rather than the
presence of any one line.
"""

from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _read(name: str) -> str:
    return (REPO / "scripts" / name).read_text()


def _code_of(function_header: str, script: str = "termux-stack.sh") -> str:
    """A shell function's body, with comment lines removed.

    Several of these functions carry comments quoting the code they
    replaced, so an assertion against the raw text fails on the explanation
    of the fix rather than on the fix.
    """
    text = _read(script)
    start = text.index(function_header)
    body = text[start : text.index("\n}\n", start)]
    return "\n".join(
        line for line in body.splitlines() if not line.lstrip().startswith("#")
    )


class TestSingleSourceOfTruth:
    def test_launcher_executes_the_repo_copy(self):
        """Not `/root/termux-stack.sh`. That path is the one that froze."""
        text = _read("termux-launch.sh")
        assert (
            "sh /root/app/open-notebook-plusplus/scripts/termux-stack.sh up" in text
        ), (
            "the launcher must run the copy in the repository, which "
            "install-bin.sh and every update keep current"
        )

    def test_launcher_does_not_execute_the_guest_root_copy(self):
        text = _read("termux-launch.sh")
        assert "sh /root/termux-stack.sh up" not in text, (
            "this is the frozen copy that ran a stale binary for hours"
        )

    def test_nothing_ships_a_second_launcher_to_the_guest_root(self):
        """Only `install-bin.sh` may write there, and it must refresh it.

        If a second installer of that path appears, this test names the
        failure rather than letting it drift again unnoticed.
        """
        writers = []
        for script in (REPO / "scripts").glob("*.sh"):
            text = script.read_text()
            if "root/termux-stack.sh" in text and script.name != "install-bin.sh":
                # A mention inside a comment or a message is fine; what is
                # not fine is something that copies or executes it.
                for line in text.splitlines():
                    if "root/termux-stack.sh" not in line:
                        continue
                    stripped = line.lstrip()
                    if stripped.startswith("#"):
                        continue
                    if any(
                        verb in line
                        for verb in ("cp ", "nohup", "sh /root/termux-stack.sh")
                    ):
                        writers.append(f"{script.name}: {stripped[:70]}")
        assert writers == [], (
            "only install-bin.sh may write the guest-root launcher:\n"
            + "\n".join(writers)
        )

    def test_install_refreshes_the_guest_root_copy(self):
        """Kept for anyone whose muscle memory reaches for the old path -
        but refreshed, so it is not a trap."""
        text = _read("install-bin.sh")
        assert "refresh_guest_launcher" in text
        assert "guest /root/termux-stack.sh" in text

    def test_the_refresh_uses_a_path_that_exists(self):
        """`REPO` in install-bin.sh already *is* the scripts directory, so
        `$REPO/scripts/termux-stack.sh` resolves to `scripts/scripts/...`.

        That shipped once. The failure was a `cp: cannot stat` line in the
        middle of an otherwise successful install, on a device where the
        real problem was one directory deeper. A warning buried in a list
        of successes is the same shape as a start that reports success
        while the process died - so the path is asserted here rather than
        left to be noticed on a phone.
        """
        import re

        text = _read("install-bin.sh")
        repo_default = re.search(r'REPO="\$\{1:-\$DEFAULT_REPO\}"', text)
        assert repo_default, "REPO should default to $DEFAULT_REPO"

        default_repo = re.search(r'DEFAULT_REPO="([^"]+)"', text)
        assert default_repo, "DEFAULT_REPO must be declared"
        assert default_repo.group(1).endswith("/scripts"), (
            "REPO points at the scripts directory itself, so a path built as "
            "$REPO/scripts/... would double the segment"
        )

        # Every source path used by the installer must be $REPO/<file>.
        for match in re.finditer(r"\$REPO/([A-Za-z0-9_./-]+)", text):
            assert "scripts/" not in match.group(1), (
                f"$REPO/{match.group(1)} would resolve to scripts/scripts/..."
            )

    def test_a_failed_refresh_is_reported_not_swallowed(self):
        """The `cp` returning non-zero must not leave the installer
        claiming everything is installed."""
        text = _read("install-bin.sh")
        refresh = text[
            text.index("refresh_guest_launcher()") : text.index(
                "\n}\n", text.index("refresh_guest_launcher()")
            )
        ]
        assert "cmp -s" in refresh, "an unchanged copy needs no write"
        assert "&&" in refresh, "the copy must gate on itself succeeding"
        assert "refresh_guest_launcher || true" in text, (
            "a refresh failure must not be fatal to the rest of the install, "
            "but it must also not be silent - the || true is only safe "
            "because the function reports its own failure"
        )


class TestProcessMatcher:
    """`uvicorn DOWN` while uvicorn was serving requests.

    The matcher compared argv[0]'s basename against the pattern head. For a
    venv console script that is wrong: `.venv/bin/uvicorn` is executed *by*
    the venv python, so the process's argv[0] is `python` and `uvicorn`
    appears only as an argument. Confirmed on the device:

        19301 python .venv/bin/uvicorn api.main:app --host 127.0.0.1 --port 5055

    so `is_up` was permanently false for the API and the worker. Every `up`
    therefore started a second copy, which is the EADDRINUSE the start guard
    was added to catch - the guard was watching for a failure the matcher was
    manufacturing.
    """

    def _matcher_body(self) -> str:
        return _code_of("pids_of() {")

    def test_matches_an_interpreter_hosted_process(self):
        body = self._matcher_body()
        assert "python | python3 | node" in body, (
            "a venv console script runs under python, so argv[0] is the "
            "interpreter; matching argv[0] alone cannot see the API"
        )
        assert 'grep -q -- "$1"' in body, (
            "the interpreter branch must match the whole pattern, not just "
            "the head, or `python manage.py` would match `manage.py`"
        )

    def test_still_matches_a_native_binary_by_argv0(self):
        """llama-server, surreal and next-server are real executables, so
        the original argv[0] match has to keep working for them."""
        body = self._matcher_body()
        assert '"$pattern_head")' in body
        assert 'grep -q -- "$pattern_tail"' in body

    def test_shells_are_not_treated_as_interpreters(self):
        """Otherwise a diagnostic quoting the pattern matches itself:
        `bash -c '... uvicorn api.main:app ...'`. That is the confusion
        this function was rewritten once already."""
        body = self._matcher_body()
        interpreter_branch = body[body.index('case "$base" in') :]
        assert "sh |" not in interpreter_branch
        assert "bash" not in interpreter_branch.split("esac")[0]

    def test_a_rewritten_process_title_still_matches(self):
        """`next-server DOWN` for a web server that was serving pages.

        Next rewrites its own process title to `next-server (v16.3.8)`, so
        argv[0] arrives with the version glued on. `${argv0##*/}` strips path
        components, not a version suffix, so the comparison could never match.

        This was not cosmetic: `is_up` gated on the same function, so `up`
        started a *second* web server while the first was running.
        """
        body = _code_of("pids_of() {")
        assert "base=${base%% (*}" in body, (
            "a rewritten process title arrives as `next-server (v16.3.8)`; "
            "the version suffix must be stripped before comparing"
        )

    def test_pid_of_returns_exactly_one_pid_from_several(self):
        """llama-server is reliably two processes, and the status table printed

            llama  up  pid 30758
            30789 ? kB

        because `pid_of` stripped at the first *space* while `pids_of` emits
        one pid per *line*. The whole multi-line value was substituted into
        `/proc/$pid/status` and awk read nothing.

        `is_up` was unaffected - a non-empty string is still non-empty - so
        the gate kept working while the display quietly lied.
        """
        body = _code_of("pid_of() {")
        assert "${_pids%% *}" not in body, (
            "pids_of emits one pid per line; stripping at the first space "
            "returns every pid, not the first"
        )
        assert "${_pids%%" in body and "*}" in body, (
            "the first *line* has to be what is taken"
        )

    def test_pid_of_does_not_close_the_pipe_early(self):
        """`pids_of | head -1` closed the pipe, so every later echo in
        pids_of failed with EPIPE - printed by the shell as
        `echo: I/O error` in the middle of a status table, from a different
        invocation than the one being reported."""
        body = _code_of("pid_of() {")
        assert "head -1" not in body, (
            "taking the first line with head closes the pipe and makes the "
            "remaining echoes in pids_of fail"
        )
        assert "_pids=$(pids_of" in body


class TestStartIsVerified:
    """A launcher that reports success while running the wrong thing."""

    def test_start_confirms_the_api_actually_answers(self):
        text = _read("termux-stack.sh")
        assert "api_answers" in text, (
            "`up` must confirm the bind took, not just that it launched a "
            "process: a survivor holding :5055 makes the new one die with "
            "EADDRINUSE while the launcher still prints a clean start"
        )
        assert "FAILED to start" in text

    def test_a_held_port_names_the_actual_fix(self):
        """The operator needs the command, not just the failure."""
        text = _read("termux-stack.sh")
        assert "address already in use" in text
        assert "pkill" in text

    def test_the_wait_polls_rather_than_guessing(self):
        """A fixed short sleep produced a false `FAILED to start` on a phone
        whose uvicorn was healthy and answering a second later.

        A guard that cries wolf is worse than none, because the next real
        failure gets read as the same noise. So the wait is a poll with a
        generous ceiling - the command modules are imported to populate the
        registry, and ADR-009 measured heavy imports at around a minute on
        low-end hardware.
        """
        text = _read("termux-stack.sh")
        assert "wait_for_api" in text
        start = text.index("wait_for_api()")
        body = text[start : text.index("\n}\n", start)]
        assert "while" in body, "poll until it answers"
        assert "sleep" in body
        assert "api_answers" in body
        # Generous ceiling, not the 2 seconds that shipped.
        assert "wait_for_api 90" in text

    def test_a_failure_report_shows_the_api_not_the_last_writer(self):
        """Every process appends to one shared log, so a bare `tail` on a
        start failure showed SurrealDB's startup lines - which say nothing
        about why uvicorn did not come up.
        """
        text = _read("termux-stack.sh")
        start = text.index("report_api_failure()")
        body = text[start : text.index("\n}\n", start)]
        assert "grep -iE" in body, "filter to lines that could be the API's"
        assert "shared log" in body, "and say it is a filtered view"
        assert 'tail -n 6 "$LOG"' not in body, (
            "an unfiltered tail of the shared log is what showed SurrealDB"
        )


class TestBindingCheckIsHonest:
    """The check that reported "loopback" while the service was on 0.0.0.0.

    `netstat` shows nothing inside Termux because Android hides /proc/net
    from apps, and `ip -4 addr` does not list wlan0, so the LAN address is
    routinely unavailable. An earlier version tested `[ -n "$lan" ]` and fell
    through to the reassuring branch when it was empty - a guard that
    answered "fine" precisely when it knew nothing.
    """

    def test_says_it_cannot_verify_rather_than_claiming_loopback(self):
        text = _read("termux-stack.sh")
        assert "cannot verify" in text

    def test_never_prints_loopback_from_an_unknown_address(self):
        """The exact bug, pinned structurally: the reassuring message and
        the "cannot verify" message must not be the same branch."""
        text = _read("termux-stack.sh")
        start = text.index("report_binding()")
        body = text[start : text.index("\n}", start)]
        assert 'if [ -z "$lan" ]' in body, (
            "the empty-address case must be handled first"
        )
        empty_branch = body.split('if [ -z "$lan" ]')[1].split("return 0")[0]
        assert "loopback-only" not in empty_branch, (
            "an undetermined address must never take the branch that says loopback-only"
        )

    def test_points_at_the_check_that_can_actually_work(self):
        """Locally unverifiable, so it names the vantage point that can."""
        text = _read("termux-stack.sh")
        assert "<device-lan-ip>" in text


class TestBindingIsRequested:
    def test_api_host_defaults_to_loopback(self):
        text = _read("termux-stack.sh")
        assert "OPEN_NOTEBOOK_API_HOST:-127.0.0.1" in text

    def test_api_host_is_honoured_when_set(self):
        """An operator who knowingly wants LAN reach can ask for it."""
        text = _read("termux-stack.sh")
        assert '--host "$api_host"' in text
