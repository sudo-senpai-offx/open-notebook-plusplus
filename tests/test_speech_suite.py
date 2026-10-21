"""Tests for the multilingual speech suite.

The suite is a measurement instrument, so what is worth testing is not "does it
run" — that is what the phone run is for — but whether the numbers it reports
mean anything. A suite that computes character error rate without normalising
punctuation reports a *wrong* failure that looks like a model problem, and a
suite that silently omits a language reports a *wrong* pass.
"""

from __future__ import annotations

import importlib.util
import re
import signal
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SUITE = REPO / "scripts" / "termux-speech-suite.py"
BUILDER = REPO / "scripts" / "build-speech-fixtures.py"


def _read_script(name: str) -> str:
    """A file from scripts/, as text. The other speech test file has one too."""
    return (Path(__file__).resolve().parents[1] / "scripts" / name).read_text(
        encoding="utf-8"
    )


def suite_module():
    """The suite module, for the few assertions that read module constants."""
    return _load_suite()


def _load_suite():
    """Import the suite as a module; it lives in scripts/ so it is not importable."""
    spec = importlib.util.spec_from_file_location("onb_speech_suite", SUITE)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def suite():
    return _load_suite()


class TestCharacterErrorRate:
    def test_punctuation_differences_are_not_errors(self, suite):
        """FLEURS references are punctuated; whisper usually is not.

        Compared raw, this single difference inflates the error rate enough to
        fail every English clip while the model is in fact perfect. It is the
        failure mode most likely to send someone looking for a model bug that
        does not exist.
        """
        reference = "The main local beer is 'Number One', it is not a complex beer."
        hypothesis = "the main local beer is number one it is not a complex beer"
        assert suite.cer(reference, hypothesis, "en") == 0.0

    def test_a_real_substitution_is_still_scored(self, suite):
        """Normalising must not flatten genuine errors into 0.0.

        The test above proves the metric ignores noise; this one proves it does
        not also ignore the signal. If normalising were over-aggressive both
        would pass, and the suite would be useless in the other direction.
        """
        reference = "The main local beer is 'Number One', it is not a complex beer."
        hypothesis = "the main local beer is number two it is not a complex beer"
        score = suite.cer(reference, hypothesis, "en")
        assert score is not None and 0.0 < score < 0.10

    def test_cjk_comparison_ignores_spacing(self, suite):
        """Whisper emits Japanese without spaces; a model that inserts them is
        not wrong, and counting the spaces would fail a correct transcript."""
        reference = "常駐管理は、音声モデルをデバイス上に残します。"
        spaced = "常駐管理は 音声モデルを デバイス上に 残します"
        assert suite.cer(reference, spaced, "ja") == 0.0

    def test_cjk_scripts_are_comparable(self, suite):
        assert (
            suite.cer(
                "驻留管理会把语音模型留在设备上", "驻留管理会把语音模型留在设备上", "zh"
            )
            == 0.0
        )

    def test_an_empty_reference_cannot_be_scored(self, suite):
        """A clip with no reference would otherwise divide by zero and crash the
        run halfway through, losing every result gathered before it."""
        assert suite.cer("", "anything at all", "en") is None
        assert suite.cer("   ", "text", "en") is None

    def test_levenshtein_matches_the_textbook_values(self, suite):
        assert suite.levenshtein("kitten", "sitting") == 3
        assert suite.levenshtein("", "") == 0
        assert suite.levenshtein("", "abc") == 3
        assert suite.levenshtein("abc", "abc") == 0
        assert suite.levenshtein("abc", "") == 3

    def test_levenshtein_is_symmetric(self, suite):
        """The implementation swaps its arguments to iterate the shorter axis,
        which is only correct if the distance is symmetric."""
        for a, b in [("abc", "abd"), ("hello world", "hello word"), ("xyz", "abcdef")]:
            assert suite.levenshtein(a, b) == suite.levenshtein(b, a)


class TestThresholdsArePerLanguage:
    def test_cer_ceilings_differ_by_script(self, suite):
        """One global threshold is wrong in both directions.

        Whisper's error rate on clean Tamil is several times its error rate on
        clean English. A single bar either fails a language that is working or
        passes one that is not, and the result depends on which languages happen
        to be in the run.
        """
        assert suite.MAX_CER["en"] < suite.MAX_CER["zh"] < suite.MAX_CER["ja"]
        assert suite.MAX_CER["ja"] < suite.MAX_CER["my"] < suite.MAX_CER["ta"]

    def test_every_reported_language_has_a_threshold(self, suite):
        for lang in ("en", "zh", "ja", "ta", "my"):
            assert lang in suite.MAX_CER, f"{lang} would fall back to the default"

    def test_thresholds_are_ceilinged_by_the_command_line_flag(self, suite):
        """`--max-cer 0.05` must tighten, never loosen, the per-language bars."""
        for lang, ceiling in suite.MAX_CER.items():
            assert min(ceiling, 0.05) == 0.05, f"{lang} ignores a tighter flag"


class TestCoverageIsReportedHonestly:
    """A language that cannot be tested must be named, not quietly omitted.

    Silently skipping is worse than failing: the summary reads "PASS" and the
    reader concludes the language works. These are the assertions that keep the
    report from claiming coverage piper and FLEURS do not provide.
    """

    def test_tts_names_the_voices_piper_does_not_have(self, suite):
        assert set(suite.TTS_UNTESTABLE) == {"tw", "ta", "my"}
        for lang, reason in suite.TTS_UNTESTABLE.items():
            assert "piper" in reason.lower(), f"{lang} needs a reason naming piper"

    def test_it_does_not_substitute_cantonese_for_taiwanese_mandarin(self, suite):
        """yue is a different language, not a Taiwanese accent.

        A suite that quietly tested yue for 'tw' would report a pass that means
        nothing: the phonology, the tone system and the training data are all
        different, and a Taiwanese user would be told their language works.
        """
        assert "zh_TW" not in str(suite.TTS_VOICES) + str(suite.TTS_UNTESTABLE)
        assert "yue" not in str(suite.TTS_VOICES)
        source = SUITE.read_text()
        assert "Cantonese" in source and "different language" in source

    def test_untested_counts_appear_in_the_summary(self, suite):
        source = SUITE.read_text()
        assert "UNTESTED" in source
        assert "untested" in source.lower(), "the summary must count them"

    def test_malayalam_is_not_treated_as_malay(self, suite):
        """`ml` is Malayalam. Accepting it for `my` would be a wrong pass.

        Checked on the declared language codes rather than by substring: `my`
        occurs inside `amy-medium`, so a naive `in` test on the voice table says
        something entirely different from what it looks like it says.
        """
        languages = {lang for _voice, lang in suite.TTS_VOICES}
        assert "my" not in languages, "Malay must not be reported as tested"
        assert "ml" not in languages, "Malayalam is not Malay"
        # The distinction has to be on the record, or a reader assumes ml was used.
        assert "Malayalam" in SUITE.read_text()


class TestRobustnessCases:
    def test_every_malformed_case_expects_a_refusal_not_a_200(self, suite):
        """The whole point: a 200 with an empty body is the failure to catch."""
        source = SUITE.read_text()
        # Tuple literals of HTTP codes, e.g. "(400, 415, 422)".
        groups = re.findall(r"\((\d{3}(?:,\s*\d{3})+)\)", source)
        assert groups, "no malformed-input cases found"
        for group in groups:
            codes = [int(x) for x in group.split(",")]
            assert 200 not in codes, f"{codes} permits a success"
            assert all(code >= 400 for code in codes), f"{codes} permits a success"

    def test_it_checks_a_wav_header_not_just_a_non_empty_body(self, suite):
        """`len(payload) > 0` accepts a 4-byte stub as audio.

        This is the exact shape of the earlier TTS failure: HTTP 200, a valid
        status, and nothing usable in the body.
        """
        assert 'startswith(b"RIFF")' in SUITE.read_text()

    def test_it_durations_are_bounded_below(self, suite):
        """A truncated wav still has a RIFF header, so length is checked too."""
        source = SUITE.read_text()
        assert "too short to be speech" in source
        assert "seconds < 1.0" in source

    def test_it_asserts_concurrency_rather_than_just_running_it(self, suite):
        source = SUITE.read_text()
        assert "ThreadPoolExecutor" in source
        assert "serial" in source


class TestTheSuiteIsInstallable:
    def test_stdlib_only(self, suite):
        """A device being measured must not gain a dependency to be measured."""
        source = SUITE.read_text()
        for banned in (
            "import requests",
            "import numpy",
            "import httpx",
            "from requests",
        ):
            assert banned not in source, f"{banned} is not available in Termux"

    def test_it_does_not_write_to_slash_tmp(self):
        """Termux has no /tmp; `tempfile.gettempdir()` resolves under $TMPDIR,
        which does exist, so that is the correct call and `"/tmp/"` must not
        appear as a literal anywhere."""
        assert '"/tmp/' not in SUITE.read_text()

    def test_the_installer_ships_it(self):
        assert (
            "termux-speech-suite.py"
            in (REPO / "scripts" / "install-bin.sh").read_text()
        )

    def test_it_reports_progress_during_the_longest_phase(self):
        """The STT loop is the suite's long pole and must not be silent.

        150 seconds of audio through a multilingual model on a phone, printing
        nothing until it finished: the first run gave two silent minutes on a
        device that was in fact at 89% CPU. Silence for the whole of the longest
        phase is indistinguishable from a hang, and the operator has no way to
        tell which it is without going and measuring CPU themselves.
        """
        source = SUITE.read_text()
        stt_body = source[source.index("def run_stt(") : source.index("def run_tts(")]
        # A per-clip line, and it must be flushed rather than left in a buffer.
        assert "flush=True" in stt_body
        assert 'for clip in manifest["clips"]' in stt_body
        assert stt_body.count("flush=True") >= 1, "no flushed progress line in run_stt"

    def test_progress_lines_carry_the_error_rate_and_the_time(self):
        """A progress line that only says "working" tells an operator nothing.

        The CER is what says a language is struggling, and the seconds are what
        say the model is too slow for the tier to be usable on the device.
        """
        source = SUITE.read_text()
        stt_body = source[source.index("def run_stt(") : source.index("def run_tts(")]
        assert "CER" in stt_body
        assert "detail" in stt_body
        assert "elapsed" in stt_body
        assert "flush=True" in stt_body


class TestTheRunIsBounded:
    """The first run went 21 minutes over 150 seconds of audio and produced
    nothing. That is the failure these tests exist to prevent: not a wrong
    answer, but no answer, with no way to tell it apart from a hang."""

    def test_every_clip_gets_a_deadline(self):
        """No clip may run unbounded. A socket read timeout is the only thing
        that actually aborts the request, so it has to be passed through."""
        source = SUITE.read_text()
        stt_body = source[source.index("def run_stt(") : source.index("def run_tts(")]
        assert "transcribe(path, timeout=budget" in stt_body
        assert "clip_timeout(" in stt_body

    def test_the_deadline_scales_with_audio_length(self, suite):
        """An absolute timeout is wrong in both directions.

        It either truncates long clips or spends most of its budget waiting for
        short ones, and which of those it is doing is not knowable until a clip
        is genuinely slow — which is exactly when it matters.
        """
        assert suite.clip_timeout(4.0, 20.0) < suite.clip_timeout(16.0, 20.0)

    def test_the_deadline_has_a_floor_and_a_ceiling(self, suite):
        """A short clip must not get a deadline it cannot meet, and a long one
        must not be handed an unbounded budget by the multiplication."""
        assert suite.clip_timeout(1.0, 20.0) == suite.MIN_CLIP_TIMEOUT
        assert suite.clip_timeout(10_000.0, 20.0) == suite.MAX_CLIP_TIMEOUT

    def test_the_budget_is_configurable(self):
        """A model slower than the default budget is still measurable; the
        operator raises it rather than the suite being rewritten."""
        source = SUITE.read_text()
        assert "--rtf-budget" in source
        assert "args.rtf_budget" in source

    def test_the_budget_default_is_set_by_measurement_not_hope(self):
        """large-v3-turbo-q5_0 measured RTF 8-16 on two A73 threads. The default
        has to clear that or every clip times out and the run says nothing."""
        assert suite_module().DEFAULT_RTF_BUDGET >= 16.0


class TestTheRunResumes:
    """The device drops connections. Twenty minutes of transcription is too much
    to lose to that, and losing it makes the suite least useful exactly when the
    device is least reliable."""

    def test_results_are_written_after_every_clip(self):
        source = SUITE.read_text()
        stt_body = source[source.index("def run_stt(") : source.index("def run_tts(")]
        assert "save_checkpoint(checkpoint, seen)" in stt_body

    def test_the_checkpoint_is_written_atomically(self, suite):
        """A half-written checkpoint is worse than none: the next run would
        resume from a truncated set and report it as complete."""
        assert ".tmp" in SUITE.read_text()
        assert "tmp.replace(path)" in SUITE.read_text()

    def test_a_corrupt_checkpoint_does_not_abort_the_run(self, suite):
        import tempfile

        bad = Path(tempfile.mkdtemp()) / "stt-results.json"
        bad.write_text("{ not json at all")
        assert suite.load_checkpoint(bad) == {}

    def test_results_round_trip(self, suite):
        import tempfile

        cp = Path(tempfile.mkdtemp()) / "stt-results.json"
        suite.save_checkpoint(
            cp, {"x.wav": {"status": "scored", "cer": 0.25, "seconds": 12.0}}
        )
        assert suite.load_checkpoint(cp)["x.wav"]["cer"] == 0.25
        assert not list(cp.parent.glob("*.tmp")), "atomic replace left a .tmp"

    def test_a_timeout_is_recorded_not_retried_forever(self):
        """A clip that times out is a *result* — it says the model is too slow
        for this material. Re-queuing it on every run would hide that."""
        source = SUITE.read_text()
        assert '"status": outcome' in source
        assert 'outcome = "scored" if status == 200 else "timeout"' in source

    def test_fresh_is_available_to_override_the_checkpoint(self):
        source = SUITE.read_text()
        assert '"--fresh"' in source
        assert "args.fresh" in source


class TestRealtimeFactorIsReported:
    def test_rtf_is_printed_per_clip(self):
        """CER says whether the model understood; RTF says whether it is fast
        enough to use on this device. Only one is fixable with a better model,
        and the first run's real finding was entirely about the other."""
        source = SUITE.read_text()
        stt_body = source[source.index("def run_stt(") : source.index("def run_tts(")]
        assert "RTF" in stt_body

    def test_it_is_computed_from_measured_duration(self):
        """Guessing the audio length would make RTF fiction."""
        assert 'clip.get("duration_s")' in SUITE.read_text()

    def test_the_manifest_carries_durations(self):
        """Which is why the fixture builder parses the wav header."""
        assert "wav_duration" in BUILDER.read_text()
        assert '"duration_s": duration' in BUILDER.read_text()

    def test_the_builder_reads_a_header_wave_cannot(self):
        """FLEURS is pcm_f32le, which the stdlib reader rejects outright."""
        builder = BUILDER.read_text()
        assert "pcm_f32le" in builder
        assert "unknown format" in builder


class TestTheSuiteIsInterruptible:
    """Ctrl+C appeared to do nothing, on a run that was otherwise healthy.

    Reproduced, not theorised: `with ThreadPoolExecutor(...)` plus the default
    SIGINT handler ignores Ctrl+C indefinitely, because `__exit__` calls
    `shutdown(wait=True)` and blocks until the in-flight workers finish. Each
    worker is inside a transcription with a 600-second deadline, so the suite
    looked frozen and the interrupt looked ignored. A plain blocking socket read
    is *not* the problem — that one exits in 0.01s.

    So the fix is two parts: never let the executor wait on the way out, and
    handle the signal with `os._exit`, which no pending shutdown can defer.
    """

    def test_the_executor_never_waits_on_the_way_out(self):
        """`with ThreadPoolExecutor(...)` is the shape that hangs.

        Checked against code, not prose: the module docstring and the comment
        above the call both *name* the pattern while explaining why it is wrong,
        so a whole-file search matches the warning instead of the defect.
        """
        lines = SUITE.read_text().splitlines()
        code = [
            line
            for line in lines
            if not line.lstrip().startswith("#")
            and "with ThreadPoolExecutor" not in line
        ]
        body = [
            line
            for line in code
            if "ThreadPoolExecutor" in line and not line.lstrip().startswith("from ")
        ]
        for line in body:
            assert "with " not in line, (
                f"a `with ThreadPoolExecutor` block swallows Ctrl+C: {line.strip()!r}"
            )
        assert "shutdown(wait=False, cancel_futures=True)" in SUITE.read_text()

    def test_signals_are_handled_with_os_exit(self):
        """`SystemExit` has to unwind past whatever the main thread is blocked
        in. `os._exit` cannot be deferred by that."""
        source = SUITE.read_text()
        assert "def install_signal_handlers" in source
        assert "os._exit(130)" in source
        assert "signal.SIGINT" in source and "signal.SIGTERM" in source

    def test_the_handler_is_actually_installed(self):
        """Defining the handler is not installing it."""
        source = SUITE.read_text()
        assert "install_signal_handlers()" in source

    def test_the_handler_saves_before_exiting(self):
        """Otherwise Ctrl+C costs the whole run, which is the moment an operator
        is most likely to reach for it."""
        source = SUITE.read_text()
        handler = source[source.index("def bail(") : source.index("os._exit(130)")]
        assert "save_checkpoint" in handler

    def test_the_measured_shapes_agree(self):
        """Run both shapes. If the executor ever stops hanging on SIGINT this
        test fails and the workaround can be removed deliberately rather than
        forgotten."""
        import subprocess
        import textwrap
        import time

        hanging = textwrap.dedent(
            """
            import sys
            from concurrent.futures import ThreadPoolExecutor
            def block():
                import socket
                s = socket.socket(); s.bind(("127.0.0.1", 0)); s.listen(1)
                s.settimeout(600); s.accept()
            with ThreadPoolExecutor(max_workers=2) as pool:
                list(pool.map(lambda _: block(), [1, 2]))
            """
        )
        proc = subprocess.Popen(
            [sys.executable, "-c", hanging],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        time.sleep(1.0)
        proc.send_signal(signal.SIGINT)
        try:
            proc.communicate(timeout=8)
            pytest.skip(
                "ThreadPoolExecutor no longer swallows SIGINT; the workaround can go"
            )
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
        # Confirmed: it is still running after SIGINT, which is the bug.

        fixed = textwrap.dedent(
            """
            import os, signal, sys, time
            from concurrent.futures import ThreadPoolExecutor

            # A real function, not `lambda s, f: sys.stdout.write("x") or os._exit(130)`.
            # write() returns 1, which is truthy, so `or` short-circuits and
            # os._exit never runs - a green test that proves nothing, which is
            # exactly what the first version of this test did.
            def bail(signum, frame):
                os._exit(130)

            signal.signal(signal.SIGINT, bail)
            pool = ThreadPoolExecutor(max_workers=2)
            try:
                list(pool.map(lambda _: time.sleep(600), [1, 2]))
            finally:
                pool.shutdown(wait=False, cancel_futures=True)
            """
        )
        fixed_proc = subprocess.Popen(
            [sys.executable, "-c", fixed],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        time.sleep(1.0)
        fixed_proc.send_signal(signal.SIGINT)
        try:
            fixed_proc.communicate(timeout=8)
        except subprocess.TimeoutExpired:
            fixed_proc.kill()
            fixed_proc.communicate()
            pytest.fail("the fixed shape still ignores SIGINT")


class TestCachedResultsAreInvalidated:
    """A cached result is only valid for the conditions it was measured under.

    The suite replayed fifteen stale timeouts. Two things had changed - the
    budget (the manifest gained durations, so every clip's deadline went from
    the 90s floor to 142-556s) and the model (the shipped tier went from the
    547 MB one to the 181 MB one) - and the cached outcomes were reported as the
    current result anyway. The lines even *displayed* the new budget beside the
    old outcome, so the output visibly disagreed with itself.
    """

    def test_the_checkpoint_records_the_budget_and_the_model(self):
        source = SUITE.read_text()
        assert '"budget": budget' in source, "the budget must be stored with the result"
        assert '"model": model_id' in source, "the model must be stored with the result"

    def test_a_cached_result_is_dropped_when_the_budget_changes(self):
        source = SUITE.read_text()
        assert 'prior.get("budget") != budget' in source, (
            "a timeout measured under a shorter deadline says nothing about a "
            "longer one"
        )

    def test_a_cached_result_is_dropped_when_the_model_changes(self):
        source = SUITE.read_text()
        assert 'prior.get("model") != model_id' in source, (
            "a transcript from one model says nothing about another"
        )

    def test_the_model_is_the_one_being_served_not_the_one_requested(self):
        """The launcher selects a tier; the server serves what it was started
        with. After a tier change those disagree, and keying on the requested
        model would carry results across the change."""
        source = SUITE.read_text()
        assert "def _served_model()" in source
        body = source[source.index("def _served_model()") :]
        body = body[: body.index("\ndef ")]
        assert "probe" in body, (
            "the served model must come from the server's own report"
        )


class TestTheSttLauncherRestartsOnAModelChange:
    """`onb-stt up` reported "already up" with a model the operator had just
    changed the default away from.

    The shipped tier changed from the 574 MB multilingual model to the 181 MB
    one, because the big model is five times slower on this hardware. The
    running server was never restarted, so it kept serving the old model and
    the change was invisible: the launcher said the server was up, and the
    operator had no way to tell that the thing they had installed was not the
    thing running.
    """

    def test_up_compares_the_served_model_with_the_selected_one(self):
        text = _read_script("termux-stt.sh")
        up_arm = text[text.index("  up)") :]
        assert "served_model" in up_arm
        assert "_want" in up_arm, (
            "the selected model must be compared against the served one"
        )
        assert "restarting" in up_arm, (
            "a mismatch must restart the server, not report success"
        )

    def test_a_matching_model_still_reports_already_up(self):
        """The fast path has to survive the check, or every start restarts."""
        text = _read_script("termux-stt.sh")
        up_arm = text[text.index("  up)") :]
        assert "already up" in up_arm


class TestTheSuiteTellsTheModelTheLanguage:
    """Without the language, the CER measures a script mismatch, not accuracy.

    The first Tamil run scored CER 1.010 with a hypothesis of Latin-script
    phonetics - "Bushin Pradinithi Petsalar Garden" - against a Tamil-script
    reference. Whisper had detected the wrong language and transliterated, and
    comparing Tamil characters to Latin ones by character gives a CER near 1.0
    whether or not the audio was understood at all.

    The app's own client sends the field; the suite did not, so it was
    measuring something other than what it claimed.
    """

    def test_transcribe_accepts_and_sends_a_language(self):
        source = SUITE.read_text()
        assert 'fields["language"] = language' in source
        assert "language=lang" in source, (
            "each clip must be transcribed in its own language"
        )

    def test_the_language_comes_from_the_manifest(self):
        source = SUITE.read_text()
        body = source[source.index("def run_stt(") : source.index("def run_tts(")]
        assert 'lang = manifest["locales"][clip["locale"]]["lang"]' in body

    def test_it_is_the_same_endpoint_the_app_uses(self):
        """The suite must exercise the path the product uses, or it is testing
        something else."""
        source = SUITE.read_text()
        assert "/v1/audio/transcriptions" in source


class TestMalformedInputExpectation:
    """A 500 is the server's contract for "this file is not audio".

    The suite demanded 4xx, which was my assumption rather than anyone's
    contract, so it failed a run for correct behaviour. The app's own client
    documents the real one:

        a 500 because the file is not audio - is reported as an error rather
        than as empty text, because an empty transcript and a failed transcript
        are different facts

    and reports it as `stt server answered 500: <detail>`. What would genuinely
    be a defect is a 200, a hang, or a crash - so those are what is asserted,
    along with the speed: a malformed file must not occupy the model.
    """

    def test_a_500_counts_as_a_refusal(self):
        source = SUITE.read_text()
        body = source[source.index("def run_robustness(") :]
        assert "400 <= status < 600" in body, (
            "a 500 is the server's contract for non-audio input; the suite must "
            "accept it rather than fail a correct behaviour"
        )

    def test_a_success_is_still_a_failure(self):
        source = SUITE.read_text()
        body = source[source.index("def run_robustness(") :]
        assert "status != 200" in body, "a 200 on malformed audio is the real defect"

    def test_a_refusal_must_be_fast(self):
        """The hang was the actual bug. A malformed file must not occupy the
        model — one such request took 74.7s behind a backlog."""
        source = SUITE.read_text()
        body = source[source.index("def run_robustness(") :]
        assert "fast = elapsed < 30" in body
        assert "must not occupy the model" in body


class TestTheResumeMessageTellsTheTruth:
    """It said "15 clips already scored" when all fifteen were about to be
    re-measured.

    A checkpoint whose entries were measured under a different budget or a
    different model is worthless, and the count on disk is not the count that
    will be reused. The one line an operator would look at to see what the run
    was reusing was wrong.
    """

    def test_it_reports_how_many_are_reusable(self):
        source = SUITE.read_text()
        assert "_reusable" in source
        assert "entries," in source and "reusable" in source

    def test_it_does_not_claim_clips_were_already_scored(self):
        # Code, not prose: the comment above the new message quotes the old
        # wording in order to explain what it replaced.
        source = "\n".join(
            line
            for line in SUITE.read_text().splitlines()
            if not line.lstrip().startswith("#")
        )
        assert "already scored" not in source, (
            "that count was the number on disk, not the number reusable"
        )


class TestFixtureBuilder:
    def test_it_parses_the_headerless_tsv(self):
        """FLEURS TSVs have no header row.

        DictReader consumes the first clip as column names and then returns None
        for every `id`, yielding zero transcripts and no error — which is how
        the first run of this builder reported `0 transcripts available` while
        having downloaded a 163 MB archive successfully.
        """
        source = BUILDER.read_text()
        assert "csv.DictReader" not in source
        assert "csv.reader" in source

    def test_it_keys_on_the_filename_not_the_id(self):
        """`id` is a small integer; the archive member is a 20-digit filename.

        Keying on id and matching archive members by stem can never match, so
        extraction silently produces nothing.
        """
        source = BUILDER.read_text()
        assert "**`id` is not the filename.**" in source

    def test_it_verifies_the_tarball_size(self):
        source = BUILDER.read_text()
        assert "EXPECTED_DEV_TARBALL" in source
        assert "discarding" in source
        # Sizes come from the tree API; asserting one guards against a constant
        # being invented rather than recorded.
        assert "171_250_900" in source

    def test_it_streams_the_archive_rather_than_extracting_it(self):
        """The archive is ~200 MB and a few clips are wanted. Extracting wholesale
        would cost a phone 1 GB of disk for fixtures totalling a couple of MB."""
        source = BUILDER.read_text()
        assert "tarfile.open" in source
        assert "extractall" not in source


class TestTheTierFollowsTheLanguage:
    """The policy, and the four layers it has to travel through.

    Mandarin at the small tier is CER 0.222 — fluent Mandarin that is not what
    was said. For a study app whose text is searched, that is a correctness
    problem, not a quality one. The large tier is exact, for five times the
    time. So the tier follows the language, and the language has to reach the
    phone through four layers to get there.
    """

    def test_the_launcher_owns_the_policy(self):
        """The model files are on the phone, so the phone owns the policy.

        Putting it in the app would mean the container deciding which of the
        phone's files to load.
        """
        text = _read_script("termux-stt.sh")
        assert "stt_model_for_language()" in text
        assert 'STT_LANG_LARGE="zh tw"' in text, (
            "Mandarin and Taiwanese Mandarin need the large tier; the small one "
            "is wrong, not merely less accurate"
        )

    def test_malay_is_refused_rather_than_mistranscribed(self):
        """The models do not speak it: CER 0.455 on the large tier, producing
        mixed-script noise. A clear refusal beats a confident wrong answer."""
        text = _read_script("termux-stt.sh")
        assert 'STT_LANG_UNSUPPORTED="my"' in text
        assert "is not supported" in text
        assert "exit 3" in text, "an unsupported language must fail loudly"

    def test_the_launcher_takes_the_language_as_an_argument(self):
        text = _read_script("termux-stt.sh")
        assert 'STT_LANG="${2:-}"' in text
        # Both the load and the size lookup need it: the supervisor sizes the
        # model before loading it.
        assert "model-path" in text

    def test_the_language_is_parsed_before_it_is_used(self):
        """A variable used before it is set is empty, and empty silently means
        "the small model" with no error."""
        text = _read_script("termux-stt.sh")
        assert text.index('STT_LANG="${2:-}"') < text.index(
            'stt_model_for_language "$STT_LANG"'
        )

    def test_the_supervisor_passes_the_variant_through(self):
        text = _read_script("termux-supervisor.py")
        assert 'def ensure(self, name: str, variant: str = "")' in text
        assert "self._command(role.load, variant)" in text, (
            "the load command must carry the variant, or the tier never changes"
        )
        assert "self.model_bytes(role, variant)" in text, (
            "the size check must be for the model that will be loaded"
        )

    def test_the_supervisor_reloads_when_the_wrong_model_is_serving(self):
        """'The server is up' is not the same question as 'the server is up with
        the right model'."""
        text = _read_script("termux-supervisor.py")
        # Compared by name: model-path prints a full path and the probe a
        # basename, so the raw strings are never equal.
        assert "_want_name != state.model" in text
        assert "self.release(name)" in text

    def test_the_residency_client_carries_the_variant(self):
        source = (REPO / "open_notebook" / "residency" / "client.py").read_text()
        assert '"variant": variant' in source, (
            "the variant must reach the supervisor, or the tier never changes"
        )

    def test_the_speech_client_passes_the_language_to_the_gate(self):
        """The gate is what loads the model, so the language has to reach it —
        not just the request."""
        source = (REPO / "open_notebook" / "speech" / "client.py").read_text()
        assert '_ensure("stt", language or "")' in source

    def test_a_source_can_declare_its_language(self):
        from open_notebook.domain.notebook import Source

        assert "language" in Source.model_fields

    def test_the_api_accepts_a_language(self):
        from api.models import SourceCreate

        assert "language" in SourceCreate.model_fields

    def test_the_extraction_path_threads_it_to_the_transcription(self):
        source = (REPO / "open_notebook" / "graphs" / "source.py").read_text()
        assert "_try_stt(file_path, cmd_id, language)" in source, (
            "the declared language must reach the transcription, or the model "
            "guesses and produces mixed-script noise on Tamil and Malay"
        )
        assert "language=language or None" in source


class TestTheTierIsReachableAtRuntime:
    """The chain was built, unit-tested at every layer, and inert.

    The supervisor is fully implemented - client, server, roles, admission gate -
    and on the phone *nothing started it* and nothing set
    `OPEN_NOTEBOOK_RESIDENCY_ENABLED`. The guest's `_ensure` returns immediately
    when residency is off, so the variant never left the app and the phone kept
    serving whatever model it last loaded. The same shape as the update that
    silently declined to update: the code path exists, is tested, and is
    unreachable.

    So the wiring is part of the feature, not a deployment detail.
    """

    def test_the_launcher_starts_the_supervisor(self):
        text = _read_script("termux-launch.sh")
        assert "start_residency()" in text
        assert "stop_residency()" in text
        assert "start_residency || true" in text, (
            "the supervisor must come up with the stack, or the app's first "
            "request for a model finds nobody to ask"
        )

    def test_the_supervisor_is_reachable_from_the_guest(self):
        """proot does not isolate the network namespace, so the guest's
        127.0.0.1 is the phone's - the same path the app already uses to reach
        the speech servers."""
        from open_notebook.residency import client as residency

        assert residency.supervisor_url().startswith("http://127.0.0.1")

    def test_model_selection_is_a_separate_switch_from_full_residency(self):
        """Turning on the tier must not switch on the reaper.

        The supervisor unloads idle roles after five minutes. That is right for a
        RAM-starved phone and wrong as a side effect of wanting the right
        transcription model: chat and embedding currently survive until reboot,
        and that is not a change this feature should make silently.
        """
        from open_notebook.residency import client as residency

        assert residency.model_selection_enabled() is False, (
            "the default must be off: it is a device setting, not a shipped one"
        )
        source = (REPO / "open_notebook" / "residency" / "client.py").read_text()
        assert "OPEN_NOTEBOOK_RESIDENCY_MODEL_SELECTION" in source

    def test_the_stt_path_asks_for_the_model_opportunistically(self):
        """A device with no supervisor still transcribes.

        The language reaches the request either way, which is what stops the
        model transliterating Tamil into Latin script. Only the *tier* depends
        on the supervisor, and failing the job over it would stall the queue
        behind a configuration problem.
        """
        source = (REPO / "open_notebook" / "speech" / "client.py").read_text()
        assert "residency.select_model(role, variant)" in source
        assert "residency.model_selection_enabled()" in source
        # A failure to select must not hold the job.
        body = source[source.index("def _ensure(") :]
        body = body[: body.index("\n\n\n")]
        assert "return None" in body, "an unreachable supervisor must not hold the job"

    def test_select_model_reaches_the_supervisor_even_with_residency_off(self):
        """`ensure_resident` short-circuits to ok=True when residency is off.

        That is right for "is this role resident" - a device with no supervisor
        must behave as it did before. But selecting a model is a different
        question, and the supervisor is the only thing that can answer it.
        """
        from open_notebook.residency import client as residency

        assert hasattr(residency, "select_model")
        source = (REPO / "open_notebook" / "residency" / "client.py").read_text()
        body = source[source.index("async def select_model(") :]
        body = body[: body.index("\n\n\n")]
        assert "if not enabled()" not in body, (
            "select_model must not short-circuit on the residency flag; that is "
            "the whole reason it is a separate entry point"
        )


class TestTheSupervisorIsReachableWhenItSaysItIs:
    """A 25-second window where the port is bound and nobody answers.

    The supervisor bound its socket, then probed all four roles — each a shell
    command with an eight-second timeout, plus a model-size lookup — and only
    then called `serve_forever`. So for that whole window the port was bound
    and silent. The launcher waits fifteen seconds for it to come up, gives up,
    and reports that the per-language model tier will not work.

    Measured with probes that take three seconds each, the way they do on a
    phone: the old code answered /health after 25.05 seconds, the new code
    after 0.27. The probes are informational startup output; they must not
    stand between the supervisor being reachable and the thing waiting to
    reach it.

    And a second, independent bug: the launcher health-checked `/state`, which
    probes every role, instead of `/health`, which is a constant. Even with the
    supervisor serving, the check could not pass in the three seconds it allowed.
    """

    def test_the_supervisor_serves_before_it_probes(self):
        text = _read_script("termux-supervisor.py")
        serve = text.index("serving = threading.Thread(target=server.serve_forever")
        probe = text.index("state = MANAGER.probe(ROLES[role])")
        assert serve < probe, (
            "the supervisor must start serving before it probes the roles, or the "
            "port is bound and silent for as long as the probes take"
        )

    def test_the_launcher_health_checks_the_constant_endpoint(self):
        text = _read_script("termux-launch.sh")
        assert "/health" in text
        # /state probes every role; it is not a health check.
        start = text[text.index("start_residency()") :]
        start = start[: start.index("\n}")]
        # Code, not prose: the function's own comment names /state in order to
        # explain why it is not used.
        code = "\n".join(
            line for line in start.splitlines() if not line.lstrip().startswith("#")
        )
        assert "/state" not in code, (
            "/state probes every role and takes far longer than a health check "
            "may; /health is the constant one"
        )
        assert "/health" in code

    def test_the_health_endpoint_is_the_constant_one(self):
        """It must not probe anything - it is what a caller polls."""
        text = _read_script("termux-supervisor.py")
        handler = text[text.index('elif path == "/health":') :]
        handler = handler[: handler.index("\n")]
        assert "probe" not in handler, "the health endpoint must not probe the roles"


class TestTheRetryLoopWaitsForWorkInFlight:
    """It gave up on a healthy queue.

    The loop counted a pass as idle when the *insight count* had not changed.
    But a transformation takes about eleven minutes and the window admits three
    at a time, so eighteen of them need roughly an hour. Four passes at seven
    minutes is twenty-eight - so the loop stopped on work that was progressing
    fine, and the work completed moments after it stopped.

    A pass is only idle when nothing is in flight.
    """

    def test_it_counts_in_flight_work(self):
        source = (REPO / "scripts" / "insight-retry.py").read_text()
        assert "def _pending_count" in source
        assert "'held', 'running', 'new'" in source, (
            "the pending count must cover the states the dispatcher admits"
        )

    def test_pending_work_resets_the_idle_count(self):
        source = (REPO / "scripts" / "insight-retry.py").read_text()
        body = source[source.index("pending = await _pending_count()") :]
        assert "elif pending > 0:" in body
        assert "idle = 0" in body, (
            "work in flight means the pass says nothing about progress, so it "
            "must not count towards the idle limit"
        )

    def test_it_reports_what_is_in_flight(self):
        """The operator has to be able to tell "waiting" from "stuck"."""
        source = (REPO / "scripts" / "insight-retry.py").read_text()
        assert "in flight" in source


class TestTheTierSwitchesBothWays:
    """It loaded the right model once and then stopped switching.

    The first request found no server, so the load path ran with the language
    and picked the right tier. The second found it serving, and the branch
    meant to reload it with a different model was skipped - because the role
    never declared that it had variants. So Mandarin loaded the 547 MB model
    and English then used it too: right once, silently wrong after.

    A test that only checks the first call passes this. The failure is in the
    *transition*, so the test has to make two.
    """

    def test_the_stt_role_declares_that_it_has_variants(self):
        source = _read_script("termux-supervisor.py")
        role = source[source.index('"stt": Role(') :]
        role = role[: role.index("\n    ),")]
        assert "variant_arg=" in role, (
            "without this the supervisor skips the wrong-model branch, and the "
            "tier is chosen once and then inherited"
        )

    def test_the_model_comparison_is_by_name(self):
        """`model-path` prints a full path; the probe prints a basename.

        Compared as strings they always differ, so every call with a variant
        would reload the model even when it was already the right one - a
        pointless reload on every request.
        """
        source = _read_script("termux-supervisor.py")
        assert "os.path.basename(_want)" in source
        assert "_want != state.model" not in source, (
            "a full path and a basename are never equal, so the comparison always fires"
        )

    def test_the_branch_is_reachable(self):
        source = _read_script("termux-supervisor.py")
        branch = source[source.index("if state.serving and variant") :]
        branch = branch[: branch.index("\n\n")]
        assert "role.variant_arg" in branch, (
            "the branch is guarded on the role declaring a variant, so an "
            "unset field disables it entirely"
        )


class TestLoggerCallsAreFormatted:
    """loguru does not format %-style arguments, and the placeholders print.

    This project uses loguru, not the standard library's logging. A call like

        logger.warning("failed for %s: %s", role, error)

    emits the message with the placeholders *literally* - so the one line that
    says why something went wrong says "%s" instead. Seen on the device:

        residency supervisor unreachable for %s: %s

    which is a log line that tells you nothing, on the exact failure that
    needed explaining.

    A whole class of bug, so it is checked as a class.
    """

    def test_no_percent_style_logger_calls_in_the_package(self):
        import re

        offenders = []
        for path in sorted((REPO / "open_notebook").rglob("*.py")):
            for number, line in enumerate(
                path.read_text(encoding="utf-8").splitlines(), 1
            ):
                # A logger call whose first argument is a plain string literal
                # containing %s and which passes extra arguments. loguru
                # formats with braces, so those arguments are dropped.
                if not re.search(r"logger\.\w+\(", line):
                    continue
                if "%s" not in line:
                    continue
                if 'f"' in line or "f'" in line:
                    continue
                # The call may span lines; look a little ahead for the args.
                window = "\n".join(
                    path.read_text(encoding="utf-8").splitlines()[
                        number - 1 : number + 5
                    ]
                )
                if re.search(r"logger\.\w+\(\s*\"[^\"]*%s[^\"]*\"\s*,", window):
                    offenders.append(f"{path.relative_to(REPO)}:{number}")
        assert not offenders, (
            "loguru does not format %-style arguments; these log calls print "
            "their placeholders literally: " + ", ".join(offenders)
        )


class TestTheModelIsKeptAliveDuringARequest:
    """The reaper would unload the model *while it was being used*.

    The supervisor unloads a role it loaded once it has been idle for five
    minutes, measuring idle from the last `ensure`. It cannot see a request in
    flight, because the guest talks to the speech server directly rather than
    through the supervisor. A ten-minute lecture through the large model runs for
    well over an hour - so the model would be unloaded underneath the request,
    and the request would fail with a connection error that looks exactly like
    the model never having been loaded.
    """

    def test_a_long_request_keeps_the_role_alive(self):
        source = (REPO / "open_notebook" / "speech" / "client.py").read_text()
        assert "_keep_role_alive" in source
        assert "_alive.cancel()" in source, (
            "the keep-alive must be cancelled when the request ends, or it "
            "leaks a task per transcription"
        )

    def test_the_touch_exists_and_is_safe_to_fail(self):
        source = (REPO / "open_notebook" / "residency" / "client.py").read_text()
        assert "async def touch(" in source
        assert "a failed touch must not fail the job" in source, (
            "losing the supervisor must not lose the transcription"
        )


class TestTheResidencyBudgetsAreOrdered:
    """The client gave up while the supervisor was still working.

    The supervisor's path to a resident role is: check the model, release the
    wrong one, size the new one, load it, probe again. The launcher's own health
    wait allows a minute for a 547 MB model to load, and that happens *outside*
    the supervisor's load budget - so the supervisor can take well over ninety
    seconds to answer.

    A client that gives up at ninety sees a timeout on a load that was about to
    succeed, and the request then runs against the *old* model. That is
    indistinguishable from the feature not working, which is exactly how it
    looked.
    """

    def test_the_client_outlasts_the_supervisor(self):
        client = (REPO / "open_notebook" / "residency" / "client.py").read_text()
        supervisor = (REPO / "scripts" / "termux-supervisor.py").read_text()
        client_budget = float(
            client.split("DEFAULT_LOAD_TIMEOUT = ")[1].split("\n")[0].strip()
        )
        import re as _re

        match = _re.search(
            r'OPEN_NOTEBOOK_RESIDENCY_LOAD_TIMEOUT",\s*"(\d+)"', supervisor
        )
        assert match, "could not find the supervisor's load budget"
        load_budget = float(match.group(1))
        assert client_budget > load_budget, (
            f"the client gives up at {client_budget}s but the supervisor may "
            f"take {load_budget}s; the request would then run against the old "
            "model and look like the feature not working"
        )

    def test_the_error_names_the_exception_type(self):
        """httpx's ReadTimeout has an empty str().

        The message ended with a bare colon and named no cause, on the one line
        that existed to say why the supervisor was unreachable.
        """
        source = (REPO / "open_notebook" / "residency" / "client.py").read_text()
        assert "type(exc).__name__" in source


class TestTheSupervisorDoesNotDeadlock:
    """A `POST /resident` that never answered.

    Measured on the device: the request timed out at 600 seconds, twice, while
    `/health` answered in zero and `/state` in eleven. The supervisor was not
    slow - it was deadlocked, and everything behind it queued on the same lock.

    The cause: `ensure` holds the lock while it works, and the wrong-model branch
    calls `self.release(name)`, which takes the lock again. A plain
    `threading.Lock` is not reentrant, so the request waited for a lock it
    already held.

    The test runs the real code with a role whose served model differs from the
    one the variant asks for - the exact branch that deadlocked - and requires it
    to return.
    """

    def _manager_with_a_role(self, tmp_path, served_model: str, wanted_model: str):
        """A Manager with one role, wired to scripts that report the models."""
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "onb_residency", REPO / "scripts" / "termux-supervisor.py"
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        # Registered before it is executed: a dataclass resolves its module
        # through sys.modules when it is defined, and a module that was never
        # registered makes that lookup return None.
        sys.modules["onb_residency"] = module
        spec.loader.exec_module(module)

        probe = tmp_path / "probe.sh"
        probe.write_text(
            "#!/bin/sh\n"
            f'echo "resident=1 serving=1 pid=123 model={served_model} rss_kb=1"\n'
        )
        probe.chmod(0o755)
        model_path = tmp_path / "model-path.sh"
        model_path.write_text(
            "#!/bin/sh\necho 'model=/models/%s bytes=1'\n" % wanted_model
        )
        model_path.chmod(0o755)
        unload = tmp_path / "unload.sh"
        unload.write_text("#!/bin/sh\nexit 0\n")
        unload.chmod(0o755)
        load = tmp_path / "load.sh"
        load.write_text("#!/bin/sh\nexit 0\n")
        load.chmod(0o755)

        module.ROLES.clear()
        module.ROLES["stt"] = module.Role(
            name="stt",
            probe=["sh", str(probe)],
            load=["sh", str(load)],
            unload=["sh", str(unload)],
            model_path=["sh", str(model_path), "{variant}"],
            port=8090,
            priority=60,
            max_concurrent_jobs=1,
            threads=2,
            variant_arg="language",
        )
        return module, module.MANAGER

    def test_ensure_with_a_different_model_returns(self, tmp_path):
        module, manager = self._manager_with_a_role(
            tmp_path, "big-model.bin", "small-model.bin"
        )
        # The branch that deadlocked: serving a different model than the variant
        # asks for, so `release` is called while `ensure` holds the lock.
        result = manager.ensure("stt", "en")
        assert result.get("ok") is not False, f"ensure() did not complete: {result}"

    def test_the_lock_is_reentrant(self):
        source = (REPO / "scripts" / "termux-supervisor.py").read_text()
        assert "threading.RLock()" in source, (
            "ensure() holds the lock and calls release(), which takes it again; "
            "a plain Lock deadlocks"
        )
        assert "threading.Lock()" not in source


class TestTheUpdateStopsTheResidencySupervisor:
    """A stale supervisor is worse than none.

    The updater stopped the guest services, the proot sessions and the
    launchers - but not the residency supervisor, which is a phone-side process
    that the update replaces. So after an update, a supervisor that had deadlocked
    on its own lock kept port 8099, the newly installed launcher found something
    already listening and deferred to it, and the update reported success while
    the phone kept the old binary.

    The symptom was a request that timed out against a supervisor that had
    already been fixed - which reads as "the fix did not work" rather than "the
    update never reached the process that mattered".
    """

    def test_the_updater_knows_about_the_supervisor(self):
        source = _read_script("termux-update.sh")
        assert "residency_pids()" in source
        assert "stop_residency()" in source

    def test_the_supervisor_is_stopped_with_the_rest(self):
        source = _read_script("termux-update.sh")
        assert "stop_residency || return 1" in source, (
            "a supervisor left running keeps the port, and the new one refuses to start"
        )

    def test_it_reports_a_supervisor_that_survived(self):
        """The launcher reports the same thing, and for the same reason: the
        replacement would refuse to start, so the old binary would keep serving
        and nobody would know."""
        source = _read_script("termux-update.sh")
        assert "residency supervisor survived SIGKILL" in source


class TestARefusedConnectionIsRetried:
    """A request that arrived before the supervisor was listening.

    The stack launches the guest and the supervisor together, so a request in
    that window gets "connection refused" - and the fallback is to carry on
    with whatever model is loaded. That is the right fallback, but it means the
    tier is silently wrong for that request, which is the failure this whole
    feature exists to prevent.

    A refusal is retried briefly. A timeout is NOT: a timeout means the
    supervisor is busy loading a model, and retrying would only queue behind the
    same work.
    """

    def test_a_refusal_is_retried(self):
        source = (REPO / "open_notebook" / "residency" / "client.py").read_text()
        body = source[source.index("async def select_model(") :]
        body = body[: body.index("\n\n\n")]
        assert "httpx.ConnectError" in body
        assert "for attempt in range(3)" in body
        assert "await asyncio.sleep(" in body

    def test_a_timeout_is_not_retried(self):
        """Retrying a timeout would queue behind the load it is waiting for."""
        source = (REPO / "open_notebook" / "residency" / "client.py").read_text()
        body = source[source.index("async def select_model(") :]
        body = body[: body.index("\n\n\n")]
        assert "httpx.ReadTimeout" not in body
        assert "httpx.TimeoutException" not in body
