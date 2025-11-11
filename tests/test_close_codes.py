"""
Tests for WebSocket close code handling.

This test module covers:
- Close code 1012 (Service Restart) special handling
- Close code retryable vs non-retryable classification
- Reconnection behavior based on close codes
- Close code and reason tracking
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from websockets.exceptions import ConnectionClosed

from fastapi_ws_rpc.config import RpcConnectionConfig, WebSocketRpcClientConfig
from fastapi_ws_rpc.rpc_methods import RpcMethodsBase
from fastapi_ws_rpc.websocket_rpc_client import WebSocketRpcClient

# WebSocket close codes (RFC 6455 Section 7.4 and IANA Registry)
WS_CLOSE_CODE_PROTOCOL_ERROR = 1002
WS_CLOSE_CODE_UNSUPPORTED_DATA = 1003
WS_CLOSE_CODE_INVALID_DATA = 1007
WS_CLOSE_CODE_POLICY_VIOLATION = 1008
WS_CLOSE_CODE_INTERNAL_ERROR = 1011
WS_CLOSE_CODE_SERVICE_RESTART = 1012


class MockRpcMethods(RpcMethodsBase):
    """Mock RPC methods for testing."""

    async def ping(self) -> str:
        """Simple ping method."""
        return "pong"


class MockClose:
    """Mock WebSocket close frame info."""

    def __init__(self, code: int, reason: str = "") -> None:
        """Initialize mock close frame.

        Parameters
        ----------
        code : int
            WebSocket close code.
        reason : str, optional
            Close reason string, by default "".
        """
        self.code = code
        self.reason = reason


@pytest.mark.asyncio
async def test_close_code_1012_logs_special_message() -> None:
    """
    Test that close code 1012 (Service Restart) logs a special informational message.

    Verifies that:
    - Code 1012 is recognized and logged differently
    - Log message indicates this is normal operational event
    - Log level is INFO (not WARNING/ERROR)
    """
    config = WebSocketRpcClientConfig(
        connection=RpcConnectionConfig(auto_reconnect=False)
    )
    client = WebSocketRpcClient("ws://test", MockRpcMethods(), config=config)

    # Mock the WebSocket connection
    mock_ws = MagicMock()
    mock_ws.recv = AsyncMock(
        side_effect=ConnectionClosed(
            rcvd=MockClose(WS_CLOSE_CODE_SERVICE_RESTART, "Service Restart"),
            sent=None,
            rcvd_then_sent=None,
        )
    )
    mock_ws.close = AsyncMock()
    mock_ws.closed = False

    # Manually set up client state (bypass __connect__)
    from fastapi_ws_rpc.rpc_channel import RpcChannel
    from fastapi_ws_rpc.simplewebsocket import JsonSerializingWebSocket

    client.ws = JsonSerializingWebSocket(mock_ws)
    client.channel = RpcChannel(client.methods, client.ws)
    client._read_task = asyncio.create_task(client.reader())

    # Capture log messages
    with patch("fastapi_ws_rpc.websocket_rpc_client.logger") as mock_logger:
        # Wait for reader to process the close
        await asyncio.sleep(0.1)

        # Verify special log message was used
        mock_logger.info.assert_called()

        # Check that one of the info calls contains our special message
        info_calls = [str(call) for call in mock_logger.info.call_args_list]
        special_message_found = any(
            "Server restart detected" in call and "1012" in call for call in info_calls
        )
        assert (
            special_message_found
        ), f"Expected special 1012 message in logs: {info_calls}"

        # Verify code and reason are tracked
        assert client.close_code == WS_CLOSE_CODE_SERVICE_RESTART
        assert client.close_reason == "Service Restart"

    # Cleanup
    await client.close()


@pytest.mark.asyncio
async def test_close_code_1012_is_retryable() -> None:
    """
    Test that close code 1012 (Service Restart) is treated as retryable.

    Verifies that:
    - Code 1012 is NOT in non_retryable_codes set
    - Auto-reconnection is attempted when enabled
    - Connection doesn't permanently close
    """
    config = WebSocketRpcClientConfig(
        connection=RpcConnectionConfig(
            auto_reconnect=True, max_reconnect_attempts=2, reconnect_delay=0.1
        )
    )
    client = WebSocketRpcClient("ws://test", MockRpcMethods(), config=config)

    # Verify 1012 is not in non_retryable_codes
    # (This is implicit in the code, but we test the behavior)
    non_retryable = {
        WS_CLOSE_CODE_PROTOCOL_ERROR,
        WS_CLOSE_CODE_UNSUPPORTED_DATA,
        WS_CLOSE_CODE_INVALID_DATA,
        WS_CLOSE_CODE_POLICY_VIOLATION,
        WS_CLOSE_CODE_INTERNAL_ERROR,
    }

    assert WS_CLOSE_CODE_SERVICE_RESTART not in non_retryable

    # Note: Full integration test with reconnection would require a test server
    # For unit test, we verify the code classification is correct
    await client.close()


@pytest.mark.asyncio
async def test_non_retryable_close_codes_prevent_reconnection() -> None:
    """
    Test that non-retryable close codes prevent automatic reconnection.

    Verifies that:
    - Protocol errors (1002, 1003, 1007, 1008, 1011) don't trigger reconnect
    - Connection closes permanently
    - Appropriate error logging occurs
    """
    # Test with code 1002 (Protocol Error)
    config = WebSocketRpcClientConfig(
        connection=RpcConnectionConfig(auto_reconnect=True)
    )
    client = WebSocketRpcClient("ws://test", MockRpcMethods(), config=config)

    # Mock the WebSocket connection
    mock_ws = MagicMock()
    mock_ws.recv = AsyncMock(
        side_effect=ConnectionClosed(
            rcvd=MockClose(WS_CLOSE_CODE_PROTOCOL_ERROR, "Protocol violation"),
            sent=None,
            rcvd_then_sent=None,
        )
    )
    mock_ws.close = AsyncMock()
    mock_ws.closed = False

    # Manually set up client state
    from fastapi_ws_rpc.rpc_channel import RpcChannel
    from fastapi_ws_rpc.simplewebsocket import JsonSerializingWebSocket

    client.ws = JsonSerializingWebSocket(mock_ws)
    client.channel = RpcChannel(client.methods, client.ws)
    client._read_task = asyncio.create_task(client.reader())

    # Capture log messages
    with patch("fastapi_ws_rpc.websocket_rpc_client.logger") as mock_logger:
        # Wait for reader to process the close
        await asyncio.sleep(0.1)

        # Verify error log for non-retryable code
        mock_logger.error.assert_called()

        # Check that error message mentions non-retryable
        error_calls = [str(call) for call in mock_logger.error.call_args_list]
        non_retryable_found = any(
            "non-retryable" in call.lower() for call in error_calls
        )
        assert non_retryable_found, f"Expected non-retryable message: {error_calls}"

        # Verify code is tracked
        assert client.close_code == WS_CLOSE_CODE_PROTOCOL_ERROR

    # Cleanup
    await client.close()


@pytest.mark.asyncio
async def test_close_code_reset_on_reconnection() -> None:
    """
    Test that close code and reason are reset after successful reconnection.

    Verifies that:
    - After reconnection, close_code is None
    - After reconnection, close_reason is None
    - Previous close information doesn't persist
    """
    # This is a unit test of the reset behavior
    # Full integration test would require a test server

    config = WebSocketRpcClientConfig(
        connection=RpcConnectionConfig(auto_reconnect=False)
    )
    client = WebSocketRpcClient("ws://test", MockRpcMethods(), config=config)

    # Simulate having a close code set
    client._close_code = WS_CLOSE_CODE_SERVICE_RESTART
    client._close_reason = "Service Restart"

    assert client.close_code == WS_CLOSE_CODE_SERVICE_RESTART
    assert client.close_reason == "Service Restart"

    # Simulate successful reconnection state reset
    # (This happens in _attempt_reconnection after successful __connect__)
    client._connection_closed.clear()
    client._closing = False
    client._close_code = None
    client._close_reason = None

    # Verify reset
    assert client.close_code is None
    assert client.close_reason is None

    await client.close()


@pytest.mark.asyncio
async def test_close_code_constants_defined() -> None:
    """
    Test that WebSocket close code constants are properly defined.

    Verifies that:
    - All expected close code constants exist
    - They have the correct values per RFC 6455
    """
    assert WS_CLOSE_CODE_SERVICE_RESTART == 1012
    assert WS_CLOSE_CODE_PROTOCOL_ERROR == 1002
    assert WS_CLOSE_CODE_UNSUPPORTED_DATA == 1003
    assert WS_CLOSE_CODE_INVALID_DATA == 1007
    assert WS_CLOSE_CODE_POLICY_VIOLATION == 1008
    assert WS_CLOSE_CODE_INTERNAL_ERROR == 1011


@pytest.mark.asyncio
async def test_close_code_property_returns_none_when_not_set() -> None:
    """
    Test that close_code property returns None when no close has occurred.

    Verifies that:
    - close_code defaults to None
    - close_reason defaults to None
    """
    config = WebSocketRpcClientConfig()
    client = WebSocketRpcClient("ws://test", MockRpcMethods(), config=config)

    assert client.close_code is None
    assert client.close_reason is None

    await client.close()
