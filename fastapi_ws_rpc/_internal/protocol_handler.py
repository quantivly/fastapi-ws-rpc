"""
Protocol handler component for JSON-RPC 2.0 message parsing and routing.

This module handles incoming JSON-RPC messages, routes them to appropriate
handlers, and formats error responses.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from ..config import RpcDebugConfig
from ..exceptions import RpcInvalidStateError
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
        debug_config: RpcDebugConfig | None = None,
    ) -> None:
        """
        Initialize the protocol handler.

        Args:
            method_invoker: Component for validating and invoking methods
            promise_manager: Component for managing request/response promises
            send_callback: Async function to send messages (usually channel.send_raw)
            debug_config: Configuration for error disclosure. If None, defaults to
                         production-safe mode (debug_mode=False).
        """
        self._method_invoker = method_invoker
        self._promise_manager = promise_manager
        self._send = send_callback
        self._debug_config = debug_config or RpcDebugConfig()

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

        except (ValidationError, ValueError) as e:
            logger.error(
                "Failed to parse JSON-RPC message", {"message": data, "error": e}
            )
            # Send error response with id: null (per JSON-RPC 2.0 spec)
            # Try to extract request ID if available, otherwise use None
            request_id = data.get("id") if isinstance(data, dict) else None

            # Determine appropriate error code
            if isinstance(e, ValueError):
                error_code = JsonRpcErrorCode.INVALID_REQUEST
                error_msg = str(e)
            else:  # ValidationError
                error_code = JsonRpcErrorCode.PARSE_ERROR
                error_msg = f"Invalid request format: {e!s}"

            await self.send_error(request_id, error_code, error_msg)
            return

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
            # When method is invalid, error_code and error_msg are guaranteed to be non-None
            # These runtime checks protect against -O optimization flag that disables assertions
            if error_code is None:
                raise RpcInvalidStateError(
                    "error_code must be set when is_valid is False"
                )
            if error_msg is None:
                raise RpcInvalidStateError(
                    "error_msg must be set when is_valid is False"
                )
            if request.id is not None:
                await self.send_error(request.id, error_code, error_msg)
            return

        # Convert and validate parameters first
        # This catches ValueError from invalid parameter formats (e.g., wrong count, keyword-only params)
        try:
            arguments = self._method_invoker.convert_params(
                request.params, method_name=method_name
            )
        except ValueError as exc:
            # Parameter format error (e.g., wrong param count, keyword-only params with positional args)
            logger.exception(
                f"Invalid parameter format for method '{method_name}': {exc}",
                extra={
                    "method": method_name,
                    "request_id": request.id,
                    "error_type": "ValueError",
                },
            )
            if request.id is not None:
                await self.send_error(
                    request.id,
                    JsonRpcErrorCode.INVALID_PARAMS,
                    f"Invalid parameters: {exc!s}",
                    {"type": "ValueError", "method": method_name},
                )
            return

        # Execute method
        try:
            method = getattr(self._method_invoker._methods, method_name)
            result = await method(**arguments)

            # Type conversion if needed
            if result is not NoResponse:
                result_type = self._method_invoker.get_return_type(method)
                # If no type given - try to convert to string
                if result_type is str and not isinstance(result, str):
                    result = str(result)

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
            logger.exception(
                f"Invalid parameters for method '{method_name}': {exc}",
                extra={
                    "method": method_name,
                    "request_id": request.id,
                    "error_type": "TypeError",
                },
            )
            if request.id is not None:
                await self.send_error(
                    request.id,
                    JsonRpcErrorCode.INVALID_PARAMS,
                    f"Invalid parameters: {exc!s}",
                    {"type": "TypeError", "method": method_name},
                )

        except Exception as exc:
            # Broad catch for JSON-RPC 2.0 compliance: any error during method
            # execution must be converted to a JSON-RPC error response.
            # This is intentionally broad to handle all application errors.
            # System exceptions (KeyboardInterrupt, SystemExit) are not caught.

            # Log full error details server-side (always, regardless of debug mode)
            logger.exception(
                f"Failed to execute method '{method_name}': {exc}",
                extra={
                    "method": method_name,
                    "request_id": request.id,
                    "error_type": type(exc).__name__,
                    "error_module": type(exc).__module__,
                },
            )

            # Send sanitized or full error to client based on debug mode
            if request.id is not None:
                if self._debug_config.debug_mode:
                    # Debug mode: send full error details
                    error_msg = f"Internal error: {exc!s}"
                    error_data = {"type": type(exc).__name__, "method": method_name}
                else:
                    # Production mode: sanitized generic error
                    error_msg = "Internal server error"
                    error_data = None

                await self.send_error(
                    request.id,
                    JsonRpcErrorCode.INTERNAL_ERROR,
                    error_msg,
                    error_data,
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
        request_id: str | int | None,
        error_code: JsonRpcErrorCode,
        message: str,
        data: dict[str, Any] | None = None,
    ) -> None:
        """
        Send a JSON-RPC 2.0 error response.

        Per JSON-RPC 2.0 spec, error responses can have id: null when
        the request ID cannot be determined (e.g., parse errors).

        Args:
            request_id: The request ID to respond to, or None for parse errors
            error_code: The JSON-RPC error code
            message: Human-readable error message
            data: Optional additional error data

        Note:
            When request_id is None, the response will include "id": null
            as required by JSON-RPC 2.0 for parse and invalid request errors.

        Example:
            ```python
            # Normal error response
            await handler.send_error(
                "123",
                JsonRpcErrorCode.METHOD_NOT_FOUND,
                "Method 'foo' not found"
            )

            # Parse error with no request ID
            await handler.send_error(
                None,
                JsonRpcErrorCode.PARSE_ERROR,
                "Invalid JSON"
            )
            ```
        """
        error = JsonRpcError(code=error_code.value, message=message, data=data)
        response = JsonRpcResponse(jsonrpc="2.0", id=request_id, error=error)
        response_dict = response.model_dump(exclude_none=True)

        # When request_id is None, explicitly include "id": null in response
        # (exclude_none=True would remove it, but JSON-RPC 2.0 requires it)
        if request_id is None:
            response_dict["id"] = None

        await self._send(response_dict)
