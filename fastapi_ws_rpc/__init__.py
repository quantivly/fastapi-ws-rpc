"""
fastapi_websocket_rpc - WebSocket RPC for FastAPI

A production-ready WebSocket RPC implementation for FastAPI applications.
Supports bidirectional JSON-RPC 2.0 communication over WebSockets.
"""

# Core RPC classes
# Connection management
from fastapi_ws_rpc.connection_manager import ConnectionManager

# Exceptions
from fastapi_ws_rpc.exceptions import (
    RemoteValueError,
    RpcBackpressureError,
    RpcChannelClosedError,
    RpcError,
    RpcInvalidStateError,
    RpcMessageTooLargeError,
    UnknownMethodError,
)

# Logging utilities
from fastapi_ws_rpc.logger import LoggingModes, get_logger, logging_config
from fastapi_ws_rpc.rpc_channel import (
    OnConnectCallback,
    OnDisconnectCallback,
    OnErrorCallback,
    RpcChannel,
)
from fastapi_ws_rpc.rpc_methods import NoResponse, RpcMethodsBase, RpcUtilityMethods

# JSON-RPC schemas
from fastapi_ws_rpc.schemas import (
    JsonRpcError,
    JsonRpcErrorCode,
    JsonRpcRequest,
    JsonRpcResponse,
    WebSocketFrameType,
)

# WebSocket abstractions (for advanced usage)
from fastapi_ws_rpc.simplewebsocket import JsonSerializingWebSocket, SimpleWebSocket

# Utility functions
from fastapi_ws_rpc.utils import gen_uid
from fastapi_ws_rpc.websocket_rpc_client import WebSocketRpcClient
from fastapi_ws_rpc.websocket_rpc_endpoint import WebSocketRpcEndpoint

__version__ = "0.1.0"

__all__ = [
    "ConnectionManager",
    "JsonRpcError",
    "JsonRpcErrorCode",
    # JSON-RPC schemas
    "JsonRpcRequest",
    "JsonRpcResponse",
    "JsonSerializingWebSocket",
    "LoggingModes",
    # Utilities
    "NoResponse",
    # Type aliases for callbacks
    "OnConnectCallback",
    "OnDisconnectCallback",
    "OnErrorCallback",
    "RemoteValueError",
    "RpcBackpressureError",
    # Core RPC classes
    "RpcChannel",
    "RpcChannelClosedError",
    # Exceptions
    "RpcError",
    "RpcInvalidStateError",
    "RpcMessageTooLargeError",
    "RpcMethodsBase",
    "RpcUtilityMethods",
    # WebSocket abstractions
    "SimpleWebSocket",
    "UnknownMethodError",
    "WebSocketFrameType",
    "WebSocketRpcClient",
    "WebSocketRpcEndpoint",
    "gen_uid",
    "get_logger",
    "logging_config",
]
