"""
Protocol definitions for breaking circular dependencies.

These protocols define interfaces that allow components to depend on abstractions
rather than concrete implementations, enabling loose coupling and better testability.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from ..schemas import JsonRpcResponse


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
