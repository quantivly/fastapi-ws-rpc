"""
Protocol handler component for JSON-RPC 2.0 message parsing and routing.

This module handles incoming JSON-RPC messages, routes them to appropriate
handlers, and formats error responses.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from ..logger import get_logger
from ..rpc_methods import NoResponse
from ..schemas import JsonRpcError, JsonRpcErrorCode, JsonRpcRequest, JsonRpcResponse
from ..utils import pydantic_parse

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from .method_invoker import RpcMethodInvoker
    from .promise_manager import RpcPromiseManager

logger = get_logger("RPC_PROTOCOL")


class RpcProtocolHandler:
    """
    Handles JSON-RPC 2.0 protocol message parsing and routing.

    This component:
    - Parses incoming messages into requests/responses
    - Routes messages to appropriate handlers
    - Formats error responses
    - Validates JSON-RPC 2.0 compliance
    """

    def __init__(
        self,
        method_invoker: RpcMethodInvoker,
        promise_manager: RpcPromiseManager,
        send_callback: Callable[[dict[str, Any]], Awaitable[None]],
    ) -> None:
        """
        Initialize the protocol handler.

        Args:
            method_invoker: Component for validating and invoking methods
            promise_manager: Component for managing request/response promises
            send_callback: Async function to send messages (usually channel.send_raw)
        """
        self._method_invoker = method_invoker
        self._promise_manager = promise_manager
        self._send = send_callback

    async def handle_message(self, data: dict[str, Any]) -> None:
        """
        Route an incoming message to the appropriate handler.

        This method parses incoming JSON-RPC messages and routes them to:
        - handle_request() for JSON-RPC requests (has "method" field)
        - handle_response() for JSON-RPC responses (has "result" or "error" field)

        Args:
            data: The parsed JSON message data

        Raises:
            ValidationError: If the message doesn't conform to JSON-RPC 2.0
            ValueError: If the message format is unknown
        """
        logger.debug(f"Processing received message: {data}")
        try:
            if not isinstance(data, dict):
                raise ValueError(f"Expected dict, got {type(data)}")

            # Request - has "method" field
            if "method" in data:
                if data.get("jsonrpc") != "2.0":
                    raise ValueError(f"Invalid JSON-RPC version: {data.get('jsonrpc')}")
                request = pydantic_parse(JsonRpcRequest, data)
                await self.handle_request(request)
                return

            # Response - has "result" or "error" field
            if "result" in data or "error" in data:
                if data.get("jsonrpc") != "2.0":
                    raise ValueError(f"Invalid JSON-RPC version: {data.get('jsonrpc')}")
                response = pydantic_parse(JsonRpcResponse, data)
                await self.handle_response(response)
                return

            # Unknown message format
            raise ValueError(f"Unknown message format: {data}")

        except ValidationError as e:
            logger.error(
                "Failed to parse JSON-RPC message", {"message": data, "error": e}
            )
            # Cannot send error response without request ID
            raise

    async def handle_request(self, request: JsonRpcRequest) -> None:
        """
        Handle an incoming JSON-RPC 2.0 request.

        This method:
        1. Validates the method using the method invoker
        2. Executes the method if valid
        3. Sends a response (unless notification or NoResponse)
        4. Sends error response if validation or execution fails

        Args:
            request: The JSON-RPC request to handle
        """
        logger.debug(f"Handling RPC request: {request}")
        method_name = request.method

        # Validate method
        is_valid, error_code, error_msg = self._method_invoker.validate_method(
            method_name
        )
        if not is_valid:
            if request.id is not None:
                await self.send_error(request.id, error_code, error_msg)
            return

        # Execute method
        try:
            result = await self._method_invoker.invoke(method_name, request.params)

            # Only send response if not a notification and result is not NoResponse
            if result is not NoResponse and request.id is not None:
                response = JsonRpcResponse(
                    jsonrpc="2.0",
                    id=request.id,
                    result=result,
                )
                await self._send(response.model_dump(exclude_none=True))

        except TypeError as exc:
            # Invalid parameters (wrong arguments, missing required args, etc.)
            logger.exception(f"Invalid parameters for method '{method_name}': {exc}")
            if request.id is not None:
                await self.send_error(
                    request.id,
                    JsonRpcErrorCode.INVALID_PARAMS,
                    f"Invalid parameters: {exc!s}",
                    {"type": "TypeError", "method": method_name},
                )

        except Exception as exc:
            # Internal error during method execution
            logger.exception(f"Failed to execute method '{method_name}': {exc}")
            if request.id is not None:
                await self.send_error(
                    request.id,
                    JsonRpcErrorCode.INTERNAL_ERROR,
                    f"Internal error: {exc!s}",
                    {"type": type(exc).__name__, "method": method_name},
                )

    async def handle_response(self, response: JsonRpcResponse) -> None:
        """
        Handle an incoming JSON-RPC 2.0 response.

        This method stores the response in the promise manager, which will
        wake up any coroutines waiting for this response.

        Args:
            response: The JSON-RPC response to handle
        """
        logger.debug(f"Handling RPC response: {response}")
        self._promise_manager.store_response(response)

    async def send_error(
        self,
        request_id: str | int,
        error_code: JsonRpcErrorCode,
        message: str,
        data: dict[str, Any] | None = None,
    ) -> None:
        """
        Send a JSON-RPC 2.0 error response.

        Args:
            request_id: The request ID to respond to
            error_code: The JSON-RPC error code
            message: Human-readable error message
            data: Optional additional error data

        Example:
            ```python
            await handler.send_error(
                "123",
                JsonRpcErrorCode.METHOD_NOT_FOUND,
                "Method 'foo' not found"
            )
            ```
        """
        error = JsonRpcError(code=error_code.value, message=message, data=data)
        response = JsonRpcResponse(jsonrpc="2.0", id=request_id, error=error)
        await self._send(response.model_dump(exclude_none=True))
