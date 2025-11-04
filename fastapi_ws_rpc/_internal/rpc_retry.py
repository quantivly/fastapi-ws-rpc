"""
Retry management component for WebSocket RPC connections.

This module provides RpcRetryManager, which handles connection retry logic
with configurable backoff strategies and exception filtering.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, TypeVar

import tenacity
from tenacity import retry, stop_after_attempt, wait
from tenacity.retry import retry_if_exception
from websockets.exceptions import InvalidStatus

from ..logger import get_logger

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

logger = get_logger(__name__)

T = TypeVar("T")


def _is_not_forbidden(value: Any) -> bool:
    """
    Check if the exception is not an authorization-related status code.

    This filter prevents retries on 401 (Unauthorized) and 403 (Forbidden)
    errors, since retrying won't help if credentials are invalid.

    Parameters
    ----------
    value : Any
        The exception to check.

    Returns
    -------
    bool
        True if the exception is not an authorization error (401/403),
        False otherwise.
    """
    return not (
        isinstance(value, InvalidStatus)
        and hasattr(value, "status_code")
        and (value.status_code == 401 or value.status_code == 403)
    )


def _log_retry_error(retry_state: tenacity.RetryCallState) -> None:
    """
    Log exception details during retry attempts.

    This callback is invoked by tenacity before each retry attempt
    to log the exception that triggered the retry.

    Parameters
    ----------
    retry_state : tenacity.RetryCallState
        The current retry state containing exception information.
    """
    outcome = retry_state.outcome
    if outcome is not None:
        logger.exception(outcome.exception())


class RpcRetryManager:
    """
    Manages retry logic for WebSocket RPC connection attempts.

    This component encapsulates tenacity-based retry configuration and
    provides a clean interface for wrapping connection functions with
    automatic retry behavior.

    The default configuration uses:
    - Random exponential backoff (0.1s to 120s)
    - Filtering to skip retries on 401/403 errors
    - Exception logging before each retry
    - Re-raising the final exception if all retries fail

    Parameters
    ----------
    retry_config : dict[str, Any] | bool | None, optional
        Retry configuration. Can be:
        - None: Use default configuration with exponential backoff
        - False: Disable retries entirely
        - dict: Custom tenacity configuration

    Usage
    -----
    ```python
    # With default retry configuration
    retry_mgr = RpcRetryManager()
    wrapped_connect = retry_mgr.wrap(client.__connect__)
    await wrapped_connect()

    # With custom configuration
    custom_config = {
        "wait": wait.wait_fixed(5),
        "stop": stop_after_attempt(3)
    }
    retry_mgr = RpcRetryManager(custom_config)

    # With retries disabled
    retry_mgr = RpcRetryManager(False)
    ```
    """

    # Default retry configuration
    DEFAULT_CONFIG: dict[str, Any] = {
        "wait": wait.wait_random_exponential(min=0.1, max=120),
        "stop": stop_after_attempt(
            10
        ),  # MUST have stop condition to prevent infinite retries
        "retry": retry_if_exception(_is_not_forbidden),
        "reraise": True,
        "retry_error_callback": _log_retry_error,
    }

    def __init__(self, retry_config: dict[str, Any] | bool | None = None) -> None:
        """
        Initialize the retry manager.

        Args:
            retry_config: Retry configuration (None=default, False=disabled, dict=custom).
        """
        if retry_config is False:
            self._config: dict[str, Any] | None = None  # Disable retries
        elif retry_config is None:
            self._config = self.DEFAULT_CONFIG.copy()  # Use defaults
        elif isinstance(retry_config, dict):
            self._config = retry_config  # Use custom config
        else:
            # Handle invalid config (bool True) by disabling
            self._config = None

    @property
    def is_enabled(self) -> bool:
        """
        Check if retries are enabled.

        Returns:
            bool: True if retry configuration exists, False if disabled.
        """
        return self._config is not None

    @property
    def config(self) -> dict[str, Any] | None:
        """
        Get the current retry configuration.

        Returns:
            dict[str, Any] | None: The tenacity configuration dict, or None if disabled.
        """
        return self._config

    def wrap(self, func: Callable[[], Awaitable[T]]) -> Callable[[], Awaitable[T]]:
        """
        Wrap an async function with retry logic.

        If retries are disabled, returns the original function unchanged.
        Otherwise, returns a wrapped version with tenacity retry behavior.

        Parameters
        ----------
        func : Callable[[], Awaitable[T]]
            The async function to wrap with retry logic.

        Returns
        -------
        Callable[[], Awaitable[T]]
            The wrapped function with retry behavior, or the original
            function if retries are disabled.

        Examples
        --------
        >>> async def connect():
        ...     # Connection logic here
        ...     pass
        >>> retry_mgr = RpcRetryManager()
        >>> wrapped_connect = retry_mgr.wrap(connect)
        >>> await wrapped_connect()  # Will retry on failures
        """
        if self._config is None:
            # Retries disabled - return function as-is
            return func

        # Wrap with tenacity retry decorator
        # Note: tenacity's retry decorator preserves the function signature
        wrapped_func: Callable[[], Awaitable[T]] = retry(**self._config)(func)
        return wrapped_func
