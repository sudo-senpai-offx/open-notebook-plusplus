"""The health check that decides whether `--restart` is allowed to proceed.

This is a regression test for a bug that reported success and changed nothing.

`termux-update.sh` probed `http://127.0.0.1:15055/api/sync/status` to decide
whether the stack was running. 15055 is the **PC's** adb-forward port - see
`phone-tunnel.sh`, which runs `adb forward tcp:15055 tcp:5055`. The script
itself runs in Termux **on the phone**, where the guest API binds 5055 and
nothing has ever listened on 15055.

curl got connection-refused, so `stack_is_up` was permanently false, and both
of its callers did nothing at all:

- the "the stack is running, stop it first" guard never fired, so an update
  could land underneath a live server;
- `--restart` never called `stop_stack`, so it updated the files and launched a
  second `~/bin/onb`, which the launcher's duplicate guard correctly refused.
  Exit status 0, "starting the stack again" printed, old processes still
  serving. The new code was on disk and never loaded.

The check looked like it was working. It was a health check that had never once
returned healthy, so it could not distinguish "nothing is running" from "I am
probing the wrong port" - and it was reporting the safe-sounding answer in the
unsafe direction.

The assertion below is deliberately cross-file. Either file alone is
unremarkable: one binds a port, the other probes a port, and each looks
correct. Only the disagreement between them is the bug, so a test that reads
one file cannot catch it.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _read(name: str) -> str:
    return (REPO / "scripts" / name).read_text(encoding="utf-8")


def _code(script: str) -> str:
    """Strip whole-line comments.

    Several of these scripts carry comments that quote the code they replaced,
    so an assertion against raw text can pass against the explanation of the
    bug rather than the fix. That has happened three times in this repo.
    """
    return "\n".join(
        line for line in script.splitlines() if not line.strip().startswith("#")
    )


class TestHealthUrlPort:
    def test_update_probes_the_port_the_stack_actually_binds(self):
        """The whole bug, in one assertion.

        `termux-stack.sh` is what binds the API port; `termux-update.sh` is what
        probes it. If they disagree, `--restart` silently becomes a no-op.
        """
        stack = _code(_read("termux-stack.sh"))
        update = _code(_read("termux-update.sh"))

        bound = re.search(
            r"uvicorn\s+api\.main:app\s+--host\s+\S+\s+--port\s+(\d+)", stack
        )
        assert bound, "could not find the uvicorn --port in termux-stack.sh"

        probed = re.search(r'API_PORT="\$\{ONB_API_PORT:-(\d+)\}"', update)
        assert probed, "could not find the API_PORT default in termux-update.sh"

        assert probed.group(1) == bound.group(1), (
            f"termux-update.sh probes :{probed.group(1)} but termux-stack.sh "
            f"binds :{bound.group(1)}. stack_is_up can then only ever be false, "
            f"so --restart skips stop_stack and updates underneath a live server."
        )

    def test_does_not_probe_a_pc_tunnel_port(self):
        """The specific value that broke it, pinned so it cannot come back.

        15055 is real and correct *on the PC* - it is what `adb forward` binds
        there. Copying a port between a phone-side script and a PC-side script
        is the mistake, so the test names the port rather than trusting a
        cross-file comparison to catch it again in some other form.
        """
        update = _read("termux-update.sh")
        default = re.search(r'API_PORT="\$\{ONB_API_PORT:-(\d+)\}"', update)
        assert default
        assert default.group(1) != "15055", (
            "15055 is the PC's adb-forward port, not a phone port. On the phone "
            "nothing listens on it, so every health check against it fails."
        )

    def test_probes_an_endpoint_that_needs_no_credential(self):
        """`/api/sync/status` answers 401 without the bearer token.

        curl exits 0 on a 401, so the check only worked by accident of never
        passing `-f`. `/health` is unauthenticated, and it is what
        `termux-stack.sh` already probes for its own liveness check - so the two
        agree on what "the API is up" means.

        Asserted against comment-stripped source: the old endpoint is named in
        the comment explaining why it was replaced, and an assertion over raw
        text would fail against its own explanation.
        """
        update = _code(_read("termux-update.sh"))
        assert 'HEALTH_URL="http://127.0.0.1:$API_PORT/health"' in update
        assert "/api/sync/status" not in update


def _stop_body() -> str:
    """`stop_stack`'s body, comments stripped."""
    code = _code(_read("termux-update.sh"))
    start = code.index("stop_stack() {")
    end = code.index("launcher_pids() {", start)
    return code[start:end]


class TestStopActuallyStops:
    def test_stop_waits_for_the_api_to_go_quiet_before_reporting_success(self):
        """The old loop returned success on the first failed probe.

        `stack_is_up || return 0` inside the loop meant a single failed check
        ended the wait immediately, so a stack still shutting down was reported
        stopped. It has to check once more after the loop, which is what the
        code now does.
        """
        body = _stop_body()
        assert "stack_is_up || return 0" not in body, (
            "returning success from inside the wait loop means the first failed "
            "probe ends the wait, however much is still running"
        )
        assert re.search(r"if stack_is_up; then", body), (
            "the API has to be re-checked after the wait, not only during it"
        )

    def test_stop_also_checks_the_launcher_survived(self):
        """A quiet port is not a stopped stack.

        `onb stop` leaves a foreground launcher parked on its sleep loop unless
        it gets a signal. If one survives, the detached start below is refused
        by the launcher's own duplicate guard - which is how `--restart` came to
        report success while the old process tree kept serving.
        """
        body = _stop_body()
        assert "launcher_alive" in body, (
            "a launcher that outlives `onb stop` turns the restart into a no-op"
        )
        assert "kill -9" in body, (
            "the launcher parks on a sleep loop and defers SIGTERM; without "
            "escalation the wait times out and the restart is skipped anyway"
        )

    def test_refuses_to_start_when_a_launcher_cannot_be_killed(self):
        """Better to fail loudly than to print 'starting the stack again'."""
        body = _stop_body()
        assert "survived SIGKILL" in body and "return 1" in body

    def test_launcher_scan_matches_the_installed_path(self):
        """The scan has to look for the name the operator actually runs.

        `~/bin/onb` is the installed copy's name; the repo file is
        `termux-launch.sh`. Matching the repo name would find nothing and the
        check would pass vacuously - which is the failure mode of a guard that
        cannot fail.
        """
        code = _code(_read("termux-update.sh"))
        assert '"$HOME/bin/onb "' in code or '"$HOME/bin/onb"' in code
        assert "termux-launch.sh" not in code.split("launcher_pids() {")[1][:400]
