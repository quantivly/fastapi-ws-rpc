"""
Protocol definitions for breaking circular dependencies.

These protocols define interfaces that allow components to depend on abstractions
rather than concrete implementations, enabling loose coupling and better testability.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from ..schemas import JsonRpcResponse


@runtime_checkable
class SocketProtocol(Protocol):
    """
    Protocol defining the interface for WebSocket-like objects.

    This protocol defines the minimal interface required for WebSocket objects
    used by the RPC system. It ensures consistent behavior across different
    WebSocket implementations (websockets library, FastAPI WebSocket, etc.).

    The protocol is runtime-checkable, allowing isinstance() checks for validation.

    Examples
    --------
    Check if an object implements the socket protocol:

    >>> if isinstance(my_socket, SocketProtocol):
    ...     await my_socket.send("message")

    Type hint a function accepting any socket implementation:

    >>> async def handle_socket(socket: SocketProtocol) -> None:
    ...     message = await socket.recv()
    ...     await socket.send(f"Echo: {message}")
    ...     await socket.close()

    See Also
    --------
    SimpleWebSocket : Abstract base class that implements this protocol.
    JsonSerializingWebSocket : Concrete implementation with JSON serialization.
    """

    async def send(self, message: Any) -> None:
        """
        Send a message over the socket.

        Parameters
        ----------
        message : Any
            The message to send. Type depends on the implementation
            (e.g., str for text frames, bytes for binary frames, or serialized objects).

        Notes
        -----
        This method should handle any necessary buffering or encoding internally.
        If the socket is closed, this should raise an appropriate exception.
        """
        ...

    async def recv(self) -> Any:
        """
        Receive a message from the socket.

        Returns
        -------
        Any
            The received message. Type depends on the implementation
            (e.g., str, bytes, or deserialized objects).
            This method blocks until a message is available.

        Notes
        -----
        This method should handle any necessary buffering or decoding internally.
        If the socket is closed or an error occurs, it should raise an appropriate exception.
        """
        ...

    async def close(self, code: int = 1000) -> None:
        """
        Close the socket connection.

        Parameters
        ----------
        code : int, optional
            WebSocket close code (default is 1000 for normal closure).
            Standard close codes include:
            - 1000: Normal closure
            - 1001: Going away
            - 1002: Protocol error
            - 1003: Unsupported data
            - 1011: Internal error

        Notes
        -----
        After calling close(), no further send() or recv() calls should be made.
        The implementation should ensure proper cleanup of resources.
        """
        ...

    @property
    def closed(self) -> bool:
        """
        Check if the socket is closed.

        Returns
        -------
        bool
            True if the socket is closed, False if it's open.

        Notes
        -----
        This property should reflect the actual state of the connection.
        It should return True after close() is called or if the connection
        was closed by the remote endpoint.
        """
        ...


class RpcPromise(Protocol):
    """Protocol for RPC promise objects that track pending requests."""

    @property
    def call_id(self) -> str:
        """Get the unique identifier for this RPC call."""
        ...

    async def wait(self) -> None:
        """Wait for the promise to be fulfilled."""
        ...

    def set(self) -> None:
        """Mark the promise as fulfilled."""
        ...


class RpcCallable(Protocol):
    """
    Protocol for objects that can make RPC calls.

    This protocol defines the interface for making remote procedure calls.
    Both RpcChannel and any mock/test implementations can conform to this protocol.

    Example:
        ```python
        async def make_remote_call(rpc: RpcCallable) -> None:
            # Works with any RpcCallable implementation
            response = await rpc.call("remote_method", {"arg": "value"})
            print(response.result)
        ```
    """

    @property
    def id(self) -> str:
        """Get the unique identifier for this RPC channel."""
        ...

    async def async_call(
        self,
        name: str,
        args: dict[str, Any] | None = None,
        call_id: str | None = None,
    ) -> Any:
        """
        Make an asynchronous RPC call and return a promise.

        Args:
            name: The name of the remote method to call
            args: Optional dictionary of named arguments
            call_id: Optional call ID (generated if not provided)

        Returns:
            A promise object that can be awaited for the response

        Raises:
            ValueError: If the call_id is already in use
        """
        ...

    async def call(
        self,
        name: str,
        args: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> JsonRpcResponse:
        """
        Make a blocking RPC call and wait for the response.

        Args:
            name: The name of the remote method to call
            args: Optional dictionary of named arguments
            timeout: Optional timeout in seconds (uses default if None)

        Returns:
            The JSON-RPC response from the remote method

        Raises:
            RpcChannelClosedError: If the channel closes before response
            asyncio.TimeoutError: If the timeout expires
        """
        ...

    async def notify(
        self,
        name: str,
        args: dict[str, Any] | None = None,
    ) -> None:
        """
        Send a notification (fire-and-forget RPC call with no response expected).

        Args:
            name: The name of the remote method to call
            args: Optional dictionary of named arguments
        """
        ...

    def get_saved_response(self, call_id: str) -> JsonRpcResponse:
        """
        Retrieve a saved response for a callback pattern.

        Args:
            call_id: The call ID of the saved response

        Returns:
            The saved JSON-RPC response

        Raises:
            KeyError: If no response is saved for the given call_id
        """
        ...

    def clear_saved_call(self, call_id: str) -> None:
        """
        Clear a saved call/response from memory.

        Args:
            call_id: The call ID to clear
        """
        ...


class MethodInvokable(Protocol):
    """
    Protocol for objects that can invoke local methods.

    This protocol defines the interface for executing local RPC methods.
    It's primarily used by RpcChannel to invoke methods on RpcMethodsBase.

    Example:
        ```python
        async def process_request(methods: MethodInvokable, method_name: str) -> Any:
            # Works with any MethodInvokable implementation
            result = await methods.invoke_method(method_name, {"param": "value"})
            return result
        ```
    """

    async def invoke_method(
        self,
        method_name: str,
        params: dict[str, Any] | list[Any] | None,
    ) -> Any:
        """
        Invoke a local method with the given parameters.

        Args:
            method_name: The name of the method to invoke
            params: Method parameters (dict for named, list for positional, None for no params)

        Returns:
            The return value from the invoked method

        Raises:
            TypeError: If the parameters are invalid for the method signature
            Exception: Any exception raised by the invoked method
        """
        ...
