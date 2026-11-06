"""Unit tests for the DeepSeek-web direct service.

No network: the SSE reassembly, the error envelope mapping and the cookie
parsing are all pure and are exercised against stubs.
"""

from open_notebook.services.deepseek_direct_service import DeepSeekDirectService


class _StubResponse:
    """Minimal stand-in for requests.Response.iter_lines()."""

    def __init__(self, lines: list[str]) -> None:
        self._lines = lines

    def iter_lines(self, decode_unicode: bool = False):
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
