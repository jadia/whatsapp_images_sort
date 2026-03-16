"""
============================================================
test_retry.py — Retry Logic Tests
============================================================
Tests retry_with_backoff for exponential back-off, jitter,
max retries, and non-retryable exception propagation.
============================================================
"""

import time
from unittest.mock import MagicMock, patch

import pytest

from src.utils.retry import retry_with_backoff


class TestRetrySuccess:
    """Tests for successful retry scenarios."""

    def test_succeeds_on_first_attempt(self):
        """Should return immediately if fn succeeds."""
        fn = MagicMock(return_value="ok")
        result = retry_with_backoff(fn, max_retries=3)
        assert result == "ok"
        fn.assert_called_once()

    def test_succeeds_on_second_attempt(self):
        """Should retry once and succeed on the second call."""
        fn = MagicMock(side_effect=[ConnectionError("fail"), "ok"])

        with patch("src.utils.retry.time.sleep"):
            result = retry_with_backoff(fn, max_retries=3, base_delay=0.01)

        assert result == "ok"
        assert fn.call_count == 2

    def test_succeeds_on_last_attempt(self):
        """Should succeed on the very last allowed attempt."""
        fn = MagicMock(side_effect=[
            ConnectionError("1"),
            ConnectionError("2"),
            ConnectionError("3"),
            "finally",
        ])

        with patch("src.utils.retry.time.sleep"):
            result = retry_with_backoff(fn, max_retries=3, base_delay=0.01)

        assert result == "finally"
        assert fn.call_count == 4


class TestRetryExhausted:
    """Tests for exhausted retry scenarios."""

    def test_raises_after_max_retries(self):
        """Should raise the last exception after all retries are exhausted."""
        fn = MagicMock(side_effect=ConnectionError("persistent failure"))

        with patch("src.utils.retry.time.sleep"):
            with pytest.raises(ConnectionError, match="persistent failure"):
                retry_with_backoff(fn, max_retries=2, base_delay=0.01)

        assert fn.call_count == 3  # 1 initial + 2 retries

    def test_zero_retries_raises_immediately(self):
        """With max_retries=0, should raise on the first failure."""
        fn = MagicMock(side_effect=ConnectionError("immediate"))

        with pytest.raises(ConnectionError, match="immediate"):
            retry_with_backoff(fn, max_retries=0)

        fn.assert_called_once()


class TestRetryNonRetryable:
    """Tests for non-retryable exceptions."""

    def test_value_error_raises_immediately(self):
        """ValueError is not retryable and should propagate immediately."""
        fn = MagicMock(side_effect=ValueError("bad input"))

        with pytest.raises(ValueError, match="bad input"):
            retry_with_backoff(fn, max_retries=3)

        fn.assert_called_once()

    def test_type_error_raises_immediately(self):
        """TypeError is not retryable and should propagate immediately."""
        fn = MagicMock(side_effect=TypeError("wrong type"))

        with pytest.raises(TypeError, match="wrong type"):
            retry_with_backoff(fn, max_retries=3)

        fn.assert_called_once()

    def test_keyboard_interrupt_raises_immediately(self):
        """KeyboardInterrupt should not be caught by retry logic."""
        fn = MagicMock(side_effect=KeyboardInterrupt())

        with pytest.raises(KeyboardInterrupt):
            retry_with_backoff(fn, max_retries=3)

        fn.assert_called_once()


class TestRetryBackoff:
    """Tests for exponential back-off timing."""

    def test_exponential_delay_pattern(self):
        """Sleep times should follow exponential back-off pattern."""
        fn = MagicMock(side_effect=[
            ConnectionError("1"),
            ConnectionError("2"),
            ConnectionError("3"),
            "ok",
        ])

        sleep_calls = []

        def mock_sleep(duration):
            sleep_calls.append(duration)

        with patch("src.utils.retry.time.sleep", side_effect=mock_sleep):
            with patch("src.utils.retry.random.uniform", return_value=0):  # Remove jitter
                retry_with_backoff(fn, max_retries=3, base_delay=1.0)

        # Expected: 1*2^0=1, 1*2^1=2, 1*2^2=4
        assert len(sleep_calls) == 3
        assert sleep_calls[0] == 1.0
        assert sleep_calls[1] == 2.0
        assert sleep_calls[2] == 4.0

    def test_max_delay_cap(self):
        """Sleep should never exceed max_delay."""
        fn = MagicMock(side_effect=[
            ConnectionError("1"),
            ConnectionError("2"),
            "ok",
        ])

        sleep_calls = []

        def mock_sleep(duration):
            sleep_calls.append(duration)

        with patch("src.utils.retry.time.sleep", side_effect=mock_sleep):
            with patch("src.utils.retry.random.uniform", return_value=0):
                retry_with_backoff(fn, max_retries=2, base_delay=10.0, max_delay=15.0)

        # 10*2^0=10, 10*2^1=20 but capped at 15
        assert sleep_calls[0] == 10.0
        assert sleep_calls[1] == 15.0

    def test_jitter_adds_randomness(self):
        """Jitter should add a random component to the delay."""
        fn = MagicMock(side_effect=[ConnectionError("1"), "ok"])

        sleep_calls = []

        def mock_sleep(duration):
            sleep_calls.append(duration)

        with patch("src.utils.retry.time.sleep", side_effect=mock_sleep):
            with patch("src.utils.retry.random.uniform", return_value=0.5):
                retry_with_backoff(fn, max_retries=1, base_delay=1.0)

        # 1*2^0 + 0.5 = 1.5
        assert sleep_calls[0] == 1.5


class TestRetryDescription:
    """Tests for logging description."""

    def test_description_used_in_logs(self):
        """The description parameter should be passed through for logging."""
        fn = MagicMock(side_effect=[ConnectionError("fail"), "ok"])

        with patch("src.utils.retry.time.sleep"):
            result = retry_with_backoff(
                fn, max_retries=1, base_delay=0.01,
                description="Test operation",
            )

        assert result == "ok"
