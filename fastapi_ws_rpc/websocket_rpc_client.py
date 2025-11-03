"""
WebSocketRpcClient module provides a client that connects to a WebSocketRpcEndpoint
via websocket and enables bi-directional RPC calls.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from typing import Any

import tenacity
import websockets
from pydantic import ValidationError
from tenacity import retry, wait
from tenacity.retry import retry_if_exception
from websockets.exceptions import (
    ConnectionClosed,
    ConnectionClosedError,
    ConnectionClosedOK,
    InvalidStatus,
    WebSocketException,
)

from .exceptions import (
    RpcBackpressureError,
    RpcChannelClosedError,
    RpcInvalidStateError,
)
from .logger import get_logger
from .rpc_channel import OnConnectCallback, OnDisconnectCallback, RpcChannel
from .rpc_methods import PING_RESPONSE, RpcMethodsBase
from .simplewebsocket import JsonSerializingWebSocket, SimpleWebSocket

logger = get_logger(__name__)


def is_not_forbidden(value: Any) -> bool:
    """
    Check if the exception is not an authorization-related status code.

    Args:
        value: The exception to check.

    Returns:
        bool: True if the exception is not an authorization-related status code,
              False otherwise.
    """
    return not (
        isinstance(value, InvalidStatus)
        and hasattr(value, "status_code")
        and (value.status_code == 401 or value.status_code == 403)
    )


class WebSocketRpcClient:
    """
    RPC client for connecting to a WebSocketRpcEndpoint server.

    This client manages the WebSocket connection lifecycle, enables bidirectional
    RPC communication, and handles automatic reconnection with configurable retry
    policies.

    The client can:
    - Call methods exposed by the server
    - Expose methods that the server can call
    - Automatically reconnect with exponential backoff
    - Maintain connection with keep-alive pings
    """

    @staticmethod
    def logerror(retry_state: tenacity.RetryCallState) -> None:
        """
        Log exception details during retry attempts.

        Args:
            retry_state: The current retry state containing exception information.
        """
        outcome = retry_state.outcome
        if outcome is not None:
            logger.exception(outcome.exception())

    # Default retry configuration for connection attempts
    DEFAULT_RETRY_CONFIG = {
        "wait": wait.wait_random_exponential(min=0.1, max=120),
        "retry": retry_if_exception(is_not_forbidden),
        "reraise": True,
        "retry_error_callback": logerror,
    }

    # RPC ping check settings for verifying connection after establishment
    WAIT_FOR_INITIAL_CONNECTION = 1  # seconds
    MAX_CONNECTION_ATTEMPTS = 5
    # Keep-alive failure threshold
    DEFAULT_MAX_CONSECUTIVE_PING_FAILURES = 3

    def __init__(
        self,
        uri: str,
        methods: RpcMethodsBase | None = None,
        retry_config: dict[str, Any] | bool | None = None,
        default_response_timeout: float | None = None,
        on_connect: list[OnConnectCallback] | None = None,
        on_disconnect: list[OnDisconnectCallback] | None = None,
        keep_alive: float = 0,
        max_message_size: int | None = None,
        max_pending_requests: int | None = None,
        max_consecutive_ping_failures: int | None = None,
        max_connection_duration: float | None = None,
        serializing_socket_cls: type[SimpleWebSocket] = JsonSerializingWebSocket,
        **kwargs: Any,
    ) -> None:
        """
        Initialize the WebSocketRpcClient.

        Args:
            uri: Server URI to connect to (e.g., 'ws://localhost/ws/client1')
            methods: RPC methods to expose to the server. Defaults to an empty
                RpcMethodsBase.
            retry_config: Configuration for tenacity retries. Can be:
                - None: Use DEFAULT_RETRY_CONFIG
                - False: Disable retries
                - Dict: Custom retry configuration
            default_response_timeout: Default timeout in seconds for RPC responses.
            on_connect: Callbacks executed when connection is established.
            on_disconnect: Callbacks executed when connection is lost.
            keep_alive: Interval in seconds to send keep-alive pings.
                        0 disables keep-alive.
            max_message_size: Maximum allowed message size in bytes (default 10MB).
                            Prevents DoS attacks via extremely large JSON payloads.
            max_pending_requests: Maximum number of pending RPC requests (default 1000).
                                Prevents resource exhaustion from request flooding.
            max_consecutive_ping_failures: Maximum consecutive ping failures before
                                          closing connection (default 3). Set to None
                                          to disable auto-disconnect on ping failures.
            max_connection_duration: Maximum time in seconds that the connection
                                    can remain open. After this time, the connection
                                    will be gracefully closed. None means no limit.
            serializing_socket_cls: Class for serializing/deserializing messages.
            **kwargs: Additional arguments passed to websockets.connect()
                      See: https://websockets.readthedocs.io/en/stable/reference/asyncio/client.html

        Usage:
            ```python
            async with WebSocketRpcClient(uri, RpcUtilityMethods()) as client:
                response = await client.call("echo", {'text': "Hello World!"})
                print(response)
            ```
        """
        self.uri = uri
        self.methods = methods or RpcMethodsBase()
        self.connect_kwargs: dict[str, Any] = kwargs
        self._max_message_size = max_message_size
        self._max_pending_requests = max_pending_requests
        self._max_connection_duration = max_connection_duration

        # Set default timeout if not specified
        self.connect_kwargs.setdefault("open_timeout", 30)

        # State variables
        self.ws: SimpleWebSocket | None = None
        self.channel: RpcChannel | None = None
        self._read_task: asyncio.Task[None] | None = None
        self._keep_alive_task: asyncio.Task[None] | None = None
        self._connection_closed = asyncio.Event()
        self._closing = False  # Flag to make close() idempotent

        # Configuration
        self._keep_alive_interval = keep_alive
        self.default_response_timeout = default_response_timeout
        self._max_consecutive_ping_failures = (
            max_consecutive_ping_failures
            if max_consecutive_ping_failures is not None
            else self.DEFAULT_MAX_CONSECUTIVE_PING_FAILURES
        )
        self._consecutive_ping_failures = 0

        # Process retry configuration
        self.retry_config: dict[str, Any] | None
        if retry_config is False:
            self.retry_config = None  # Disable retries
        elif retry_config is None:
            self.retry_config = self.DEFAULT_RETRY_CONFIG  # Use defaults
        else:
            # Ensure it's a dict, not bool True
            self.retry_config = retry_config if isinstance(retry_config, dict) else None

        # Event handlers
        self._on_disconnect: list[OnDisconnectCallback] = on_disconnect or []
        self._on_connect: list[OnConnectCallback] = on_connect or []

        # Serialization
        self._serializing_socket_cls = serializing_socket_cls

    def _validate_connected(self) -> None:
        """
        Validate that the client is connected and operational.

        This method checks that all necessary components are initialized
        and the connection is in a valid state for RPC operations.

        Raises:
            RpcInvalidStateError: If WebSocket or RPC channel is not initialized,
                                 or if the WebSocket is closed.

        Usage:
            This should be called at the start of any method that requires
            an active connection (e.g., ping(), call(), reader()).
        """
        if self.ws is None:
            raise RpcInvalidStateError(
                "WebSocket not initialized. "
                "Call __aenter__() or await client.__connect__() first to establish connection."
            )
        if self.channel is None:
            raise RpcInvalidStateError(
                "RPC channel not initialized. "
                "Call __aenter__() or await client.__connect__() first to establish connection."
            )
        # Check if WebSocket is closed (websockets library sets .closed attribute)
        if getattr(self.ws, "closed", False):
            raise RpcInvalidStateError(
                "WebSocket connection is closed. "
                "Create a new WebSocketRpcClient instance to reconnect."
            )

    async def __connect__(self) -> WebSocketRpcClient:
        """
        Internal method to establish the WebSocket connection and initialize the RPC
        channel.

        This performs the actual connection steps:
        1. Create WebSocket connection
        2. Wrap it with serialization
        3. Set up the RPC channel
        4. Start reading messages
        5. Start keep-alive if enabled
        6. Check that RPC layer is responsive
        7. Trigger connect callbacks

        Returns:
            WebSocketRpcClient: Self reference for chaining.

        Raises:
            Various exceptions from websockets.connect() or during initialization.
        """
        try:
            logger.info(f"Connecting to {self.uri}...")

            # Step 1: Create WebSocket connection
            # This can raise connection-related exceptions
            logger.debug(
                f"Creating WebSocket connection to {self.uri} with parameters: {self.connect_kwargs}"
            )
            raw_ws = await websockets.connect(self.uri, **self.connect_kwargs)

            # Step 2-4: Initialize RPC layer (wrap socket, create channel, register handlers)
            # This can raise validation/initialization errors, so we wrap in try-except
            try:
                logger.debug(
                    f"Wrapping WebSocket with {self._serializing_socket_cls.__name__}"
                )
                # Pass max_message_size if specified, otherwise use default
                if self._max_message_size is not None:
                    self.ws = self._serializing_socket_cls(raw_ws, max_message_size=self._max_message_size)  # type: ignore[call-arg]
                else:
                    self.ws = self._serializing_socket_cls(raw_ws)  # type: ignore[call-arg]

                # Create RPC channel with production hardening parameters
                self.channel = RpcChannel(
                    self.methods,
                    self.ws,
                    default_response_timeout=self.default_response_timeout,
                    max_pending_requests=self._max_pending_requests,
                    max_connection_duration=self._max_connection_duration,
                )

                # Register handlers
                self.channel.register_connect_handler(self._on_connect)
                self.channel.register_disconnect_handler(self._on_disconnect)

            except (ValidationError, ValueError, TypeError) as e:
                # Clean up on RPC initialization errors
                logger.error(f"RPC initialization failed: {type(e).__name__}: {e}")
                await self._cleanup_partial_init()
                raise

            # Step 5-6: Start tasks and verify connection
            # These use the initialized channel, so failures here need cleanup
            try:
                # Start reader task
                self._read_task = asyncio.create_task(self.reader())

                # Start keep-alive if enabled
                self._start_keep_alive_task()

                # Verify RPC is responsive
                await self.wait_on_rpc_ready()

            except Exception as e:
                # Clean up on post-init failures
                logger.error(f"Connection verification failed: {type(e).__name__}: {e}")
                await self._cleanup_partial_init()
                raise

            # Reset ping failure counter on new connection
            self._consecutive_ping_failures = 0

            # Step 7: Trigger connect callbacks
            # This can raise exceptions from user callbacks
            try:
                await self.channel.on_connect()
            except Exception as e:
                # Log but don't fail connection if user callbacks fail
                logger.error(f"Connect callback failed: {type(e).__name__}: {e}")
                # We could raise here if we want strict callback handling

            return self

        except ConnectionRefusedError:
            logger.info("RPC connection was refused by server")
            raise
        except ConnectionClosedError:
            logger.info("RPC connection lost")
            raise
        except ConnectionClosedOK:
            logger.info("RPC connection closed")
            raise
        except InvalidStatus as err:
            status_code = getattr(err, "status_code", None) or getattr(
                err, "code", None
            )
            logger.info(f"RPC WebSocket failed with invalid status code {status_code}")
            raise
        except WebSocketException as err:
            logger.info(f"RPC WebSocket failed with {err}")
            raise
        except OSError as err:
            logger.info(f"RPC Connection failed - {err}")
            raise
        except (ValidationError, ValueError, TypeError, RuntimeError) as err:
            # Catch data validation and processing errors during connection
            logger.exception(f"RPC Error: {type(err).__name__}")
            raise

    async def _cleanup_partial_init(self) -> None:
        """
        Clean up resources after a failed connection attempt.

        This method is async to properly await task cancellations.
        """
        if self.ws is not None:
            with suppress(Exception):
                await self.ws.close()

        if self.channel is not None:
            with suppress(Exception):
                await self.channel.close()

        # Cancel tasks (now async)
        await self.cancel_tasks()

    async def __aenter__(self) -> WebSocketRpcClient:
        """
        Async context manager entry.

        Establishes connection with retry logic if configured.

        Returns:
            WebSocketRpcClient: The connected client instance.
        """
        if self.retry_config is None:
            return await self.__connect__()
        connect_with_retry = retry(**self.retry_config)(self.__connect__)
        return await connect_with_retry()  # type: ignore[no-any-return]

    async def __aexit__(self, *args: Any, **kwargs: Any) -> None:
        """
        Async context manager exit.

        Ensures clean shutdown of the connection and associated resources.
        """
        await self.close()

    async def close(self) -> None:
        """
        Close the WebSocket connection and clean up resources.

        This method is idempotent and can be safely called multiple times.

        This method:
        1. Checks if already closing (prevents race conditions)
        2. Closes the WebSocket connection if open
        3. Notifies disconnect handlers
        4. Cancels background tasks
        """
        # Check if already closing (make this method idempotent)
        if self._closing:
            logger.debug("Close already in progress, skipping duplicate call")
            return

        self._closing = True
        logger.info("Closing RPC client...")

        # Signal that connection is closed
        self._connection_closed.set()

        # Close underlying connection
        if self.ws is not None:
            with suppress(RuntimeError, ConnectionClosed, WebSocketException):
                await self.ws.close()

        # Notify callbacks (but just once)
        if self.channel and not self.channel.is_closed():
            try:
                # Notify handlers
                await self.channel.on_disconnect()
                await self.channel.close()
            except (RuntimeError, ValueError, TypeError, AttributeError) as e:
                # Log but don't raise - this is cleanup code that should not prevent shutdown
                logger.exception(f"Error during channel closure: {type(e).__name__}")

        # Clear tasks (now async)
        await self.cancel_tasks()

    async def cancel_tasks(self) -> None:
        """
        Cancel all background tasks (keep-alive and reader) and wait for cleanup.

        This method is async to properly await task cancellations, ensuring
        clean shutdown without warnings about un-awaited coroutines.
        """
        # Stop keep-alive if enabled
        await self._cancel_keep_alive_task()

        # Stop reader task
        await self.cancel_reader_task()

    async def cancel_reader_task(self) -> None:
        """
        Cancel the reader task if it exists and wait for cancellation to complete.

        This ensures proper cleanup and prevents warnings about un-awaited
        cancellation exceptions.
        """
        if self._read_task is not None:
            logger.debug("Cancelling reader task...")
            self._read_task.cancel()

            # Wait for cancellation to complete (suppress CancelledError)
            with suppress(asyncio.CancelledError):
                await self._read_task

            self._read_task = None
            logger.debug("Reader task cancelled successfully")

    async def reader(self) -> None:
        """
        Background task that continuously reads messages from the WebSocket.

        This task runs until the connection is closed or cancelled.
        Each received message is processed through the RPC channel.

        Raises:
            RpcInvalidStateError: If WebSocket or channel is not properly initialized.
        """
        # Validate connection state before starting reader loop
        self._validate_connected()

        # After validation, ws and channel are guaranteed to be non-None
        assert self.ws is not None, "WebSocket must be connected"
        assert self.channel is not None, "Channel must be initialized"

        try:
            while True:
                raw_message = await self.ws.recv()
                logger.debug("Received message via reader")
                await self.channel.on_message(raw_message)

        except asyncio.CancelledError:
            logger.info("RPC read task was cancelled.")
            self._connection_closed.set()

        except ConnectionClosed:
            logger.info("Connection was terminated.")
            self._connection_closed.set()
            await self.close()

        except (ValidationError, ValueError, TypeError, RuntimeError, KeyError) as e:
            # Message processing errors in the reader loop
            logger.exception(f"RPC reader task failed: {type(e).__name__}")
            self._connection_closed.set()
            raise

    async def _keep_alive(self) -> None:
        """
        Background task that sends periodic ping messages to keep the connection alive.

        This task runs at the interval specified by _keep_alive_interval.
        Tracks consecutive ping failures and closes the connection after
        max_consecutive_ping_failures threshold is reached.
        """
        try:
            while True:
                await asyncio.sleep(self._keep_alive_interval)

                try:
                    # Send ping with timeout
                    ping_timeout = min(self._keep_alive_interval / 2, 10.0)
                    answer = await asyncio.wait_for(self.ping(), timeout=ping_timeout)

                    # Validate response
                    if (
                        not answer
                        or not hasattr(answer, "result")
                        or answer.result != PING_RESPONSE
                    ):
                        self._consecutive_ping_failures += 1
                        logger.warning(
                            f"Keepalive ping returned unexpected response "
                            f"(failure {self._consecutive_ping_failures}/"
                            f"{self._max_consecutive_ping_failures}): {answer}"
                        )
                    else:
                        # Success - reset failure counter
                        if self._consecutive_ping_failures > 0:
                            logger.debug(
                                f"Keepalive recovered after "
                                f"{self._consecutive_ping_failures} failures"
                            )
                        self._consecutive_ping_failures = 0
                        logger.debug("Keepalive ping successful")

                except asyncio.TimeoutError:
                    self._consecutive_ping_failures += 1
                    logger.warning(
                        f"Keepalive ping timed out "
                        f"(failure {self._consecutive_ping_failures}/"
                        f"{self._max_consecutive_ping_failures})"
                    )

                except (RuntimeError, ConnectionError, ValueError) as e:
                    self._consecutive_ping_failures += 1
                    logger.warning(
                        f"Keepalive ping failed with {type(e).__name__}: {e} "
                        f"(failure {self._consecutive_ping_failures}/"
                        f"{self._max_consecutive_ping_failures})"
                    )

                # Check if we've exceeded max consecutive failures
                if (
                    self._consecutive_ping_failures
                    >= self._max_consecutive_ping_failures
                ):
                    logger.error(
                        f"Keepalive exceeded maximum consecutive failures "
                        f"({self._max_consecutive_ping_failures}), closing connection"
                    )
                    # Close the connection - this will trigger cleanup
                    await self.close()
                    break

        except asyncio.CancelledError:
            logger.debug("Keep-alive task cancelled")

    async def wait_on_rpc_ready(self) -> None:
        """
        Verify that the RPC channel is ready by sending test pings.

        This confirms not just socket connectivity but RPC functionality.
        Makes multiple attempts with timeout before giving up.
        """
        received_response = None
        attempt_count = 0

        while (
            received_response is None and attempt_count < self.MAX_CONNECTION_ATTEMPTS
        ):
            try:
                logger.debug(
                    f"RPC ready check attempt {attempt_count + 1}/{self.MAX_CONNECTION_ATTEMPTS}"
                )
                received_response = await asyncio.wait_for(
                    self.ping(), self.WAIT_FOR_INITIAL_CONNECTION
                )
                logger.debug(f"RPC ready check succeeded: {received_response}")

            except asyncio.TimeoutError:
                attempt_count += 1
                logger.debug(f"RPC ready check timed out (attempt {attempt_count})")

            except (
                RpcChannelClosedError,
                RpcBackpressureError,
                RpcInvalidStateError,
                ConnectionError,
                OSError,
            ) as e:
                # Catch expected errors during initial connection establishment
                # Don't catch system exceptions (KeyboardInterrupt, SystemExit, etc.)
                logger.warning(f"RPC ready check failed: {e}")
                attempt_count += 1

    async def ping(self) -> Any:
        """
        Send a ping request to the server and wait for a response.

        Returns:
            The response object from the server's ping method.

        Raises:
            RpcInvalidStateError: If the RPC channel is not initialized.
        """
        # Validate that channel is initialized
        self._validate_connected()

        logger.debug("Pinging server...")
        answer = await self.channel.other.ping()  # type: ignore[union-attr]
        logger.debug(f"Got ping response: {answer}")
        return answer

    async def _cancel_keep_alive_task(self) -> None:
        """
        Cancel the keep-alive task if it exists and wait for cancellation to complete.

        This ensures proper cleanup and prevents warnings about un-awaited
        cancellation exceptions.
        """
        if self._keep_alive_task is not None:
            logger.debug("Cancelling keep-alive task...")
            self._keep_alive_task.cancel()

            # Wait for cancellation to complete
            with suppress(asyncio.CancelledError):
                await self._keep_alive_task

            self._keep_alive_task = None
            logger.debug("Keep-alive task cancelled successfully")

    def _start_keep_alive_task(self) -> None:
        """
        Start the keep-alive task if keep_alive_interval > 0.
        """
        if self._keep_alive_interval > 0:
            logger.debug(
                f"Starting keep-alive task (interval: {self._keep_alive_interval}s)"
            )
            self._keep_alive_task = asyncio.create_task(self._keep_alive())
        else:
            logger.debug("Keep-alive is disabled")

    async def wait_on_reader(self) -> None:
        """
        Wait for the reader task to complete.

        Useful for graceful shutdown or waiting for processing to finish.
        """
        if self._read_task:
            try:
                await self._read_task
            except asyncio.CancelledError:
                logger.info("RPC Reader task was cancelled.")

    async def call(
        self,
        name: str,
        args: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> Any:
        """
        Call a remote method on the server and wait for the response.

        Args:
            name: Name of the method to call (as defined on the server's RpcMethods)
            args: Keyword arguments to pass to the remote method
            timeout: Optional custom timeout for this specific call

        Returns:
            The result returned by the remote method

        Raises:
            RpcInvalidStateError: If the RPC channel is not initialized.
            Various exceptions from the underlying RPC channel or if the connection
            is closed.
        """
        # Validate that channel is initialized
        self._validate_connected()

        args = args or {}
        return await self.channel.call(name, args, timeout=timeout)  # type: ignore[union-attr]

    @property
    def other(self) -> Any:
        """
        Proxy object for calling remote methods.

        This allows convenient attribute-style access to remote methods:
        ```python
        result = await client.other.remote_method(param1=value1)
        ```

        Returns:
            RpcCaller: Proxy object for remote method calls
        """
        return self.channel.other if self.channel else None

    @property
    def is_connected(self) -> bool:
        """
        Check if the client is currently connected.

        Returns:
            bool: True if connected and operational, False otherwise
        """
        return (
            self.ws is not None
            and not getattr(self.ws, "closed", False)
            and self.channel is not None
            and not self.channel.is_closed()
            and self._read_task is not None
            and not self._read_task.done()
        )

    @property
    def is_closed_event(self) -> asyncio.Event:
        """
        Event that is set when the connection is closed.

        This event is set when:
        - The reader task detects a ConnectionClosed exception
        - The reader task is cancelled
        - The close() method is called
        - Any exception occurs in the reader task

        Returns:
            asyncio.Event: Event that will be set when the connection closes.
        """
        return self._connection_closed
