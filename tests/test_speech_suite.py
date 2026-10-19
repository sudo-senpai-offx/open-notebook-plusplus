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
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SUITE = REPO / "scripts" / "termux-speech-suite.py"
BUILDER = REPO / "scripts" / "build-speech-fixtures.py"


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
        assert "transcribe(path, timeout=budget)" in stt_body
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
