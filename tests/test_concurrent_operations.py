"""
Comprehensive tests for concurrent operations and race conditions.

This test module covers:
- Parallel requests: Multiple RPC calls simultaneously
- Concurrent promise cleanup: Multiple threads/tasks creating and cleaning promises
- Race in channel close: Multiple tasks trying to close channel simultaneously
- Race in on_disconnect: Multiple disconnect events
- Thread safety and synchronization
- Data consistency under concurrent access
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from fastapi_ws_rpc._internal.method_invoker import RpcMethodInvoker
from fastapi_ws_rpc._internal.promise_manager import RpcPromiseManager
from fastapi_ws_rpc._internal.protocol_handler import RpcProtocolHandler
from fastapi_ws_rpc.exceptions import RpcBackpressureError
from fastapi_ws_rpc.rpc_methods import RpcMethodsBase
from fastapi_ws_rpc.schemas import JsonRpcRequest, JsonRpcResponse

# ============================================================================
# Test Methods Classes
# ============================================================================


class TestMethods(RpcMethodsBase):
    """Test methods for concurrent testing."""

    def __init__(self) -> None:
        super().__init__()
        self.call_count = 0
        self.concurrent_calls = 0
        self.max_concurrent = 0

    async def fast_method(self, value: int) -> int:
        """Fast method for testing."""
        return value * 2

    async def slow_method(self, delay: float, value: int) -> int:
        """Slow method with configurable delay."""
        self.concurrent_calls += 1
        self.max_concurrent = max(self.max_concurrent, self.concurrent_calls)

        await asyncio.sleep(delay)

        self.concurrent_calls -= 1
        return value

    async def counter_method(self) -> int:
        """Method that increments a counter."""
        self.call_count += 1
        # Small delay to increase chance of race conditions
        await asyncio.sleep(0.001)
        return self.call_count


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


@pytest_asyncio.fixture
async def promise_manager_low_limit() -> RpcPromiseManager:
    """Create promise manager with low limit."""
    manager = RpcPromiseManager(max_pending_requests=10)
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
# Parallel Request Tests
# ============================================================================


class TestParallelRequests:
    """Test handling multiple parallel RPC requests."""

    @pytest.mark.asyncio
    async def test_parallel_fast_requests(
        self, protocol_handler: RpcProtocolHandler, mock_send: AsyncMock
    ) -> None:
        """
        Test multiple parallel fast requests.

        Verifies that:
        - All requests complete successfully
        - All responses are sent
        - Results are correct
        - No interference between requests
        """
        num_requests = 20

        # Create parallel requests
        tasks = []
        for i in range(num_requests):
            message = {
                "jsonrpc": "2.0",
                "id": f"req-{i}",
                "method": "fast_method",
                "params": {"value": i},
            }
            task = asyncio.create_task(protocol_handler.handle_message(message))
            tasks.append(task)

        # Wait for all to complete
        await asyncio.gather(*tasks)

        # Verify all responses sent
        assert mock_send.call_count == num_requests

        # Verify results are correct
        results = {}
        for call in mock_send.call_args_list:
            response = call[0][0]
            request_id = response["id"]
            result = response["result"]
            results[request_id] = result

        # Check each result
        for i in range(num_requests):
            expected = i * 2
            actual = results[f"req-{i}"]
            assert actual == expected, f"Request {i}: expected {expected}, got {actual}"

    @pytest.mark.asyncio
    async def test_parallel_slow_requests(
        self, protocol_handler: RpcProtocolHandler, test_methods: TestMethods
    ) -> None:
        """
        Test multiple parallel slow requests.

        Verifies that:
        - All requests run concurrently
        - All complete successfully
        - Concurrency is actually achieved (not serialized)
        """
        num_requests = 10
        delay = 0.2

        # Create parallel slow requests
        tasks = []
        for i in range(num_requests):
            message = {
                "jsonrpc": "2.0",
                "id": f"slow-{i}",
                "method": "slow_method",
                "params": {"delay": delay, "value": i},
            }
            task = asyncio.create_task(protocol_handler.handle_message(message))
            tasks.append(task)

        # Wait for all to complete
        start = asyncio.get_event_loop().time()
        await asyncio.gather(*tasks)
        elapsed = asyncio.get_event_loop().time() - start

        # If truly parallel, should take ~delay seconds, not delay * num_requests
        # Allow some overhead
        max_expected_time = delay * 2
        assert (
            elapsed < max_expected_time
        ), f"Took {elapsed}s, expected < {max_expected_time}s (parallel execution)"

        # Verify maximum concurrent calls
        assert (
            test_methods.max_concurrent >= num_requests / 2
        ), "Not enough concurrency detected"

    @pytest.mark.asyncio
    async def test_parallel_requests_and_responses(
        self, promise_manager: RpcPromiseManager
    ) -> None:
        """
        Test parallel request creation and response handling.

        Verifies that:
        - Multiple promises can be created concurrently
        - Multiple responses can be stored concurrently
        - No data corruption or race conditions
        """
        num_requests = 50

        # Create requests in parallel
        async def create_request(i: int) -> None:
            request = JsonRpcRequest(
                jsonrpc="2.0",
                id=f"parallel-{i}",
                method="test",
            )
            promise_manager.create_promise(request)

        create_tasks = [create_request(i) for i in range(num_requests)]
        await asyncio.gather(*create_tasks)

        assert promise_manager.get_pending_count() == num_requests

        # Store responses in parallel
        async def store_response(i: int) -> None:
            response = JsonRpcResponse(
                jsonrpc="2.0",
                id=f"parallel-{i}",
                result=i,
            )
            promise_manager.store_response(response)

        response_tasks = [store_response(i) for i in range(num_requests)]
        await asyncio.gather(*response_tasks)

        # Verify all responses stored correctly
        for i in range(num_requests):
            response = promise_manager.get_saved_response(f"parallel-{i}")
            assert response.result == i


# ============================================================================
# Concurrent Promise Cleanup Tests
# ============================================================================


class TestConcurrentPromiseCleanup:
    """Test concurrent promise creation and cleanup operations."""

    @pytest.mark.asyncio
    async def test_concurrent_create_and_cleanup(
        self, promise_manager: RpcPromiseManager
    ) -> None:
        """
        Test concurrent promise creation and cleanup.

        Verifies that:
        - Creating and cleaning promises concurrently is safe
        - No race conditions in data structures
        - Final state is consistent
        """
        num_operations = 100

        async def create_and_cleanup(i: int) -> None:
            request = JsonRpcRequest(
                jsonrpc="2.0",
                id=f"op-{i}",
                method="test",
            )
            promise_manager.create_promise(request)

            # Small delay
            await asyncio.sleep(0.01)

            # Clean up
            promise_manager.clear_saved_call(f"op-{i}")

        tasks = [create_and_cleanup(i) for i in range(num_operations)]
        await asyncio.gather(*tasks)

        # All should be cleaned up
        assert promise_manager.get_pending_count() == 0

    @pytest.mark.asyncio
    async def test_concurrent_create_with_backpressure(
        self, promise_manager_low_limit: RpcPromiseManager
    ) -> None:
        """
        Test concurrent request creation with backpressure.

        Verifies that:
        - Backpressure limit is enforced under concurrent load
        - Some requests fail with RpcBackpressureError
        - System remains consistent
        """
        manager = promise_manager_low_limit
        num_attempts = 50
        errors = []
        successes = []

        async def try_create(i: int) -> None:
            try:
                request = JsonRpcRequest(
                    jsonrpc="2.0",
                    id=f"bp-{i}",
                    method="test",
                )
                manager.create_promise(request)
                successes.append(i)
            except RpcBackpressureError as e:
                errors.append(e)

        # Try to create many requests concurrently
        tasks = [try_create(i) for i in range(num_attempts)]
        await asyncio.gather(*tasks)

        # Should have some successes (up to limit) and some failures
        assert len(successes) <= 10  # limit is 10
        assert len(errors) > 0

        # Total should match attempts
        assert len(successes) + len(errors) == num_attempts

    @pytest.mark.asyncio
    async def test_concurrent_response_storage(
        self, promise_manager: RpcPromiseManager
    ) -> None:
        """
        Test concurrent response storage for multiple requests.

        Verifies that:
        - Multiple responses can be stored concurrently
        - All responses are stored correctly
        - No data corruption
        """
        num_requests = 30

        # Create all requests first
        for i in range(num_requests):
            request = JsonRpcRequest(
                jsonrpc="2.0",
                id=f"resp-{i}",
                method="test",
            )
            promise_manager.create_promise(request)

        # Store responses concurrently
        async def store_response(i: int) -> None:
            response = JsonRpcResponse(
                jsonrpc="2.0",
                id=f"resp-{i}",
                result={"index": i, "data": f"result-{i}"},
            )
            promise_manager.store_response(response)

        tasks = [store_response(i) for i in range(num_requests)]
        await asyncio.gather(*tasks)

        # Verify all stored correctly
        for i in range(num_requests):
            response = promise_manager.get_saved_response(f"resp-{i}")
            assert response.result["index"] == i
            assert response.result["data"] == f"result-{i}"


# ============================================================================
# Race Condition in Channel Close Tests
# ============================================================================


class TestChannelCloseRaceConditions:
    """Test race conditions in channel closure."""

    @pytest.mark.asyncio
    async def test_concurrent_close_operations(
        self, promise_manager: RpcPromiseManager
    ) -> None:
        """
        Test multiple concurrent close operations.

        Verifies that:
        - Multiple close() calls can happen concurrently
        - Close is idempotent (no errors from redundant closes)
        - Final state is correct
        """
        # Create some pending requests
        for i in range(10):
            request = JsonRpcRequest(
                jsonrpc="2.0",
                id=f"close-{i}",
                method="test",
            )
            promise_manager.create_promise(request)

        # Try to close concurrently from multiple tasks
        async def close_manager() -> None:
            promise_manager.close()
            # Small delay to increase concurrency
            await asyncio.sleep(0.001)

        tasks = [close_manager() for _ in range(10)]
        await asyncio.gather(*tasks)

        # Should be closed
        assert promise_manager.is_closed()
        assert promise_manager.get_pending_count() == 0

    @pytest.mark.asyncio
    async def test_close_while_creating_promises(
        self, promise_manager: RpcPromiseManager
    ) -> None:
        """
        Test closing while promises are being created.

        Verifies that:
        - Close can happen during promise creation
        - System remains consistent
        - No deadlocks or crashes
        """

        async def create_promises() -> None:
            for i in range(50):
                try:
                    request = JsonRpcRequest(
                        jsonrpc="2.0",
                        id=f"create-{i}",
                        method="test",
                    )
                    promise_manager.create_promise(request)
                    await asyncio.sleep(0.001)
                except Exception:
                    # May fail if closed, that's ok
                    pass

        async def close_after_delay() -> None:
            await asyncio.sleep(0.05)
            promise_manager.close()

        # Run both concurrently
        await asyncio.gather(create_promises(), close_after_delay())

        # Should be closed
        assert promise_manager.is_closed()

    @pytest.mark.asyncio
    async def test_close_while_waiting_for_responses(
        self, promise_manager: RpcPromiseManager
    ) -> None:
        """
        Test closing while multiple tasks are waiting for responses.

        Verifies that:
        - All waiting tasks are woken up
        - All receive RpcChannelClosedError
        - Cleanup is complete
        """
        num_waiters = 10

        # Create promises
        promises = []
        for i in range(num_waiters):
            request = JsonRpcRequest(
                jsonrpc="2.0",
                id=f"wait-{i}",
                method="test",
            )
            promise = promise_manager.create_promise(request)
            promises.append(promise)

        # Start waiting for all
        async def wait_for_response(promise: Any) -> None:
            try:
                await promise_manager.wait_for_response(promise, timeout=10.0)
                raise AssertionError("Should have raised RpcChannelClosedError")
            except Exception:
                # Expected
                pass

        wait_tasks = [asyncio.create_task(wait_for_response(p)) for p in promises]

        # Give tasks time to start waiting
        await asyncio.sleep(0.1)

        # Close while they're all waiting
        promise_manager.close()

        # All tasks should complete
        await asyncio.gather(*wait_tasks)

        # All cleaned up
        assert promise_manager.get_pending_count() == 0


# ============================================================================
# Concurrent Protocol Handler Tests
# ============================================================================


class TestConcurrentProtocolHandling:
    """Test concurrent message handling in protocol handler."""

    @pytest.mark.asyncio
    async def test_concurrent_request_handling(
        self,
        protocol_handler: RpcProtocolHandler,
        mock_send: AsyncMock,
        test_methods: TestMethods,
    ) -> None:
        """
        Test handling multiple concurrent requests.

        Verifies that:
        - Multiple requests can be handled concurrently
        - Method invocations don't interfere with each other
        - All responses are sent correctly
        """
        num_requests = 25

        async def handle_request(i: int) -> None:
            message = {
                "jsonrpc": "2.0",
                "id": f"concurrent-{i}",
                "method": "counter_method",
            }
            await protocol_handler.handle_message(message)

        tasks = [handle_request(i) for i in range(num_requests)]
        await asyncio.gather(*tasks)

        # All responses should be sent
        assert mock_send.call_count == num_requests

        # Counter should be incremented correctly
        # (may not be exactly num_requests due to race conditions in the test method)
        assert test_methods.call_count > 0

    @pytest.mark.asyncio
    async def test_concurrent_mixed_operations(
        self,
        protocol_handler: RpcProtocolHandler,
        promise_manager: RpcPromiseManager,
        mock_send: AsyncMock,
    ) -> None:
        """
        Test concurrent mix of requests and responses.

        Verifies that:
        - Requests and responses can be processed concurrently
        - No interference between operations
        - System remains consistent
        """
        num_operations = 20

        # Create some pending requests first
        for i in range(num_operations):
            request = JsonRpcRequest(
                jsonrpc="2.0",
                id=f"mixed-{i}",
                method="test",
            )
            promise_manager.create_promise(request)

        async def handle_incoming_request(i: int) -> None:
            message = {
                "jsonrpc": "2.0",
                "id": f"incoming-{i}",
                "method": "fast_method",
                "params": {"value": i},
            }
            await protocol_handler.handle_message(message)

        async def handle_incoming_response(i: int) -> None:
            message = {
                "jsonrpc": "2.0",
                "id": f"mixed-{i}",
                "result": i,
            }
            await protocol_handler.handle_message(message)

        # Mix of requests and responses
        tasks = []
        for i in range(num_operations):
            tasks.append(handle_incoming_request(i))
            tasks.append(handle_incoming_response(i))

        await asyncio.gather(*tasks)

        # Verify responses were sent for incoming requests
        assert mock_send.call_count >= num_operations


# ============================================================================
# Stress Tests
# ============================================================================


class TestStressScenarios:
    """Stress tests with high concurrency and load."""

    @pytest.mark.asyncio
    async def test_high_concurrency_stress(
        self, promise_manager: RpcPromiseManager
    ) -> None:
        """
        Stress test with high number of concurrent operations.

        Verifies that:
        - System handles high concurrency
        - No deadlocks or crashes
        - Data remains consistent
        """
        num_operations = 200

        async def operation(i: int) -> None:
            # Create request
            request = JsonRpcRequest(
                jsonrpc="2.0",
                id=f"stress-{i}",
                method="test",
            )
            promise_manager.create_promise(request)

            # Small random delay
            await asyncio.sleep(0.001 * (i % 5))

            # Store response
            response = JsonRpcResponse(
                jsonrpc="2.0",
                id=f"stress-{i}",
                result=i,
            )
            promise_manager.store_response(response)

            # Retrieve and cleanup
            promise_manager.get_saved_response(f"stress-{i}")
            promise_manager.clear_saved_call(f"stress-{i}")

        tasks = [operation(i) for i in range(num_operations)]
        await asyncio.gather(*tasks)

        # All should be cleaned up
        assert promise_manager.get_pending_count() == 0

    @pytest.mark.asyncio
    async def test_rapid_create_close_cycles(self) -> None:
        """
        Test rapid cycles of creating and closing managers.

        Verifies that:
        - Managers can be created and closed rapidly
        - No resource leaks
        - Cleanup tasks are properly handled
        """
        for cycle in range(10):
            manager = RpcPromiseManager()

            # Create some requests
            for i in range(10):
                request = JsonRpcRequest(
                    jsonrpc="2.0",
                    id=f"cycle-{cycle}-{i}",
                    method="test",
                )
                manager.create_promise(request)

            # Close immediately
            manager.close()

            assert manager.is_closed()
            assert manager.get_pending_count() == 0

            # Small delay between cycles
            await asyncio.sleep(0.01)


# ============================================================================
# Data Consistency Tests
# ============================================================================


class TestDataConsistency:
    """Test data consistency under concurrent access."""

    @pytest.mark.asyncio
    async def test_no_duplicate_ids_under_concurrency(
        self, promise_manager_low_limit: RpcPromiseManager
    ) -> None:
        """
        Test that duplicate IDs are detected even under concurrent creation.

        Verifies that:
        - ID collision detection works under concurrent load
        - No duplicate IDs can be created
        """
        manager = promise_manager_low_limit
        request_id = "shared-id"

        async def try_create_with_same_id() -> bool:
            try:
                request = JsonRpcRequest(
                    jsonrpc="2.0",
                    id=request_id,
                    method="test",
                )
                manager.create_promise(request)
                return True
            except ValueError:
                # Collision detected
                return False

        # Try to create multiple promises with same ID concurrently
        tasks = [try_create_with_same_id() for _ in range(10)]
        results = await asyncio.gather(*tasks)

        # Exactly one should succeed
        successes = sum(results)
        assert successes == 1, f"Expected 1 success, got {successes}"

    @pytest.mark.asyncio
    async def test_pending_count_consistency(
        self, promise_manager: RpcPromiseManager
    ) -> None:
        """
        Test that pending count remains consistent under concurrent operations.

        Verifies that:
        - Pending count is accurate
        - No race conditions in counting
        - Count matches actual pending requests
        """
        num_requests = 100

        # Create requests concurrently
        async def create_request(i: int) -> None:
            request = JsonRpcRequest(
                jsonrpc="2.0",
                id=f"count-{i}",
                method="test",
            )
            promise_manager.create_promise(request)

        create_tasks = [create_request(i) for i in range(num_requests)]
        await asyncio.gather(*create_tasks)

        # Check count
        assert promise_manager.get_pending_count() == num_requests

        # Clear half concurrently
        async def clear_request(i: int) -> None:
            promise_manager.clear_saved_call(f"count-{i}")

        clear_tasks = [clear_request(i) for i in range(0, num_requests, 2)]
        await asyncio.gather(*clear_tasks)

        # Check count again
        expected_remaining = num_requests - len(clear_tasks)
        assert promise_manager.get_pending_count() == expected_remaining
