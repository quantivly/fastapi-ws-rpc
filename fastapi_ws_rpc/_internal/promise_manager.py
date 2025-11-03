"""
Promise management component for tracking RPC requests and responses.

This module handles the lifecycle of RPC promises, including request tracking,
response matching, and cleanup on channel closure.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from ..exceptions import RpcBackpressureError, RpcChannelClosedError

if TYPE_CHECKING:
    from collections.abc import Awaitable

    from ..schemas import JsonRpcRequest, JsonRpcResponse

# Default maximum number of pending requests per channel
# This prevents resource exhaustion from request flooding
DEFAULT_MAX_PENDING_REQUESTS = 1000


class RpcPromise:
    """
    Simple Event and id wrapper/proxy that holds the state of a pending request.

    This class wraps an asyncio.Event to provide a promise-like interface for
    tracking RPC requests and waiting for their corresponding responses.
    """

    def __init__(self, request: JsonRpcRequest) -> None:
        self._request = request
        self._id = request.id
        # event used to wait for the completion of the request
        # (upon receiving its matching response)
        self._event = asyncio.Event()

    @property
    def request(self) -> JsonRpcRequest:
        return self._request

    @property
    def call_id(self) -> str:
        # JSON-RPC 2.0 spec: id can be string, number, or null, but we use string
        return str(self._id) if self._id is not None else ""

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

    def __init__(self, max_pending_requests: int | None = None) -> None:
        """
        Initialize the promise manager.

        Args:
            max_pending_requests: Maximum number of pending requests allowed.
                                 If None, uses DEFAULT_MAX_PENDING_REQUESTS (1000).
                                 When this limit is reached, create_promise() will
                                 raise RpcBackpressureError to prevent resource exhaustion.
        """
        # Pending requests - id-mapped to async-event
        self._requests: dict[str, RpcPromise] = {}
        # Received responses
        self._responses: dict[str, JsonRpcResponse] = {}
        # Internal event signaling channel closure
        self._closed = asyncio.Event()
        # Max pending requests for backpressure control
        self._max_pending_requests = (
            max_pending_requests
            if max_pending_requests is not None
            else DEFAULT_MAX_PENDING_REQUESTS
        )

    def create_promise(self, request: JsonRpcRequest) -> RpcPromise:
        """
        Create and store a promise for a request.

        Args:
            request: The JSON-RPC request to create a promise for

        Returns:
            A new RpcPromise instance

        Raises:
            RpcBackpressureError: If max pending requests limit is reached
            ValueError: If the request ID is already in use (collision)
        """
        call_id = str(request.id) if request.id is not None else ""

        # Check backpressure limit BEFORE creating the promise
        if len(self._requests) >= self._max_pending_requests:
            raise RpcBackpressureError(
                f"Channel backpressure: maximum pending requests ({self._max_pending_requests}) "
                f"reached. Wait for responses before sending more requests. "
                f"Current pending: {len(self._requests)}"
            )

        # Validate request ID is not already in use
        if call_id in self._requests:
            raise ValueError(
                f"Request ID collision detected: '{call_id}' is already in use for a pending request. "
                f"Each RPC call must have a unique ID. This likely indicates a bug in call ID generation."
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

        # If the channel was closed before we could finish
        if response is None:
            raise RpcChannelClosedError(
                f"Channel Closed before RPC response for {call_id} could be received"
            )

        self.clear_saved_call(call_id)
        return response

    def close(self) -> None:
        """
        Signal closure and wake all waiting promises.

        This method:
        1. Signals that the channel is closed
        2. Wakes up all waiting coroutines
        3. Clears all pending requests and responses
        """
        # Signal closure first so waiting calls can detect it
        self._closed.set()

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
