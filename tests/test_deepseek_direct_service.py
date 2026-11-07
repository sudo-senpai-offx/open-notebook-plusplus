"""Unit tests for the DeepSeek-web direct service.

No network: the SSE reassembly, the error envelope mapping and the cookie
parsing are all pure and are exercised against stubs.
"""

from typing import Any, Iterator

import requests

from open_notebook.services.deepseek_direct_service import DeepSeekDirectService


class _StubResponse(requests.Response):
    """Minimal stand-in for requests.Response.iter_lines().

    Subclasses the real type so the production annotation on ``_read_sse``
    keeps describing the contract it is called with; only the line source
    is replaced.
    """

    def __init__(self, lines: list[str]) -> None:
        self._lines = lines

    def iter_lines(
        self,
        chunk_size: int | None = 512,
        decode_unicode: bool = False,
        delimiter: str | bytes | None = None,
    ) -> Iterator[Any]:
        return iter(self._lines)


def test_read_sse_snapshot_only() -> None:
    lines = [
        "event: ready",
        'data: {"request_message_id":1,"response_message_id":2}',
        "",
        'data: {"v":{"response":{"fragments":[{"id":2,"type":"RESPONSE","content":"OK"}]}}}',
        "",
        'data: {"p":"response/status","o":"SET","v":"FINISHED"}',
    ]
    assert DeepSeekDirectService._read_sse(_StubResponse(lines)) == "OK"


def test_read_sse_appends_streaming_patches() -> None:
    lines = [
        'data: {"v":{"response":{"fragments":[{"id":2,"type":"RESPONSE","content":"Hel"}]}}}',
        'data: {"p":"response/fragments/-1/content","o":"APPEND","v":"lo"}',
        'data: {"p":"response/fragments/-1/content","o":"APPEND","v":" world"}',
    ]
    assert DeepSeekDirectService._read_sse(_StubResponse(lines)) == "Hello world"


def test_read_sse_appends_p_less_continuations() -> None:
    """DeepSeek drops ``p`` on continuations; they belong to the last fragment.

    Captured from a live stream: a snapshot, one explicit APPEND, then
    ``{"v": " three"}`` and friends with no path at all. Ignoring those
    truncated the answer to "one two".
    """
    lines = [
        'data: {"v":{"response":{"fragments":['
        '{"id":2,"type":"RESPONSE","content":"one"}]}}}',
        'data: {"p":"response/fragments/-1/content","o":"APPEND","v":" two"}',
        'data: {"v":" three"}',
        'data: {"v":" four"}',
        'data: {"v":" five"}',
        'data: {"p":"response","o":"BATCH","v":['
        '{"p":"accumulated_token_usage","v":47},'
        '{"p":"quasi_status","v":"FINISHED"}]}',
        'data: {"p":"response/status","o":"SET","v":"FINISHED"}',
    ]
    assert (
        DeepSeekDirectService._read_sse(_StubResponse(lines))
        == "one two three four five"
    )


def test_read_sse_p_less_continuation_targets_snapshot_fragment() -> None:
    """No explicit patch first: the snapshot's fragment is the target."""
    lines = [
        'data: {"v":{"response":{"fragments":['
        '{"id":2,"type":"RESPONSE","content":"Hel"}]}}}',
        'data: {"v":"lo"}',
    ]
    assert DeepSeekDirectService._read_sse(_StubResponse(lines)) == "Hello"


def test_read_sse_p_less_continuation_without_fragment_is_ignored() -> None:
    """Nothing has been written yet, so there is no target to append to."""
    lines = [
        'data: {"v":"orphan"}',
        'data: {"p":"response/status","o":"SET","v":"FINISHED"}',
    ]
    assert DeepSeekDirectService._read_sse(_StubResponse(lines)) == ""


def test_read_sse_non_fragment_patch_does_not_repoint_continuation() -> None:
    """A BATCH on ``response`` must not capture the following continuation."""
    lines = [
        'data: {"v":{"response":{"fragments":['
        '{"id":2,"type":"RESPONSE","content":"a"}]}}}',
        'data: {"p":"response/accumulated_token_usage","o":"SET","v":12}',
        'data: {"v":"b"}',
    ]
    assert DeepSeekDirectService._read_sse(_StubResponse(lines)) == "ab"


def test_read_sse_skips_thinking_fragments() -> None:
    lines = [
        'data: {"v":{"response":{"fragments":['
        '{"id":1,"type":"THINK","content":"secret reasoning"},'
        '{"id":2,"type":"RESPONSE","content":"answer"}]}}}',
    ]
    assert DeepSeekDirectService._read_sse(_StubResponse(lines)) == "answer"


def test_read_sse_untyped_fragments_are_kept() -> None:
    lines = [
        'data: {"v":{"response":{"fragments":[{"id":1,"content":"plain"}]}}}',
    ]
    assert DeepSeekDirectService._read_sse(_StubResponse(lines)) == "plain"


def test_describe_json_error_reads_code_and_biz_msg() -> None:
    body = '{"code":40301,"msg":"INVALID_POW_RESPONSE","data":null}'
    message = DeepSeekDirectService._describe_json_error(body)
    assert "40301" in message
    assert "INVALID_POW_RESPONSE" in message


def test_describe_json_error_falls_back_on_plain_text() -> None:
    assert (
        "unexpected" in DeepSeekDirectService._describe_json_error("not json").lower()
    )


def test_cookie_string_is_parsed_into_jar() -> None:
    service = DeepSeekDirectService(
        ds_session_id="ds_session_id=abc123; aws-waf-token=token-value",
        authorization_token="Bearer t",
    )
    session = service._get_session()
    assert session.cookies.get("ds_session_id") == "abc123"
    assert session.cookies.get("aws-waf-token") == "token-value"


def test_bare_session_id_is_set_as_cookie() -> None:
    service = DeepSeekDirectService(
        ds_session_id="abc123",
        authorization_token="Bearer t",
    )
    session = service._get_session()
    assert session.cookies.get("ds_session_id") == "abc123"
