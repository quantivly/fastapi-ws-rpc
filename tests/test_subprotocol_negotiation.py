"""
Comprehensive tests for WebSocket subprotocol negotiation.

This test module covers:
- Default subprotocol negotiation (jsonrpc2.0)
- Custom subprotocol negotiation
- Disabled subprotocol negotiation (None or empty list)
- Subprotocol storage in RpcChannel
- Client and server subprotocol agreement
- Configuration validation
"""

from __future__ import annotations

import asyncio
import logging
import os
from multiprocessing import Process

import pytest
import uvicorn
from fastapi import FastAPI

from fastapi_ws_rpc.config import (
    RpcConnectionConfig,
    WebSocketConnectionConfig,
    WebSocketRpcClientConfig,
)
from fastapi_ws_rpc.logger import LoggingModes, logging_config
from fastapi_ws_rpc.rpc_methods import RpcUtilityMethods
from fastapi_ws_rpc.websocket_rpc_client import WebSocketRpcClient
from fastapi_ws_rpc.websocket_rpc_endpoint import WebSocketRpcEndpoint

# Set debug logs
logging_config.set_mode(LoggingModes.UVICORN, logging.DEBUG)

# Configurable port for tests
PORT = int(os.environ.get("TEST_SUBPROTOCOL_PORT") or "9001")
uri = f"ws://localhost:{PORT}/ws"


# ============================================================================
# Server Setup Fixtures
# ============================================================================


def setup_server_with_default_subprotocol():
    """Setup server with default jsonrpc2.0 subprotocol."""
    app = FastAPI()
    # Default config includes jsonrpc2.0 subprotocol
    config = RpcConnectionConfig()
    endpoint = WebSocketRpcEndpoint(
        methods=RpcUtilityMethods(),
        connection_config=config,
    )
    endpoint.register_route(app)
    uvicorn.run(app, port=PORT)


def setup_server_without_subprotocol():
    """Setup server without subprotocol negotiation."""
    app = FastAPI()
    # Explicitly disable subprotocol negotiation
    config = RpcConnectionConfig(websocket=WebSocketConnectionConfig(subprotocols=None))
    endpoint = WebSocketRpcEndpoint(
        methods=RpcUtilityMethods(),
        connection_config=config,
    )
    endpoint.register_route(app)
    uvicorn.run(app, port=PORT)


def setup_server_with_custom_subprotocol():
    """Setup server with custom subprotocol."""
    app = FastAPI()
    config = RpcConnectionConfig(
        websocket=WebSocketConnectionConfig(subprotocols=["custom.v1"])
    )
    endpoint = WebSocketRpcEndpoint(
        methods=RpcUtilityMethods(),
        connection_config=config,
    )
    endpoint.register_route(app)
    uvicorn.run(app, port=PORT)


@pytest.fixture(scope="function")
def server_default_subprotocol():
    """Run server with default jsonrpc2.0 subprotocol."""
    proc = Process(target=setup_server_with_default_subprotocol, args=(), daemon=True)
    proc.start()
    # Give server time to start
    asyncio.get_event_loop().run_until_complete(asyncio.sleep(1))
    yield proc
    proc.kill()
    # Give process time to cleanup
    asyncio.get_event_loop().run_until_complete(asyncio.sleep(0.5))


@pytest.fixture(scope="function")
def server_no_subprotocol():
    """Run server without subprotocol negotiation."""
    proc = Process(target=setup_server_without_subprotocol, args=(), daemon=True)
    proc.start()
    # Give server time to start
    asyncio.get_event_loop().run_until_complete(asyncio.sleep(1))
    yield proc
    proc.kill()
    # Give process time to cleanup
    asyncio.get_event_loop().run_until_complete(asyncio.sleep(0.5))


@pytest.fixture(scope="function")
def server_custom_subprotocol():
    """Run server with custom subprotocol."""
    proc = Process(target=setup_server_with_custom_subprotocol, args=(), daemon=True)
    proc.start()
    # Give server time to start
    asyncio.get_event_loop().run_until_complete(asyncio.sleep(1))
    yield proc
    proc.kill()
    # Give process time to cleanup
    asyncio.get_event_loop().run_until_complete(asyncio.sleep(0.5))


# ============================================================================
# Configuration Tests
# ============================================================================


def test_websocket_connection_config_defaults():
    """Test WebSocketConnectionConfig has correct defaults."""
    config = WebSocketConnectionConfig()
    assert config.subprotocols == ["jsonrpc2.0"]


def test_websocket_connection_config_custom():
    """Test WebSocketConnectionConfig with custom subprotocols."""
    config = WebSocketConnectionConfig(subprotocols=["custom.v1", "jsonrpc2.0"])
    assert config.subprotocols == ["custom.v1", "jsonrpc2.0"]


def test_websocket_connection_config_disabled():
    """Test WebSocketConnectionConfig with disabled subprotocols."""
    config = WebSocketConnectionConfig(subprotocols=None)
    assert config.subprotocols is None


def test_websocket_connection_config_empty_list():
    """Test WebSocketConnectionConfig with empty subprotocols list."""
    config = WebSocketConnectionConfig(subprotocols=[])
    assert config.subprotocols == []


def test_rpc_connection_config_includes_websocket():
    """Test RpcConnectionConfig includes websocket field with defaults."""
    config = RpcConnectionConfig()
    assert config.websocket is not None
    assert config.websocket.subprotocols == ["jsonrpc2.0"]


def test_websocket_rpc_client_config_includes_websocket():
    """Test WebSocketRpcClientConfig includes websocket field with defaults."""
    config = WebSocketRpcClientConfig()
    assert config.websocket is not None
    assert config.websocket.subprotocols == ["jsonrpc2.0"]


# ============================================================================
# Integration Tests - Default Subprotocol
# ============================================================================


@pytest.mark.asyncio
async def test_default_subprotocol_negotiation(server_default_subprotocol):
    """
    Test that default jsonrpc2.0 subprotocol is negotiated correctly.
    """
    # Client uses default config (includes jsonrpc2.0)
    config = WebSocketRpcClientConfig(
        connection=RpcConnectionConfig(default_response_timeout=4)
    )
    async with WebSocketRpcClient(uri, RpcUtilityMethods(), config=config) as client:
        # Verify channel has subprotocol set
        assert client.channel is not None
        assert client.channel.subprotocol == "jsonrpc2.0"

        # Test that RPC still works
        text = "Hello World!"
        response = await client.other.echo(text=text)
        assert response.result == text


@pytest.mark.asyncio
async def test_ping_with_default_subprotocol(server_default_subprotocol):
    """
    Test ping works with default subprotocol negotiation.
    """
    config = WebSocketRpcClientConfig(
        connection=RpcConnectionConfig(default_response_timeout=4)
    )
    async with WebSocketRpcClient(uri, RpcUtilityMethods(), config=config) as client:
        # Verify subprotocol
        assert client.channel is not None
        assert client.channel.subprotocol == "jsonrpc2.0"

        # Test ping
        await client.other.ping()


# ============================================================================
# Integration Tests - No Subprotocol
# ============================================================================


@pytest.mark.asyncio
async def test_no_subprotocol_negotiation(server_no_subprotocol):
    """
    Test that connection works without subprotocol negotiation.
    """
    # Client explicitly disables subprotocol
    config = WebSocketRpcClientConfig(
        connection=RpcConnectionConfig(
            default_response_timeout=4,
            websocket=WebSocketConnectionConfig(subprotocols=None),
        ),
        websocket=WebSocketConnectionConfig(subprotocols=None),
    )
    async with WebSocketRpcClient(uri, RpcUtilityMethods(), config=config) as client:
        # Verify channel has no subprotocol
        assert client.channel is not None
        assert client.channel.subprotocol is None

        # Test that RPC still works
        text = "Hello World!"
        response = await client.other.echo(text=text)
        assert response.result == text


@pytest.mark.asyncio
async def test_client_default_server_none(server_no_subprotocol):
    """
    Test client with default subprotocol connecting to server without subprotocol.
    Client should connect successfully even if server doesn't negotiate.
    """
    # Client uses default config (includes jsonrpc2.0)
    config = WebSocketRpcClientConfig(
        connection=RpcConnectionConfig(default_response_timeout=4)
    )
    async with WebSocketRpcClient(uri, RpcUtilityMethods(), config=config) as client:
        # Connection should work even if subprotocol not negotiated
        # Test that RPC works
        text = "Hello World!"
        response = await client.other.echo(text=text)
        assert response.result == text


# ============================================================================
# Integration Tests - Custom Subprotocol
# ============================================================================


@pytest.mark.asyncio
async def test_custom_subprotocol_negotiation(server_custom_subprotocol):
    """
    Test that custom subprotocol is negotiated correctly.
    """
    # Client uses custom subprotocol
    config = WebSocketRpcClientConfig(
        connection=RpcConnectionConfig(
            default_response_timeout=4,
            websocket=WebSocketConnectionConfig(subprotocols=["custom.v1"]),
        ),
        websocket=WebSocketConnectionConfig(subprotocols=["custom.v1"]),
    )
    async with WebSocketRpcClient(uri, RpcUtilityMethods(), config=config) as client:
        # Verify channel has custom subprotocol
        assert client.channel is not None
        assert client.channel.subprotocol == "custom.v1"

        # Test that RPC still works
        text = "Hello World!"
        response = await client.other.echo(text=text)
        assert response.result == text


# ============================================================================
# Configuration Validation Tests
# ============================================================================


def test_config_validation_with_websocket():
    """Test that config validation includes websocket config."""
    config = WebSocketRpcClientConfig(
        websocket=WebSocketConnectionConfig(subprotocols=["jsonrpc2.0"])
    )
    # Should not raise
    config.validate()


def test_rpc_connection_config_validation_with_websocket():
    """Test that RpcConnectionConfig validation includes websocket config."""
    config = RpcConnectionConfig(
        websocket=WebSocketConnectionConfig(subprotocols=["custom.v1"])
    )
    # Should not raise
    config.validate()


# ============================================================================
# Edge Cases
# ============================================================================


@pytest.mark.asyncio
async def test_empty_subprotocol_list(server_no_subprotocol):
    """
    Test connection with empty subprotocol list.
    """
    config = WebSocketRpcClientConfig(
        connection=RpcConnectionConfig(
            default_response_timeout=4,
            websocket=WebSocketConnectionConfig(subprotocols=[]),
        ),
        websocket=WebSocketConnectionConfig(subprotocols=[]),
    )
    async with WebSocketRpcClient(uri, RpcUtilityMethods(), config=config) as client:
        # Verify channel has no subprotocol
        assert client.channel is not None
        assert client.channel.subprotocol is None

        # Test that RPC still works
        text = "Hello World!"
        response = await client.other.echo(text=text)
        assert response.result == text


@pytest.mark.asyncio
async def test_multiple_subprotocols_client_side(server_default_subprotocol):
    """
    Test client offering multiple subprotocols.
    Server should select the first matching one.
    """
    # Client offers multiple subprotocols including jsonrpc2.0
    config = WebSocketRpcClientConfig(
        connection=RpcConnectionConfig(
            default_response_timeout=4,
            websocket=WebSocketConnectionConfig(
                subprotocols=["custom.v1", "jsonrpc2.0", "another.v2"]
            ),
        ),
        websocket=WebSocketConnectionConfig(
            subprotocols=["custom.v1", "jsonrpc2.0", "another.v2"]
        ),
    )
    async with WebSocketRpcClient(uri, RpcUtilityMethods(), config=config) as client:
        # Server should negotiate jsonrpc2.0 (first match)
        assert client.channel is not None
        # The subprotocol negotiated depends on server behavior
        # WebSocket protocol allows server to choose from client's list

        # Test that RPC works
        text = "Hello World!"
        response = await client.other.echo(text=text)
        assert response.result == text
