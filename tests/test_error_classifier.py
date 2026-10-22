"""A timeout is not a broken connection, and the message must say so.

Both were classified as "Could not connect to the AI provider. Please check
your network connection and provider URL." A provider that is asked for
something too big, on a device too slow, or while too busy, times out - and
the reader is then sent to look at the network, which is working perfectly.

It cost hours of investigation: the provider was answering every request, and
the failure said the network was down. The message is the part of the
software the reader sees, so it has to point at the actual fault.
"""

from __future__ import annotations

import httpx

from open_notebook.exceptions import NetworkError
from open_notebook.utils.error_classifier import classify_error


class TestTimeoutsAreNotConnectionFailures:
    def test_a_read_timeout_is_reported_as_a_timeout(self):
        """The provider was reached. It just did not answer in time."""
        error, message = classify_error(httpx.ReadTimeout("timed out"))
        assert error is NetworkError
        assert "did not answer in time" in message
        assert "network connection" not in message, (
            "the message points at the network, which is not what failed"
        )

    def test_a_connect_timeout_is_reported_as_a_timeout(self):
        error, message = classify_error(httpx.ConnectTimeout("timed out"))
        assert error is NetworkError
        assert "did not answer in time" in message

    def test_a_plain_timeout_is_reported_as_a_timeout(self):
        error, message = classify_error(TimeoutError("timeout"))
        assert error is NetworkError
        assert "did not answer in time" in message

    def test_a_refused_connection_still_says_the_network(self):
        """The real connection failure keeps its own message."""
        error, message = classify_error(httpx.ConnectError("connection refused"))
        assert error is NetworkError
        assert "Could not connect to the AI provider" in message

    def test_a_connect_error_still_says_the_network(self):
        error, message = classify_error(httpx.ConnectError("connect error"))
        assert error is NetworkError
        assert "Could not connect to the AI provider" in message
