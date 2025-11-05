"""
Promise management component for tracking RPC requests and responses.

This module handles the lifecycle of RPC promises, including request tracking,
response matching, and cleanup on channel closure.
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import TYPE_CHECKING

from ..exceptions import RpcBackpressureError, RpcChannelClosedError
from ..logger import get_logger

if TYPE_CHECKING:
    from collections.abc import Awaitable

    from ..schemas import JsonRpcRequest, JsonRpcResponse

logger = get_logger("RPC_PROMISE_MANAGER")

# Default maximum number of pending requests per channel
# This prevents resource exhaustion from request flooding
DEFAULT_MAX_PENDING_REQUESTS = 1000

# Default TTL for promises (in seconds)
# Promises older than this will be cleaned up to prevent memory leaks
# Reduced to 60s for faster cleanup in high-throughput scenarios
DEFAULT_PROMISE_TTL = 60.0  # 1 minute

# Cleanup interval for expired promises (in seconds)
PROMISE_CLEANUP_INTERVAL = 60.0  # 1 minute

# Maximum allowed length for request IDs (in characters)
# Prevents DoS attacks via extremely long ID strings
MAX_REQUEST_ID_LENGTH = 256

# Cooldown period for request ID reuse (in seconds)
# After a request is completed, its ID cannot be reused for this duration
# This prevents:
# - Accidental UUID collisions from being exploited
# - Malicious clients from intentionally reusing IDs
REQUEST_ID_COOLDOWN = 10.0

# Maximum number of recently used IDs to track
# Older entries are purged to prevent unbounded memory growth
MAX_RECENT_IDS = 10000


class RpcPromise:
    """
    Simple Event and id wrapper/proxy that holds the state of a pending request.

    This class wraps an asyncio.Event to provide a promise-like interface for
    tracking RPC requests and waiting for their corresponding responses.

    Attributes:
        created_at: Timestamp when the promise was created (for TTL tracking)
    """

    def __init__(self, request: JsonRpcRequest) -> None:
        self._request = request
        self._id = request.id
        # event used to wait for the completion of the request
        # (upon receiving its matching response)
        self._event = asyncio.Event()
        # Timestamp for TTL tracking to prevent memory leaks
        self.created_at = time.time()

    @property
    def request(self) -> JsonRpcRequest:
        return self._request

    @property
    def call_id(self) -> str:
        # JSON-RPC 2.0 spec: id can be string, number, or null, but we use string
        return str(self._id) if self._id is not None else ""

    def age(self) -> float:
        """
        Get the age of this promise in seconds.

        Returns:
            Number of seconds since promise creation
        """
        return time.time() - self.created_at

    def set(self) -> None:
        """
        Signal completion of request with received response.
        """
        self._event.set()

    def wait(self) -> Awaitable[bool]:
        """
        Wait on the internal event - triggered on response.
        """
        return self._event.wait()


class RpcPromiseManager:
    """
    Manages the lifecycle of RPC promises for tracking requests and responses.

    This component handles:
    - Creating and storing promises for outgoing requests
    - Enforcing max pending request limits (backpressure)
    - Matching incoming responses to pending requests
    - Cleaning up completed requests
    - Handling channel closure
    """

    def __init__(
        self,
        max_pending_requests: int | None = None,
        promise_ttl: float | None = None,
    ) -> None:
        """
        Initialize the promise manager.

        Args:
            max_pending_requests: Maximum number of pending requests allowed.
                                 If None, uses DEFAULT_MAX_PENDING_REQUESTS (1000).
                                 When this limit is reached, create_promise() will
                                 raise RpcBackpressureError to prevent resource exhaustion.
            promise_ttl: Time-to-live for promises in seconds. Promises older than
                        this will be cleaned up to prevent memory leaks from timeouts
                        or lost responses. If None, uses DEFAULT_PROMISE_TTL (300s / 5min).
        """
        # Pending requests - id-mapped to async-event
        self._requests: dict[str, RpcPromise] = {}
        # Received responses
        self._responses: dict[str, JsonRpcResponse] = {}
        # Recently used request IDs for cooldown enforcement
        # Maps request ID -> timestamp of last use
        self._recently_used_ids: dict[str, float] = {}
        # Internal event signaling channel closure
        self._closed = asyncio.Event()
        # Max pending requests for backpressure control
        self._max_pending_requests = (
            max_pending_requests
            if max_pending_requests is not None
            else DEFAULT_MAX_PENDING_REQUESTS
        )
        # Promise TTL for memory leak prevention
        self._promise_ttl = (
            promise_ttl if promise_ttl is not None else DEFAULT_PROMISE_TTL
        )
        # Background cleanup task
        self._cleanup_task: asyncio.Task[None] | None = None
        # Start periodic cleanup (only if there's a running event loop)
        try:
            self._cleanup_task = asyncio.create_task(self._periodic_cleanup())
        except RuntimeError:
            # No event loop running yet - cleanup will start on first operation
            logger.debug("No event loop running, cleanup task will start lazily")

    def _ensure_cleanup_task_started(self) -> None:
        """
        Ensure the cleanup task is running. Start it if not already started.

        This allows lazy initialization of the cleanup task for cases where
        the promise manager is created before an event loop is running.
        """
        if self._cleanup_task is None or self._cleanup_task.done():
            try:
                self._cleanup_task = asyncio.create_task(self._periodic_cleanup())
                logger.debug("Started periodic promise cleanup task")
            except RuntimeError:
                # Still no event loop - will try again on next operation
                pass

    def create_promise(self, request: JsonRpcRequest) -> RpcPromise:
        """
        Create and store a promise for a request.

        Args:
            request: The JSON-RPC request to create a promise for

        Returns:
            A new RpcPromise instance

        Raises:
            RpcBackpressureError: If max pending requests limit is reached
            ValueError: If the request ID is already in use (collision) or if
                       the ID was recently used and is still in cooldown period
        """
        # Ensure cleanup task is running
        self._ensure_cleanup_task_started()

        call_id = str(request.id) if request.id is not None else ""

        # Validate request ID length to prevent DoS attacks
        if len(call_id) > MAX_REQUEST_ID_LENGTH:
            raise ValueError(
                f"Request ID too long: {len(call_id)} characters "
                f"(maximum allowed: {MAX_REQUEST_ID_LENGTH}). "
                f"This may indicate a malicious or buggy client."
            )

        # Check if ID was recently used (cooldown enforcement)
        current_time = time.time()
        if call_id in self._recently_used_ids:
            last_use_time = self._recently_used_ids[call_id]
            time_since_last_use = current_time - last_use_time
            if time_since_last_use < REQUEST_ID_COOLDOWN:
                raise ValueError(
                    f"Request ID '{call_id}' was used {time_since_last_use:.1f}s ago. "
                    f"Cooldown period: {REQUEST_ID_COOLDOWN}s. "
                    f"Please wait {REQUEST_ID_COOLDOWN - time_since_last_use:.1f}s before reusing this ID. "
                    f"This prevents accidental or malicious ID reuse."
                )

        # Check backpressure limit BEFORE creating the promise
        if len(self._requests) >= self._max_pending_requests:
            raise RpcBackpressureError(
                f"Channel backpressure: maximum pending requests ({self._max_pending_requests}) "
                f"reached. Wait for responses before sending more requests. "
                f"Current pending: {len(self._requests)}"
            )

        # Validate request ID is not already in use for a pending request
        if call_id in self._requests:
            raise ValueError(
                f"Request ID collision detected: '{call_id}' is already in use for a pending request. "
                f"Each RPC call must have a unique ID. This likely indicates a bug in call ID generation."
            )

        # Mark ID as used with current timestamp
        self._recently_used_ids[call_id] = current_time

        # Cleanup old entries if we exceed max size to prevent unbounded growth
        if len(self._recently_used_ids) > MAX_RECENT_IDS:
            cutoff_time = current_time - REQUEST_ID_COOLDOWN
            # Fix: Use > instead of >= to properly exclude boundary timestamps
            self._recently_used_ids = {
                k: v for k, v in self._recently_used_ids.items() if v > cutoff_time
            }
            logger.debug(
                f"Cleaned up old recently-used IDs. "
                f"Remaining: {len(self._recently_used_ids)}/{MAX_RECENT_IDS}"
            )

        promise = RpcPromise(request)
        self._requests[call_id] = promise
        return promise

    def store_response(self, response: JsonRpcResponse) -> bool:
        """
        Store a response and signal the waiting promise.

        Args:
            response: The JSON-RPC response to store

        Returns:
            True if the response matched a pending request, False otherwise
        """
        if response.id is None:
            return False

        response_id = str(response.id)
        if response_id not in self._requests:
            # Response for unknown request - could be a timeout/cancellation
            return False

        self._responses[response_id] = response
        self._requests[response_id].set()
        return True

    def get_saved_promise(self, call_id: str) -> RpcPromise:
        """
        Retrieve a stored promise by call ID.

        Args:
            call_id: The unique identifier for the RPC call

        Returns:
            The RpcPromise associated with the call ID

        Raises:
            KeyError: If no promise exists for the given call ID
        """
        return self._requests[call_id]

    def get_saved_response(self, call_id: str) -> JsonRpcResponse:
        """
        Retrieve a stored response by call ID.

        Args:
            call_id: The unique identifier for the RPC call

        Returns:
            The JsonRpcResponse associated with the call ID

        Raises:
            KeyError: If no response exists for the given call ID
        """
        return self._responses[call_id]

    def clear_saved_call(self, call_id: str) -> None:
        """
        Clean up a completed call by removing its promise and response.

        Args:
            call_id: The unique identifier for the RPC call to clean up
        """
        self._requests.pop(call_id, None)
        self._responses.pop(call_id, None)

    async def wait_for_response(
        self,
        promise: RpcPromise,
        timeout: float | None = None,
    ) -> JsonRpcResponse:
        """
        Wait for a response with timeout and closure detection.

        This method waits for either:
        1. The promise to be fulfilled with a response
        2. The channel to close
        3. The timeout to expire (if specified)

        Args:
            promise: The promise to wait on
            timeout: Optional timeout in seconds (None = no timeout)

        Returns:
            The JSON-RPC response for the request

        Raises:
            asyncio.TimeoutError: If the timeout expires
            RpcChannelClosedError: If the channel closes before response arrives
        """
        # Wait for the promise or until the channel is terminated
        _, pending = await asyncio.wait(
            [
                asyncio.ensure_future(promise.wait()),
                asyncio.ensure_future(self._closed.wait()),
            ],
            timeout=timeout,
            return_when=asyncio.FIRST_COMPLETED,
        )

        # Cancel all pending futures
        for fut in pending:
            fut.cancel()

        call_id = promise.call_id
        response = self._responses.get(call_id)

        # Distinguish between timeout and channel closure
        if response is None:
            # Clean up immediately to prevent memory leaks from timed-out promises
            # Don't wait for periodic cleanup (which runs every 60s)
            self.clear_saved_call(call_id)

            if self.is_closed():
                # Channel was actually closed - connection lost
                raise RpcChannelClosedError(
                    f"Channel closed before response for call {call_id} could be received"
                )
            else:
                # Timeout expired but channel is still open
                raise asyncio.TimeoutError(
                    f"Timeout waiting for response to call {call_id}"
                )

        # Clean up the promise regardless of success or failure
        self.clear_saved_call(call_id)
        return response

    def _cleanup_expired_promises(self) -> int:
        """
        Clean up expired promises that exceed TTL.

        This prevents memory leaks from requests that timeout or never receive
        responses due to network issues or server crashes.

        Returns:
            Number of expired promises cleaned up
        """
        if self._closed.is_set():
            return 0

        current_time = time.time()
        # Create snapshot to avoid RuntimeError if dict is modified during iteration
        expired_ids = [
            call_id
            for call_id, promise in list(self._requests.items())
            if current_time - promise.created_at > self._promise_ttl
        ]

        if expired_ids:
            logger.warning(
                f"Cleaning up {len(expired_ids)} expired promises "
                f"(TTL: {self._promise_ttl}s)"
            )
            for call_id in expired_ids:
                self.clear_saved_call(call_id)

        return len(expired_ids)

    async def _periodic_cleanup(self) -> None:
        """
        Periodically clean up expired promises to prevent memory leaks.

        This task runs in the background and wakes up every PROMISE_CLEANUP_INTERVAL
        to check for and remove expired promises.
        """
        try:
            while not self._closed.is_set():
                await asyncio.sleep(PROMISE_CLEANUP_INTERVAL)
                if self._closed.is_set():
                    break

                cleaned = self._cleanup_expired_promises()
                if cleaned > 0:
                    logger.info(
                        f"Periodic cleanup removed {cleaned} expired promises. "
                        f"Pending: {len(self._requests)}"
                    )

        except asyncio.CancelledError:
            logger.debug("Promise cleanup task cancelled")
        except (RuntimeError, ValueError, TypeError, KeyError, AttributeError) as e:
            # Expected exceptions from cleanup operations - log and continue
            # These can occur during dict iteration/modification or state checks
            logger.error(
                f"Expected error in periodic promise cleanup: {type(e).__name__}: {e}",
                exc_info=True,
            )
        except Exception as e:
            # Unexpected exception - log as critical and re-raise in development
            # This helps catch bugs during development while maintaining production stability
            logger.critical(
                f"Unexpected error in periodic promise cleanup: {type(e).__name__}: {e}",
                exc_info=True,
                extra={
                    "pending_count": len(self._requests),
                    "is_closed": self._closed.is_set(),
                },
            )
            # Re-raise in development mode to surface unexpected errors
            if os.environ.get("ENV") == "development":
                raise

    def close(self) -> None:
        """
        Signal closure and wake all waiting promises.

        This method:
        1. Signals that the channel is closed
        2. Cancels the periodic cleanup task
        3. Wakes up all waiting coroutines
        4. Clears all pending requests and responses
        """
        # Signal closure first so waiting calls can detect it
        self._closed.set()

        # Cancel the cleanup task
        if self._cleanup_task is not None and not self._cleanup_task.done():
            self._cleanup_task.cancel()

        # Wake up all waiting coroutines so they can see the channel is closed
        for promise in list(self._requests.values()):
            promise.set()

        # Clear the dictionaries to prevent memory leaks
        self._requests.clear()
        self._responses.clear()

    def is_closed(self) -> bool:
        """
        Check if the promise manager has been closed.

        Returns:
            True if closed, False otherwise
        """
        return self._closed.is_set()

    async def wait_until_closed(self) -> bool:
        """
        Wait until the promise manager is closed.

        Returns:
            True when the promise manager is closed
        """
        return await self._closed.wait()

    def get_pending_count(self) -> int:
        """
        Get the current number of pending requests.

        Returns:
            Number of requests awaiting responses
        """
        return len(self._requests)

    def get_max_pending_requests(self) -> int:
        """
        Get the maximum allowed pending requests.

        Returns:
            Maximum pending request limit
        """
        return self._max_pending_requests
