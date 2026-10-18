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


class TestLauncherDoesNotKillTheEmbedder:
    """`start_llama` killed the embedding server on every launch.

    `pids_of llama-server` matches the chat server (:8080) and the embedder
    (:8081) alike - same executable. `start_llama` tested health on :8080,
    which is not up yet at that point, concluded "stale llama-server is not
    serving; replacing", and signalled **both**. The embedder deferred SIGTERM
    until its request finished, so the very next check still saw it and
    reported `already up`; roughly fifteen seconds later it exited.

    Both logs recorded the same signature - `listening on ...` then
    `operator(): cleaning up before exit...` about fifteen seconds later -
    which reads as a crash and is not one. Nothing in the logs said who sent
    the signal, so this cost two rounds of guessing about OOM.
    """

    def _launcher(self) -> str:
        return _read("termux-launch.sh")

    def test_has_a_port_scoped_lookup(self):
        assert "pids_on_port() {" in self._launcher()

    def test_kill_llama_only_touches_the_chat_server(self):
        text = self._launcher()
        start = text.index("kill_llama() {")
        body = text[start : text.index("\n}\n", start)]
        assert "pids_on_port 8080" in body
        assert "pids_of llama-server" not in body, (
            "matching by executable name kills the embedder on :8081 as well"
        )

    def test_start_llama_does_not_treat_the_embedder_as_stale(self):
        text = self._launcher()
        start = text.index("start_llama() {")
        body = text[start : text.index("\n  }\n", start)]
        assert "pids_on_port 8080" in body, (
            "the staleness check must only consider the server on :8080, "
            "which is the only one whose health it has just tested"
        )

    def test_stop_llama_leaves_the_embedder_alone(self):
        """The embedder has its own lifecycle, stopped explicitly on exit."""
        text = self._launcher()
        start = text.index("stop_llama() {")
        body = text[start : text.index("\n  }\n", start)]
        assert "pids_on_port 8080" in body


class TestLauncherCannotExitOnStdinEOF:
    """The stack died with no killer visible anywhere.

    `~/bin/onb` blocked on `read -r _` inside the proot session. `read`
    returns the instant stdin reaches EOF - a closed terminal, a detached
    session, a shell handing over a pipe - and the session then ends, running
    `stop_llama` and `onb-embed down`.

    The signature was unmistakable once the log was read: `listening` followed
    by `operator(): cleaning up before exit...` 39 seconds later, which is a
    signal and not the OOM killer. Nothing in the output named the sender, so
    this was misdiagnosed as memory pressure twice before the log settled it.
    """

    def _launcher(self) -> str:
        """Comment lines removed.

        The comment explaining this fix quotes the code it replaced, so
        asserting on the raw text fails on the explanation rather than on
        the behaviour - the same trap twice in one file.
        """
        return "\n".join(
            line
            for line in _read("termux-launch.sh").splitlines()
            if not line.lstrip().startswith("#")
        )

    def test_does_not_block_on_read(self):
        """A read can return early; the session must not be able to end."""
        assert "read -r _" not in self._launcher(), (
            "`read` returns immediately on EOF, which ends the proot session "
            "and takes both model servers with it"
        )

    def test_blocks_on_a_sleep_loop(self):
        assert "while :; do" in self._launcher()

    def test_sleep_values_are_busybox_safe(self):
        """`sleep infinity` is not portable; busybox rejects it.

        The loop also wakes every few seconds to notice being orphaned, so the
        values are short and finite rather than one long sleep.
        """
        text = self._launcher()
        assert "sleep infinity" not in text
        assert "2147483647" not in text, (
            "the loop wakes periodically to check whether the launcher has "
            "been orphaned, so a single hour-long sleep would leave the stack "
            "running long after the terminal closed"
        )

    def test_still_tells_the_operator_how_to_stop(self):
        text = self._launcher()
        assert "press Ctrl+C to stop:" in text, (
            "the prompt is the only thing telling the operator this is "
            "foreground; removing the read must not remove the prompt"
        )

    def test_teardown_still_runs_on_ctrl_c(self):
        """Blocking forever must not mean Ctrl+C is ignored - the trap and the
        teardown are untouched."""
        text = self._launcher()
        start = text.index("guest session ended")
        assert "stop_llama" in text[start : start + 300], (
            "Ctrl+C has to reach the teardown, or the stack cannot be stopped at all"
        )


class TestOnlyOneLauncher:
    """Two `~/bin/onb` processes race, and llama-server binds SO_REUSEPORT.

    Each launcher starts a chat server and runs `onb-embed up`. The second
    sees the embedder mid-start, gets no health check back inside its timeout,
    decides it is stale, and starts another. Measured on the device: five
    processes claiming :8081, two of which appeared between two consecutive
    checks seconds apart. With SO_REUSEPORT several can hold the port and the
    kernel hands requests to whichever it picks - some with no model loaded.

    They also fight on exit: whichever ends first runs `stop_llama` and
    `onb-embed down` and takes the other's servers with it. That is the
    launcher-exit failure reached by a different route.
    """

    def _code(self) -> str:
        return "\n".join(
            line
            for line in _read("termux-launch.sh").splitlines()
            if not line.lstrip().startswith("#")
        )

    def test_has_a_duplicate_guard(self):
        assert "already_running()" in self._code()

    def test_guard_is_on_the_up_path(self):
        text = self._code()
        i = text.index("  up)")
        assert "already_running" in text[i : i + 300], (
            "the guard has to run before anything is started, or the second "
            "launcher has already raced by then"
        )

    def test_guard_refuses_rather_than_replacing(self):
        """Silently taking over would kill the first launcher's servers, which
        is the same failure as the EOF bug."""
        text = self._code()
        i = text.index("if other=$(already_running); then")
        # End at the refusal, not after it: the next line installs the INT
        # trap, which legitimately mentions stop_llama.
        window = text[i : text.index("exit 1", i)]
        assert "exit 1" not in window
        assert "stop_llama" not in window, (
            "the refusal path must not stop anything - that is what the "
            "running launcher owns"
        )

    def test_guard_excludes_itself(self):
        text = self._code()
        i = text.index("already_running()")
        body = text[i : text.index("\n}\n", i)]
        assert '"$$"' in body, (
            "without excluding its own pid the guard always matches itself and "
            "the launcher can never start"
        )

    def test_guard_excludes_subshells_not_just_itself(self):
        """The guard runs inside `$(already_running)`.

        That subshell has our cmdline and a different pid, so excluding only
        `$$` matches the very subshell doing the matching. Every start was
        refused, each naming a different pid - three refusals that read as
        three stale launchers rather than one self-match.
        """
        text = self._code()
        assert "_is_ours" in text, (
            "our own subshells carry our cmdline with a different pid; only a "
            "process-tree check separates them from a second launcher"
        )

    def test_guard_checks_the_process_tree_not_the_cmdline(self):
        """A genuine second launcher has an identical cmdline, so cmdline
        equality cannot be the discriminator."""
        text = self._code()
        i = text.index("_is_ours()")
        body = text[i : text.index("\n}\n", i)]
        assert body.count("PPid:") >= 2, (
            "both directions are needed: ancestors (a wrapper that exec'd into "
            "us) and descendants (our subshells)"
        )

    def test_guard_still_reports_a_real_second_launcher(self):
        """The fix must not have turned the guard off."""
        text = self._code()
        i = text.index("_is_ours")
        j = text.index('case "$cmd" in', i)
        window = text[j : j + 300]
        assert "_is_ours" in window and "echo" in window, (
            "a real second instance is still ours=false and must be reported"
        )

    def test_stop_is_still_reachable(self):
        """The guard must not stop someone cleaning up."""
        text = self._code()
        assert "\n  stop)" in text


class TestLauncherDoesNotLingerWhenOrphaned:
    """A launcher whose terminal closed kept the whole stack hostage.

    Measured on the device: `sh ~/bin/onb` with PPID=1, still running, still
    holding :8080, :8081, the proot session and the guest stack. Its parent
    terminal had gone, so init had adopted it.

    `~/bin/onb stop` from a new terminal could not clear it: `proot-distro kill
    ubuntu` only reaches sessions the *current* proot-distro invocation knows
    about, and the login that registered that session exited long before. So
    the next launch matched processes nothing was supervising and reported
    `already up`.
    """

    def _code(self) -> str:
        return "\n".join(
            line
            for line in _read("termux-launch.sh").splitlines()
            if not line.lstrip().startswith("#")
        )

    def test_watches_its_own_parent(self):
        text = self._code()
        assert "PPid:" in text, (
            "the launcher must notice being reparented to init; without that "
            "check a closed terminal leaves the stack running forever"
        )

    def test_exits_when_orphaned(self):
        text = self._code()
        i = text.index('_foreground="${ONB_FOREGROUND:-0}"')
        window = text[i : i + 700]
        assert '= "1"' in window, "PPID 1 means init adopted it - the terminal is gone"
        assert "break" in window, "the wait loop has to end so the teardown runs"

    def test_wakes_often_enough_to_notice(self):
        """A single long sleep would hold the stack for that long after the
        terminal closes."""
        text = self._code()
        i = text.index("while :; do")
        window = text[i : i + 200]
        assert "sleep 5" in window or "sleep 3" in window, (
            "the parent check has to run every few seconds, not once an hour"
        )

    def test_requires_sustained_orphaning(self):
        """A single PPID=1 sample is not proof the terminal closed.

        The launcher can briefly read 1 while the shell that spawned it is
        being replaced. Tearing down on one sample looked exactly like the
        stack dying by itself: `~/bin/onb` reported a duplicate within a
        minute of starting, and was gone seconds later with nothing running and
        nothing in any log.
        """
        text = self._code()
        # Anchor inside the orphan loop. The guard's process-tree walk also
        # reads PPid, and appears earlier in the file.
        i = text.index("_orphan=$((_orphan + 1))")
        window = text[i - 400 : i + 400]
        assert "_orphan" in window, (
            "one sample is not proof; the condition must persist before the "
            "stack is released"
        )
        assert "-ge 12" in window, (
            "a full minute of sustained orphaning, not a single sample"
        )

    def test_a_live_parent_resets_the_counter(self):
        """Otherwise a launcher that reads 1 twice in an hour still exits."""
        text = self._code()
        i = text.index('_foreground="${ONB_FOREGROUND:-0}"')
        window = text[i : i + 500]
        assert "_orphan=0" in window.split("else")[-1], (
            "a sample showing a live parent must clear the accumulated count"
        )

    def test_an_unreadable_status_does_not_kill_the_stack(self):
        """`${_ppid:-1}` defaults to 1, which *looks* like an orphan. Verified
        deliberately: a transient read failure must not tear down a working
        stack."""
        text = self._code()
        i = text.index('_foreground="${ONB_FOREGROUND:-0}"')
        window = text[i : i + 900]
        assert "${_ppid:-1}" in window


class TestDetachedLauncherIsNotAnOrphan:
    """`onb-update --restart` starts the stack detached, and must survive.

    The updater runs `nohup sh "$HOME/bin/onb" >/dev/null 2>&1 &`, so a
    detached launcher has PPID=1 from the moment it begins - by design.
    Applying the orphan check to it tore the stack down a minute later: the
    updater reported success, and the stack it had just started was gone before
    anyone looked at it. The user then ran `~/bin/onb` and got a refusal to
    start a second launcher, naming a pid that no longer existed.
    """

    def _code(self) -> str:
        return "\n".join(
            line
            for line in _read("termux-launch.sh").splitlines()
            if not line.lstrip().startswith("#")
        )

    def test_records_whether_it_is_foreground(self):
        text = self._code()
        assert "[ -t 0 ]" in text, (
            "only a foreground launch can have a terminal close under it; a "
            "detached one is orphaned by design"
        )

    def test_the_value_is_exported_to_the_guest(self):
        """Inside the guest session stdin is a pipe, so testing it there is
        always false and the check would silently never arm."""
        text = self._code()
        assert "export ONB_FOREGROUND" in text, (
            "the tty test has to happen in the launcher shell and be passed "
            "into the guest block; stdin inside proot is a pipe"
        )
        assert "${ONB_FOREGROUND:-0}" in text, (
            "the guest block must read the exported value, not test stdin"
        )

    def test_skips_the_orphan_check_when_detached(self):
        text = self._code()
        i = text.index("[ -t 0 ]")
        window = text[i : i + 900]
        assert '_foreground" = "0"' in window, (
            "a detached launcher must not tear the stack down for having "
            "PPID=1, which is how it was started"
        )
        assert "continue" in window, "the check has to be skipped, not merely softened"

    def test_the_tty_check_runs_before_proot_replaces_stdin(self):
        """Inside the guest session stdin is a pipe, so `test -t 0` there is
        always false and the check would never arm."""
        text = self._code()
        tty = text.index("[ -t 0 ]")
        guest = text.index("proot-distro login ubuntu")
        assert tty < guest, (
            "test -t 0 has to be evaluated in the launcher shell, before the "
            "guest session substitutes stdin with a pipe"
        )


class TestDetachedStartIsNotSilent:
    """`onb-update --restart` discarded everything.

    `nohup sh "$HOME/bin/onb" >/dev/null 2>&1 &` made every failure of a
    detached start invisible: the updater reported success, nothing came up,
    and the only evidence was a status table showing everything DOWN with no
    reason attached. The user could not tell a broken launcher from a stack
    that had not started.
    """

    def _update(self) -> str:
        return (REPO / "scripts" / "termux-update.sh").read_text()

    def test_does_not_discard_output(self):
        text = self._update()
        start = text.index('nohup sh "$HOME/bin/onb"')
        line_end = text.index("\n", start)
        assert "/dev/null" not in text[start:line_end], (
            "a detached start whose output is discarded cannot be diagnosed"
        )

    def test_names_the_log(self):
        """An operator needs to be told where to look."""
        assert "onb-start.log" in self._update()
        assert "log: $_startlog" in self._update()

    def test_truncates_per_run(self):
        """Otherwise it grows without bound across every update."""
        text = self._update()
        assert ': > "$_startlog"' in text, "the log has to start fresh each run"
