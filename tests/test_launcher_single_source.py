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
