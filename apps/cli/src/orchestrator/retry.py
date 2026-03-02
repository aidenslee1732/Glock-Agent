"""Retry Logic for Model B.

Provides exponential backoff retry for transient failures:
- LLM request timeouts
- Rate limiting (429)
- Temporary network errors
- Server errors (5xx)

This ensures resilience without manual intervention.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Coroutine, Optional, TypeVar, Union

logger = logging.getLogger(__name__)


class RetryableErrorType(str, Enum):
    """Types of retryable errors."""
    TIMEOUT = "timeout"
    RATE_LIMIT = "rate_limit"
    CONNECTION = "connection"
    SERVER_ERROR = "server_error"
    TRANSIENT = "transient"


@dataclass
class RetryConfig:
    """Configuration for retry behavior."""
    # Maximum number of retry attempts
    max_retries: int = 3

    # Initial delay between retries (seconds)
    initial_delay: float = 1.0

    # Maximum delay between retries (seconds)
    max_delay: float = 30.0

    # Exponential backoff base
    exponential_base: float = 2.0

    # Add jitter to prevent thundering herd
    jitter: bool = True

    # Maximum jitter as fraction of delay (0.0-1.0)
    jitter_factor: float = 0.25

    # Error types/codes that should trigger retry
    retryable_errors: set[str] = field(default_factory=lambda: {
        "timeout",
        "TimeoutError",
        "asyncio.TimeoutError",
        "rate_limit",
        "rate_limited",
        "429",
        "connection",
        "ConnectionError",
        "ConnectionRefusedError",
        "ConnectionResetError",
        "503",
        "502",
        "504",
        "server_error",
        "overloaded",
        "temporarily_unavailable",
    })

    # HTTP status codes that should trigger retry
    retryable_status_codes: set[int] = field(default_factory=lambda: {
        429,  # Too Many Requests
        502,  # Bad Gateway
        503,  # Service Unavailable
        504,  # Gateway Timeout
    })

    @classmethod
    def aggressive(cls) -> "RetryConfig":
        """Create aggressive retry config for critical operations."""
        return cls(
            max_retries=5,
            initial_delay=0.5,
            max_delay=60.0,
            exponential_base=2.0,
        )

    @classmethod
    def conservative(cls) -> "RetryConfig":
        """Create conservative retry config for less critical operations."""
        return cls(
            max_retries=2,
            initial_delay=2.0,
            max_delay=15.0,
            exponential_base=1.5,
        )

    @classmethod
    def no_retry(cls) -> "RetryConfig":
        """Create config that disables retries."""
        return cls(max_retries=0)


@dataclass
class RetryAttempt:
    """Information about a retry attempt."""
    attempt_number: int
    delay_seconds: float
    error: str
    error_type: Optional[RetryableErrorType] = None


@dataclass
class RetryResult:
    """Result of a retried operation."""
    success: bool
    result: Any
    attempts: int
    total_delay_seconds: float
    retry_history: list[RetryAttempt] = field(default_factory=list)
    final_error: Optional[str] = None


T = TypeVar("T")


class RetryableOperation:
    """Execute operations with automatic retry on failure.

    Usage:
        retry_op = RetryableOperation(config)
        result = await retry_op.execute(some_async_function, arg1, arg2)

        # Or with decorator-style:
        @retry_op.wrap
        async def my_function():
            ...
    """

    def __init__(self, config: Optional[RetryConfig] = None):
        """Initialize retryable operation.

        Args:
            config: Retry configuration
        """
        self.config = config or RetryConfig()

    def is_retryable(self, error: Exception) -> tuple[bool, Optional[RetryableErrorType]]:
        """Check if an error is retryable.

        Args:
            error: The exception to check

        Returns:
            Tuple of (is_retryable, error_type)
        """
        error_str = str(error).lower()
        error_type_name = type(error).__name__

        # Check error type name
        if error_type_name in self.config.retryable_errors:
            return True, self._classify_error(error)

        # Check error message content
        for keyword in self.config.retryable_errors:
            if keyword.lower() in error_str:
                return True, self._classify_error(error)

        # Check for HTTP status codes in error
        for code in self.config.retryable_status_codes:
            if str(code) in error_str:
                return True, self._classify_error(error)

        return False, None

    def _classify_error(self, error: Exception) -> RetryableErrorType:
        """Classify an error into a type."""
        error_str = str(error).lower()
        error_type_name = type(error).__name__

        if "timeout" in error_str or "Timeout" in error_type_name:
            return RetryableErrorType.TIMEOUT

        if "429" in error_str or "rate" in error_str:
            return RetryableErrorType.RATE_LIMIT

        if "connection" in error_str.lower() or "Connection" in error_type_name:
            return RetryableErrorType.CONNECTION

        if any(str(code) in error_str for code in [502, 503, 504]):
            return RetryableErrorType.SERVER_ERROR

        return RetryableErrorType.TRANSIENT

    def calculate_delay(self, attempt: int) -> float:
        """Calculate delay for a retry attempt.

        Args:
            attempt: Current attempt number (0-indexed)

        Returns:
            Delay in seconds
        """
        # Exponential backoff
        delay = self.config.initial_delay * (
            self.config.exponential_base ** attempt
        )

        # Cap at max delay
        delay = min(delay, self.config.max_delay)

        # Add jitter
        if self.config.jitter:
            jitter_range = delay * self.config.jitter_factor
            delay += random.uniform(-jitter_range, jitter_range)
            delay = max(0.1, delay)  # Ensure positive

        return delay

    async def execute(
        self,
        operation: Callable[..., Coroutine[Any, Any, T]],
        *args: Any,
        on_retry: Optional[Callable[[RetryAttempt], None]] = None,
        **kwargs: Any,
    ) -> RetryResult:
        """Execute an operation with retry.

        Args:
            operation: Async function to execute
            *args: Positional arguments for the operation
            on_retry: Optional callback when retry occurs
            **kwargs: Keyword arguments for the operation

        Returns:
            RetryResult with success status and result
        """
        attempts = 0
        total_delay = 0.0
        retry_history: list[RetryAttempt] = []

        while attempts <= self.config.max_retries:
            try:
                result = await operation(*args, **kwargs)
                return RetryResult(
                    success=True,
                    result=result,
                    attempts=attempts + 1,
                    total_delay_seconds=total_delay,
                    retry_history=retry_history,
                )

            except Exception as e:
                is_retryable, error_type = self.is_retryable(e)

                if not is_retryable or attempts >= self.config.max_retries:
                    # Not retryable or exhausted retries
                    return RetryResult(
                        success=False,
                        result=None,
                        attempts=attempts + 1,
                        total_delay_seconds=total_delay,
                        retry_history=retry_history,
                        final_error=str(e),
                    )

                # Calculate delay and record attempt
                delay = self.calculate_delay(attempts)
                total_delay += delay

                retry_attempt = RetryAttempt(
                    attempt_number=attempts + 1,
                    delay_seconds=delay,
                    error=str(e),
                    error_type=error_type,
                )
                retry_history.append(retry_attempt)

                logger.warning(
                    f"Retry {attempts + 1}/{self.config.max_retries} "
                    f"after {delay:.2f}s due to {error_type}: {e}"
                )

                if on_retry:
                    on_retry(retry_attempt)

                # Wait before retry
                await asyncio.sleep(delay)
                attempts += 1

        # Should not reach here, but handle edge case
        return RetryResult(
            success=False,
            result=None,
            attempts=attempts,
            total_delay_seconds=total_delay,
            retry_history=retry_history,
            final_error="Max retries exceeded",
        )

    def wrap(
        self,
        func: Callable[..., Coroutine[Any, Any, T]],
    ) -> Callable[..., Coroutine[Any, Any, T]]:
        """Decorator to wrap a function with retry logic.

        Usage:
            @retry_op.wrap
            async def my_function():
                ...
        """
        async def wrapper(*args: Any, **kwargs: Any) -> T:
            result = await self.execute(func, *args, **kwargs)
            if result.success:
                return result.result
            else:
                raise RuntimeError(
                    f"Operation failed after {result.attempts} attempts: "
                    f"{result.final_error}"
                )

        return wrapper


def retry_on_failure(
    config: Optional[RetryConfig] = None,
) -> Callable:
    """Decorator factory for retry behavior.

    Usage:
        @retry_on_failure(RetryConfig(max_retries=5))
        async def my_function():
            ...
    """
    retry_config = config or RetryConfig()

    def decorator(
        func: Callable[..., Coroutine[Any, Any, T]]
    ) -> Callable[..., Coroutine[Any, Any, T]]:
        retry_op = RetryableOperation(retry_config)
        return retry_op.wrap(func)

    return decorator


class RetryContext:
    """Context manager for retry operations.

    Usage:
        async with RetryContext(config) as retry:
            result = await retry.call(my_async_func, arg1, arg2)
            if not retry.success:
                handle_failure(retry.last_error)
    """

    def __init__(self, config: Optional[RetryConfig] = None):
        self.config = config or RetryConfig()
        self._retry_op = RetryableOperation(self.config)
        self._last_result: Optional[RetryResult] = None

    async def __aenter__(self) -> "RetryContext":
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> bool:
        return False

    async def call(
        self,
        operation: Callable[..., Coroutine[Any, Any, T]],
        *args: Any,
        **kwargs: Any,
    ) -> Optional[T]:
        """Call an operation with retry.

        Returns:
            Result if successful, None otherwise
        """
        self._last_result = await self._retry_op.execute(
            operation, *args, **kwargs
        )
        return self._last_result.result if self._last_result.success else None

    @property
    def success(self) -> bool:
        """Check if last call was successful."""
        return self._last_result.success if self._last_result else False

    @property
    def last_error(self) -> Optional[str]:
        """Get last error message."""
        return self._last_result.final_error if self._last_result else None

    @property
    def attempts(self) -> int:
        """Get number of attempts in last call."""
        return self._last_result.attempts if self._last_result else 0

    @property
    def last_result(self) -> Optional[RetryResult]:
        """Get full result of last call."""
        return self._last_result
