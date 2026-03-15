"""
============================================================
retry.py — Retry with Exponential Back-off
============================================================
Provides a reusable retry wrapper for API calls that may
fail due to rate limits (HTTP 429), transient network
errors, or server errors (5xx).

Uses exponential back-off with jitter to avoid thundering
herd problems when multiple threads retry simultaneously.
============================================================
"""

import logging
import random
import time
from typing import Callable, Tuple, Type, TypeVar

logger = logging.getLogger("whatsapp_sorter")

T = TypeVar("T")

# Exceptions that are safe to retry
RETRYABLE_EXCEPTIONS: Tuple[Type[BaseException], ...] = (
    ConnectionError,
    TimeoutError,
    OSError,
)

# Try to import Google-specific exceptions for 429/5xx handling
try:
    from google.api_core.exceptions import (
        ResourceExhausted,  # 429
        ServiceUnavailable,  # 503
        DeadlineExceeded,  # 504
        InternalServerError,  # 500
    )
    RETRYABLE_EXCEPTIONS = RETRYABLE_EXCEPTIONS + (
        ResourceExhausted,
        ServiceUnavailable,
        DeadlineExceeded,
        InternalServerError,
    )
except ImportError:
    pass


def retry_with_backoff(
    fn: Callable[[], T],
    max_retries: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 60.0,
    description: str = "API call",
) -> T:
    """
    Execute `fn()` with exponential back-off on retryable errors.

    On each retry, the delay doubles with random jitter:
        delay = min(base_delay * 2^attempt + jitter, max_delay)

    Args:
        fn: A zero-argument callable to execute.
        max_retries: Maximum number of retry attempts (0 = no retries).
        base_delay: Initial delay in seconds before the first retry.
        max_delay: Maximum delay cap in seconds.
        description: Human-readable label for log messages.

    Returns:
        The return value of `fn()` on success.

    Raises:
        The last exception if all retries are exhausted, or
        immediately if the exception is not retryable.
    """
    last_exception = None

    for attempt in range(max_retries + 1):
        try:
            return fn()
        except RETRYABLE_EXCEPTIONS as exc:
            last_exception = exc

            if attempt == max_retries:
                logger.error(
                    "%s failed after %d attempts: %s",
                    description, max_retries + 1, exc,
                )
                raise

            # Exponential back-off with jitter
            delay = min(base_delay * (2 ** attempt) + random.uniform(0, 1), max_delay)
            logger.warning(
                "%s failed (attempt %d/%d): %s — retrying in %.1fs",
                description, attempt + 1, max_retries + 1, exc, delay,
            )
            time.sleep(delay)

    # Should never reach here, but satisfy type checker
    raise last_exception  # type: ignore
