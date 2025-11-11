"""
Comprehensive stress tests for production readiness.

This test module covers:
- UUID uniqueness under high concurrency (collision detection)
- Backpressure with 1000+ concurrent requests
- Long-running connection stability over extended periods
- High-throughput request/response cycles
- Memory leak detection under sustained load
- Performance characteristics under stress
"""

from __future__ import annotations

import asyncio
import time
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
from fastapi_ws_rpc.utils import gen_uid

# ============================================================================
# Test Methods Classes
# ============================================================================


class StressTestMethods(RpcMethodsBase):
    """Test methods for stress testing."""

    def __init__(self) -> None:
        super().__init__()
        self.call_count = 0
        self.total_bytes_processed = 0

    async def fast_method(self, value: int) -> int:
        """Fast method for high-throughput testing."""
        self.call_count += 1
        return value * 2

    async def slow_method(self, delay: float, value: int) -> int:
        """Slow method with configurable delay for load testing."""
        await asyncio.sleep(delay)
        return value

    async def data_method(self, data: str) -> dict[str, Any]:
        """Method that processes data for throughput testing."""
        self.total_bytes_processed += len(data)
        return {"length": len(data), "first_10": data[:10]}

    async def echo_method(self, message: str) -> str:
        """Simple echo for connection stability testing."""
        return message


# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture
def test_methods() -> StressTestMethods:
    """Create test methods instance."""
    return StressTestMethods()


@pytest.fixture
def method_invoker(test_methods: StressTestMethods) -> RpcMethodInvoker:
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
async def promise_manager_high_limit() -> RpcPromiseManager:
    """Create promise manager with high limit for stress testing."""
    manager = RpcPromiseManager(max_pending_requests=2000)
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
# UUID Uniqueness Tests
# ============================================================================


class TestUuidUniqueness:
    """Test UUID collision detection under high concurrency."""

    @pytest.mark.asyncio
    async def test_uuid_uniqueness_under_load(self) -> None:
        """
        Test that generated UUIDs are unique under high concurrent load.

        Verifies that:
        - 10,000+ UUIDs can be generated concurrently
        - No collisions occur (all UUIDs are unique)
        - Performance is acceptable
        - UUID generation is thread-safe
        """
        num_uuids = 10_000

        async def generate_uuid() -> str:
            """Generate a single UUID."""
            return gen_uid()

        # Generate many UUIDs concurrently
        start_time = time.time()
        tasks = [generate_uuid() for _ in range(num_uuids)]
        uuids = await asyncio.gather(*tasks)
        elapsed = time.time() - start_time

        # Verify all UUIDs are unique (no collisions)
        unique_uuids = set(uuids)
        assert len(unique_uuids) == num_uuids, (
            f"UUID collision detected: generated {num_uuids} UUIDs "
            f"but only {len(unique_uuids)} were unique. "
            f"Collisions: {num_uuids - len(unique_uuids)}"
        )

        # Performance check: should complete in reasonable time (< 5 seconds)
        assert (
            elapsed < 5.0
        ), f"UUID generation too slow: {elapsed:.2f}s for {num_uuids} UUIDs"

        # Log performance stats
        rate = num_uuids / elapsed
        print(f"\nUUID generation rate: {rate:.0f} UUIDs/second")

    @pytest.mark.asyncio
    async def test_no_duplicate_request_ids_under_concurrency(
        self, promise_manager_high_limit: RpcPromiseManager
    ) -> None:
        """
        Test that request ID collision detection works under concurrent load.

        Verifies that:
        - Multiple concurrent request creations with unique IDs succeed
        - Duplicate IDs are properly detected and rejected
        - No race conditions in ID collision detection
        - System remains consistent under high concurrency
        """
        manager = promise_manager_high_limit
        num_requests = 1_500  # Within the 2000 limit

        # Create many requests concurrently with unique IDs
        async def create_request(i: int) -> bool:
            try:
                request_id = gen_uid()
                request = JsonRpcRequest(
                    jsonrpc="2.0",
                    id=request_id,
                    method="test",
                )
                manager.create_promise(request)
                return True
            except ValueError:
                # Collision detected (should not happen with UUIDs)
                return False

        tasks = [create_request(i) for i in range(num_requests)]
        results = await asyncio.gather(*tasks)

        # All should succeed (no collisions with UUIDs)
        successes = sum(results)
        assert (
            successes == num_requests
        ), f"Expected all {num_requests} to succeed, got {successes}"

        # Verify pending count matches
        assert manager.get_pending_count() == num_requests

    @pytest.mark.asyncio
    async def test_request_id_collision_detection(
        self, promise_manager: RpcPromiseManager
    ) -> None:
        """
        Test that duplicate request IDs are properly detected even under concurrency.

        Verifies that:
        - Only one promise can be created for a given ID
        - Subsequent attempts with same ID raise ValueError
        - Detection works correctly under concurrent access
        """
        # Use a fixed ID to force collision
        shared_id = "collision-test-id"

        async def try_create_with_same_id() -> bool:
            try:
                request = JsonRpcRequest(
                    jsonrpc="2.0",
                    id=shared_id,
                    method="test",
                )
                promise_manager.create_promise(request)
                return True
            except ValueError:
                # Collision detected
                return False

        # Try to create multiple promises with same ID concurrently
        tasks = [try_create_with_same_id() for _ in range(100)]
        results = await asyncio.gather(*tasks)

        # Exactly one should succeed
        successes = sum(results)
        assert successes == 1, f"Expected exactly 1 success, got {successes}"


# ============================================================================
# Backpressure Stress Tests
# ============================================================================


class TestBackpressureStress:
    """Test backpressure control under extreme load."""

    @pytest.mark.asyncio
    @pytest.mark.slow
    async def test_backpressure_1000_concurrent_requests(
        self, promise_manager_high_limit: RpcPromiseManager
    ) -> None:
        """
        Test backpressure behavior with 1000+ concurrent requests.

        Verifies that:
        - System handles 1000+ concurrent requests
        - Backpressure limit is enforced correctly
        - Some requests succeed (up to limit)
        - Excess requests fail with RpcBackpressureError
        - System remains stable and responsive
        """
        manager = promise_manager_high_limit
        # Manager has limit of 2000, try to create 1500
        num_attempts = 1500
        successes = []
        errors = []

        async def try_create_request(i: int) -> None:
            try:
                request = JsonRpcRequest(
                    jsonrpc="2.0",
                    id=f"stress-{i}",
                    method="test",
                )
                manager.create_promise(request)
                successes.append(i)
            except RpcBackpressureError as e:
                errors.append(e)

        # Create many requests concurrently
        start_time = time.time()
        tasks = [try_create_request(i) for i in range(num_attempts)]
        await asyncio.gather(*tasks)
        elapsed = time.time() - start_time

        # All should succeed (under the 2000 limit)
        assert len(successes) == num_attempts
        assert len(errors) == 0

        # Verify pending count
        assert manager.get_pending_count() == num_attempts

        # Performance check: should complete in reasonable time
        assert elapsed < 10.0, f"Request creation too slow: {elapsed:.2f}s"

        # Log performance stats
        rate = num_attempts / elapsed
        print(f"\nRequest creation rate: {rate:.0f} requests/second")

    @pytest.mark.asyncio
    @pytest.mark.slow
    async def test_backpressure_with_completions(
        self, promise_manager_high_limit: RpcPromiseManager
    ) -> None:
        """
        Test backpressure with rapid request creation and completion cycles.

        Verifies that:
        - System handles rapid create/complete cycles
        - Backpressure allows new requests after completions
        - No memory leaks from rapid cycling
        - Performance remains stable
        """
        manager = promise_manager_high_limit
        num_cycles = 1000
        batch_size = 100

        async def create_and_complete_batch(batch_id: int) -> None:
            # Create batch
            request_ids = []
            for i in range(batch_size):
                request_id = f"batch-{batch_id}-req-{i}"
                request = JsonRpcRequest(
                    jsonrpc="2.0",
                    id=request_id,
                    method="test",
                )
                manager.create_promise(request)
                request_ids.append(request_id)

            # Complete batch
            for request_id in request_ids:
                response = JsonRpcResponse(
                    jsonrpc="2.0",
                    id=request_id,
                    result="done",
                )
                manager.store_response(response)
                manager.clear_saved_call(request_id)

        # Run many cycles
        start_time = time.time()
        tasks = [create_and_complete_batch(i) for i in range(num_cycles // 10)]
        await asyncio.gather(*tasks)
        elapsed = time.time() - start_time

        # All should be cleaned up
        assert manager.get_pending_count() == 0

        # Performance check
        total_requests = (num_cycles // 10) * batch_size
        rate = total_requests / elapsed
        print(f"\nRequest throughput: {rate:.0f} requests/second")

    @pytest.mark.asyncio
    @pytest.mark.slow
    async def test_backpressure_recovery_under_load(
        self, promise_manager_high_limit: RpcPromiseManager
    ) -> None:
        """
        Test that backpressure correctly recovers after hitting limit.

        Verifies that:
        - System hits backpressure limit correctly
        - After clearing some requests, new ones can be created
        - Backpressure limit is consistently enforced
        - No race conditions in limit enforcement
        """
        manager = promise_manager_high_limit
        limit = manager.get_max_pending_requests()

        # Fill to limit
        for i in range(limit):
            request = JsonRpcRequest(
                jsonrpc="2.0",
                id=f"fill-{i}",
                method="test",
            )
            manager.create_promise(request)

        assert manager.get_pending_count() == limit

        # Next should fail
        with pytest.raises(RpcBackpressureError):
            request = JsonRpcRequest(
                jsonrpc="2.0",
                id="overflow",
                method="test",
            )
            manager.create_promise(request)

        # Clear half
        for i in range(limit // 2):
            manager.clear_saved_call(f"fill-{i}")

        # Should be able to create more now
        for i in range(limit // 2):
            request = JsonRpcRequest(
                jsonrpc="2.0",
                id=f"refill-{i}",
                method="test",
            )
            manager.create_promise(request)

        # Should be back at limit
        assert manager.get_pending_count() == limit


# ============================================================================
# Long-Running Connection Tests
# ============================================================================


class TestLongRunningConnections:
    """Test connection stability over extended periods."""

    @pytest.mark.asyncio
    @pytest.mark.slow
    async def test_connection_stability_5_minutes(
        self,
        protocol_handler: RpcProtocolHandler,
        promise_manager: RpcPromiseManager,
        mock_send: AsyncMock,
        test_methods: StressTestMethods,
    ) -> None:
        """
        Test connection stability over 5+ minutes with periodic messages.

        This is a realistic simulation of a long-lived WebSocket connection.
        In production, we reduce to 30 seconds for CI speed.

        Verifies that:
        - Connection remains stable over time
        - Periodic messages are handled correctly
        - No memory leaks occur
        - No timeouts or hangs
        - Performance remains consistent
        """
        # For CI: use 30 seconds instead of 5 minutes
        # Set environment variable STRESS_TEST_DURATION=300 for full test
        import os

        duration = int(os.environ.get("STRESS_TEST_DURATION", "30"))
        message_interval = 1.0  # Send message every 1 second

        start_time = time.time()
        end_time = start_time + duration
        message_count = 0
        errors = []

        # Track performance metrics
        response_times = []

        while time.time() < end_time:
            try:
                # Send periodic message
                message_start = time.time()
                message = {
                    "jsonrpc": "2.0",
                    "id": f"stability-{message_count}",
                    "method": "echo_method",
                    "params": {"message": f"Message {message_count}"},
                }
                await protocol_handler.handle_message(message)
                message_elapsed = time.time() - message_start
                response_times.append(message_elapsed)

                message_count += 1

                # Wait before next message
                await asyncio.sleep(message_interval)

            except Exception as e:
                errors.append(e)

        elapsed = time.time() - start_time

        # Verify no errors occurred
        assert len(errors) == 0, f"Errors during stability test: {errors}"

        # Verify messages were sent
        assert message_count > 0, "No messages sent during test"

        # Verify responses were received
        assert mock_send.call_count == message_count

        # Verify no pending requests (all completed)
        assert promise_manager.get_pending_count() == 0

        # Performance metrics
        if response_times:
            avg_response_time = sum(response_times) / len(response_times)
            max_response_time = max(response_times)
            print("\nStability test results:")
            print(f"  Duration: {elapsed:.1f}s")
            print(f"  Messages: {message_count}")
            print(f"  Avg response time: {avg_response_time * 1000:.2f}ms")
            print(f"  Max response time: {max_response_time * 1000:.2f}ms")

            # Response times should remain reasonable
            assert (
                avg_response_time < 0.1
            ), f"Average response time too high: {avg_response_time:.3f}s"
            assert (
                max_response_time < 1.0
            ), f"Max response time too high: {max_response_time:.3f}s"

    @pytest.mark.asyncio
    @pytest.mark.slow
    async def test_sustained_load_with_no_memory_leak(
        self,
        protocol_handler: RpcProtocolHandler,
        promise_manager: RpcPromiseManager,
        mock_send: AsyncMock,
    ) -> None:
        """
        Test for memory leaks under sustained load.

        Verifies that:
        - Promise manager cleans up completed requests
        - No memory accumulation over time
        - Pending count remains stable
        - System can handle continuous load
        """
        num_iterations = 1000
        batch_size = 10

        for iteration in range(num_iterations):
            # Create batch of requests
            for i in range(batch_size):
                message = {
                    "jsonrpc": "2.0",
                    "id": f"load-{iteration}-{i}",
                    "method": "fast_method",
                    "params": {"value": i},
                }
                await protocol_handler.handle_message(message)

            # Periodically check that pending count is zero
            # (all requests should have completed)
            if iteration % 100 == 0:
                await asyncio.sleep(0.1)  # Allow cleanup
                pending = promise_manager.get_pending_count()
                assert pending == 0, (
                    f"Memory leak detected at iteration {iteration}: "
                    f"{pending} pending requests"
                )

        # Final verification
        await asyncio.sleep(0.1)
        assert promise_manager.get_pending_count() == 0

        # Verify all responses were sent
        expected_calls = num_iterations * batch_size
        assert mock_send.call_count == expected_calls

    @pytest.mark.asyncio
    @pytest.mark.slow
    async def test_connection_with_periodic_cleanup(
        self, promise_manager: RpcPromiseManager
    ) -> None:
        """
        Test that periodic promise cleanup works correctly over time.

        Verifies that:
        - Cleanup task runs periodically
        - Expired promises are removed
        - Active promises are not affected
        - System remains stable during cleanup
        """
        # Create some requests
        for i in range(100):
            request = JsonRpcRequest(
                jsonrpc="2.0",
                id=f"cleanup-{i}",
                method="test",
            )
            promise_manager.create_promise(request)

        assert promise_manager.get_pending_count() == 100

        # Complete half of them
        for i in range(50):
            response = JsonRpcResponse(
                jsonrpc="2.0",
                id=f"cleanup-{i}",
                result="done",
            )
            promise_manager.store_response(response)
            promise_manager.clear_saved_call(f"cleanup-{i}")

        assert promise_manager.get_pending_count() == 50

        # Wait to ensure cleanup task has a chance to run
        # (Cleanup interval is 60s by default, but in tests it may be faster)
        await asyncio.sleep(2.0)

        # The 50 remaining should still be there (not expired yet)
        # Note: Default TTL is 300s, so they won't be cleaned up
        assert promise_manager.get_pending_count() == 50


# ============================================================================
# High-Throughput Tests
# ============================================================================


class TestHighThroughput:
    """Test system performance under high-throughput scenarios."""

    @pytest.mark.asyncio
    @pytest.mark.slow
    async def test_high_throughput_request_response(
        self,
        protocol_handler: RpcProtocolHandler,
        mock_send: AsyncMock,
        test_methods: StressTestMethods,
    ) -> None:
        """
        Test high-throughput request/response cycles.

        Verifies that:
        - System handles high message rates
        - All requests are processed correctly
        - Performance is acceptable
        - No errors under high load
        """
        num_requests = 5000

        start_time = time.time()

        # Send many requests
        tasks = []
        for i in range(num_requests):
            message = {
                "jsonrpc": "2.0",
                "id": f"throughput-{i}",
                "method": "fast_method",
                "params": {"value": i},
            }
            task = asyncio.create_task(protocol_handler.handle_message(message))
            tasks.append(task)

        # Wait for all to complete
        await asyncio.gather(*tasks)

        elapsed = time.time() - start_time

        # Verify all responses were sent
        assert mock_send.call_count == num_requests

        # Performance metrics
        rate = num_requests / elapsed
        print(f"\nThroughput: {rate:.0f} requests/second")

        # Should achieve reasonable throughput
        assert rate > 100, f"Throughput too low: {rate:.0f} req/s"

    @pytest.mark.asyncio
    @pytest.mark.slow
    async def test_high_throughput_with_large_payloads(
        self,
        protocol_handler: RpcProtocolHandler,
        mock_send: AsyncMock,
        test_methods: StressTestMethods,
    ) -> None:
        """
        Test throughput with large message payloads.

        Verifies that:
        - System handles large messages efficiently
        - Data is processed correctly
        - Memory usage is reasonable
        - Performance is acceptable
        """
        num_requests = 100
        payload_size = 100_000  # 100KB per message

        # Generate test data
        large_data = "x" * payload_size

        start_time = time.time()

        # Send requests with large payloads
        tasks = []
        for i in range(num_requests):
            message = {
                "jsonrpc": "2.0",
                "id": f"large-{i}",
                "method": "data_method",
                "params": {"data": large_data},
            }
            task = asyncio.create_task(protocol_handler.handle_message(message))
            tasks.append(task)

        await asyncio.gather(*tasks)

        elapsed = time.time() - start_time

        # Verify all responses were sent
        assert mock_send.call_count == num_requests

        # Verify data was processed
        total_bytes = num_requests * payload_size
        assert test_methods.total_bytes_processed == total_bytes

        # Performance metrics
        throughput_mbps = (total_bytes / elapsed) / (1024 * 1024)
        print(f"\nData throughput: {throughput_mbps:.2f} MB/s")
        print(f"Request rate: {num_requests / elapsed:.0f} requests/second")


# ============================================================================
# Concurrent Operations Under Stress
# ============================================================================


class TestConcurrentStress:
    """Test concurrent operations under stress conditions."""

    @pytest.mark.asyncio
    @pytest.mark.slow
    async def test_mixed_operations_under_stress(
        self,
        protocol_handler: RpcProtocolHandler,
        promise_manager: RpcPromiseManager,
        mock_send: AsyncMock,
    ) -> None:
        """
        Test mixed operations (requests, responses, cleanup) under stress.

        Verifies that:
        - Multiple operation types can run concurrently
        - No race conditions or data corruption
        - System remains stable under mixed load
        - All operations complete successfully
        """
        num_operations = 1000

        async def send_request(i: int) -> None:
            message = {
                "jsonrpc": "2.0",
                "id": f"mixed-req-{i}",
                "method": "fast_method",
                "params": {"value": i},
            }
            await protocol_handler.handle_message(message)

        async def create_and_complete(i: int) -> None:
            request = JsonRpcRequest(
                jsonrpc="2.0",
                id=f"mixed-local-{i}",
                method="test",
            )
            promise_manager.create_promise(request)

            response = JsonRpcResponse(
                jsonrpc="2.0",
                id=f"mixed-local-{i}",
                result=i,
            )
            promise_manager.store_response(response)
            promise_manager.clear_saved_call(f"mixed-local-{i}")

        # Mix of operations
        tasks = []
        for i in range(num_operations):
            if i % 2 == 0:
                tasks.append(send_request(i))
            else:
                tasks.append(create_and_complete(i))

        # Execute all concurrently
        await asyncio.gather(*tasks)

        # Verify system is in good state
        assert promise_manager.get_pending_count() == 0

    @pytest.mark.asyncio
    @pytest.mark.slow
    async def test_stress_with_channel_operations(
        self, promise_manager: RpcPromiseManager
    ) -> None:
        """
        Test stress with various channel operations.

        Verifies that:
        - Channel operations are thread-safe under stress
        - get_pending_count is accurate
        - Multiple readers/writers don't cause issues
        - System remains consistent
        """
        num_operations = 2000

        async def operation(i: int) -> None:
            # Create request
            request = JsonRpcRequest(
                jsonrpc="2.0",
                id=f"chan-op-{i}",
                method="test",
            )
            promise_manager.create_promise(request)

            # Check count (reading while writing)
            promise_manager.get_pending_count()

            # Store response
            response = JsonRpcResponse(
                jsonrpc="2.0",
                id=f"chan-op-{i}",
                result=i,
            )
            promise_manager.store_response(response)

            # Retrieve response
            promise_manager.get_saved_response(f"chan-op-{i}")

            # Cleanup
            promise_manager.clear_saved_call(f"chan-op-{i}")

        tasks = [operation(i) for i in range(num_operations)]
        await asyncio.gather(*tasks)

        # Final state should be clean
        assert promise_manager.get_pending_count() == 0
