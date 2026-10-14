"""Process matching in the device launcher scripts.

`pids_of` decided whether the stack was up by grepping every cmdline in
/proc for a substring. That reported `llama up pid 26053 3236 kB` for a
process serving nothing, while `termux-launch.sh` - carrying its own copy
of the same function - correctly reported the port unhealthy. The status
table was confidently wrong, and the phone's chat could not work.

These tests pin the properties that fix it:

  * the executable is matched against argv[0], so a log file's name or a
    grep's own arguments cannot impersonate a process;
  * a pattern with arguments still matches arguments, because the status
    table asks for "uvicorn api.main:app" and argv[0] is only "uvicorn";
  * every match is returned, not the first, because llama-server is not
    reliably one process and signalling only the first leaves the port
    held by another.

Needs real /proc entries to match, so it is skipped where there are
none - which is every environment except a real POSIX host.
"""

import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    not Path("/proc/self/cmdline").exists(),
    reason="needs /proc with readable cmdlines",
)

STACK = Path(__file__).resolve().parents[1] / "scripts/termux-stack.sh"


@pytest.fixture(scope="module")
def pids_of(tmp_path_factory):
    """The shipped `pids_of`, extracted verbatim, with stand-in processes.

    Extracted rather than imported so the test exercises the code that
    ships rather than a copy that can drift from it.
    """
    text = STACK.read_text()
    start = text.index("pids_of() {")
    end = text.index("\n}", start) + 2
    script = tmp_path_factory.mktemp("match") / "pids_of.sh"
    script.write_text(f"#!/bin/sh\n{text[start:end]}\n")

    # Real executables under borrowed names, because a shebang script
    # shows up in /proc as its interpreter: a `#!/bin/sh` wrapper named
    # uvicorn reports argv[0] `/bin/sh`, which is not what ships.
    stand_in = tmp_path_factory.mktemp("bin")
    donor = shutil.which("tail") or shutil.which("cat")
    if donor is None:
        pytest.skip("no binary available to stand in for the real ones")
    for name in ("uvicorn", "llama-server", "next-server"):
        shutil.copy(donor, stand_in / name)
        os.chmod(stand_in / name, 0o755)

    operands = tmp_path_factory.mktemp("args")
    for name in ("api.main:app", "something.else", "x.gguf"):
        (operands / name).write_text("")

    def run(pattern: str) -> list[str]:
        out = subprocess.run(
            ["sh", "-c", f'. "{script}"\npids_of "{pattern}"'],
            capture_output=True,
            text=True,
            timeout=30,
        )
        return [line for line in out.stdout.split() if line.isdigit()]

    run.stand_in = stand_in  # type: ignore[attr-defined]
    run.operands = operands  # type: ignore[attr-defined]
    return run


def _spawn(binary: Path, operand: Path) -> subprocess.Popen:
    proc = subprocess.Popen(
        [str(binary), "-f", str(operand)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    time.sleep(0.3)
    assert proc.poll() is None, "fixture process died; the test would be vacuous"
    return proc


class TestArgumentsAreStillMatched:
    def test_a_two_word_pattern_matches_its_argument(self, pids_of):
        """argv[0] alone is only `uvicorn`; the argument is the discriminator."""
        proc = _spawn(pids_of.stand_in / "uvicorn", pids_of.operands / "api.main:app")
        try:
            assert str(proc.pid) in pids_of("uvicorn api.main:app")
        finally:
            proc.kill()

    def test_a_two_word_pattern_skips_the_wrong_argument(self, pids_of):
        proc = _spawn(pids_of.stand_in / "uvicorn", pids_of.operands / "something.else")
        try:
            assert str(proc.pid) not in pids_of("uvicorn api.main:app")
        finally:
            proc.kill()

    def test_a_one_word_pattern_matches_despite_any_arguments(self, pids_of):
        """`next-server` is the whole pattern; Next rewrites its own title."""
        proc = _spawn(pids_of.stand_in / "next-server", pids_of.operands / "x.gguf")
        try:
            assert str(proc.pid) in pids_of("next-server")
        finally:
            proc.kill()


class TestSubstringsCannotImpersonate:
    def test_a_process_named_like_a_log_file_is_not_the_server(self, pids_of):
        """The bug. A cmdline mentioning llama-server is not llama-server."""
        proc = subprocess.Popen(
            ["sh", "-c", 'exec -a "llama-server.log" sleep 300'],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        time.sleep(0.3)
        try:
            assert str(proc.pid) not in pids_of("llama-server")
        finally:
            proc.kill()

    def test_a_prefix_of_the_name_is_not_a_match(self, pids_of):
        proc = subprocess.Popen(
            ["sh", "-c", 'exec -a "not-llama-server" sleep 300'],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        time.sleep(0.3)
        try:
            assert str(proc.pid) not in pids_of("llama-server")
        finally:
            proc.kill()


class TestEveryMatchIsReturned:
    def test_two_processes_with_one_name_both_come_back(self, pids_of):
        """Signalling only the first would leave the port held by the other."""
        procs = [
            _spawn(pids_of.stand_in / "uvicorn", pids_of.operands / "api.main:app"),
            _spawn(pids_of.stand_in / "uvicorn", pids_of.operands / "something.else"),
        ]
        try:
            found = pids_of("uvicorn")
            for proc in procs:
                assert str(proc.pid) in found
        finally:
            for proc in procs:
                proc.kill()


class TestTheScanIsQuiet:
    def test_a_process_exiting_mid_scan_prints_nothing(self, pids_of):
        """Redirection order, not a style preference.

        POSIX applies redirections left to right, so
        `tr ... < /proc/<pid>/cmdline 2>/dev/null` attempts the input
        open *before* stderr is redirected, and a process that exits
        between the glob and the read sprays the error across the status
        output every single time.
        """
        churn = [
            subprocess.Popen(
                ["sleep", "0.05"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
            for _ in range(40)
        ]
        result = subprocess.run(
            ["sh", "-c", f'. "{_script_path()}"\npids_of uvicorn >/dev/null'],
            capture_output=True,
            text=True,
            timeout=30,
        )
        for proc in churn:
            proc.wait()

        assert result.stderr == "", f"the scan leaked to stderr: {result.stderr!r}"


def _script_path() -> str:
    """Where the module fixture wrote the extracted function."""
    # Recreated here rather than threaded through, so this test does not
    # depend on fixture ordering.
    text = STACK.read_text()
    start = text.index("pids_of() {")
    end = text.index("\n}", start) + 2
    path = Path(os.environ.get("TMPDIR", "/tmp")) / f"pids_of_test_{os.getpid()}.sh"
    path.write_text(f"#!/bin/sh\n{text[start:end]}\n")
    return str(path)
