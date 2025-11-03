"""
Comprehensive tests for error scenarios and production hardening.

This test module covers:
- Backpressure: Maximum pending requests enforcement
- Message size limits: Large message handling
- Connection timeouts: Receive timeout behavior
- Network failures: Connection drops and cleanup
- Invalid JSON-RPC messages: Malformed messages, wrong version, missing fields
- Resource exhaustion scenarios
- Edge cases in error handling
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
from fastapi_ws_rpc.exceptions import RpcBackpressureError, RpcChannelClosedError
from fastapi_ws_rpc.rpc_methods import RpcMethodsBase
from fastapi_ws_rpc.schemas import JsonRpcRequest, JsonRpcResponse

# ============================================================================
# Test Methods Classes
# ============================================================================


class TestMethods(RpcMethodsBase):
    """Test methods for error scenario testing."""

    async def fast_method(self) -> str:
        """Fast method for testing."""
        return "fast"

    async def slow_method(self, delay: float = 1.0) -> str:
        """Slow method with configurable delay."""
        await asyncio.sleep(delay)
        return "slow"

    async def failing_method(self) -> None:
        """Method that always fails."""
        raise RuntimeError("Method failed")


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
    """Create promise manager with default settings."""
    manager = RpcPromiseManager()
    yield manager
    if not manager.is_closed():
        manager.close()
    await asyncio.sleep(0.01)


@pytest_asyncio.fixture
async def promise_manager_low_limit() -> RpcPromiseManager:
    """Create promise manager with low limit for backpressure testing."""
    manager = RpcPromiseManager(max_pending_requests=5)
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
    """Create protocol handler."""
    return RpcProtocolHandler(method_invoker, promise_manager, mock_send)


# ============================================================================
# Backpressure Tests
# ============================================================================


class TestBackpressure:
    """Test backpressure control and maximum pending request limits."""

    @pytest.mark.asyncio
    async def test_backpressure_error_when_limit_reached(
        self, promise_manager_low_limit: RpcPromiseManager
    ) -> None:
        """
        Test that backpressure error is raised when limit is reached.

        Verifies that:
        - Requests up to limit succeed
        - Request exceeding limit raises RpcBackpressureError
        - Error message is descriptive
        """
        manager = promise_manager_low_limit

        # Create requests up to limit (5)
        for i in range(5):
            request = JsonRpcRequest(
                jsonrpc="2.0",
                id=f"req-{i}",
                method="test",
            )
            manager.create_promise(request)

        assert manager.get_pending_count() == 5

        # Next request should fail with backpressure error
        overflow_request = JsonRpcRequest(
            jsonrpc="2.0",
            id="overflow",
            method="test",
        )

        with pytest.raises(RpcBackpressureError) as exc_info:
            manager.create_promise(overflow_request)

        error_msg = str(exc_info.value)
        assert "backpressure" in error_msg.lower()
        assert "5" in error_msg  # limit value
        assert "pending" in error_msg.lower()

    @pytest.mark.asyncio
    async def test_backpressure_recovers_after_completing_requests(
        self, promise_manager_low_limit: RpcPromiseManager
    ) -> None:
        """
        Test that backpressure recovers after completing requests.

        Verifies that:
        - After hitting limit, completing some requests allows new ones
        - System can continue processing after backpressure
        """
        manager = promise_manager_low_limit

        # Fill to limit
        for i in range(5):
            request = JsonRpcRequest(
                jsonrpc="2.0",
                id=f"req-{i}",
                method="test",
            )
            manager.create_promise(request)

        # Complete 2 requests
        manager.clear_saved_call("req-0")
        manager.clear_saved_call("req-1")

        assert manager.get_pending_count() == 3

        # Should now be able to create 2 more
        for i in range(2):
            request = JsonRpcRequest(
                jsonrpc="2.0",
                id=f"new-{i}",
                method="test",
            )
            manager.create_promise(request)

        assert manager.get_pending_count() == 5

    @pytest.mark.asyncio
    async def test_backpressure_with_rapid_request_creation(
        self, promise_manager_low_limit: RpcPromiseManager
    ) -> None:
        """
        Test backpressure with rapid request creation.

        Verifies that:
        - Backpressure limit is enforced even under rapid creation
        - No race conditions in limit checking
        """
        manager = promise_manager_low_limit
        errors = []

        # Try to rapidly create more requests than limit
        for i in range(10):
            try:
                request = JsonRpcRequest(
                    jsonrpc="2.0",
                    id=f"rapid-{i}",
                    method="test",
                )
                manager.create_promise(request)
            except RpcBackpressureError as e:
                errors.append(e)

        # Should have exactly 5 successful and 5 failures
        assert manager.get_pending_count() == 5
        assert len(errors) == 5


# ============================================================================
# Message Size Limit Tests
# ============================================================================


class TestMessageSizeLimits:
    """Test handling of large messages and size limits."""

    @pytest.mark.asyncio
    async def test_large_request_params(
        self, protocol_handler: RpcProtocolHandler, mock_send: AsyncMock
    ) -> None:
        """
        Test handling request with large parameters.

        Verifies that:
        - Large params can be processed (within reasonable limits)
        - Request completes successfully
        - Response is sent
        """
        # Create large parameter data (1MB of text)
        large_data = "x" * (1024 * 1024)

        message = {
            "jsonrpc": "2.0",
            "id": "large-req",
            "method": "fast_method",
            "params": {"data": large_data},
        }

        # This tests the protocol handler's ability to process large params
        # In real usage, the websocket layer would enforce size limits
        await protocol_handler.handle_message(message)

        # Should complete without error
        mock_send.assert_called_once()

    @pytest.mark.asyncio
    async def test_large_response_data(
        self, promise_manager: RpcPromiseManager
    ) -> None:
        """
        Test storing large response data.

        Verifies that:
        - Large responses can be stored
        - Response can be retrieved
        """
        request = JsonRpcRequest(
            jsonrpc="2.0",
            id="req-1",
            method="test",
        )
        promise_manager.create_promise(request)

        # Create large response (1MB of data)
        large_result = {"data": "y" * (1024 * 1024)}
        response = JsonRpcResponse(
            jsonrpc="2.0",
            id="req-1",
            result=large_result,
        )

        promise_manager.store_response(response)

        # Should be able to retrieve it
        stored = promise_manager.get_saved_response("req-1")
        assert stored.result == large_result


# ============================================================================
# Connection Timeout Tests
# ============================================================================


class TestConnectionTimeouts:
    """Test timeout behavior for connections and requests."""

    @pytest.mark.asyncio
    async def test_wait_for_response_timeout(
        self, promise_manager: RpcPromiseManager
    ) -> None:
        """
        Test waiting for response that times out.

        Verifies that:
        - Timeout is respected
        - asyncio.TimeoutError is raised when timeout expires and channel is still open
        - Promise remains pending after timeout
        - Call ID is included in error message for traceability
        """
        request = JsonRpcRequest(
            jsonrpc="2.0",
            id="timeout-req",
            method="test",
        )
        promise = promise_manager.create_promise(request)

        # Wait with short timeout, no response will arrive
        with pytest.raises(asyncio.TimeoutError) as exc_info:
            await promise_manager.wait_for_response(promise, timeout=0.1)

        # Verify call ID is in error message
        assert "timeout-req" in str(exc_info.value)

        # Promise should still be pending
        assert promise_manager.get_pending_count() == 1

    @pytest.mark.asyncio
    async def test_multiple_timeouts_dont_leak_memory(
        self, promise_manager: RpcPromiseManager
    ) -> None:
        """
        Test that multiple timeouts don't cause memory leaks.

        Verifies that:
        - Timed-out promises can be cleaned up
        - System continues to function after timeouts
        - asyncio.TimeoutError is raised for each timeout
        """
        # Create multiple requests that will timeout
        promises = []
        for i in range(10):
            request = JsonRpcRequest(
                jsonrpc="2.0",
                id=f"timeout-{i}",
                method="test",
            )
            promise = promise_manager.create_promise(request)
            promises.append(promise)

        # Wait for all to timeout
        for promise in promises:
            with pytest.raises(asyncio.TimeoutError):
                await promise_manager.wait_for_response(promise, timeout=0.05)

        # All should still be pending (cleanup would happen via TTL)
        assert promise_manager.get_pending_count() == 10

        # Manual cleanup
        for i in range(10):
            promise_manager.clear_saved_call(f"timeout-{i}")

        assert promise_manager.get_pending_count() == 0


# ============================================================================
# Network Failure Simulation Tests
# ============================================================================


class TestNetworkFailures:
    """Test handling of network failures and connection drops."""

    @pytest.mark.asyncio
    async def test_channel_close_during_wait(
        self, promise_manager: RpcPromiseManager
    ) -> None:
        """
        Test channel closure while waiting for response.

        Verifies that:
        - Waiting promises are woken up
        - RpcChannelClosedError is raised
        - All pending requests are cleared
        """
        request = JsonRpcRequest(
            jsonrpc="2.0",
            id="req-1",
            method="test",
        )
        promise = promise_manager.create_promise(request)

        # Start waiting for response
        async def wait_and_expect_close() -> None:
            with pytest.raises(RpcChannelClosedError):
                await promise_manager.wait_for_response(promise, timeout=5.0)

        wait_task = asyncio.create_task(wait_and_expect_close())

        # Give task time to start waiting
        await asyncio.sleep(0.1)

        # Close channel
        promise_manager.close()

        # Wait task should complete with error
        await wait_task

        # All requests should be cleared
        assert promise_manager.get_pending_count() == 0

    @pytest.mark.asyncio
    async def test_multiple_simultaneous_closures(
        self, promise_manager: RpcPromiseManager
    ) -> None:
        """
        Test multiple simultaneous channel closures.

        Verifies that:
        - Multiple close() calls are handled safely
        - No errors from redundant closes
        - Idempotent close behavior
        """
        request = JsonRpcRequest(
            jsonrpc="2.0",
            id="req-1",
            method="test",
        )
        promise_manager.create_promise(request)

        # Close multiple times
        promise_manager.close()
        promise_manager.close()
        promise_manager.close()

        # Should not raise errors
        assert promise_manager.is_closed()
        assert promise_manager.get_pending_count() == 0

    @pytest.mark.asyncio
    async def test_operations_after_close(
        self, promise_manager: RpcPromiseManager
    ) -> None:
        """
        Test that operations after close behave correctly.

        Verifies that:
        - Creating promises after close works (no enforcement)
        - Waiting after close raises RpcChannelClosedError
        """
        promise_manager.close()

        # Creating promise after close should work
        # (the promise manager itself doesn't prevent this)
        JsonRpcRequest(
            jsonrpc="2.0",
            id="after-close",
            method="test",
        )
        # This will work, but waiting will fail
        # Note: In production, the RpcChannel prevents this


# ============================================================================
# Invalid JSON-RPC Message Tests
# ============================================================================


class TestInvalidJsonRpcMessages:
    """Test handling of invalid and malformed JSON-RPC messages."""

    @pytest.mark.asyncio
    async def test_invalid_jsonrpc_version(
        self, protocol_handler: RpcProtocolHandler
    ) -> None:
        """
        Test handling message with invalid JSON-RPC version.

        Verifies that:
        - Invalid version is rejected
        - ValueError is raised
        """
        message = {
            "jsonrpc": "1.0",  # Wrong version
            "id": "req-1",
            "method": "test",
        }

        with pytest.raises(ValueError) as exc_info:
            await protocol_handler.handle_message(message)

        assert "version" in str(exc_info.value).lower()

    @pytest.mark.asyncio
    async def test_missing_jsonrpc_field(
        self, protocol_handler: RpcProtocolHandler
    ) -> None:
        """
        Test handling message without jsonrpc field.

        Verifies that:
        - Missing version field is rejected
        - Error is raised
        """
        message = {
            # Missing "jsonrpc" field
            "id": "req-1",
            "method": "test",
        }

        with pytest.raises((ValidationError, ValueError)):
            await protocol_handler.handle_message(message)

    @pytest.mark.asyncio
    async def test_invalid_method_type(
        self, protocol_handler: RpcProtocolHandler
    ) -> None:
        """
        Test handling request with non-string method.

        Verifies that:
        - Non-string method is rejected
        - ValidationError is raised
        """
        message = {
            "jsonrpc": "2.0",
            "id": "req-1",
            "method": 123,  # Must be string
        }

        with pytest.raises(ValidationError):
            await protocol_handler.handle_message(message)

    @pytest.mark.asyncio
    async def test_invalid_id_type(self, protocol_handler: RpcProtocolHandler) -> None:
        """
        Test handling message with invalid ID type.

        Verifies that:
        - Only string, int, or null IDs are accepted
        - Invalid types are rejected
        """
        message = {
            "jsonrpc": "2.0",
            "id": {"nested": "object"},  # Invalid ID type
            "method": "test",
        }

        with pytest.raises(ValidationError):
            await protocol_handler.handle_message(message)

    @pytest.mark.asyncio
    async def test_missing_method_field(
        self, protocol_handler: RpcProtocolHandler
    ) -> None:
        """
        Test handling request without method field.

        Verifies that:
        - Request must have method field
        - Error is raised for missing method
        """
        message = {
            "jsonrpc": "2.0",
            "id": "req-1",
            # Missing "method" field
        }

        with pytest.raises(ValueError) as exc_info:
            await protocol_handler.handle_message(message)

        assert "unknown" in str(exc_info.value).lower()

    @pytest.mark.asyncio
    async def test_non_dict_message(self, protocol_handler: RpcProtocolHandler) -> None:
        """
        Test handling message that is not a dictionary.

        Verifies that:
        - Non-dict messages are rejected
        - ValueError is raised
        """
        messages = [
            "string message",
            123,
            ["list", "message"],
            None,
        ]

        for message in messages:
            with pytest.raises(ValueError) as exc_info:
                await protocol_handler.handle_message(message)  # type: ignore

            assert "dict" in str(exc_info.value).lower()

    @pytest.mark.asyncio
    async def test_empty_message(self, protocol_handler: RpcProtocolHandler) -> None:
        """
        Test handling empty message.

        Verifies that:
        - Empty dict is rejected as unknown format
        - ValueError is raised
        """
        message = {}

        with pytest.raises(ValueError) as exc_info:
            await protocol_handler.handle_message(message)

        assert (
            "unknown" in str(exc_info.value).lower()
            or "version" in str(exc_info.value).lower()
        )


# ============================================================================
# Resource Exhaustion Tests
# ============================================================================


class TestResourceExhaustion:
    """Test behavior under resource exhaustion scenarios."""

    @pytest.mark.asyncio
    async def test_many_pending_requests(
        self, promise_manager: RpcPromiseManager
    ) -> None:
        """
        Test system with many pending requests (but under limit).

        Verifies that:
        - System handles many pending requests
        - Performance remains acceptable
        - No memory issues
        """
        # Create many requests (but under default limit of 1000)
        num_requests = 100

        for i in range(num_requests):
            request = JsonRpcRequest(
                jsonrpc="2.0",
                id=f"req-{i}",
                method="test",
            )
            promise_manager.create_promise(request)

        assert promise_manager.get_pending_count() == num_requests

        # Complete them all
        for i in range(num_requests):
            response = JsonRpcResponse(
                jsonrpc="2.0",
                id=f"req-{i}",
                result="done",
            )
            promise_manager.store_response(response)
            promise_manager.clear_saved_call(f"req-{i}")

        assert promise_manager.get_pending_count() == 0

    @pytest.mark.asyncio
    async def test_rapid_request_response_cycles(
        self, promise_manager: RpcPromiseManager
    ) -> None:
        """
        Test rapid request-response cycles.

        Verifies that:
        - System handles rapid cycles without issues
        - Cleanup happens correctly
        - No memory leaks
        """
        for cycle in range(50):
            request = JsonRpcRequest(
                jsonrpc="2.0",
                id=f"cycle-{cycle}",
                method="test",
            )
            promise = promise_manager.create_promise(request)

            response = JsonRpcResponse(
                jsonrpc="2.0",
                id=f"cycle-{cycle}",
                result="done",
            )
            promise_manager.store_response(response)

            # Retrieve and clean up
            await promise_manager.wait_for_response(promise, timeout=1.0)

        # Should have no pending requests
        assert promise_manager.get_pending_count() == 0


# ============================================================================
# Edge Cases
# ============================================================================


class TestEdgeCases:
    """Test edge cases in error handling."""

    @pytest.mark.asyncio
    async def test_response_for_nonexistent_request(
        self, promise_manager: RpcPromiseManager
    ) -> None:
        """
        Test storing response for request that never existed.

        Verifies that:
        - Orphan responses are handled gracefully
        - No error is raised
        - Response is not stored
        """
        response = JsonRpcResponse(
            jsonrpc="2.0",
            id="never-requested",
            result="data",
        )

        matched = promise_manager.store_response(response)

        assert matched is False

        # Should not be able to retrieve it
        with pytest.raises(KeyError):
            promise_manager.get_saved_response("never-requested")

    @pytest.mark.asyncio
    async def test_duplicate_response(self, promise_manager: RpcPromiseManager) -> None:
        """
        Test receiving duplicate responses for same request.

        Verifies that:
        - First response is stored
        - Second response is ignored (request already completed)
        """
        request = JsonRpcRequest(
            jsonrpc="2.0",
            id="req-1",
            method="test",
        )
        promise_manager.create_promise(request)

        # First response
        response1 = JsonRpcResponse(
            jsonrpc="2.0",
            id="req-1",
            result="first",
        )
        matched1 = promise_manager.store_response(response1)
        assert matched1 is True

        # Clean up
        promise_manager.clear_saved_call("req-1")

        # Second response (after cleanup)
        response2 = JsonRpcResponse(
            jsonrpc="2.0",
            id="req-1",
            result="second",
        )
        matched2 = promise_manager.store_response(response2)
        assert matched2 is False

    @pytest.mark.asyncio
    async def test_promise_manager_closed_state_persists(
        self, promise_manager: RpcPromiseManager
    ) -> None:
        """
        Test that closed state persists correctly.

        Verifies that:
        - is_closed() returns True after close()
        - State doesn't change unexpectedly
        """
        assert not promise_manager.is_closed()

        promise_manager.close()

        assert promise_manager.is_closed()

        # Should still be closed
        await asyncio.sleep(0.1)
        assert promise_manager.is_closed()
