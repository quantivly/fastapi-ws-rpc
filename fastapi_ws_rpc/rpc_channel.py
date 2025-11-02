"""
Definition for an RPC channel protocol on top of a websocket -
enabling bi-directional request/response interactions
"""

import asyncio
from inspect import _empty, signature
from typing import Any, Optional

from pydantic import ValidationError

from .logger import get_logger
from .rpc_methods import EXPOSED_BUILT_IN_METHODS, NoResponse, RpcMethodsBase
from .schemas import JsonRpcError, JsonRpcRequest, JsonRpcResponse
from .utils import gen_uid, pydantic_parse

logger = get_logger("RPC_CHANNEL")


class DEFAULT_TIMEOUT:
    pass


class RemoteValueError(ValueError):
    pass


class RpcException(Exception):
    pass


class RpcChannelClosedException(Exception):
    """
    Raised when the channel is closed mid-operation
    """

    pass


class UnknownMethodException(RpcException):
    pass


class RpcPromise:
    """
    Simple Event and id wrapper/proxy
    Holds the state of a pending request
    """

    def __init__(self, request: JsonRpcRequest):
        self._request = request
        self._id = request.id
        # event used to wait for the completion of the request
        # (upon receiving its matching response)
        self._event = asyncio.Event()

    @property
    def request(self):
        return self._request

    @property
    def call_id(self):
        return self._id

    def set(self):
        """
        Signal completion of request with received response
        """
        self._event.set()

    def wait(self):
        """
        Wait on the internal event - triggered on response
        """
        return self._event.wait()


class RpcProxy:
    """Provides a proxy to a remote method on the other side of the channel."""

    def __init__(self, channel, method_name: str) -> None:
        self.channel = channel
        self.method_name = method_name

    def __call__(self, **kwargs: Any) -> Any:
        """Calls the remote method with the given keyword arguments.

        Parameters
        ----------
        kwargs : dict
            Keyword arguments to pass to the remote method.

        Returns
        -------
        Any
            Return value of the remote method.
        """
        logger.debug("Calling RPC method: %s", self.method_name)
        return self.channel.call(self.method_name, args=kwargs)


class RpcCaller:
    """Calls remote methods on the other side of the channel."""

    def __init__(self, channel: "RpcChannel", methods=None) -> None:
        self._channel = channel

    def __getattribute__(self, name: str):
        """Returns an :class:`~fastapi_ws_rpc.rpc_channel.RpcProxy` instance
        for exposed RPC methods.

        Parameters
        ----------
        name : str
            Name of the requested attribute (or RPC method).

        Returns
        -------
        Any
            Attribute or RPC method.
        """
        is_exposed = not name.startswith("_") or name in EXPOSED_BUILT_IN_METHODS
        if is_exposed:
            logger.debug("%s was detected to be a remote RPC method.", name)
            return RpcProxy(self._channel, name)
        return super().__getattribute__(name)


# Callback signatures
async def OnConnectCallback(channel):
    pass


async def OnDisconnectCallback(channel):
    pass


async def OnErrorCallback(channel, err: Exception):
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
        socket,
        channel_id=None,
        default_response_timeout=None,
        sync_channel_id=False,
        **kwargs,
    ):
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
        """
        logger.debug("Initializing RPC channel...")
        self.methods = methods._copy_()
        # allow methods to access channel (for recursive calls - e.g. call me as
        # a response for me calling you)
        self.methods._set_channel_(self)
        # Pending requests - id-mapped to async-event
        self.requests: dict[str, asyncio.Event] = {}
        # Received responses
        self.responses = {}
        self.socket = socket
        # timeout
        self.default_response_timeout = default_response_timeout
        # Unique channel id
        self.id = channel_id if channel_id is not None else gen_uid()
        # flag control should we retrieve the channel id of the other side
        self._sync_channel_id = sync_channel_id
        # The channel id of the other side (if sync_channel_id is True)
        self._other_channel_id = None
        # asyncio event to check if we got the channel id of the other side
        self._channel_id_synced = asyncio.Event()
        #
        # convenience caller
        # TODO - pass remote methods object to support validation before call
        self.other = RpcCaller(self)
        # core event callback registers
        self._connect_handlers: list[OnConnectCallback] = []
        self._disconnect_handlers: list[OnDisconnectCallback] = []
        self._error_handlers = []
        # internal event
        self._closed = asyncio.Event()

        # any other kwarg goes straight to channel context (Accessible to methods)
        self._context = kwargs or {}
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
        return self._other_channel_id

    def get_return_type(self, method):
        method_signature = signature(method)
        return (
            method_signature.return_annotation
            if method_signature.return_annotation is not _empty
            else str
        )

    async def send(self, data):
        """
        For internal use. wrap calls to underlying socket
        """
        await self.socket.send(data)

    async def send_raw(self, data):
        """
        Send raw dictionary data as JSON
        """
        await self.socket.send(data)

    async def receive(self):
        """
        For internal use. wrap calls to underlying socket
        """
        return await self.socket.recv()

    async def close(self):
        res = await self.socket.close()
        # signal closer
        self._closed.set()
        return res

    def isClosed(self):
        return self._closed.is_set()

    async def wait_until_closed(self):
        """
        Waits until the close internal event happens.
        """
        return await self._closed.wait()

    async def on_message(self, data):
        """Handle an incoming JSON-RPC 2.0 message."""
        logger.debug(f"Processing received message: {data}")
        try:
            from .schemas import JsonRpcRequest, JsonRpcResponse

            if not isinstance(data, dict):
                raise ValueError(f"Expected dict, got {type(data)}")

            # Check if it's a JSON-RPC 2.0 request
            if "method" in data:
                if data.get("jsonrpc") != "2.0":
                    raise ValueError(f"Invalid JSON-RPC version: {data.get('jsonrpc')}")

                request = pydantic_parse(JsonRpcRequest, data)
                await self.on_json_rpc_request(request)
                return

            # Check if it's a JSON-RPC 2.0 response
            if "result" in data or "error" in data:
                if data.get("jsonrpc") != "2.0":
                    raise ValueError(f"Invalid JSON-RPC version: {data.get('jsonrpc')}")

                response = pydantic_parse(JsonRpcResponse, data)
                await self.on_json_rpc_response(response)
                return

            # Unknown message format
            raise ValueError(f"Unknown message format: {data}")

        except ValidationError as e:
            logger.error(
                "Failed to parse JSON-RPC message", {"message": data, "error": e}
            )
            await self.on_error(e)
        except Exception as e:
            await self.on_error(e)
            raise

    def register_connect_handler(self, coros: Optional[list[OnConnectCallback]] = None):
        """
        Register a connection handler callback that will be called (As an async
        task)) with the channel
        Args:
            coros (List[Coroutine]): async callback
        """
        if coros is not None:
            self._connect_handlers.extend(coros)

    def register_disconnect_handler(
        self, coros: Optional[list[OnDisconnectCallback]] = None
    ):
        """
        Register a disconnect handler callback that will be called (As an async
        task)) with the channel id
        Args:
            coros (List[Coroutine]): async callback
        """
        if coros is not None:
            self._disconnect_handlers.extend(coros)

    def register_error_handler(self, coros: Optional[list[OnErrorCallback]] = None):
        """
        Register an error handler callback that will be called (As an async
        task)) with the channel and triggered error.
        Args:
            coros (List[Coroutine]): async callback
        """
        if coros is not None:
            self._error_handlers.extend(coros)

    async def on_handler_event(self, handlers, *args, **kwargs):
        await asyncio.gather(*(callback(*args, **kwargs) for callback in handlers))

    async def on_connect(self):
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

    async def _get_other_channel_id(self):
        """
        Perform call to the other side of the channel to get its channel id
        Each side is generating the channel id by itself so there is no way
        to identify a connection without this sync
        """
        if self._other_channel_id is None:
            logger.debug("No cached channel ID found, calling _get_channel_id_()...")
            other_channel_id = await self.other._get_channel_id_()
            self._other_channel_id = (
                other_channel_id.result
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

    async def on_disconnect(self):
        # disconnect happened - mark the channel as closed
        self._closed.set()
        await self.on_handler_event(self._disconnect_handlers, self)

    async def on_error(self, error: Exception):
        await self.on_handler_event(self._error_handlers, self, error)

    async def on_json_rpc_request(self, request: JsonRpcRequest):
        """
        Handle incoming JSON-RPC 2.0 requests - calling relevant exposed method
        Note: methods prefixed with "_" are protected and ignored.

        Args:
            request (JsonRpcRequest): the JSON-RPC 2.0 request with the method to call
        """
        logger.debug(f"Handling RPC request on channel {self.id}: {request}")
        method_name = request.method
        # Ignore "_" prefixed methods (except built-in methods like "ping")
        if isinstance(method_name, str) and (
            not method_name.startswith("_") or method_name in EXPOSED_BUILT_IN_METHODS
        ):
            method = getattr(self.methods, method_name)
            if callable(method):
                try:
                    # Convert params to arguments dict
                    if isinstance(request.params, dict):
                        arguments = request.params
                    elif isinstance(request.params, list):
                        # For positional parameters, create a params dict
                        arguments = {"params": request.params}
                    else:
                        arguments = (
                            {} if request.params is None else {"params": request.params}
                        )

                    result = await method(**arguments)
                    if result is not NoResponse:
                        # get indicated type
                        result_type = self.get_return_type(method)
                        # if no type given - try to convert to string
                        if result_type is str and not isinstance(result, str):
                            result = str(result)

                        # Send JSON-RPC 2.0 response (only if not a notification)
                        if request.id is not None:
                            response = JsonRpcResponse(
                                jsonrpc="2.0",
                                id=request.id,
                                result=result,
                            )
                            await self.send_raw(response.model_dump(exclude_none=True))

                except Exception as exc:
                    logger.exception(f"Failed to handle RPC request: {exc}")
                    # Send JSON-RPC 2.0 error response (only if not a notification)
                    if request.id is not None:
                        error = JsonRpcError(
                            code=-32603,  # Internal error
                            message=str(exc),
                            data={"type": type(exc).__name__},
                        )
                        response = JsonRpcResponse(
                            jsonrpc="2.0", id=request.id, error=error
                        )
                        await self.send_raw(response.model_dump(exclude_none=True))

    def get_saved_promise(self, call_id):
        return self.requests[call_id]

    def get_saved_response(self, call_id):
        return self.responses[call_id]

    def clear_saved_call(self, call_id):
        del self.requests[call_id]
        del self.responses[call_id]

    async def on_json_rpc_response(self, response: JsonRpcResponse):
        """
        Handle an incoming JSON-RPC 2.0 response to a previous RPC call

        Args:
            response (JsonRpcResponse): the received JSON-RPC 2.0 response
        """
        logger.debug("Handling RPC response - %s", {"response": response})
        if response.id is not None and response.id in self.requests:
            self.responses[response.id] = response
            promise = self.requests[response.id]
            promise.set()

    async def wait_for_response(
        self, promise, timeout=DEFAULT_TIMEOUT
    ) -> JsonRpcResponse:
        """
        Wait on a previously made call
        Args:
            promise (RpcPromise): the awaitable-wrapper returned from the RPC
            request call
            timeout (float, None, or DEFAULT_TIMEOUT): the timeout to wait on
            the response, defaults to DEFAULT_TIMEOUT.
                - DEFAULT_TIMEOUT - use the value passed as
                'default_response_timeout' in channel init
                - None - no timeout
                - a number - seconds to wait before timing out
        Raises:
            asyncio.exceptions.TimeoutError - on timeout
            RpcChannelClosedException - if the channel fails before wait could
            be completed
        """
        if timeout is DEFAULT_TIMEOUT:
            timeout = self.default_response_timeout
        # wait for the promise or until the channel is terminated
        _, pending = await asyncio.wait(
            [
                asyncio.ensure_future(promise.wait()),
                asyncio.ensure_future(self._closed.wait()),
            ],
            timeout=timeout,
            return_when=asyncio.FIRST_COMPLETED,
        )
        # Cancel all pending futures and then detect if close was the first done
        for fut in pending:
            fut.cancel()
        response = self.responses.get(promise.call_id, NoResponse)
        # if the channel was closed before we could finish
        if response is NoResponse:
            raise RpcChannelClosedException(
                f"Channel Closed before RPC response for {promise.call_id} could be received"
            )
        self.clear_saved_call(promise.call_id)
        return response

    async def async_call(self, name, args=None, call_id=None) -> RpcPromise:
        """
        Call a method and return the event and the sent message (including the
        chosen call_id)
        use self.wait_for_response on the event and call_id to get the return
        value of the call
        Args:
            name (str): name of the method to call on the other side
            args (dict): keyword args to pass tot he other side
            call_id (string, optional): a UUID to use to track the call () -
            override only with true UUIDs
        """
        if args is None:
            args = {}
        call_id = call_id or gen_uid()

        # Create JSON-RPC 2.0 request directly
        from .schemas import JsonRpcRequest

        json_rpc_request = JsonRpcRequest(
            id=call_id, method=name, params=args if args else None
        )

        logger.debug("Sending JSON-RPC 2.0 request: %s", json_rpc_request)
        await self.send_raw(json_rpc_request.model_dump(exclude_none=True))

        # Create promise for response tracking
        promise = self.requests[call_id] = RpcPromise(json_rpc_request)
        return promise

    async def call(self, name, args=None, timeout=DEFAULT_TIMEOUT):
        """
        Call a method and wait for a response to be received
        """
        if args is None:
            args = {}
        promise = await self.async_call(name, args)
        return await self.wait_for_response(promise, timeout=timeout)
