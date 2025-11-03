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
from .exceptions import RemoteValueError
from .logger import get_logger
from .schemas import JsonRpcRequest, JsonRpcResponse
from .utils import gen_uid

if TYPE_CHECKING:
    from typing_extensions import TypeAlias

    from .rpc_methods import RpcMethodsBase

# Type aliases for callbacks (using string quotes for forward references)
OnConnectCallback: TypeAlias = Callable[["RpcChannel"], Awaitable[None]]
OnDisconnectCallback: TypeAlias = Callable[["RpcChannel"], Awaitable[None]]
OnErrorCallback: TypeAlias = Callable[["RpcChannel", Exception], Awaitable[None]]

logger = get_logger("RPC_CHANNEL")


class DEFAULT_TIMEOUT:
    pass


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
        **kwargs: Any,
    ) -> None:
        """

        Args:
            methods (RpcMethodsBase): RPC methods to expose to other side
            socket: socket object providing simple send/recv methods
            channel_id (str, optional): uuid for channel. Defaults to None in
            which case a random UUID is generated.
            default_response_timeout(float, optional) default timeout for RPC
            call responses. Defaults to None - i.e. no timeout
            sync_channel_id(bool, optional) should get the other side of the
            channel id, helps to identify connections, cost a bit networking time.
                Defaults to False - i.e. not getting the other side channel id
            max_pending_requests (int, optional): Maximum number of pending RPC
            requests before backpressure is applied. Defaults to 1000.
            Prevents resource exhaustion from request flooding.
            max_connection_duration (float, optional): Maximum time in seconds
            that this channel can remain open. After this time, the connection
            will be gracefully closed. None means no limit. Defaults to None.
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
        #
        # convenience caller
        # TODO - pass remote methods object to support validation before call
        self.other = RpcCaller(self)
        # core event callback registers
        self._connect_handlers: list[OnConnectCallback] = []
        self._disconnect_handlers: list[OnDisconnectCallback] = []
        self._error_handlers: list[OnErrorCallback] = []

        # Connection duration tracking
        self._max_connection_duration = max_connection_duration
        self._connection_start_time = asyncio.get_event_loop().time()
        self._duration_watchdog_task: asyncio.Task[None] | None = None

        # Start duration watchdog if limit is set
        if self._max_connection_duration is not None:
            self._duration_watchdog_task = asyncio.create_task(
                self._connection_duration_watchdog()
            )

        # any other kwarg goes straight to channel context (Accessible to methods)
        self._context: dict[str, Any] = kwargs or {}
        logger.debug("RPC channel initialized.")

    @property
    def context(self) -> dict[str, Any]:
        return self._context

    async def get_other_channel_id(self) -> str:
        """
        Method to get the channel id of the other side of the channel
        The _channel_id_synced verify we have it
        Timeout exception can be raised if the value isn't available
        """
        await asyncio.wait_for(
            self._channel_id_synced.wait(), self.default_response_timeout
        )
        if self._other_channel_id is None:
            raise RemoteValueError("Other channel ID not available")
        return self._other_channel_id

    async def send(self, data: Any) -> None:
        """
        For internal use. wrap calls to underlying socket
        """
        await self.socket.send(data)

    async def send_raw(self, data: dict[str, Any]) -> None:
        """
        Send raw dictionary data as JSON
        """
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
        # Make this method idempotent - return immediately if already closed
        if self.is_closed():
            logger.debug(f"Channel {self.id} already closed, skipping")
            return None

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

    def register_connect_handler(
        self, coros: list[OnConnectCallback] | None = None
    ) -> None:
        """
        Register a connection handler callback that will be called (As an async
        task)) with the channel
        Args:
            coros (List[Coroutine]): async callback
        """
        if coros is not None:
            self._connect_handlers.extend(coros)

    def register_disconnect_handler(
        self, coros: list[OnDisconnectCallback] | None = None
    ) -> None:
        """
        Register a disconnect handler callback that will be called (As an async
        task)) with the channel id
        Args:
            coros (List[Coroutine]): async callback
        """
        if coros is not None:
            self._disconnect_handlers.extend(coros)

    def register_error_handler(
        self, coros: list[OnErrorCallback] | None = None
    ) -> None:
        """
        Register an error handler callback that will be called (As an async
        task)) with the channel and triggered error.
        Args:
            coros (List[Coroutine]): async callback
        """
        if coros is not None:
            self._error_handlers.extend(coros)

    async def on_handler_event(
        self,
        handlers: (
            list[OnConnectCallback] | list[OnDisconnectCallback] | list[OnErrorCallback]
        ),
        *args: Any,
        **kwargs: Any,
    ) -> None:
        await asyncio.gather(*(callback(*args, **kwargs) for callback in handlers))

    async def on_connect(self) -> None:
        """
        Run get other channel id if sync_channel_id is True
        Run all callbacks from self._connect_handlers
        """
        if self._sync_channel_id:
            logger.debug("Syncing channel ID...")
            self._get_other_channel_id_task = asyncio.create_task(
                self._get_other_channel_id()
            )
        await self.on_handler_event(self._connect_handlers, self)

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

        This method is idempotent - disconnect handlers are only called once.
        Subsequent calls will be no-ops.
        """
        # Check if already closed to prevent double-calling disconnect handlers
        if self.is_closed():
            logger.debug(f"Channel {self.id} already disconnected, skipping handlers")
            return

        # disconnect happened - mark the channel as closed
        self._promise_manager.close()
        await self.on_handler_event(self._disconnect_handlers, self)

    async def on_error(self, error: Exception) -> None:
        await self.on_handler_event(self._error_handlers, self, error)

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
        timeout: type[DEFAULT_TIMEOUT] | float | None = DEFAULT_TIMEOUT,
    ) -> JsonRpcResponse:
        """
        Call a method and wait for a response to be received.

        Args:
            name: Name of the method to call on the remote side
            args: Keyword arguments to pass to the method
            timeout: Maximum time to wait for response (seconds).
                    Can be DEFAULT_TIMEOUT (use channel default), a number
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

        # Convert DEFAULT_TIMEOUT to actual timeout value
        actual_timeout: float | None
        if timeout is DEFAULT_TIMEOUT:
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
        from .schemas import JsonRpcRequest

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

            # Trigger disconnect handlers
            await self.on_disconnect()

            # Close the channel
            await self.close()

        except asyncio.CancelledError:
            # Normal cancellation during shutdown
            logger.debug("Connection duration watchdog cancelled")
