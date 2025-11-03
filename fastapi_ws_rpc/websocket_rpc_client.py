"""
WebSocketRpcClient module provides a client that connects to a WebsocketRPCEndpoint
via websocket and enables bi-directional RPC calls.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from typing import Any

import tenacity
import websockets
from tenacity import retry, wait
from tenacity.retry import retry_if_exception
from websockets.exceptions import (
    ConnectionClosed,
    ConnectionClosedError,
    ConnectionClosedOK,
    InvalidStatus,
    WebSocketException,
)

from .logger import get_logger
from .rpc_channel import RpcChannel, on_connect_callback, on_disconnect_callback
from .rpc_methods import PING_RESPONSE, RpcMethodsBase
from .simplewebsocket import JsonSerializingWebSocket, SimpleWebSocket

logger = get_logger(__name__)


def isNotForbidden(value: Any) -> bool:
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
    RPC client for connecting to a WebsocketRPCEndpoint server.

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
        logger.exception(retry_state.outcome.exception())

    # Default retry configuration for connection attempts
    DEFAULT_RETRY_CONFIG = {
        "wait": wait.wait_random_exponential(min=0.1, max=120),
        "retry": retry_if_exception(isNotForbidden),
        "reraise": True,
        "retry_error_callback": logerror,
    }

    # RPC ping check settings for verifying connection after establishment
    WAIT_FOR_INITIAL_CONNECTION = 1  # seconds
    MAX_CONNECTION_ATTEMPTS = 5

    def __init__(
        self,
        uri: str,
        methods: RpcMethodsBase | None = None,
        retry_config: dict | bool | None = None,
        default_response_timeout: float | None = None,
        on_connect: list[on_connect_callback] | None = None,
        on_disconnect: list[on_disconnect_callback] | None = None,
        keep_alive: float = 0,
        serializing_socket_cls: type[SimpleWebSocket] = JsonSerializingWebSocket,
        **kwargs,
    ):
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
        self.connect_kwargs = kwargs

        # Set default timeout if not specified
        self.connect_kwargs.setdefault("open_timeout", 30)

        # State variables
        self.ws = None
        self.channel = None
        self._read_task = None
        self._keep_alive_task = None
        self._connection_closed = asyncio.Event()

        # Configuration
        self._keep_alive_interval = keep_alive
        self.default_response_timeout = default_response_timeout

        # Process retry configuration
        if retry_config is False:
            self.retry_config = None  # Disable retries
        elif retry_config is None:
            self.retry_config = self.DEFAULT_RETRY_CONFIG  # Use defaults
        else:
            self.retry_config = retry_config  # Use custom config

        # Event handlers
        self._on_disconnect = on_disconnect or []
        self._on_connect = on_connect or []

        # Serialization
        self._serializing_socket_cls = serializing_socket_cls

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
            try:
                logger.info(f"Connecting to {self.uri}...")

                # Create WebSocket connection
                logger.debug(
                    f"Creating WebSocket connection to {self.uri} with parameters: {self.connect_kwargs}"
                )
                raw_ws = await websockets.connect(self.uri, **self.connect_kwargs)

                # Wrap with serialization
                logger.debug(
                    f"Wrapping WebSocket with {self._serializing_socket_cls.__name__}"
                )
                self.ws = self._serializing_socket_cls(raw_ws)

                # Create RPC channel
                self.channel = RpcChannel(
                    self.methods,
                    self.ws,
                    default_response_timeout=self.default_response_timeout,
                )

                # Register handlers
                self.channel.register_connect_handler(self._on_connect)
                self.channel.register_disconnect_handler(self._on_disconnect)

                # Start reader task
                self._read_task = asyncio.create_task(self.reader())

                # Start keep-alive if enabled
                self._start_keep_alive_task()

                # Verify RPC is responsive
                await self.wait_on_rpc_ready()

                # Trigger connect callbacks
                await self.channel.on_connect()

                return self
            except Exception:
                # Clean up partial initialization on error
                await self._cleanup_partial_init()
                raise

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
        except Exception:
            logger.exception("RPC Error")
            raise

    async def _cleanup_partial_init(self) -> None:
        """
        Clean up resources after a failed connection attempt.
        """
        if self.ws is not None:
            with suppress(Exception):
                await self.ws.close()

        if self.channel is not None:
            with suppress(Exception):
                await self.channel.close()

        self.cancel_tasks()

    async def __aenter__(self) -> WebSocketRpcClient:
        """
        Async context manager entry.

        Establishes connection with retry logic if configured.

        Returns:
            WebSocketRpcClient: The connected client instance.
        """
        if self.retry_config is False or self.retry_config is None:
            return await self.__connect__()
        else:
            connect_with_retry = retry(**self.retry_config)(self.__connect__)
            return await connect_with_retry()

    async def __aexit__(self, *args, **kwargs) -> None:
        """
        Async context manager exit.

        Ensures clean shutdown of the connection and associated resources.
        """
        await self.close()

    async def close(self) -> None:
        """
        Close the WebSocket connection and clean up resources.

        This method:
        1. Closes the WebSocket connection if open
        2. Notifies disconnect handlers
        3. Cancels background tasks
        """
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
            except Exception:
                logger.exception("Error during channel closure")

        # Clear tasks
        self.cancel_tasks()

    def cancel_tasks(self) -> None:
        """
        Cancel all background tasks (keep-alive and reader).
        """
        # Stop keep-alive if enabled
        self._cancel_keep_alive_task()

        # Stop reader task
        self.cancel_reader_task()

    def cancel_reader_task(self) -> None:
        """
        Cancel the reader task if it exists.
        """
        if self._read_task is not None:
            self._read_task.cancel()
            self._read_task = None

    async def reader(self) -> None:
        """
        Background task that continuously reads messages from the WebSocket.

        This task runs until the connection is closed or cancelled.
        Each received message is processed through the RPC channel.
        """
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

        except Exception:
            logger.exception("RPC reader task failed.")
            self._connection_closed.set()
            raise

    async def _keep_alive(self) -> None:
        """
        Background task that sends periodic ping messages to keep the connection alive.

        This task runs at the interval specified by _keep_alive_interval.
        """
        try:
            while True:
                await asyncio.sleep(self._keep_alive_interval)
                answer = await self.ping()
                if (
                    not answer
                    or not hasattr(answer, "result")
                    or answer.result != PING_RESPONSE
                ):
                    logger.warning(f"Unexpected ping response: {answer}")

        except asyncio.CancelledError:
            logger.debug("Keep-alive task cancelled")

        except Exception:
            logger.exception("Keep-alive task failed")

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

            except Exception as e:
                logger.warning(f"RPC ready check failed: {e}")
                attempt_count += 1

    async def ping(self):
        """
        Send a ping request to the server and wait for a response.

        Returns:
            The response object from the server's ping method.
        """
        logger.debug("Pinging server...")
        answer = await self.channel.other.ping()
        logger.debug(f"Got ping response: {answer}")
        return answer

    def _cancel_keep_alive_task(self) -> None:
        """
        Cancel the keep-alive task if it exists.
        """
        if self._keep_alive_task is not None:
            logger.debug("Cancelling keep-alive task")
            self._keep_alive_task.cancel()
            self._keep_alive_task = None

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
        self, name: str, args: dict | None = None, timeout: float | None = None
    ):
        """
        Call a remote method on the server and wait for the response.

        Args:
            name: Name of the method to call (as defined on the server's RpcMethods)
            args: Keyword arguments to pass to the remote method
            timeout: Optional custom timeout for this specific call

        Returns:
            The result returned by the remote method

        Raises:
            Various exceptions from the underlying RPC channel or if the connection
            is closed.
        """
        args = args or {}
        return await self.channel.call(name, args, timeout=timeout)

    @property
    def other(self):
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
