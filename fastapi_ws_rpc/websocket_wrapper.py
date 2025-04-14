"""
Websocket wrapper module to ensure backward compatibility with different
websockets versions and improve consistent error handling.
"""
import os
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

    # Handle proxy settings
    # For v15+, use environment variables
    if IS_V15_OR_HIGHER:
        http_proxy_host = kwargs.pop("http_proxy_host", None)
        http_proxy_port = kwargs.pop("http_proxy_port", None)
        http_proxy_auth = kwargs.pop("http_proxy_auth", None)

        if http_proxy_host:
            # Check if we're connecting via wss:// or ws://
            is_secure = uri.startswith("wss://")
            env_var_name = "WSS_PROXY" if is_secure else "WS_PROXY"

            # If the proxy environment variable isn't already set, set it now
            if env_var_name not in os.environ and http_proxy_host:
                proxy_url = f"http://{http_proxy_host}"
                if http_proxy_port:
                    proxy_url += f":{http_proxy_port}"
                if http_proxy_auth:
                    # Split the auth into username and password if it contains a colon
                    if ":" in http_proxy_auth:
                        username, password = http_proxy_auth.split(":", 1)
                        proxy_url = f"http://{username}:{password}@{http_proxy_host}"
                        if http_proxy_port:
                            proxy_url += f":{http_proxy_port}"

                logger.info(
                    f"Setting {env_var_name}={proxy_url} for websockets v15+ proxy support"  # noqa: E501
                )
                os.environ[env_var_name] = proxy_url

                # Also set the standard proxy variables as fallback
                if is_secure:
                    os.environ.setdefault("HTTPS_PROXY", proxy_url)
                else:
                    os.environ.setdefault("HTTP_PROXY", proxy_url)

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
        # For v10 and earlier, everything works differently
        # Add proxy settings back for older versions
        if not IS_V15_OR_HIGHER and "http_proxy_host" in kwargs:
            # This block won't execute with v15+ because we've already
            # removed these keys from kwargs
            return await websockets.connect(uri, **kwargs)
        else:
            return await websockets.connect(uri, **kwargs)
