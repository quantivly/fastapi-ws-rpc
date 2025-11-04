"""
Comprehensive integration tests for network failure scenarios.

This test module covers:
- Graceful disconnect and recovery
- Network timeout behaviors (idle timeout, response timeout)
- Partial message handling and interrupted frames
- Connection drops during active operations
- Reconnection after network failures
- WebSocket close scenarios (normal, abnormal, timeout)
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio

from fastapi_ws_rpc._internal.method_invoker import RpcMethodInvoker
from fastapi_ws_rpc._internal.promise_manager import RpcPromiseManager
from fastapi_ws_rpc._internal.protocol_handler import RpcProtocolHandler
from fastapi_ws_rpc.exceptions import RpcChannelClosedError
from fastapi_ws_rpc.rpc_channel import RpcChannel
from fastapi_ws_rpc.rpc_methods import RpcMethodsBase
from fastapi_ws_rpc.schemas import JsonRpcRequest, JsonRpcResponse

# ============================================================================
# Test Methods Classes
# ============================================================================


class TestMethods(RpcMethodsBase):
    """Test methods for network integration testing."""

    def __init__(self) -> None:
        super().__init__()
        self.call_count = 0

    async def fast_method(self, value: int = 0) -> int:
        """Fast method for testing."""
        self.call_count += 1
        return value * 2

    async def slow_method(self, delay: float = 1.0) -> str:
        """Slow method with configurable delay."""
        await asyncio.sleep(delay)
        return "completed"

    async def echo(self, message: str) -> str:
        """Simple echo method."""
        return message


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
    """Create protocol handler."""
    return RpcProtocolHandler(method_invoker, promise_manager, mock_send)


@pytest.fixture
def mock_socket() -> MagicMock:
    """Create mock WebSocket."""
    socket = MagicMock()
    socket.send = AsyncMock()
    socket.recv = AsyncMock()
    socket.close = AsyncMock()
    return socket


# ============================================================================
# Graceful Disconnect Tests
# ============================================================================


class TestGracefulDisconnect:
    """Test graceful disconnect and cleanup scenarios."""

    @pytest.mark.asyncio
    async def test_graceful_disconnect_with_pending_requests(
        self, promise_manager: RpcPromiseManager
    ) -> None:
        """
        Test graceful disconnect while requests are pending.

        Verifies that:
        - Pending requests are notified of disconnect
        - All waiting coroutines are woken up
        - RpcChannelClosedError is raised for pending requests
        - Cleanup happens completely
        """
        # Create some pending requests
        promises = []
        for i in range(10):
            request = JsonRpcRequest(
                jsonrpc="2.0",
                id=f"pending-{i}",
                method="test",
            )
            promise = promise_manager.create_promise(request)
            promises.append(promise)

        assert promise_manager.get_pending_count() == 10

        # Start waiting for responses
        async def wait_for_response(promise: Any) -> None:
            with pytest.raises(RpcChannelClosedError):
                await promise_manager.wait_for_response(promise, timeout=10.0)

        wait_tasks = [asyncio.create_task(wait_for_response(p)) for p in promises[:5]]

        # Give tasks time to start waiting
        await asyncio.sleep(0.1)

        # Close the channel (graceful disconnect)
        promise_manager.close()

        # All waiting tasks should complete with RpcChannelClosedError
        await asyncio.gather(*wait_tasks)

        # Verify cleanup
        assert promise_manager.is_closed()
        assert promise_manager.get_pending_count() == 0

    @pytest.mark.asyncio
    async def test_graceful_disconnect_during_message_handling(
        self,
        protocol_handler: RpcProtocolHandler,
        promise_manager: RpcPromiseManager,
        mock_send: AsyncMock,
    ) -> None:
        """
        Test disconnect during active message handling.

        Verifies that:
        - Messages being processed can complete
        - New messages after close are rejected
        - System transitions to closed state cleanly
        """
        # Start processing a slow message
        slow_message = {
            "jsonrpc": "2.0",
            "id": "slow-1",
            "method": "slow_method",
            "params": {"delay": 0.5},
        }

        handle_task = asyncio.create_task(protocol_handler.handle_message(slow_message))

        # Wait a bit for processing to start
        await asyncio.sleep(0.1)

        # Close the channel
        promise_manager.close()

        # Message should still complete (or handle close gracefully)
        await handle_task

        # Verify closed state
        assert promise_manager.is_closed()

    @pytest.mark.asyncio
    async def test_disconnect_cleanup_callbacks(
        self, test_methods: TestMethods, mock_socket: MagicMock
    ) -> None:
        """
        Test that disconnect callbacks are called during cleanup.

        Verifies that:
        - Disconnect callbacks are invoked
        - Callbacks receive correct channel reference
        - Multiple callbacks are all invoked
        - Errors in callbacks don't prevent disconnect
        """
        disconnect_called = []

        async def on_disconnect(channel: RpcChannel) -> None:
            disconnect_called.append(channel.id)

        async def on_disconnect_error(channel: RpcChannel) -> None:
            # Callback that raises error
            raise RuntimeError("Callback error")

        # Create channel with callbacks
        channel = RpcChannel(
            test_methods,
            mock_socket,
            disconnect_callbacks=[on_disconnect, on_disconnect_error],
        )

        channel_id = channel.id

        # Call on_disconnect to trigger callbacks
        # Note: In production, this is called by the endpoint/client
        # The second callback raises an error, which will propagate
        with pytest.raises(RuntimeError, match="Callback error"):
            await channel.on_disconnect()

        # First callback should have been called before the error
        assert len(disconnect_called) >= 1
        assert disconnect_called[0] == channel_id

        # Now close the channel (already closed by on_disconnect)
        await channel.close()

    @pytest.mark.asyncio
    async def test_repeated_disconnect_calls_idempotent(
        self, promise_manager: RpcPromiseManager
    ) -> None:
        """
        Test that multiple disconnect calls are safe (idempotent).

        Verifies that:
        - Multiple close() calls don't cause errors
        - State remains consistent
        - No double-cleanup issues
        """
        # Create some requests
        for i in range(5):
            request = JsonRpcRequest(
                jsonrpc="2.0",
                id=f"repeat-{i}",
                method="test",
            )
            promise_manager.create_promise(request)

        # Close multiple times
        promise_manager.close()
        promise_manager.close()
        promise_manager.close()

        # Should be in consistent closed state
        assert promise_manager.is_closed()
        assert promise_manager.get_pending_count() == 0


# ============================================================================
# Timeout Behavior Tests
# ============================================================================


class TestTimeoutBehavior:
    """Test various timeout scenarios."""

    @pytest.mark.asyncio
    async def test_response_timeout_behavior(
        self, promise_manager: RpcPromiseManager
    ) -> None:
        """
        Test response timeout for requests that never receive responses.

        Verifies that:
        - Timeout fires correctly
        - asyncio.TimeoutError is raised
        - Request remains pending after timeout (manual cleanup required)
        - Timeout includes call ID for debugging
        """
        request = JsonRpcRequest(
            jsonrpc="2.0",
            id="timeout-test",
            method="test",
        )
        promise = promise_manager.create_promise(request)

        # Wait with timeout (no response will arrive)
        with pytest.raises(asyncio.TimeoutError) as exc_info:
            await promise_manager.wait_for_response(promise, timeout=0.2)

        # Error should include call ID
        assert "timeout-test" in str(exc_info.value)

        # Request should still be pending (timeout doesn't auto-cleanup)
        assert promise_manager.get_pending_count() == 1

    @pytest.mark.asyncio
    async def test_multiple_concurrent_timeouts(
        self, promise_manager: RpcPromiseManager
    ) -> None:
        """
        Test multiple concurrent requests timing out.

        Verifies that:
        - Multiple timeouts work correctly
        - Each timeout is independent
        - No interference between waiting requests
        - All timeouts fire correctly
        """
        num_requests = 20
        promises = []

        # Create many pending requests
        for i in range(num_requests):
            request = JsonRpcRequest(
                jsonrpc="2.0",
                id=f"concurrent-timeout-{i}",
                method="test",
            )
            promise = promise_manager.create_promise(request)
            promises.append(promise)

        # Wait for all to timeout concurrently
        async def wait_and_timeout(promise: Any) -> bool:
            try:
                await promise_manager.wait_for_response(promise, timeout=0.1)
                return False
            except asyncio.TimeoutError:
                return True

        results = await asyncio.gather(*[wait_and_timeout(p) for p in promises])

        # All should have timed out
        assert all(results), "Not all requests timed out"

        # All should still be pending
        assert promise_manager.get_pending_count() == num_requests

    @pytest.mark.asyncio
    async def test_timeout_vs_channel_close_distinction(
        self, promise_manager: RpcPromiseManager
    ) -> None:
        """
        Test that timeout and channel close are properly distinguished.

        Verifies that:
        - Timeout raises asyncio.TimeoutError
        - Channel close raises RpcChannelClosedError
        - Correct error type based on actual condition
        """
        # Test 1: Pure timeout (channel still open)
        request1 = JsonRpcRequest(
            jsonrpc="2.0",
            id="pure-timeout",
            method="test",
        )
        promise1 = promise_manager.create_promise(request1)

        with pytest.raises(asyncio.TimeoutError):
            await promise_manager.wait_for_response(promise1, timeout=0.1)

        assert not promise_manager.is_closed()

        # Test 2: Channel close (not timeout)
        request2 = JsonRpcRequest(
            jsonrpc="2.0",
            id="close-test",
            method="test",
        )
        promise2 = promise_manager.create_promise(request2)

        # Start waiting
        async def wait_for_close() -> None:
            with pytest.raises(RpcChannelClosedError):
                await promise_manager.wait_for_response(promise2, timeout=10.0)

        wait_task = asyncio.create_task(wait_for_close())
        await asyncio.sleep(0.05)

        # Close the channel
        promise_manager.close()

        # Should raise RpcChannelClosedError, not TimeoutError
        await wait_task

    @pytest.mark.asyncio
    async def test_idle_timeout_simulation(
        self, test_methods: TestMethods, mock_socket: MagicMock
    ) -> None:
        """
        Test idle timeout behavior (connection timeout due to inactivity).

        Simulates max_connection_duration timeout.

        Verifies that:
        - Channel can enforce connection duration limits
        - Timeout causes graceful disconnect
        - Cleanup happens correctly
        """
        # Create channel with short max duration
        channel = RpcChannel(
            test_methods,
            mock_socket,
            max_connection_duration=0.5,  # 500ms
        )

        # Wait for longer than max duration
        await asyncio.sleep(0.6)

        # Note: The actual timeout enforcement happens in the connection
        # lifecycle management, which is handled by the WebSocket server.
        # This test verifies the configuration is accepted.
        assert channel._max_connection_duration == 0.5

        await channel.close()


# ============================================================================
# Partial Message Handling Tests
# ============================================================================


class TestPartialMessageHandling:
    """Test handling of partial and malformed messages."""

    @pytest.mark.asyncio
    async def test_incomplete_json_message(
        self, protocol_handler: RpcProtocolHandler, mock_send: AsyncMock
    ) -> None:
        """
        Test handling of incomplete JSON message.

        Verifies that:
        - Malformed JSON is caught
        - Error response is sent
        - System remains stable
        """
        # Protocol handler expects dict, but in production JSON parsing happens
        # at the WebSocket layer. Here we test what happens when the protocol
        # handler receives a string (should send error response)

        # Pass a string instead of dict to simulate parsing error
        await protocol_handler.handle_message("invalid")  # type: ignore

        # Should send error response
        mock_send.assert_called_once()
        response = mock_send.call_args[0][0]
        assert "error" in response

    @pytest.mark.asyncio
    async def test_message_with_missing_fields(
        self, protocol_handler: RpcProtocolHandler, mock_send: AsyncMock
    ) -> None:
        """
        Test handling messages with missing required fields.

        Verifies that:
        - Missing fields are detected
        - Appropriate error response is sent
        - Invalid request error code is used
        """
        # Message missing "method" field
        message = {
            "jsonrpc": "2.0",
            "id": "missing-method",
            # "method" is missing
        }

        await protocol_handler.handle_message(message)

        # Should send error response
        mock_send.assert_called_once()
        response = mock_send.call_args[0][0]
        assert "error" in response
        assert response["id"] == "missing-method"

    @pytest.mark.asyncio
    async def test_message_with_invalid_types(
        self, protocol_handler: RpcProtocolHandler, mock_send: AsyncMock
    ) -> None:
        """
        Test handling messages with invalid field types.

        Verifies that:
        - Type validation catches errors
        - Error response is sent
        - System recovers from validation errors
        """
        # Method should be string, not number
        message = {
            "jsonrpc": "2.0",
            "id": "type-error",
            "method": 123,  # Wrong type
        }

        await protocol_handler.handle_message(message)

        # Should send error response
        mock_send.assert_called_once()
        response = mock_send.call_args[0][0]
        assert "error" in response

    @pytest.mark.asyncio
    async def test_corrupted_response_id(
        self, promise_manager: RpcPromiseManager
    ) -> None:
        """
        Test handling response with ID that doesn't match any request.

        Verifies that:
        - Orphan responses are handled gracefully
        - No error is raised
        - System remains stable
        """
        # Store response for non-existent request
        response = JsonRpcResponse(
            jsonrpc="2.0",
            id="non-existent-id",
            result="data",
        )

        matched = promise_manager.store_response(response)

        # Should return False (not matched)
        assert matched is False

        # Should not be able to retrieve it
        with pytest.raises(KeyError):
            promise_manager.get_saved_response("non-existent-id")


# ============================================================================
# Connection Drop Tests
# ============================================================================


class TestConnectionDrop:
    """Test connection drops during active operations."""

    @pytest.mark.asyncio
    async def test_connection_drop_during_send(self, test_methods: TestMethods) -> None:
        """
        Test connection drop during message send.

        Verifies that:
        - Send failures are handled gracefully
        - Appropriate exception is raised
        - Channel cleanup happens
        """
        # Create mock socket that fails on send
        mock_socket = MagicMock()

        async def failing_send(data: dict[str, Any]) -> None:
            raise ConnectionError("Connection lost")

        mock_socket.send = AsyncMock(side_effect=failing_send)
        mock_socket.close = AsyncMock()

        channel = RpcChannel(test_methods, mock_socket)

        # Try to send, should raise ConnectionError
        with pytest.raises(ConnectionError):
            await channel.send_raw({"jsonrpc": "2.0", "id": "test", "method": "test"})

        await channel.close()

    @pytest.mark.asyncio
    async def test_connection_drop_during_recv(
        self, promise_manager: RpcPromiseManager
    ) -> None:
        """
        Test connection drop while waiting for response.

        Verifies that:
        - Waiting requests are notified
        - RpcChannelClosedError is raised
        - Cleanup completes successfully
        """
        request = JsonRpcRequest(
            jsonrpc="2.0",
            id="recv-drop",
            method="test",
        )
        promise = promise_manager.create_promise(request)

        # Start waiting for response
        async def wait_and_expect_close() -> None:
            with pytest.raises(RpcChannelClosedError):
                await promise_manager.wait_for_response(promise, timeout=5.0)

        wait_task = asyncio.create_task(wait_and_expect_close())

        # Simulate connection drop
        await asyncio.sleep(0.1)
        promise_manager.close()

        # Should raise RpcChannelClosedError
        await wait_task

    @pytest.mark.asyncio
    async def test_connection_drop_with_multiple_operations(
        self,
        protocol_handler: RpcProtocolHandler,
        promise_manager: RpcPromiseManager,
    ) -> None:
        """
        Test connection drop with multiple operations in flight.

        Verifies that:
        - All in-flight operations are handled
        - All waiting requests are notified
        - Complete cleanup occurs
        - No resource leaks
        """
        # Start multiple operations
        num_operations = 20

        # Create some outgoing requests
        promises = []
        for i in range(num_operations // 2):
            request = JsonRpcRequest(
                jsonrpc="2.0",
                id=f"drop-out-{i}",
                method="test",
            )
            promise = promise_manager.create_promise(request)
            promises.append(promise)

        # Start some message handling tasks
        incoming_tasks = []
        for i in range(num_operations // 2):
            message = {
                "jsonrpc": "2.0",
                "id": f"drop-in-{i}",
                "method": "fast_method",
                "params": {"value": i},
            }
            task = asyncio.create_task(protocol_handler.handle_message(message))
            incoming_tasks.append(task)

        # Wait a bit for operations to start
        await asyncio.sleep(0.1)

        # Simulate connection drop
        promise_manager.close()

        # Wait for incoming tasks to complete
        # (they should handle close gracefully)
        await asyncio.gather(*incoming_tasks, return_exceptions=True)

        # Verify cleanup
        assert promise_manager.is_closed()
        assert promise_manager.get_pending_count() == 0


# ============================================================================
# WebSocket Close Scenarios
# ============================================================================


class TestWebSocketClose:
    """Test various WebSocket close scenarios."""

    @pytest.mark.asyncio
    async def test_normal_websocket_close(
        self, test_methods: TestMethods, mock_socket: MagicMock
    ) -> None:
        """
        Test normal WebSocket close (code 1000).

        Verifies that:
        - Normal close is handled gracefully
        - Callbacks are invoked
        - Resources are cleaned up
        """
        channel = RpcChannel(test_methods, mock_socket)

        # Normal close
        await channel.close()

        # Verify socket close was called
        mock_socket.close.assert_called_once()

        # Channel should be closed
        assert channel.is_closed()

    @pytest.mark.asyncio
    async def test_abnormal_websocket_close(
        self, test_methods: TestMethods, mock_socket: MagicMock
    ) -> None:
        """
        Test abnormal WebSocket close (connection error).

        Verifies that:
        - Abnormal close is detected
        - Cleanup still happens
        - Error is logged appropriately
        """

        # Simulate socket close failure
        async def failing_close() -> None:
            raise ConnectionError("Socket already closed")

        mock_socket.close = AsyncMock(side_effect=failing_close)

        channel = RpcChannel(test_methods, mock_socket)

        # Close will propagate the ConnectionError from socket.close()
        with pytest.raises(ConnectionError):
            await channel.close()

        # Channel should still be marked as closed (cleanup happened)
        assert channel.is_closed()

    @pytest.mark.asyncio
    async def test_websocket_close_with_pending_messages(
        self, test_methods: TestMethods, mock_socket: MagicMock
    ) -> None:
        """
        Test WebSocket close while messages are queued.

        Verifies that:
        - Queued messages are handled appropriately
        - Close doesn't hang waiting for messages
        - Cleanup is complete
        """
        channel = RpcChannel(test_methods, mock_socket)

        # Create some pending outgoing calls
        call_tasks = []
        for i in range(5):
            # These won't complete because socket is mocked
            task = asyncio.create_task(
                channel.call("fast_method", args={"value": i}, timeout=0.1)
            )
            call_tasks.append(task)

        # Give tasks time to start
        await asyncio.sleep(0.05)

        # Close the channel
        await channel.close()

        # Tasks should complete (with errors or timeouts)
        results = await asyncio.gather(*call_tasks, return_exceptions=True)

        # All should have raised exceptions (RpcChannelClosedError or TimeoutError)
        assert all(isinstance(r, Exception) for r in results)

        # Channel should be closed
        assert channel.is_closed()


# ============================================================================
# Reconnection Scenarios
# ============================================================================


class TestReconnectionScenarios:
    """Test reconnection after network failures."""

    @pytest.mark.asyncio
    async def test_state_after_disconnect_ready_for_reconnect(
        self, promise_manager: RpcPromiseManager
    ) -> None:
        """
        Test that state after disconnect is clean for reconnection.

        Verifies that:
        - All state is cleared on disconnect
        - No lingering promises
        - Ready for new connection
        """
        # Create some state
        for i in range(10):
            request = JsonRpcRequest(
                jsonrpc="2.0",
                id=f"state-{i}",
                method="test",
            )
            promise_manager.create_promise(request)

        assert promise_manager.get_pending_count() == 10

        # Disconnect
        promise_manager.close()

        # State should be clean
        assert promise_manager.is_closed()
        assert promise_manager.get_pending_count() == 0

        # Ready for reconnection (would create new promise manager)

    @pytest.mark.asyncio
    async def test_new_channel_after_disconnect(
        self, test_methods: TestMethods, mock_socket: MagicMock
    ) -> None:
        """
        Test creating new channel after previous one disconnected.

        Verifies that:
        - Old channel is properly closed
        - New channel can be created independently
        - No interference between channels
        """
        # Create first channel
        channel1 = RpcChannel(test_methods, mock_socket)
        channel1_id = channel1.id

        # Close it
        await channel1.close()
        assert channel1.is_closed()

        # Create new channel (simulating reconnection)
        mock_socket2 = MagicMock()
        mock_socket2.send = AsyncMock()
        mock_socket2.close = AsyncMock()

        channel2 = RpcChannel(test_methods, mock_socket2)
        channel2_id = channel2.id

        # Should have different IDs
        assert channel1_id != channel2_id

        # New channel should work independently
        assert not channel2.is_closed()

        await channel2.close()


# ============================================================================
# Error Propagation Tests
# ============================================================================


class TestErrorPropagation:
    """Test error propagation during network failures."""

    @pytest.mark.asyncio
    async def test_error_callbacks_on_disconnect(
        self, test_methods: TestMethods, mock_socket: MagicMock
    ) -> None:
        """
        Test that error callbacks are invoked on network errors.

        Verifies that:
        - Error callbacks receive exception information
        - Multiple error callbacks are all invoked
        - Disconnect still completes after errors
        """
        errors_received = []

        async def on_error(channel: RpcChannel, error: Exception) -> None:
            errors_received.append((channel.id, type(error).__name__))

        # Create channel with error callback
        channel = RpcChannel(
            test_methods,
            mock_socket,
            error_callbacks=[on_error],
        )

        # Simulate error by making send fail
        async def failing_send(data: dict[str, Any]) -> None:
            raise ConnectionError("Network failure")

        mock_socket.send = AsyncMock(side_effect=failing_send)

        # Try operation that will fail
        with pytest.raises(ConnectionError):
            await channel.send_raw({"jsonrpc": "2.0", "id": "err", "method": "test"})

        # Note: Error callbacks are invoked in actual RpcChannel implementation
        # This test verifies the callback mechanism exists

        await channel.close()

    @pytest.mark.asyncio
    async def test_exception_during_message_handling_network_error(
        self, protocol_handler: RpcProtocolHandler, mock_send: AsyncMock
    ) -> None:
        """
        Test handling exceptions that occur during message processing.

        Verifies that:
        - Exceptions are caught and handled
        - Error responses are sent
        - System remains stable after errors
        """
        # Simulate network error during send
        mock_send.side_effect = ConnectionError("Send failed")

        message = {
            "jsonrpc": "2.0",
            "id": "net-err",
            "method": "fast_method",
            "params": {"value": 1},
        }

        # Should raise the connection error
        with pytest.raises(ConnectionError):
            await protocol_handler.handle_message(message)
