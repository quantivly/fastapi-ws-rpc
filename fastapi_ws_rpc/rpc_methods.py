from __future__ import annotations

import asyncio
import copy
import os
import sys
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

from .utils import gen_uid

if TYPE_CHECKING:
    from ._internal.protocols import RpcCallable

PING_RESPONSE = "pong"
# list of internal methods that can be called from remote
EXPOSED_BUILT_IN_METHODS = ["ping", "_get_channel_id_"]
# NULL default value - indicating no response was received


class NoResponse:
    """
    Sentinel class indicating no response was received for an RPC call.

    This is used as a NULL/None alternative when None itself might be
    a valid return value from an RPC method.

    Examples
    --------
    >>> result = await client.call("method")
    >>> if isinstance(result, NoResponse):
    ...     print("No response received")
    """


class RpcMethodsBase:
    """
    Base class for defining RPC methods that can be called remotely.

    This class provides the interface that RPC channels expect method groups
    to implement. Subclass this to create custom RPC method collections that
    can be exposed to remote clients or servers.

    The class provides built-in methods for connection verification (ping)
    and channel identification (_get_channel_id_), and manages the bidirectional
    channel reference for recursive RPC calls.

    Attributes
    ----------
    _channel : RpcCallable | None
        Reference to the RPC channel, set automatically when the methods
        are attached to a channel. Allows methods to make calls back to
        the remote side.

    Notes
    -----
    - All public methods (not starting with _) are automatically exposed as RPC methods
    - Methods starting with _ are private except for built-ins in EXPOSED_BUILT_IN_METHODS
    - Methods should be async (coroutines) for proper async/await support
    - Use type hints for better validation and documentation

    See Also
    --------
    RpcChannel : The channel that manages RPC communication.
    WebSocketRpcClient : Client-side WebSocket RPC implementation.
    WebSocketRpcEndpoint : Server-side WebSocket RPC endpoint.

    Examples
    --------
    Create a custom RPC method collection:

    >>> class MyMethods(RpcMethodsBase):
    ...     async def add(self, a: int, b: int) -> int:
    ...         return a + b
    ...
    ...     async def get_status(self) -> dict[str, Any]:
    ...         return {"status": "ok"}

    Use with a server endpoint:

    >>> methods = MyMethods()
    >>> endpoint = WebSocketRpcEndpoint(methods)
    >>> endpoint.register_route(app, path="/ws")

    Call back to the remote side from within a method:

    >>> class CallbackMethods(RpcMethodsBase):
    ...     async def process_and_notify(self, data: str) -> str:
    ...         result = f"Processed: {data}"
    ...         if self.channel:
    ...             await self.channel.notify("log", {"message": result})
    ...         return result
    """

    def __init__(self) -> None:
        self._channel: RpcCallable | None = None

    def _set_channel_(self, channel: RpcCallable) -> None:
        """
        Set the RPC channel reference for this methods instance.

        This is called automatically by the RPC channel when attaching
        methods. It allows methods to make calls back to the remote side
        via self.channel.

        Parameters
        ----------
        channel : RpcCallable
            The RPC channel instance managing communication.
        """
        self._channel = channel

    @property
    def channel(self) -> RpcCallable | None:
        """
        Get the RPC channel reference.

        Returns
        -------
        RpcCallable | None
            The RPC channel if set, None otherwise.
        """
        return self._channel

    def _copy_(self) -> RpcMethodsBase:
        """
        Create a shallow copy of this methods instance.

        Used by RPC channels to create isolated method instances per connection.
        Subclasses with additional state may need to override this method.

        Returns
        -------
        RpcMethodsBase
            A shallow copy of this instance.

        Notes
        -----
        The channel reference is copied but will be replaced by the new
        channel via _set_channel_() after copying.
        """
        return copy.copy(self)

    async def ping(self) -> str:
        """
        Built-in ping method for connection verification and keep-alive.

        Returns
        -------
        str
            Always returns "pong" to confirm connection is alive.

        Examples
        --------
        >>> async with WebSocketRpcClient(uri) as client:
        ...     response = await client.other.ping()
        ...     print(response.result)  # "pong"
        """
        return PING_RESPONSE

    async def _get_channel_id_(self) -> str:
        """
        Built-in method to get the channel ID of this side.

        Used for identifying connections when sync_channel_id is enabled.
        This allows the remote side to retrieve this channel's unique ID.

        Returns
        -------
        str
            The unique channel ID (UUID).

        Raises
        ------
        RuntimeError
            If the channel has not been initialized.

        Examples
        --------
        >>> # Remote side calls this to get our channel ID
        >>> response = await client.other._get_channel_id_()
        >>> print(response.result)  # "550e8400-e29b-41d4-a716-446655440000"
        """
        if self._channel is None:
            raise RuntimeError("Channel not initialized")
        return self._channel.id


class ProcessDetails(BaseModel):
    """
    Process information model containing runtime details.

    Attributes
    ----------
    pid : int
        Process ID of the current Python process.
    cmd : list[str]
        Command-line arguments used to start the process.
    workingdir : str
        Current working directory of the process.

    Examples
    --------
    >>> details = ProcessDetails()
    >>> print(f"PID: {details.pid}, CWD: {details.workingdir}")
    """

    pid: int = os.getpid()
    cmd: list[str] = sys.argv
    workingdir: str = os.getcwd()


class RpcUtilityMethods(RpcMethodsBase):
    """
    Utility RPC methods for management, testing, and diagnostics.

    This class extends RpcMethodsBase with utility methods useful for
    testing RPC functionality, getting process information, and implementing
    callback patterns.

    Examples
    --------
    Use with a server endpoint:

    >>> methods = RpcUtilityMethods()
    >>> endpoint = WebSocketRpcEndpoint(methods)
    >>> endpoint.register_route(app, path="/ws")

    Client can call utility methods:

    >>> async with WebSocketRpcClient(uri) as client:
    ...     # Get server process details
    ...     details = await client.other.get_process_details()
    ...     print(f"Server PID: {details.result.pid}")
    ...
    ...     # Echo test
    ...     response = await client.other.echo(text="hello")
    ...     print(response.result)  # "hello"
    """

    def __init__(self) -> None:
        super().__init__()

    async def get_process_details(self) -> ProcessDetails:
        """
        Get runtime details about the current process.

        Returns
        -------
        ProcessDetails
            Object containing PID, command-line args, and working directory.

        Examples
        --------
        >>> response = await client.other.get_process_details()
        >>> print(f"Remote PID: {response.result.pid}")
        """
        return ProcessDetails()

    async def call_me_back(
        self, method_name: str = "", args: dict[str, Any] | None = None
    ) -> str:
        """
        Demonstrate callback pattern by calling back to the remote side.

        This method receives a method name and arguments, then calls that
        method on the remote side asynchronously without waiting for the
        response. Returns a call ID that can be used to retrieve the
        response later via get_response().

        Parameters
        ----------
        method_name : str, optional
            Name of the method to call on the remote side (default is "").
        args : dict[str, Any] | None, optional
            Arguments to pass to the remote method (default is None).

        Returns
        -------
        str
            Call ID (UUID) that can be used to retrieve the response later,
            or empty string if channel is not available.

        Notes
        -----
        This demonstrates the fire-and-forget callback pattern. The call
        is made asynchronously in the background, and the response must
        be retrieved later using get_response() with the returned call ID.

        Examples
        --------
        >>> # Server calls back to client's method
        >>> call_id = await client.other.call_me_back(
        ...     method_name="process_data",
        ...     args={"data": "example"}
        ... )
        >>> # Later, retrieve the response
        >>> response = await client.other.get_response(call_id=call_id)
        """
        if args is None:
            args = {}
        if self.channel is not None:
            # generate a uid we can use to track this request
            call_id = gen_uid()
            # Call async -  without waiting to avoid locking the event_loop
            asyncio.create_task(
                self.channel.async_call(method_name, args=args, call_id=call_id)
            )
            # return the id- which can be used to check the response once it's received
            return call_id
        return ""

    async def get_response(self, call_id: str = "") -> Any:
        """
        Retrieve a previously initiated RPC call response by call ID.

        Used in conjunction with call_me_back() to retrieve responses from
        asynchronous callbacks.

        Parameters
        ----------
        call_id : str, optional
            The call ID returned from a previous call_me_back() invocation
            (default is "").

        Returns
        -------
        Any
            The response from the RPC call, or None if not found or
            channel is not available.

        Notes
        -----
        This method clears the saved call after retrieving it, so it can
        only be called once per call ID.

        Examples
        --------
        >>> # After initiating a callback
        >>> call_id = await client.other.call_me_back(...)
        >>> # Retrieve the result
        >>> response = await client.other.get_response(call_id=call_id)
        >>> print(response)
        """
        if self.channel is not None:
            res = self.channel.get_saved_response(call_id)
            self.channel.clear_saved_call(call_id)
            return res
        return None

    async def echo(self, text: str) -> str:
        """
        Echo back the provided text unchanged.

        Simple method for testing RPC connectivity and message passing.

        Parameters
        ----------
        text : str
            The text to echo back.

        Returns
        -------
        str
            The same text that was provided.

        Examples
        --------
        >>> response = await client.other.echo(text="hello world")
        >>> print(response.result)  # "hello world"
        """
        return text
