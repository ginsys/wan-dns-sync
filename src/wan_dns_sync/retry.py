"""Retry decorator with exponential backoff for API calls."""

import random
import time
from collections.abc import Callable
from functools import wraps
from typing import ParamSpec, TypeVar

import structlog

logger = structlog.get_logger()

P = ParamSpec("P")
T = TypeVar("T")


def retry_with_backoff(
    max_retries: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 60.0,
    retryable_exceptions: tuple[type[Exception], ...] = (),
    should_retry: Callable[[Exception], bool] | None = None,
) -> Callable[[Callable[P, T]], Callable[P, T]]:
    """Decorator for retrying API calls with exponential backoff.

    Args:
        max_retries: Maximum number of retry attempts
        base_delay: Initial delay in seconds
        max_delay: Maximum delay in seconds
        retryable_exceptions: Tuple of exception types to retry on

    Returns:
        Decorated function with retry logic
    """

    def decorator(func: Callable[P, T]) -> Callable[P, T]:
        @wraps(func)
        def wrapper(*args: P.args, **kwargs: P.kwargs) -> T:
            last_exception: Exception | None = None

            for attempt in range(max_retries + 1):
                try:
                    return func(*args, **kwargs)
                except retryable_exceptions as e:
                    last_exception = e

                    if should_retry is not None and not should_retry(e):
                        raise

                    if attempt < max_retries:
                        # Calculate delay with exponential backoff and jitter
                        delay = min(base_delay * (2**attempt), max_delay)
                        delay *= 0.5 + random.random()  # Add jitter

                        logger.warning(
                            "Request failed, retrying",
                            attempt=attempt + 1,
                            max_retries=max_retries,
                            delay=round(delay, 2),
                            error=str(e),
                        )
                        time.sleep(delay)

            # All retries exhausted
            if last_exception is not None:
                raise last_exception
            raise RuntimeError("Unexpected state: no exception but all retries failed")

        return wrapper

    return decorator
