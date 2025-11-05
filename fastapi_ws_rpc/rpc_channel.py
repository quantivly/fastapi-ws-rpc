"""
Definition for an RPC channel protocol on top of a websocket -
enabling bi-directional request/response interactions
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import suppress
from typing import TYPE_CHECKING, Any

from ._internal.caller import RpcCaller
from ._internal.method_invoker import RpcMethodInvoker
from ._internal.promise_manager import RpcPromise, RpcPromiseManager
from ._internal.protocol_handler import RpcProtocolHandler
from .exceptions import RemoteValueError, RpcBackpressureError, RpcChannelClosedError
from .logger import get_logger
from .schemas import JsonRpcRequest, JsonRpcResponse
from .utils import gen_uid

if TYPE_CHECKING:
    from typing_extensions import TypeAlias

    from .config import RpcDebugConfig
    from .rpc_methods import RpcMethodsBase

# Type aliases for callbacks (using string quotes for forward references)
OnConnectCallback: TypeAlias = Callable[["RpcChannel"], Awaitable[None]]
OnDisconnectCallback: TypeAlias = Callable[["RpcChannel"], Awaitable[None]]
OnErrorCallback: TypeAlias = Callable[["RpcChannel", Exception], Awaitable[None]]

logger = get_logger("RPC_CHANNEL")

# Sentinel object for default timeout (more type-safe than a class)
# Using a module-level constant allows proper type checking
_DEFAULT_TIMEOUT_SENTINEL = object()


class RpcChannel:
    """
    A wire agnostic json-rpc channel protocol for both server and client.
    Enable each side to send RPC-requests (calling exposed methods on other side)
    and receive rpc-responses with the return value

    provides a .other property for calling remote methods.
    e.g. answer = channel.other.add(a=1,b=1) will (For example) ask the other
    side to perform 1+1 and will return an RPC-response of 2
    """

    def __init__(
        self,
        methods: RpcMethodsBase,
        socket: Any,
        channel_id: str | None = None,
        default_response_timeout: float | None = None,
        sync_channel_id: bool = False,
        max_pending_requests: int | None = None,
        max_connection_duration: float | None = None,
        debug_config: RpcDebugConfig | None = None,
        subprotocol: str | None = None,
        connect_callbacks: list[OnConnectCallback] | None = None,
        disconnect_callbacks: list[OnDisconnectCallback] | None = None,
        error_callbacks: list[OnErrorCallback] | None = None,
        max_send_queue_size: int | None = None,
        **kwargs: Any,
    ) -> None:
        """Initialize an RPC channel.

        Parameters
        ----------
        methods : RpcMethodsBase
            RPC methods to expose to other side.
        socket : Any
            Socket object providing simple send/recv methods.
        channel_id : str | None, optional
            UUID for channel. Defaults to None in which case a random UUID
            is generated.
        default_response_timeout : float | None, optional
            Default timeout for RPC call responses. Defaults to None (no timeout).
        sync_channel_id : bool, optional
            Should get the other side of the channel id. Helps to identify
            connections, costs a bit networking time. Defaults to False.
        max_pending_requests : int | None, optional
            Maximum number of pending RPC requests before backpressure is applied.
            Defaults to 1000. Prevents resource exhaustion from request flooding.
        max_connection_duration : float | None, optional
            Maximum time in seconds that this channel can remain open. After this
            time, the connection will be gracefully closed. None means no limit.
            Defaults to None.
        debug_config : RpcDebugConfig | None, optional
            Configuration for error disclosure. If None, defaults to production-safe
            mode (debug_mode=False).
        subprotocol : str | None, optional
            Negotiated WebSocket subprotocol. This is stored for runtime checks
            and logging. Defaults to None.
        connect_callbacks : list[OnConnectCallback] | None, optional
            List of callbacks to call when connection is established. Defaults to None.
        disconnect_callbacks : list[OnDisconnectCallback] | None, optional
            List of callbacks to call when connection is closed. Defaults to None.
        error_callbacks : list[OnErrorCallback] | None, optional
            List of callbacks to call when an error occurs. Defaults to None.
        max_send_queue_size : int | None, optional
            Maximum number of pending outgoing messages before backpressure is applied.
            Defaults to 1000. Set to 0 to disable send backpressure. Prevents send-side
            overwhelm and memory exhaustion from queued messages.
        **kwargs : Any
            Additional context data accessible to methods via channel.context.
        """
        logger.debug("Initializing RPC channel...")
        self.methods = methods._copy_()
        # allow methods to access channel (for recursive calls - e.g. call me as
        # a response for me calling you)
        self.methods._set_channel_(self)

        # Initialize specialized components with backpressure control
        self._promise_manager = RpcPromiseManager(
            max_pending_requests=max_pending_requests
        )
        self._method_invoker = RpcMethodInvoker(self.methods)
        self._protocol_handler = RpcProtocolHandler(
            self._method_invoker,
            self._promise_manager,
            self.send_raw,
            debug_config=debug_config,
        )

        self.socket = socket
        # timeout
        self.default_response_timeout = default_response_timeout
        # Unique channel id
        self.id = channel_id if channel_id is not None else gen_uid()
        # flag control should we retrieve the channel id of the other side
        self._sync_channel_id = sync_channel_id
        # The channel id of the other side (if sync_channel_id is True)
        self._other_channel_id: str | None = None
        # asyncio event to check if we got the channel id of the other side
        self._channel_id_synced = asyncio.Event()
        # Store negotiated WebSocket subprotocol for runtime checks
        self.subprotocol = subprotocol
        # Convenience caller provides dynamic proxy access to remote methods
        # No validation is performed (remote schema is unknown)
        self.other = RpcCaller(self)

        # Log subprotocol if present
        if self.subprotocol:
            logger.debug(
                f"RPC channel initialized with subprotocol: {self.subprotocol}"
            )
        # core event callback registers
        self._connect_callbacks: list[OnConnectCallback] = connect_callbacks or []
        self._disconnect_callbacks: list[OnDisconnectCallback] = (
            disconnect_callbacks or []
        )
        self._error_callbacks: list[OnErrorCallback] = error_callbacks or []

        # Connection duration tracking
        self._max_connection_duration = max_connection_duration
        self._connection_start_time = asyncio.get_event_loop().time()
        self._duration_watchdog_task: asyncio.Task[None] | None = None
        self._closing = False  # Flag to prevent concurrent close() calls
        self._close_lock = asyncio.Lock()  # Lock to prevent race conditions in close()

        # Start duration watchdog if limit is set
        if self._max_connection_duration is not None:
            self._duration_watchdog_task = asyncio.create_task(
                self._connection_duration_watchdog()
            )

        # Send backpressure tracking
        # Use semaphore to limit concurrent sends, preventing memory exhaustion
        # from queued outgoing messages
        self._max_send_queue_size = (
            max_send_queue_size if max_send_queue_size is not None else 1000
        )
        self._send_semaphore: asyncio.Semaphore | None = None
        self._pending_sends = (
            0  # Track pending sends to avoid accessing semaphore._value
        )
        if self._max_send_queue_size > 0:
            self._send_semaphore = asyncio.Semaphore(self._max_send_queue_size)

        # any other kwarg goes straight to channel context (Accessible to methods)
        self._context: dict[str, Any] = kwargs or {}
        logger.debug("RPC channel initialized.")

    @property
    def context(self) -> dict[str, Any]:
        return self._context

    async def get_other_channel_id(self) -> str:
        """Get the channel id of the other side of the channel.

        Returns
        -------
        str
            The channel ID of the remote side.

        Raises
        ------
        asyncio.TimeoutError
            If the value isn't available within the default response timeout.
        RemoteValueError
            If the remote channel ID is not available.

        Notes
        -----
        The _channel_id_synced event verifies we have the remote ID.
        """
        await asyncio.wait_for(
            self._channel_id_synced.wait(), self.default_response_timeout
        )
        if self._other_channel_id is None:
            raise RemoteValueError("Other channel ID not available")
        return self._other_channel_id

    async def send(self, data: Any) -> None:
        """Wrap calls to underlying socket.

        Parameters
        ----------
        data : Any
            Data to send over the socket.

        Notes
        -----
        For internal use.
        """
        await self.socket.send(data)

    async def send_raw(self, data: dict[str, Any]) -> None:
        """Send raw dictionary data as JSON with backpressure control.

        This method enforces send-side backpressure to prevent memory exhaustion
        from queued outgoing messages. If max_send_queue_size is configured and
        the send queue is full, this method will raise RpcBackpressureError.

        Parameters
        ----------
        data : dict[str, Any]
            Dictionary to send as JSON over the socket.

        Raises
        ------
        RpcBackpressureError
            If send queue is full (too many pending sends).
        RpcChannelClosedError
            If channel is closed.
        """
        # Check if channel is closed first
        if self.is_closed():
            raise RpcChannelClosedError("Cannot send on closed channel")

        # Apply send backpressure if configured
        if self._send_semaphore is not None:
            # Check if send queue is full (fail fast instead of queuing)
            if self._pending_sends >= self._max_send_queue_size:
                raise RpcBackpressureError(
                    f"Send queue full: {self._pending_sends}/{self._max_send_queue_size} "
                    f"messages pending. Slow down sending or increase max_send_queue_size."
                )

            # Acquire semaphore with timeout to prevent deadlocks
            # Use a short timeout (1s) to fail fast if backpressure race occurs
            try:
                await asyncio.wait_for(self._send_semaphore.acquire(), timeout=1.0)
            except asyncio.TimeoutError as e:
                # Semaphore acquisition timed out - queue likely full due to race condition
                raise RpcBackpressureError(
                    f"Send queue backpressure timeout: unable to acquire send slot within 1s. "
                    f"Current pending: {self._pending_sends}/{self._max_send_queue_size}"
                ) from e

            self._pending_sends += 1

            try:
                # Send the message (socket handles serialization)
                await self.socket.send(data)
            finally:
                # Always release semaphore and decrement counter after send completes (or fails)
                self._send_semaphore.release()
                self._pending_sends -= 1
        else:
            # No backpressure - send directly
            await self.socket.send(data)

    async def receive(self) -> Any:
        """
        For internal use. wrap calls to underlying socket
        """
        return await self.socket.recv()

    async def close(self) -> Any:
        """
        Close the RPC channel and clean up resources.

        This method is idempotent and can be safely called multiple times.
        Subsequent calls after the first will be no-ops.

        Returns:
            Result from closing the underlying socket
        """
        # Use lock to ensure atomic check-and-set of closing flag
        async with self._close_lock:
            # Make this method idempotent - return immediately if already closed/closing
            if self._closing or self.is_closed():
                logger.debug(f"Channel {self.id} already closed/closing, skipping")
                return None

            # Set closing flag to prevent concurrent close() calls
            self._closing = True

        logger.debug(f"Closing channel {self.id}...")

        # Cancel duration watchdog if running
        if self._duration_watchdog_task is not None:
            self._duration_watchdog_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._duration_watchdog_task
            self._duration_watchdog_task = None

        # Delegate cleanup to promise manager (this sets the closed flag)
        self._promise_manager.close()

        # Close the underlying socket
        res = await self.socket.close()
        logger.debug(f"Channel {self.id} closed successfully")
        return res

    def is_closed(self) -> bool:
        """
        Check if the channel has been closed.

        Returns:
            True if closed, False otherwise
        """
        return self._promise_manager.is_closed()

    async def wait_until_closed(self) -> bool:
        """
        Wait until the channel is closed.

        Returns:
            True when the channel is closed
        """
        return await self._promise_manager.wait_until_closed()

    async def on_message(self, data: dict[str, Any]) -> None:
        """
        Handle an incoming JSON-RPC 2.0 message.

        Delegates to the protocol handler for parsing and routing.

        Args:
            data: The received message data
        """
        logger.debug(f"Processing received message: {data}")
        try:
            await self._protocol_handler.handle_message(data)
        except Exception as e:
            # Catch all application errors for logging and error handler callbacks
            # The exception is re-raised to propagate to the caller
            # System exceptions (KeyboardInterrupt, SystemExit) are not caught
            logger.error(f"Error processing RPC message: {type(e).__name__}: {e}")
            await self.on_error(e)
            raise

    def add_connect_callback(self, callback: OnConnectCallback) -> None:
        """Add a callback to be called when connection is established.

        Args:
            callback: Async function called with (channel) when connected
        """
        self._connect_callbacks.append(callback)

    def remove_connect_callback(self, callback: OnConnectCallback) -> bool:
        """Remove a connect callback.

        Args:
            callback: The callback to remove

        Returns:
            True if callback was found and removed, False otherwise
        """
        try:
            self._connect_callbacks.remove(callback)
            return True
        except ValueError:
            return False

    def add_disconnect_callback(self, callback: OnDisconnectCallback) -> None:
        """Add a callback to be called when connection is closed.

        Args:
            callback: Async function called with (channel) when disconnected
        """
        self._disconnect_callbacks.append(callback)

    def remove_disconnect_callback(self, callback: OnDisconnectCallback) -> bool:
        """Remove a disconnect callback.

        Args:
            callback: The callback to remove

        Returns:
            True if callback was found and removed, False otherwise
        """
        try:
            self._disconnect_callbacks.remove(callback)
            return True
        except ValueError:
            return False

    def add_error_callback(self, callback: OnErrorCallback) -> None:
        """Add a callback to be called when an error occurs.

        Args:
            callback: Async function called with (channel, error) when error occurs
        """
        self._error_callbacks.append(callback)

    def remove_error_callback(self, callback: OnErrorCallback) -> bool:
        """Remove an error callback.

        Args:
            callback: The callback to remove

        Returns:
            True if callback was found and removed, False otherwise
        """
        try:
            self._error_callbacks.remove(callback)
            return True
        except ValueError:
            return False

    async def _invoke_callbacks(
        self,
        callbacks: (
            list[OnConnectCallback] | list[OnDisconnectCallback] | list[OnErrorCallback]
        ),
        *args: Any,
        **kwargs: Any,
    ) -> None:
        """Invoke all callbacks in the list with the provided arguments.

        Args:
            callbacks: List of callbacks to invoke
            *args: Positional arguments to pass to callbacks
            **kwargs: Keyword arguments to pass to callbacks

        Notes
        -----
        Uses return_exceptions=True to ensure one failing callback doesn't
        cancel others. Individual callback failures are logged but don't
        propagate to prevent cascade failures.
        """
        results = await asyncio.gather(
            *(callback(*args, **kwargs) for callback in callbacks),
            return_exceptions=True,
        )

        # Log any callback failures without propagating them
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                callback_name = getattr(callbacks[i], "__name__", str(callbacks[i]))
                logger.error(
                    f"Callback {callback_name} failed: {type(result).__name__}: {result}",
                    exc_info=True,
                )

    async def on_connect(self) -> None:
        """
        Run get other channel id if sync_channel_id is True
        Run all callbacks from self._connect_callbacks
        """
        if self._sync_channel_id:
            logger.debug("Syncing channel ID...")
            self._get_other_channel_id_task = asyncio.create_task(
                self._get_other_channel_id()
            )
        await self._invoke_callbacks(self._connect_callbacks, self)

    async def _get_other_channel_id(self) -> str:
        """
        Perform call to the other side of the channel to get its channel id
        Each side is generating the channel id by itself so there is no way
        to identify a connection without this sync
        """
        if self._other_channel_id is None:
            logger.debug("No cached channel ID found, calling _get_channel_id_()...")
            other_channel_id = await self.other._get_channel_id_()
            self._other_channel_id = (
                str(other_channel_id.result)
                if other_channel_id and other_channel_id.result
                else None
            )
            logger.debug("Got channel ID: %s", self._other_channel_id)
            if self._other_channel_id is None:
                raise RemoteValueError()
            # update asyncio event that we received remote channel id
            self._channel_id_synced.set()
            return self._other_channel_id
        return self._other_channel_id

    async def on_disconnect(self) -> None:
        """
        Handle channel disconnection.

        This method is idempotent - disconnect callbacks are only called once.
        Subsequent calls will be no-ops.
        """
        # Check if already closed to prevent double-calling disconnect callbacks
        if self.is_closed():
            logger.debug(f"Channel {self.id} already disconnected, skipping callbacks")
            return

        # disconnect happened - mark the channel as closed
        self._promise_manager.close()
        await self._invoke_callbacks(self._disconnect_callbacks, self)

    async def on_error(self, error: Exception) -> None:
        """Invoke error callbacks.

        Args:
            error: The exception that occurred
        """
        await self._invoke_callbacks(self._error_callbacks, self, error)

    def get_saved_promise(self, call_id: str) -> RpcPromise:
        """
        Retrieve a stored promise by call ID.

        Args:
            call_id: The unique identifier for the RPC call

        Returns:
            The RpcPromise associated with the call ID
        """
        return self._promise_manager.get_saved_promise(call_id)

    def get_saved_response(self, call_id: str) -> JsonRpcResponse:
        """
        Retrieve a stored response by call ID.

        Args:
            call_id: The unique identifier for the RPC call

        Returns:
            The JsonRpcResponse associated with the call ID
        """
        return self._promise_manager.get_saved_response(call_id)

    def clear_saved_call(self, call_id: str) -> None:
        """
        Clean up a completed call by removing its promise and response.

        Args:
            call_id: The unique identifier for the RPC call to clean up
        """
        self._promise_manager.clear_saved_call(call_id)

    async def async_call(
        self,
        name: str,
        args: dict[str, Any] | None = None,
        call_id: str | None = None,
    ) -> RpcPromise:
        """
        Call a method and return a promise for tracking the response.

        Use self.call() or wait_for_response() on the returned promise to get
        the return value of the call.

        Args:
            name: Name of the method to call on the remote side
            args: Keyword arguments to pass to the method
            call_id: Optional UUID to track the call. If not provided, a new UUID
                    is generated. WARNING: Using duplicate call_ids will cause
                    request collision and undefined behavior.

        Returns:
            RpcPromise: A promise object that can be awaited for the response

        Raises:
            ValueError: If call_id is already in use (request ID collision)

        Example:
            ```python
            promise = await channel.async_call("my_method", {"arg": "value"})
            response = await channel.call(promise, timeout=5.0)
            ```
        """
        if args is None:
            args = {}
        call_id = call_id or gen_uid()

        # Create JSON-RPC 2.0 request
        json_rpc_request = JsonRpcRequest(
            id=call_id, method=name, params=args if args else None
        )

        # Create promise (includes collision detection)
        promise = self._promise_manager.create_promise(json_rpc_request)

        logger.debug("Sending JSON-RPC 2.0 request: %s", json_rpc_request)
        await self.send_raw(json_rpc_request.model_dump(exclude_none=True))

        return promise

    async def call(
        self,
        name: str,
        args: dict[str, Any] | None = None,
        timeout: object | float | None = _DEFAULT_TIMEOUT_SENTINEL,
    ) -> JsonRpcResponse:
        """
        Call a method and wait for a response to be received.

        Args:
            name: Name of the method to call on the remote side
            args: Keyword arguments to pass to the method
            timeout: Maximum time to wait for response (seconds).
                    Can be _DEFAULT_TIMEOUT_SENTINEL (use channel default), a number
                    (seconds), or None (no timeout).

        Returns:
            JsonRpcResponse: The response from the remote method

        Raises:
            RpcChannelClosedError: If channel is closed during the call
            asyncio.TimeoutError: If the call times out
        """
        if args is None:
            args = {}
        promise = await self.async_call(name, args)

        # Convert _DEFAULT_TIMEOUT_SENTINEL to actual timeout value
        actual_timeout: float | None
        if timeout is _DEFAULT_TIMEOUT_SENTINEL:
            actual_timeout = self.default_response_timeout
        else:
            actual_timeout = timeout  # type: ignore[assignment]

        return await self._promise_manager.wait_for_response(promise, actual_timeout)

    async def notify(self, name: str, args: dict[str, Any] | None = None) -> None:
        """
        Send a notification (one-way message with no response expected).

        This sends a JSON-RPC 2.0 notification, which is a request without an id.
        The remote side will execute the method but will not send back a response.
        Use this for fire-and-forget messages where you don't need confirmation.

        Args:
            name: Name of the method to call on the remote side
            args: Keyword arguments to pass to the method

        Example:
            ```python
            # Send a notification to log something on the remote side
            await channel.notify("log_event", {"level": "info", "message": "Hello"})
            ```

        Note:
            - Notifications never receive responses, even if the method fails
            - The remote side may silently drop notifications if the method doesn't exist
            - For operations that need confirmation, use call() instead
        """
        if args is None:
            args = {}

        # Create JSON-RPC 2.0 notification (request with id=None)
        notification = JsonRpcRequest(
            id=None,  # Notifications have no id
            method=name,
            params=args if args else None,
        )

        logger.debug("Sending JSON-RPC 2.0 notification: %s", notification)
        await self.send_raw(notification.model_dump(exclude_none=True))

    def get_connection_age(self) -> float:
        """
        Get the age of the connection in seconds.

        Returns:
            Number of seconds since the channel was created
        """
        current_time = asyncio.get_event_loop().time()
        return current_time - self._connection_start_time

    def get_remaining_duration(self) -> float | None:
        """
        Get the remaining connection duration before automatic closure.

        Returns:
            Remaining seconds, or None if no duration limit is set
        """
        if self._max_connection_duration is None:
            return None

        age = self.get_connection_age()
        remaining = self._max_connection_duration - age
        return max(0.0, remaining)

    async def _connection_duration_watchdog(self) -> None:
        """
        Background task that monitors connection duration.

        When max_connection_duration is reached, this task will:
        1. Log a warning
        2. Trigger on_disconnect handlers
        3. Close the channel gracefully
        """
        if self._max_connection_duration is None:
            return

        try:
            await asyncio.sleep(self._max_connection_duration)

            # Connection duration exceeded
            logger.warning(
                f"Channel {self.id} exceeded maximum connection duration "
                f"({self._max_connection_duration}s). Closing gracefully."
            )

            # Use lock to check flag and trigger disconnect exactly once
            # Note: Don't call close() inside lock as it also acquires the lock (deadlock)
            should_close = False
            async with self._close_lock:
                # Check if already closing to prevent race condition with external close() calls
                if not self.is_closed() and not self._closing:
                    self._closing = True  # Set flag before triggering disconnect
                    should_close = True
                    # Trigger disconnect handlers (inside lock to ensure exactly-once semantics)
                    await self.on_disconnect()

            # Call close() outside lock to avoid deadlock (close() also acquires the lock)
            if should_close:
                await self.close()

        except asyncio.CancelledError:
            # Normal cancellation during shutdown
            logger.debug("Connection duration watchdog cancelled")
