"""
Caller components for invoking remote RPC methods.

This module provides proxy classes for calling remote methods on the other
side of an RPC channel using a convenient attribute-based syntax.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ..logger import get_logger
from ..rpc_methods import EXPOSED_BUILT_IN_METHODS

if TYPE_CHECKING:
    from collections.abc import Awaitable

    from ..rpc_channel import RpcChannel

logger = get_logger("RPC_CALLER")


class RpcProxy:
    """
    Provides a proxy to a remote method on the other side of the channel.

    This class wraps a remote method call, allowing it to be invoked with
    keyword arguments and returning an awaitable response.

    Example:
        ```python
        proxy = RpcProxy(channel, "add")
        result = await proxy(a=1, b=2)
        ```
    """

    def __init__(self, channel: RpcChannel, method_name: str) -> None:
        """
        Initialize the RPC proxy.

        Args:
            channel: The RPC channel to use for the call
            method_name: Name of the remote method to call
        """
        self.channel = channel
        self.method_name = method_name

    def __call__(self, **kwargs: Any) -> Awaitable[Any]:
        """
        Call the remote method with the given keyword arguments.

        Args:
            **kwargs: Keyword arguments to pass to the remote method

        Returns:
            Awaitable that resolves to the return value of the remote method

        Example:
            ```python
            result = await proxy(x=10, y=20)
            ```
        """
        logger.debug("Calling RPC method: %s", self.method_name)
        return self.channel.call(self.method_name, args=kwargs)


class RpcCaller:
    """
    Calls remote methods on the other side of the channel.

    This class provides a convenient attribute-based interface for calling
    remote methods. Any attribute access that doesn't start with "_" (or is
    in the list of exposed built-in methods) returns an RpcProxy for that method.

    Example:
        ```python
        caller = RpcCaller(channel)
        result = await caller.my_method(arg1=value1)
        ```
    """

    def __init__(self, channel: RpcChannel, methods: Any = None) -> None:
        """
        Initialize the RPC caller.

        Args:
            channel: The RPC channel to use for calls
            methods: Optional methods object (unused, kept for compatibility)
        """
        self._channel = channel

    def __getattribute__(self, name: str) -> Any:
        """
        Return an RpcProxy instance for exposed RPC methods.

        This method intercepts attribute access to provide dynamic proxy
        creation for remote method calls. Methods starting with "_" are
        considered private unless they're in the EXPOSED_BUILT_IN_METHODS list.

        Args:
            name: Name of the requested attribute (or RPC method)

        Returns:
            RpcProxy for remote methods, or the actual attribute for private names

        Example:
            ```python
            # Public method - returns proxy
            caller.my_method  # -> RpcProxy(channel, "my_method")

            # Built-in method - returns proxy
            caller.ping  # -> RpcProxy(channel, "ping")

            # Private attribute - returns actual attribute
            caller._channel  # -> actual _channel attribute
            ```
        """
        is_exposed = not name.startswith("_") or name in EXPOSED_BUILT_IN_METHODS
        if is_exposed:
            logger.debug("%s was detected to be a remote RPC method.", name)
            return RpcProxy(self._channel, name)
        return super().__getattribute__(name)
