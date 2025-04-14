"""
Websocket wrapper module to ensure backward compatibility with different
websockets versions and improve consistent error handling.
"""
from typing import Any

import websockets

from .logger import get_logger

logger = get_logger("WebSocketWrapper")

# Detect websocket library version
WEBSOCKETS_VERSION = tuple(int(x) for x in websockets.__version__.split(".")[:2])
IS_V10_OR_HIGHER = WEBSOCKETS_VERSION >= (10, 0)
IS_V11_OR_HIGHER = WEBSOCKETS_VERSION >= (11, 0)
IS_V13_OR_HIGHER = WEBSOCKETS_VERSION >= (13, 0)
IS_V15_OR_HIGHER = WEBSOCKETS_VERSION >= (15, 0)

logger.debug(f"Detected websockets version: {websockets.__version__}")

# Import the appropriate exception classes based on version
if IS_V15_OR_HIGHER:
    # v15+ has reorganized the exceptions
    # In v15+, InvalidStatusCode is in the http module
    from websockets.exceptions import (
        ConnectionClosed,
        ConnectionClosedError,
        ConnectionClosedOK,
        InvalidStatus,
        WebSocketException,
    )
elif IS_V13_OR_HIGHER:
    # v13-v14 exceptions
    from websockets.exceptions import (
        ConnectionClosed,
        ConnectionClosedError,
        ConnectionClosedOK,
        InvalidStatusCode,
        WebSocketException,
    )

    # Alias for compatibility
    InvalidStatus = InvalidStatusCode
elif IS_V10_OR_HIGHER:
    # v10-v12 exceptions
    from websockets.exceptions import (
        ConnectionClosed,
        ConnectionClosedError,
        ConnectionClosedOK,
        InvalidStatusCode,
        WebSocketException,
    )

    # Alias for compatibility
    InvalidStatus = InvalidStatusCode
else:
    # Pre-v10 exceptions
    try:
        from websockets.exceptions import (
            ConnectionClosed,
            ConnectionClosedError,
            ConnectionClosedOK,
            InvalidHandshake,
            WebSocketException,
        )

        # Alias for compatibility
        InvalidStatus = InvalidHandshake
    except ImportError:
        # Fall back to v10+ style if specific imports fail
        from websockets.exceptions import (
            ConnectionClosed,
            ConnectionClosedError,
            ConnectionClosedOK,
            InvalidStatusCode,
            WebSocketException,
        )

        # Alias for compatibility
        InvalidStatus = InvalidStatusCode

# Export all the exception types
__all__ = [
    "connect",
    "ConnectionClosed",
    "ConnectionClosedOK",
    "ConnectionClosedError",
    "InvalidStatus",
    "WebSocketException",
]


async def connect(uri: str, **kwargs: Any) -> Any:
    """
    Connect to a WebSocket server, with version-appropriate handling.

    Args:
        uri: WebSocket URI to connect to
        **kwargs: Additional arguments to pass to websockets.connect

    Returns:
        WebSocketClientProtocol or ClientConnection depending on library version
    """
    logger.debug(f"Connecting to {uri} with websockets v{websockets.__version__}")

    # Handle ping_interval and ping_timeout in a version-compatible way
    if IS_V11_OR_HIGHER:
        # In v11+, these are attributes of the connection object, not connect params
        ping_interval = kwargs.pop("ping_interval", 20)
        ping_timeout = kwargs.pop("ping_timeout", 20)

        # Connect with remaining kwargs
        connection = await websockets.connect(uri, **kwargs)

        # Set ping settings on the connection object if not None
        if ping_interval is not None:
            connection.ping_interval = ping_interval
        if ping_timeout is not None:
            connection.ping_timeout = ping_timeout

        return connection
    else:
        # For v10 and earlier, ping settings are passed directly to connect
        return await websockets.connect(uri, **kwargs)
