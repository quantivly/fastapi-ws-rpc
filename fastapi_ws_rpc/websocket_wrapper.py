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

    # Extract proxy settings which need special handling
    http_proxy_host = kwargs.pop("http_proxy_host", None)
    http_proxy_port = kwargs.pop("http_proxy_port", None)
    http_proxy_auth = kwargs.pop("http_proxy_auth", None)

    # In v15+, proxy settings are handled differently
    if IS_V15_OR_HIGHER and http_proxy_host:
        logger.debug(f"Setting up proxy for v15+: {http_proxy_host}:{http_proxy_port}")

        try:
            # In websockets v15+, Proxy is now in websockets.legacy.proxy
            from websockets.legacy.proxy import Proxy

            # Create a proxy from the settings
            if http_proxy_auth:
                proxy_url = (
                    f"http://{http_proxy_auth}@{http_proxy_host}:{http_proxy_port}"
                )
            else:
                proxy_url = f"http://{http_proxy_host}:{http_proxy_port}"

            proxy = Proxy(proxy_url)
            kwargs["proxy"] = proxy
            logger.debug(f"Created proxy object for {proxy_url}")

        except ImportError:
            # If we can't import Proxy from legacy.proxy, try another path
            try:
                from websockets.typing import Proxy  # type: ignore[import]

                # Create a proxy from the settings
                if http_proxy_auth:
                    proxy_url = (
                        f"http://{http_proxy_auth}@{http_proxy_host}:{http_proxy_port}"
                    )
                else:
                    proxy_url = f"http://{http_proxy_host}:{http_proxy_port}"

                proxy = Proxy(proxy_url)
                kwargs["proxy"] = proxy
                logger.debug(f"Created proxy object from typing for {proxy_url}")

            except ImportError:
                # Last resort: try to get any Proxy class from the websockets package
                logger.warning(
                    "Could not import Proxy class from expected locations. "
                    "Attempting to find any available Proxy class in websockets."
                )
                try:
                    # Try a more general approach to find the Proxy class
                    import pkgutil

                    proxy_class = None
                    for _, name, _ in pkgutil.iter_modules(websockets.__path__):
                        try:
                            module = __import__(
                                f"websockets.{name}", fromlist=["Proxy"]
                            )
                            if hasattr(module, "Proxy"):
                                proxy_class = module.Proxy
                                logger.debug(f"Found Proxy class in websockets.{name}")
                                break
                        except ImportError:
                            continue

                    if proxy_class:
                        # Create a proxy from the settings
                        if http_proxy_auth:
                            proxy_url = f"http://{http_proxy_auth}@{http_proxy_host}:{http_proxy_port}"  # noqa: E501
                        else:
                            proxy_url = f"http://{http_proxy_host}:{http_proxy_port}"

                        proxy = proxy_class(proxy_url)
                        kwargs["proxy"] = proxy
                        logger.debug(
                            f"Created proxy object from discovered class for {proxy_url}"  # noqa: E501
                        )
                    else:
                        logger.error(
                            "Could not find Proxy class in websockets. "
                            "Proxy settings will not be applied."
                        )
                except Exception as e:
                    logger.error(f"Error searching for Proxy class: {e}")

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
        # For v10 and earlier versions
        # If we have proxy settings, add them back for pre-v15
        if not IS_V15_OR_HIGHER and http_proxy_host:
            kwargs["http_proxy_host"] = http_proxy_host
            if http_proxy_port:
                kwargs["http_proxy_port"] = http_proxy_port
            if http_proxy_auth:
                kwargs["http_proxy_auth"] = http_proxy_auth

        return await websockets.connect(uri, **kwargs)
