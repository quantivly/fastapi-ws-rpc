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

    This component supports two ping modes:
    - **Protocol-level ping** (default): Uses WebSocket ping/pong frames (RFC 6455)
      for 80-90% better performance than RPC pings. This is the recommended mode.
    - **RPC-level ping** (fallback): Uses JSON-RPC ping method calls. Less efficient
      but works with any RPC implementation.

    The keepalive manager:
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
    websocket : Any
        Raw WebSocket instance (e.g., websockets.WebSocketClientProtocol).
        Used for protocol-level pings.
    close_fn : Callable[[], Awaitable[None]]
        Async function to call when connection should be closed due to failures.
    use_protocol_ping : bool, optional
        If True (default), use WebSocket protocol ping/pong frames.
        If False, use RPC-level pings via ping_fn.
    ping_fn : Callable[[], Awaitable[Any]] | None, optional
        Async function to call for RPC-level pings. Required if use_protocol_ping
        is False. Should return response object with a 'result' attribute.

    Usage
    -----
    Protocol ping mode (recommended):
    ```python
    keepalive = RpcKeepalive(
        interval=30.0,
        max_consecutive_failures=3,
        websocket=raw_ws,
        close_fn=client.close,
        use_protocol_ping=True
    )
    keepalive.start()
    ```

    RPC ping mode (fallback):
    ```python
    keepalive = RpcKeepalive(
        interval=30.0,
        max_consecutive_failures=3,
        websocket=raw_ws,
        close_fn=client.close,
        use_protocol_ping=False,
        ping_fn=client.ping
    )
    keepalive.start()
    ```
    """

    def __init__(
        self,
        interval: float,
        max_consecutive_failures: int,
        websocket: Any,
        close_fn: Callable[[], Awaitable[None]],
        use_protocol_ping: bool = True,
        ping_fn: Callable[[], Awaitable[Any]] | None = None,
    ) -> None:
        """
        Initialize the keepalive manager.

        Args:
            interval: Time in seconds between ping attempts.
            max_consecutive_failures: Max consecutive failures before closing.
            websocket: Raw WebSocket instance for protocol pings.
            close_fn: Async function to close connection.
            use_protocol_ping: If True, use protocol pings; if False, use RPC pings.
            ping_fn: Async function to send RPC pings (required if use_protocol_ping=False).

        Raises:
            ValueError: If interval <= 0 or max_consecutive_failures < 1, or if
                use_protocol_ping=False but ping_fn is None.
        """
        if interval <= 0:
            raise ValueError("Keep-alive interval must be > 0")
        if max_consecutive_failures < 1:
            raise ValueError("max_consecutive_failures must be >= 1")
        if not use_protocol_ping and ping_fn is None:
            raise ValueError("ping_fn is required when use_protocol_ping=False")

        self._interval = interval
        self._max_failures = max_consecutive_failures
        self._websocket = websocket
        self._ping_fn = ping_fn
        self._close_fn = close_fn
        self._use_protocol_ping = use_protocol_ping

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
        Includes timeout protection to prevent deadlocks.
        """
        if self._task is None:
            logger.debug("No keepalive task to stop")
            return

        logger.debug("Stopping keepalive task...")
        self._task.cancel()

        # Wait for cancellation to complete with timeout to prevent deadlocks
        # Use 5 second timeout as keepalive intervals are typically 30s
        try:
            with suppress(asyncio.CancelledError):
                await asyncio.wait_for(self._task, timeout=5.0)
        except asyncio.TimeoutError:
            logger.warning(
                "Keepalive task did not stop within 5s timeout. "
                "This may indicate a deadlock or hung task."
            )
            # Task reference will be cleared anyway to prevent future issues

        self._task = None
        logger.debug("Keepalive task stopped successfully")

    async def _send_protocol_ping(self) -> None:
        """
        Send WebSocket protocol ping frame.

        This uses the WebSocket ping/pong mechanism (RFC 6455) which is much
        more efficient than RPC-level pings. Falls back to RPC ping if the
        WebSocket implementation doesn't support protocol pings.

        The method tries multiple WebSocket implementations:
        1. websockets library (client-side) - has ping() method that returns awaitable
        2. FastAPI WebSocket (server-side) - has send_ping() method if supported
        3. RPC ping fallback - uses ping_fn if protocol ping not available

        Raises
        ------
        asyncio.TimeoutError
            If pong response not received within timeout.
        RuntimeError
            If no ping method is available (neither protocol nor RPC).
        ValueError
            If RPC ping returns unexpected response.
        """
        ping_timeout = min(self._interval / 2, 10.0)

        # Try websockets library (client-side)
        # The ping() method returns a Future that completes when pong is received
        if hasattr(self._websocket, "ping"):
            pong_waiter = await self._websocket.ping()
            await asyncio.wait_for(pong_waiter, timeout=ping_timeout)
            logger.debug("Protocol ping successful (websockets library)")
            return

        # Try FastAPI WebSocket (server-side) - if supported
        # Some WebSocket implementations expose send_ping() method
        if hasattr(self._websocket, "send_ping"):
            await asyncio.wait_for(self._websocket.send_ping(), timeout=ping_timeout)
            logger.debug("Protocol ping successful (FastAPI)")
            return

        # Fallback to RPC ping if protocol ping not available
        if self._ping_fn is not None:
            logger.debug("Protocol ping not available, using RPC ping fallback")
            answer = await asyncio.wait_for(self._ping_fn(), timeout=ping_timeout)
            if (
                not answer
                or not hasattr(answer, "result")
                or answer.result != PING_RESPONSE
            ):
                raise ValueError(f"Unexpected ping response: {answer}")
            logger.debug("RPC ping successful (fallback)")
        else:
            raise RuntimeError("No ping method available (neither protocol nor RPC)")

    async def _keepalive_loop(self) -> None:
        """
        Background task that sends periodic ping messages.

        This loop runs until cancelled or until max consecutive failures
        is reached, at which point it closes the connection.

        Uses protocol-level pings by default for better performance, with
        automatic fallback to RPC-level pings if configured.
        """
        try:
            while True:
                await asyncio.sleep(self._interval)

                try:
                    if self._use_protocol_ping:
                        # Use WebSocket protocol ping/pong (RFC 6455)
                        # This is 80-90% more efficient than RPC pings
                        await self._send_protocol_ping()
                    elif self._ping_fn is not None:
                        # RPC ping mode (fallback) - sends JSON-RPC ping method call
                        ping_timeout = min(self._interval / 2, 10.0)
                        answer = await asyncio.wait_for(
                            self._ping_fn(), timeout=ping_timeout
                        )

                        # Validate RPC response
                        if (
                            not answer
                            or not hasattr(answer, "result")
                            or answer.result != PING_RESPONSE
                        ):
                            raise ValueError(f"Unexpected ping response: {answer}")
                        logger.debug("RPC ping successful")
                    else:
                        raise RuntimeError("No ping method configured")

                    # Success - reset failure counter
                    if self._consecutive_failures > 0:
                        logger.debug(
                            f"Keepalive recovered after "
                            f"{self._consecutive_failures} failures"
                        )
                    self._consecutive_failures = 0

                except asyncio.TimeoutError:
                    self._consecutive_failures += 1
                    ping_type = "Protocol" if self._use_protocol_ping else "RPC"
                    logger.warning(
                        f"Keepalive {ping_type} ping timed out "
                        f"(failure {self._consecutive_failures}/"
                        f"{self._max_failures})"
                    )

                except (RuntimeError, ConnectionError, ValueError) as e:
                    self._consecutive_failures += 1
                    ping_type = "Protocol" if self._use_protocol_ping else "RPC"
                    logger.warning(
                        f"Keepalive {ping_type} ping failed with "
                        f"{type(e).__name__}: {e} "
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
