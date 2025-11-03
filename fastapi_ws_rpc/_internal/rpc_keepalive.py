"""
Keepalive component for maintaining WebSocket RPC connections.

This module provides RpcKeepalive, which manages periodic ping operations
to detect and handle unresponsive connections.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from collections.abc import Awaitable

from ..logger import get_logger
from ..rpc_methods import PING_RESPONSE

logger = get_logger(__name__)


class RpcKeepalive:
    """
    Manages keep-alive pinging for WebSocket RPC connections.

    This component:
    - Sends periodic ping messages to verify connection health
    - Tracks consecutive ping failures
    - Triggers connection closure after max failures threshold
    - Provides clean start/stop lifecycle management

    Parameters
    ----------
    interval : float
        Time in seconds between ping attempts (must be > 0).
    max_consecutive_failures : int
        Maximum consecutive ping failures before closing connection.
    ping_fn : Callable[[], Awaitable[Any]]
        Async function to call for sending pings. Should return response object
        with a 'result' attribute.
    close_fn : Callable[[], Awaitable[None]]
        Async function to call when connection should be closed due to failures.

    Usage
    -----
    ```python
    keepalive = RpcKeepalive(
        interval=30.0,
        max_consecutive_failures=3,
        ping_fn=client.ping,
        close_fn=client.close
    )
    keepalive.start()
    # Later...
    await keepalive.stop()
    ```
    """

    def __init__(
        self,
        interval: float,
        max_consecutive_failures: int,
        ping_fn: Callable[[], Awaitable[Any]],
        close_fn: Callable[[], Awaitable[None]],
    ) -> None:
        """
        Initialize the keepalive manager.

        Args:
            interval: Time in seconds between ping attempts.
            max_consecutive_failures: Max consecutive failures before closing.
            ping_fn: Async function to send pings.
            close_fn: Async function to close connection.

        Raises:
            ValueError: If interval <= 0 or max_consecutive_failures < 1.
        """
        if interval <= 0:
            raise ValueError("Keep-alive interval must be > 0")
        if max_consecutive_failures < 1:
            raise ValueError("max_consecutive_failures must be >= 1")

        self._interval = interval
        self._max_failures = max_consecutive_failures
        self._ping_fn = ping_fn
        self._close_fn = close_fn

        self._task: asyncio.Task[None] | None = None
        self._consecutive_failures = 0

    @property
    def is_running(self) -> bool:
        """
        Check if keepalive task is currently running.

        Returns:
            bool: True if the task exists and is not done.
        """
        return self._task is not None and not self._task.done()

    @property
    def consecutive_failures(self) -> int:
        """
        Get the current count of consecutive ping failures.

        Returns:
            int: Number of consecutive failures.
        """
        return self._consecutive_failures

    def reset_failures(self) -> None:
        """
        Reset the consecutive failure counter to zero.

        This should be called when a new connection is established.
        """
        self._consecutive_failures = 0
        logger.debug("Keepalive failure counter reset")

    def start(self) -> None:
        """
        Start the keepalive task.

        If the task is already running, this is a no-op.
        """
        if self.is_running:
            logger.debug("Keepalive task already running, skipping start")
            return

        logger.debug(f"Starting keepalive task (interval: {self._interval}s)")
        self._task = asyncio.create_task(self._keepalive_loop())

    async def stop(self) -> None:
        """
        Stop the keepalive task and wait for it to complete.

        This method is idempotent and can be safely called multiple times.
        """
        if self._task is None:
            logger.debug("No keepalive task to stop")
            return

        logger.debug("Stopping keepalive task...")
        self._task.cancel()

        # Wait for cancellation to complete
        with suppress(asyncio.CancelledError):
            await self._task

        self._task = None
        logger.debug("Keepalive task stopped successfully")

    async def _keepalive_loop(self) -> None:
        """
        Background task that sends periodic ping messages.

        This loop runs until cancelled or until max consecutive failures
        is reached, at which point it closes the connection.
        """
        try:
            while True:
                await asyncio.sleep(self._interval)

                try:
                    # Send ping with timeout (half interval or 10s, whichever is smaller)
                    ping_timeout = min(self._interval / 2, 10.0)
                    answer = await asyncio.wait_for(
                        self._ping_fn(), timeout=ping_timeout
                    )

                    # Validate response
                    if (
                        not answer
                        or not hasattr(answer, "result")
                        or answer.result != PING_RESPONSE
                    ):
                        self._consecutive_failures += 1
                        logger.warning(
                            f"Keepalive ping returned unexpected response "
                            f"(failure {self._consecutive_failures}/"
                            f"{self._max_failures}): {answer}"
                        )
                    else:
                        # Success - reset failure counter
                        if self._consecutive_failures > 0:
                            logger.debug(
                                f"Keepalive recovered after "
                                f"{self._consecutive_failures} failures"
                            )
                        self._consecutive_failures = 0
                        logger.debug("Keepalive ping successful")

                except asyncio.TimeoutError:
                    self._consecutive_failures += 1
                    logger.warning(
                        f"Keepalive ping timed out "
                        f"(failure {self._consecutive_failures}/"
                        f"{self._max_failures})"
                    )

                except (RuntimeError, ConnectionError, ValueError) as e:
                    self._consecutive_failures += 1
                    logger.warning(
                        f"Keepalive ping failed with {type(e).__name__}: {e} "
                        f"(failure {self._consecutive_failures}/"
                        f"{self._max_failures})"
                    )

                # Check if we've exceeded max consecutive failures
                if self._consecutive_failures >= self._max_failures:
                    logger.error(
                        f"Keepalive exceeded maximum consecutive failures "
                        f"({self._max_failures}), closing connection"
                    )
                    # Trigger connection close in background to avoid deadlock.
                    # We don't await here because close() will call cancel_tasks(),
                    # which tries to cancel and await this keepalive task.
                    # By using create_task(), we allow this task to exit immediately
                    # while close() runs independently in the background.
                    asyncio.create_task(self._close_fn())  # type: ignore[arg-type]
                    break

        except asyncio.CancelledError:
            logger.debug("Keepalive task cancelled")
