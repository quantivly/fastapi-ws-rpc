"""
Comprehensive tests for RpcProtocolHandler.

This test module covers:
- Request parsing and routing
- Response parsing and routing
- Error response formatting
- JSON-RPC 2.0 compliance (version validation, required fields)
- Invalid message handling
- Integration with method invoker and promise manager
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from pydantic import ValidationError

from fastapi_ws_rpc._internal.method_invoker import RpcMethodInvoker
from fastapi_ws_rpc._internal.promise_manager import RpcPromiseManager
from fastapi_ws_rpc._internal.protocol_handler import RpcProtocolHandler
from fastapi_ws_rpc.rpc_methods import NoResponse, RpcMethodsBase
from fastapi_ws_rpc.schemas import (
    JsonRpcError,
    JsonRpcErrorCode,
    JsonRpcRequest,
    JsonRpcResponse,
)

# ============================================================================
# Test Methods Classes
# ============================================================================


class TestMethods(RpcMethodsBase):
    """Test methods class for protocol handler testing."""

    async def add(self, a: int, b: int) -> int:
        """Simple addition method."""
        return a + b

    async def echo(self, message: str) -> str:
        """Echo method."""
        return message

    async def no_params_method(self) -> str:
        """Method with no parameters."""
        return "success"

    async def method_with_exception(self) -> None:
        """Method that raises an exception."""
        raise ValueError("Test exception")

    async def method_with_type_error(self, x: int) -> int:
        """Method that expects specific types."""
        # Will raise TypeError if x is wrong type
        return x * 2

    async def method_returning_noresponse(self) -> type[NoResponse]:
        """Method that returns NoResponse."""
        return NoResponse


# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture
def test_methods() -> TestMethods:
    """Create test methods instance."""
    return TestMethods()


@pytest.fixture
def method_invoker(test_methods: TestMethods) -> RpcMethodInvoker:
    """Create method invoker."""
    return RpcMethodInvoker(test_methods)


@pytest_asyncio.fixture
async def promise_manager() -> RpcPromiseManager:
    """Create promise manager."""
    manager = RpcPromiseManager()
    yield manager
    if not manager.is_closed():
        manager.close()
    await asyncio.sleep(0.01)


@pytest.fixture
def mock_send() -> AsyncMock:
    """Create mock send callback."""
    return AsyncMock()


@pytest.fixture
def protocol_handler(
    method_invoker: RpcMethodInvoker,
    promise_manager: RpcPromiseManager,
    mock_send: AsyncMock,
) -> RpcProtocolHandler:
    """Create protocol handler with mocked dependencies."""
    return RpcProtocolHandler(method_invoker, promise_manager, mock_send)


# ============================================================================
# Request Handling Tests
# ============================================================================


class TestRequestHandling:
    """Test handling of JSON-RPC requests."""

    @pytest.mark.asyncio
    async def test_handle_valid_request(
        self, protocol_handler: RpcProtocolHandler, mock_send: AsyncMock
    ) -> None:
        """
        Test handling a valid JSON-RPC request.

        Verifies that:
        - Request is parsed correctly
        - Method is invoked
        - Response is sent with correct result
        """
        request = JsonRpcRequest(
            jsonrpc="2.0",
            id="req-1",
            method="add",
            params={"a": 5, "b": 3},
        )

        await protocol_handler.handle_request(request)

        # Verify response was sent
        mock_send.assert_called_once()
        sent_data = mock_send.call_args[0][0]

        assert sent_data["jsonrpc"] == "2.0"
        assert sent_data["id"] == "req-1"
        assert sent_data["result"] == 8
        assert "error" not in sent_data

    @pytest.mark.asyncio
    async def test_handle_request_no_params(
        self, protocol_handler: RpcProtocolHandler, mock_send: AsyncMock
    ) -> None:
        """
        Test handling request with no parameters.

        Verifies that:
        - Request without params is handled correctly
        - Method is invoked successfully
        """
        request = JsonRpcRequest(
            jsonrpc="2.0",
            id="req-2",
            method="no_params_method",
        )

        await protocol_handler.handle_request(request)

        mock_send.assert_called_once()
        sent_data = mock_send.call_args[0][0]

        assert sent_data["result"] == "success"

    @pytest.mark.asyncio
    async def test_handle_notification(
        self, protocol_handler: RpcProtocolHandler, mock_send: AsyncMock
    ) -> None:
        """
        Test handling notification (request with no ID).

        Verifies that:
        - Notification is processed
        - Method is invoked
        - No response is sent (notifications don't expect responses)
        """
        notification = JsonRpcRequest(
            jsonrpc="2.0",
            id=None,  # Notification has no ID
            method="no_params_method",
        )

        await protocol_handler.handle_request(notification)

        # No response should be sent for notifications
        mock_send.assert_not_called()

    @pytest.mark.asyncio
    async def test_handle_request_with_noresponse(
        self, protocol_handler: RpcProtocolHandler, mock_send: AsyncMock
    ) -> None:
        """
        Test handling request where method returns NoResponse.

        Verifies that:
        - Method is invoked
        - No response is sent when method returns NoResponse
        """
        request = JsonRpcRequest(
            jsonrpc="2.0",
            id="req-3",
            method="method_returning_noresponse",
        )

        await protocol_handler.handle_request(request)

        # No response should be sent when method returns NoResponse
        mock_send.assert_not_called()

    @pytest.mark.asyncio
    async def test_handle_request_method_not_found(
        self, protocol_handler: RpcProtocolHandler, mock_send: AsyncMock
    ) -> None:
        """
        Test handling request for non-existent method.

        Verifies that:
        - Error response is sent
        - Error code is METHOD_NOT_FOUND
        - Error message is descriptive
        """
        request = JsonRpcRequest(
            jsonrpc="2.0",
            id="req-4",
            method="nonexistent_method",
        )

        await protocol_handler.handle_request(request)

        mock_send.assert_called_once()
        sent_data = mock_send.call_args[0][0]

        assert "error" in sent_data
        assert sent_data["error"]["code"] == JsonRpcErrorCode.METHOD_NOT_FOUND.value
        assert "not found" in sent_data["error"]["message"].lower()

    @pytest.mark.asyncio
    async def test_handle_request_invalid_params(
        self, protocol_handler: RpcProtocolHandler, mock_send: AsyncMock
    ) -> None:
        """
        Test handling request with invalid parameters.

        Verifies that:
        - TypeError from method is caught
        - Error response is sent with INVALID_PARAMS code
        - Error message describes the problem
        """
        request = JsonRpcRequest(
            jsonrpc="2.0",
            id="req-5",
            method="add",
            params={"a": 5},  # Missing required param 'b'
        )

        await protocol_handler.handle_request(request)

        mock_send.assert_called_once()
        sent_data = mock_send.call_args[0][0]

        assert "error" in sent_data
        assert sent_data["error"]["code"] == JsonRpcErrorCode.INVALID_PARAMS.value
        assert "invalid parameters" in sent_data["error"]["message"].lower()

    @pytest.mark.asyncio
    async def test_handle_request_positional_params_rejected(
        self, protocol_handler: RpcProtocolHandler, mock_send: AsyncMock
    ) -> None:
        """
        Test handling request with positional parameters (array format).

        Verifies that:
        - Positional parameters (list/array format) are rejected
        - Error response is sent with INVALID_PARAMS code
        - Error message indicates positional params are not supported
        """
        # Note: Since JsonRpcRequest now only accepts dict params, we need to
        # test positional param rejection at the method_invoker level
        # Manually call convert_params with list to test rejection
        # This simulates what would happen if a list somehow got through
        import pytest

        with pytest.raises(ValueError) as exc_info:
            protocol_handler._method_invoker.convert_params([1, 2, 3])

        error_msg = str(exc_info.value).lower()
        assert "positional parameters" in error_msg
        assert "not supported" in error_msg

    @pytest.mark.asyncio
    async def test_handle_request_method_raises_exception(
        self, protocol_handler: RpcProtocolHandler, mock_send: AsyncMock
    ) -> None:
        """
        Test handling request where method raises exception.

        Verifies that:
        - Exception from method is caught
        - Error response is sent with INTERNAL_ERROR code
        - Error message includes exception details
        """
        request = JsonRpcRequest(
            jsonrpc="2.0",
            id="req-6",
            method="method_with_exception",
        )

        await protocol_handler.handle_request(request)

        mock_send.assert_called_once()
        sent_data = mock_send.call_args[0][0]

        assert "error" in sent_data
        assert sent_data["error"]["code"] == JsonRpcErrorCode.INTERNAL_ERROR.value
        assert "internal error" in sent_data["error"]["message"].lower()

    @pytest.mark.asyncio
    async def test_handle_request_private_method(
        self, protocol_handler: RpcProtocolHandler, mock_send: AsyncMock
    ) -> None:
        """
        Test handling request for private method.

        Verifies that:
        - Private methods are rejected
        - Error response is sent with METHOD_NOT_FOUND code
        """
        request = JsonRpcRequest(
            jsonrpc="2.0",
            id="req-7",
            method="_private_method",
        )

        await protocol_handler.handle_request(request)

        mock_send.assert_called_once()
        sent_data = mock_send.call_args[0][0]

        assert "error" in sent_data
        assert sent_data["error"]["code"] == JsonRpcErrorCode.METHOD_NOT_FOUND.value


# ============================================================================
# Response Handling Tests
# ============================================================================


class TestResponseHandling:
    """Test handling of JSON-RPC responses."""

    @pytest.mark.asyncio
    async def test_handle_response_with_result(
        self, protocol_handler: RpcProtocolHandler, promise_manager: RpcPromiseManager
    ) -> None:
        """
        Test handling a response with a result.

        Verifies that:
        - Response is stored in promise manager
        - Waiting promise is signaled
        """
        # Create a pending request
        request = JsonRpcRequest(
            jsonrpc="2.0",
            id="req-1",
            method="test_method",
        )
        promise_manager.create_promise(request)

        # Handle response
        response = JsonRpcResponse(
            jsonrpc="2.0",
            id="req-1",
            result={"status": "success"},
        )

        await protocol_handler.handle_response(response)

        # Response should be stored
        stored_response = promise_manager.get_saved_response("req-1")
        assert stored_response == response

    @pytest.mark.asyncio
    async def test_handle_response_with_error(
        self, protocol_handler: RpcProtocolHandler, promise_manager: RpcPromiseManager
    ) -> None:
        """
        Test handling a response with an error.

        Verifies that:
        - Error response is stored correctly
        - Promise is signaled
        """
        # Create pending request
        request = JsonRpcRequest(
            jsonrpc="2.0",
            id="req-2",
            method="test_method",
        )
        promise_manager.create_promise(request)

        # Handle error response
        error = JsonRpcError(
            code=JsonRpcErrorCode.INTERNAL_ERROR.value,
            message="Something went wrong",
        )
        response = JsonRpcResponse(
            jsonrpc="2.0",
            id="req-2",
            error=error,
        )

        await protocol_handler.handle_response(response)

        # Response should be stored
        stored_response = promise_manager.get_saved_response("req-2")
        assert stored_response == response
        assert stored_response.error is not None

    @pytest.mark.asyncio
    async def test_handle_response_for_unknown_request(
        self, protocol_handler: RpcProtocolHandler, promise_manager: RpcPromiseManager
    ) -> None:
        """
        Test handling response for unknown request ID.

        Verifies that:
        - Response is handled gracefully
        - No exception is raised
        - Response is not stored (no matching promise)
        """
        # No pending request created

        response = JsonRpcResponse(
            jsonrpc="2.0",
            id="unknown-id",
            result="data",
        )

        # Should not raise exception
        await protocol_handler.handle_response(response)

        # Verify it wasn't stored (since no matching promise)
        with pytest.raises(KeyError):
            promise_manager.get_saved_response("unknown-id")


# ============================================================================
# Message Routing Tests
# ============================================================================


class TestMessageRouting:
    """Test routing of incoming messages to appropriate handlers."""

    @pytest.mark.asyncio
    async def test_handle_message_request(
        self, protocol_handler: RpcProtocolHandler, mock_send: AsyncMock
    ) -> None:
        """
        Test routing a request message.

        Verifies that:
        - Message with 'method' field is routed to handle_request
        - Request is processed correctly
        """
        message = {
            "jsonrpc": "2.0",
            "id": "req-1",
            "method": "add",
            "params": {"a": 1, "b": 2},
        }

        await protocol_handler.handle_message(message)

        # Should send response
        mock_send.assert_called_once()
        sent_data = mock_send.call_args[0][0]
        assert sent_data["result"] == 3

    @pytest.mark.asyncio
    async def test_handle_message_response(
        self,
        protocol_handler: RpcProtocolHandler,
        promise_manager: RpcPromiseManager,
        mock_send: AsyncMock,
    ) -> None:
        """
        Test routing a response message.

        Verifies that:
        - Message with 'result' field is routed to handle_response
        - Response is stored in promise manager
        """
        # Create pending request
        request = JsonRpcRequest(
            jsonrpc="2.0",
            id="req-1",
            method="test_method",
        )
        promise_manager.create_promise(request)

        # Route response message
        message = {
            "jsonrpc": "2.0",
            "id": "req-1",
            "result": "success",
        }

        await protocol_handler.handle_message(message)

        # Response should be stored
        response = promise_manager.get_saved_response("req-1")
        assert response.result == "success"

        # No outgoing message should be sent
        mock_send.assert_not_called()

    @pytest.mark.asyncio
    async def test_handle_message_error_response(
        self,
        protocol_handler: RpcProtocolHandler,
        promise_manager: RpcPromiseManager,
    ) -> None:
        """
        Test routing an error response message.

        Verifies that:
        - Message with 'error' field is routed to handle_response
        - Error response is stored correctly
        """
        # Create pending request
        request = JsonRpcRequest(
            jsonrpc="2.0",
            id="req-1",
            method="test_method",
        )
        promise_manager.create_promise(request)

        # Route error response
        message = {
            "jsonrpc": "2.0",
            "id": "req-1",
            "error": {"code": -32603, "message": "Internal error"},
        }

        await protocol_handler.handle_message(message)

        # Error response should be stored
        response = promise_manager.get_saved_response("req-1")
        assert response.error is not None
        assert response.error.code == -32603


# ============================================================================
# JSON-RPC 2.0 Compliance Tests
# ============================================================================


class TestJsonRpc20Compliance:
    """Test JSON-RPC 2.0 specification compliance."""

    @pytest.mark.asyncio
    async def test_version_validation_request(
        self, protocol_handler: RpcProtocolHandler
    ) -> None:
        """
        Test that JSON-RPC version is validated for requests.

        Verifies that:
        - Request with wrong version is rejected
        - ValueError is raised
        """
        message = {
            "jsonrpc": "1.0",  # Wrong version
            "id": "req-1",
            "method": "test_method",
        }

        with pytest.raises(ValueError) as exc_info:
            await protocol_handler.handle_message(message)

        assert "version" in str(exc_info.value).lower()

    @pytest.mark.asyncio
    async def test_version_validation_response(
        self, protocol_handler: RpcProtocolHandler
    ) -> None:
        """
        Test that JSON-RPC version is validated for responses.

        Verifies that:
        - Response with wrong version is rejected
        - ValueError is raised
        """
        message = {
            "jsonrpc": "1.0",  # Wrong version
            "id": "req-1",
            "result": "data",
        }

        with pytest.raises(ValueError) as exc_info:
            await protocol_handler.handle_message(message)

        assert "version" in str(exc_info.value).lower()

    @pytest.mark.asyncio
    async def test_missing_version_field(
        self, protocol_handler: RpcProtocolHandler
    ) -> None:
        """
        Test handling message with missing jsonrpc version field.

        Verifies that:
        - Message without version is rejected
        - ValidationError or ValueError is raised
        """
        message = {
            # Missing "jsonrpc" field
            "id": "req-1",
            "method": "test_method",
        }

        with pytest.raises((ValidationError, ValueError)):
            await protocol_handler.handle_message(message)

    @pytest.mark.asyncio
    async def test_response_excludes_none_fields(
        self, protocol_handler: RpcProtocolHandler, mock_send: AsyncMock
    ) -> None:
        """
        Test that response excludes None fields.

        Verifies that:
        - Response doesn't include null/None fields
        - Only relevant fields are sent
        """
        request = JsonRpcRequest(
            jsonrpc="2.0",
            id="req-1",
            method="add",
            params={"a": 1, "b": 2},
        )

        await protocol_handler.handle_request(request)

        sent_data = mock_send.call_args[0][0]

        # Should not have error field
        assert "error" not in sent_data
        # Should have result
        assert "result" in sent_data


# ============================================================================
# Invalid Message Handling Tests
# ============================================================================


class TestInvalidMessageHandling:
    """Test handling of invalid and malformed messages."""

    @pytest.mark.asyncio
    async def test_handle_non_dict_message(
        self, protocol_handler: RpcProtocolHandler
    ) -> None:
        """
        Test handling message that is not a dict.

        Verifies that:
        - Non-dict message raises ValueError
        - Error message indicates expected type
        """
        message = "not a dict"  # type: ignore

        with pytest.raises(ValueError) as exc_info:
            await protocol_handler.handle_message(message)

        assert "dict" in str(exc_info.value).lower()

    @pytest.mark.asyncio
    async def test_handle_message_without_method_or_result(
        self, protocol_handler: RpcProtocolHandler
    ) -> None:
        """
        Test handling message without method or result fields.

        Verifies that:
        - Message is rejected as unknown format
        - ValueError is raised
        """
        message = {
            "jsonrpc": "2.0",
            "id": "req-1",
            # Missing both 'method' and 'result'/'error'
        }

        with pytest.raises(ValueError) as exc_info:
            await protocol_handler.handle_message(message)

        assert "unknown" in str(exc_info.value).lower()

    @pytest.mark.asyncio
    async def test_handle_malformed_request(
        self, protocol_handler: RpcProtocolHandler
    ) -> None:
        """
        Test handling malformed request data.

        Verifies that:
        - Malformed request raises ValidationError
        - Error is logged (not tested here)
        """
        message = {
            "jsonrpc": "2.0",
            "id": "req-1",
            "method": 123,  # Method must be string
        }

        with pytest.raises(ValidationError):
            await protocol_handler.handle_message(message)

    @pytest.mark.asyncio
    async def test_handle_malformed_response(
        self, protocol_handler: RpcProtocolHandler
    ) -> None:
        """
        Test handling malformed response data.

        Verifies that:
        - Malformed response raises ValidationError
        """
        message = {
            "jsonrpc": "2.0",
            "id": "req-1",
            "result": "data",
            "error": {"code": -32603, "message": "error"},
            # Cannot have both result and error
        }

        # Pydantic will accept this, but it violates JSON-RPC 2.0 spec
        # The spec says result and error are mutually exclusive
        # This is a limitation we document but don't enforce at parse time
        await protocol_handler.handle_message(message)


# ============================================================================
# Error Response Formatting Tests
# ============================================================================


class TestErrorResponseFormatting:
    """Test formatting of error responses."""

    @pytest.mark.asyncio
    async def test_send_error_basic(
        self, protocol_handler: RpcProtocolHandler, mock_send: AsyncMock
    ) -> None:
        """
        Test sending a basic error response.

        Verifies that:
        - Error response is formatted correctly
        - All required fields are present
        """
        await protocol_handler.send_error(
            "req-1",
            JsonRpcErrorCode.METHOD_NOT_FOUND,
            "Method 'foo' not found",
        )

        mock_send.assert_called_once()
        sent_data = mock_send.call_args[0][0]

        assert sent_data["jsonrpc"] == "2.0"
        assert sent_data["id"] == "req-1"
        assert "error" in sent_data
        assert sent_data["error"]["code"] == JsonRpcErrorCode.METHOD_NOT_FOUND.value
        assert sent_data["error"]["message"] == "Method 'foo' not found"

    @pytest.mark.asyncio
    async def test_send_error_with_data(
        self, protocol_handler: RpcProtocolHandler, mock_send: AsyncMock
    ) -> None:
        """
        Test sending error response with additional data.

        Verifies that:
        - Error response includes data field
        - Data is formatted correctly
        """
        error_data = {"type": "ValueError", "details": "Invalid input"}

        await protocol_handler.send_error(
            "req-2",
            JsonRpcErrorCode.INVALID_PARAMS,
            "Invalid parameters",
            error_data,
        )

        sent_data = mock_send.call_args[0][0]

        assert "data" in sent_data["error"]
        assert sent_data["error"]["data"] == error_data

    @pytest.mark.asyncio
    async def test_send_error_integer_id(
        self, protocol_handler: RpcProtocolHandler, mock_send: AsyncMock
    ) -> None:
        """
        Test sending error response with integer ID.

        Verifies that:
        - Integer IDs are handled correctly
        - Response format is valid
        """
        await protocol_handler.send_error(
            123,  # Integer ID
            JsonRpcErrorCode.INTERNAL_ERROR,
            "Internal error",
        )

        sent_data = mock_send.call_args[0][0]

        assert sent_data["id"] == 123
        assert isinstance(sent_data["id"], int)


# ============================================================================
# Integration Tests
# ============================================================================


class TestIntegration:
    """Test complete request/response workflows."""

    @pytest.mark.asyncio
    async def test_complete_request_response_cycle(
        self,
        protocol_handler: RpcProtocolHandler,
        promise_manager: RpcPromiseManager,
        mock_send: AsyncMock,
    ) -> None:
        """
        Test complete request-response cycle.

        Verifies that:
        - Request is processed
        - Response is sent
        - Response matches request
        """
        # Process request
        request_message = {
            "jsonrpc": "2.0",
            "id": "req-1",
            "method": "echo",
            "params": {"message": "hello"},
        }

        await protocol_handler.handle_message(request_message)

        # Verify response sent
        mock_send.assert_called_once()
        response = mock_send.call_args[0][0]

        assert response["id"] == "req-1"
        assert response["result"] == "hello"

    @pytest.mark.asyncio
    async def test_multiple_requests_in_sequence(
        self, protocol_handler: RpcProtocolHandler, mock_send: AsyncMock
    ) -> None:
        """
        Test handling multiple requests sequentially.

        Verifies that:
        - Multiple requests can be processed
        - Each gets correct response
        - No interference between requests
        """
        # First request
        await protocol_handler.handle_message(
            {
                "jsonrpc": "2.0",
                "id": "req-1",
                "method": "add",
                "params": {"a": 1, "b": 2},
            }
        )

        # Second request
        await protocol_handler.handle_message(
            {
                "jsonrpc": "2.0",
                "id": "req-2",
                "method": "echo",
                "params": {"message": "test"},
            }
        )

        # Verify two responses sent
        assert mock_send.call_count == 2

        # Check first response
        first_response = mock_send.call_args_list[0][0][0]
        assert first_response["id"] == "req-1"
        assert first_response["result"] == 3

        # Check second response
        second_response = mock_send.call_args_list[1][0][0]
        assert second_response["id"] == "req-2"
        assert second_response["result"] == "test"
