"""The embed launcher must not let a stale server pass as the expected one.

`llama-server`'s `/v1/embeddings` **ignores the `model` field and echoes it
back**. Asking for `"model": "jina"` against a server running bge-small-en
returns bge-small-en's 384-dim vectors labelled `jina`. Measured on the phone:

    served: bge-small-en-v1.5-q8_0.gguf
    request: {"model": "jina", "input": "hello"}
    response: dims 384, model echoed back "jina"

So a stale embedder survives a model change silently. The app embeds at the
served model's dimensions while the stored index holds another model's, every
search returns nothing, and nothing is logged anywhere. The registry on the
phone pointed at a 1024-dim model while its index held 196 vectors at 384 - a
state that is indistinguishable from "no search results yet" unless you go and
count the dimensions.

The `already up` branch is where this bites: a server left from a previous
launch keeps its own model, and reporting "already up" without looking is
exactly how that survives.
"""

from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "termux-embed.sh"


def _read() -> str:
    return SCRIPT.read_text()


def _code_of(header: str) -> str:
    """Function body with comment lines removed.

    The comments here quote the code they replaced, so asserting on raw text
    fails on the explanation of the fix rather than the fix.
    """
    text = _read()
    start = text.index(header)
    body = text[start : text.index("\n  }\n", start)]
    return "\n".join(
        line for line in body.splitlines() if not line.lstrip().startswith("#")
    )


class TestServedModelIsTheTruth:
    def test_asks_v1_models_not_the_request_field(self):
        """/v1/embeddings echoes back whatever `model` you send, so asking it
        is asking a question that always agrees with itself."""
        body = _code_of("  served_model() {")
        assert "/v1/models" in body
        assert "embeddings" not in body, (
            "the /v1/embeddings endpoint echoes the requested model name "
            "instead of reporting what is loaded"
        )

    def test_reduces_to_a_basename(self):
        """The registry stores a full path; the server reports a full path too,
        and only the filenames should be compared."""
        body = _code_of("  served_model() {")
        assert "sed 's|.*/||'" in body or "basename" in body


class TestMismatchIsFatal:
    def test_compares_against_the_expected_model(self):
        body = _code_of("  require_expected_model() {")
        assert "basename" in body
        assert "!=" in body

    def test_returns_failure_on_mismatch(self):
        body = _code_of("  require_expected_model() {")
        assert "return 1" in body

    def test_does_not_invent_a_failure_when_it_cannot_tell(self):
        """No response is not the same as a wrong response. Refusing to start
        because /v1/models was briefly unreachable would be its own outage."""
        body = _code_of("  require_expected_model() {")
        assert '[ -n "$_actual" ] || return 0' in body

    def test_says_what_to_do(self):
        """A loud failure with no remedy just gets the stack turned off."""
        body = _code_of("  require_expected_model() {")
        assert "onb-embed down" in body


class TestBothPathsAreChecked:
    def test_the_already_up_path_checks(self):
        """The path that actually leaks a stale model.

        `up` used to print "already up" and exit 0, having looked at nothing
        but whether a process existed.
        """
        text = _read()
        i = text.index("already up")
        window = text[i : i + 400]
        assert "require_expected_model" in window, (
            "reporting 'already up' without verifying the model is how a "
            "stale embedder survives a model change"
        )

    def test_the_fresh_start_path_checks(self):
        """A port held by something else answers /health happily and
        /v1/models with its own model."""
        text = _read()
        i = text.index("if wait_healthy; then")
        window = text[i : i + 1400]
        assert "require_expected_model" in window


class TestDimensionProbe:
    def test_reports_the_dimension(self):
        """This is the number that has to agree with the stored
        `source_embedding` vectors, and printing it is the only way an
        operator sees the two side by side."""
        text = _read()
        assert "dims:" in text

    def test_counts_every_element(self):
        """Off-by-one, and it is silent.

        Counting lines that start with `-` misses the first element, which
        begins `[-`. Measured against a real 384-dim response: 187, not 384.
        """
        text = _read()
        i = text.index("dimension probe")
        window = text[i : i + 600]
        assert "sed 's/^\\[" in window or "s/^\\\\[" in window, (
            "the opening bracket must be stripped before counting, or the "
            "first element is missed"
        )
        assert "grep -c '^-'" not in window, (
            "the first embedding element starts with `[-`, so matching a "
            "leading `-` undercounts by one"
        )

    def test_can_be_skipped(self):
        """One probe is ~0.06s on bge-small but ~25s on bge-m3, so an operator
        debugging a slow embedder needs to turn it off."""
        assert "EMBED_PROBE" in _read()


class TestProbesAreOptional:
    def test_no_jq_dependency(self):
        """Termux does not assume jq is installed; two of these deployments
        were already lost to unverified assumptions about the phone."""
        body = _code_of("  served_model() {")
        assert "jq" not in body
