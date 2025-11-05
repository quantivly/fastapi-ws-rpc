"""
Simple wrappers for websocket objects to provide a common interface.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from typing import Any, Protocol, TypeVar, runtime_checkable

from .exceptions import RpcMessageTooLargeError
from .logger import get_logger
from .utils import pydantic_serialize

logger = get_logger(__name__)

# Type variable for the socket message type
T = TypeVar("T")

# Default maximum message size: 10MB
# This prevents DoS attacks via extremely large JSON payloads
DEFAULT_MAX_MESSAGE_SIZE = 10 * 1024 * 1024  # 10 MB


@runtime_checkable
class SerializingSocketProtocol(Protocol):
    """
    Protocol for WebSocket wrapper classes that support JSON serialization.

    This protocol defines the interface for socket wrapper classes that can
    optionally accept a max_message_size parameter during initialization.
    It enables type-safe usage of different serializing socket implementations.

    Notes
    -----
    This protocol is used to type-hint socket classes that may or may not
    support the max_message_size parameter, allowing graceful fallback.
    """

    def __init__(
        self,
        websocket: Any,
        max_message_size: int = DEFAULT_MAX_MESSAGE_SIZE,
    ) -> None:
        """
        Initialize the serializing socket with an optional message size limit.

        Parameters
        ----------
        websocket : Any
            The underlying WebSocket connection.
        max_message_size : int, optional
            Maximum allowed message size in bytes.
        """
        ...


class SimpleWebSocket(ABC):
    """
    Abstract base class for WebSocket wrapper implementations.

    This class defines the minimal interface required for WebSocket objects
    used by the RPC system. Implementations should handle the specific
    WebSocket library's API and provide these standardized methods.

    Notes
    -----
    Subclasses must implement send(), recv(), and close() methods.
    The closed property has a default implementation but can be overridden.

    See Also
    --------
    JsonSerializingWebSocket : Concrete implementation with JSON serialization.

    Examples
    --------
    Implement a custom WebSocket wrapper:

    >>> class MyWebSocket(SimpleWebSocket):
    ...     def __init__(self, ws):
    ...         self._ws = ws
    ...
    ...     async def send(self, message):
    ...         await self._ws.send(message)
    ...
    ...     async def recv(self):
    ...         return await self._ws.recv()
    ...
    ...     async def close(self, code=1000):
    ...         await self._ws.close(code)
    """

    @property
    def closed(self) -> bool:
        """
        Check if the WebSocket connection is closed.

        Returns
        -------
        bool
            True if the socket is closed, False otherwise.
            Default implementation always returns False.

        Notes
        -----
        Subclasses should override this to check the actual socket state.
        """
        return False

    @abstractmethod
    async def send(self, message: Any) -> None:
        """
        Send a message over the WebSocket.

        Parameters
        ----------
        message : Any
            The message to send. Type depends on the implementation.

        Notes
        -----
        Implementations should handle serialization if needed.
        """
        ...

    @abstractmethod
    async def recv(self) -> Any:
        """
        Receive a message from the WebSocket.

        Returns
        -------
        Any
            The received message. Type depends on the implementation.

        Notes
        -----
        Implementations should handle deserialization if needed.
        This method should block until a message is available.
        """
        ...

    @abstractmethod
    async def close(self, code: int = 1000) -> None:
        """
        Close the WebSocket connection.

        Parameters
        ----------
        code : int, optional
            WebSocket close code (default is 1000 for normal closure).
            Standard codes:
            - 1000: Normal closure
            - 1001: Going away
            - 1002: Protocol error
            - 1011: Internal error

        Notes
        -----
        After calling close(), no further send() or recv() calls should be made.
        """
        ...


class JsonSerializingWebSocket(SimpleWebSocket):
    """
    WebSocket wrapper with automatic JSON serialization/deserialization.

    This class wraps a raw WebSocket connection and handles JSON encoding/decoding
    automatically. It also provides message size validation to prevent
    denial-of-service attacks from extremely large payloads.

    Parameters
    ----------
    websocket : Any
        A WebSocket object with send/recv/close methods. This can be a
        websockets.WebSocketClientProtocol, FastAPI WebSocket, or any object
        that provides these async methods.
    max_message_size : int, optional
        Maximum allowed message size in bytes (default is 10MB).
        Messages exceeding this size will be rejected before deserialization
        to prevent resource exhaustion.

    Attributes
    ----------
    _websocket : Any
        The underlying WebSocket connection.
    _max_message_size : int
        Maximum message size limit in bytes.

    See Also
    --------
    SimpleWebSocket : Abstract base class interface.
    DEFAULT_MAX_MESSAGE_SIZE : Default 10MB message size limit.

    Examples
    --------
    Wrap a websockets library connection:

    >>> import websockets
    >>> async with websockets.connect(uri) as ws:
    ...     json_ws = JsonSerializingWebSocket(ws)
    ...     await json_ws.send({"method": "ping"})
    ...     response = await json_ws.recv()

    Use with custom message size limit:

    >>> json_ws = JsonSerializingWebSocket(
    ...     ws,
    ...     max_message_size=1024 * 1024  # 1MB limit
    ... )
    """

    def __init__(
        self, websocket: Any, max_message_size: int = DEFAULT_MAX_MESSAGE_SIZE
    ) -> None:
        self._websocket = websocket
        self._max_message_size = max_message_size

    @property
    def closed(self) -> bool:
        """
        Check if the underlying WebSocket is closed.

        Returns
        -------
        bool
            True if the websocket is closed, False otherwise.
            Checks the underlying websocket's closed attribute if available.
        """
        return getattr(self._websocket, "closed", False)

    def _serialize(self, message: Any) -> str:
        """
        Serialize a message to JSON string.

        Parameters
        ----------
        message : Any
            The message to serialize. Can be a dict or Pydantic model.

        Returns
        -------
        str
            JSON-encoded message string.

        Notes
        -----
        For dict objects, uses json.dumps(). For Pydantic models,
        uses pydantic_serialize() which handles model_dump().
        """
        if isinstance(message, dict):
            return json.dumps(message)
        return pydantic_serialize(message)

    def _deserialize(self, buffer: str | bytes) -> Any:
        """
        Deserialize a JSON message to Python object.

        Parameters
        ----------
        buffer : str | bytes
            The JSON message to deserialize. Bytes will be decoded as UTF-8.

        Returns
        -------
        Any
            Deserialized Python object (typically dict).

        Notes
        -----
        Handles both str and bytes input automatically.
        Logs the deserialization for debugging purposes.
        """
        if isinstance(buffer, bytes):
            buffer = buffer.decode("utf-8")

        logger.debug(f"Deserializing message: {buffer}")
        return json.loads(buffer)

    async def send(self, message: Any) -> None:
        """
        Serialize and send a message over the WebSocket.

        Automatically converts the message to JSON before sending.

        Parameters
        ----------
        message : Any
            The message to send. Will be JSON-serialized automatically.
            Can be dict, Pydantic model, or any JSON-serializable object.

        Examples
        --------
        >>> await json_ws.send({"method": "ping", "params": {}})
        """
        await self._websocket.send(self._serialize(message))

    async def recv(self) -> Any:
        """
        Receive and deserialize a message from the WebSocket.

        Validates message size before deserialization to prevent DoS attacks
        via extremely large JSON payloads. Messages exceeding the size limit
        are rejected immediately.

        Returns
        -------
        Any
            Deserialized message (typically dict).

        Raises
        ------
        RpcMessageTooLargeError
            If the message exceeds max_message_size limit.

        Notes
        -----
        Size validation occurs BEFORE JSON parsing to prevent resource
        exhaustion from malicious payloads. This is critical for production
        deployments exposed to untrusted clients.

        Examples
        --------
        >>> message = await json_ws.recv()
        >>> print(message["method"])  # Access deserialized data
        """
        logger.debug("Waiting for message...")
        message = await self._websocket.recv()

        # Validate message size BEFORE deserialization
        # This prevents memory exhaustion from huge JSON payloads
        message_size: int
        if isinstance(message, str):
            message_size = len(message.encode("utf-8"))
        elif isinstance(message, bytes):
            message_size = len(message)
        else:
            message_size = 0

        if message_size > self._max_message_size:
            logger.error(
                f"Received message exceeds size limit: {message_size} bytes "
                f"(limit: {self._max_message_size} bytes)"
            )
            raise RpcMessageTooLargeError(
                f"Incoming message size ({message_size} bytes) exceeds limit "
                f"({self._max_message_size} bytes). "
                f"This may indicate a malicious payload or misconfiguration."
            )

        logger.debug(f"Received raw message ({message_size} bytes)")

        deserialized = self._deserialize(message)
        logger.debug(f"Deserialized message: {deserialized}")

        return deserialized

    async def close(self, code: int = 1000) -> None:
        """
        Close the WebSocket connection.

        Parameters
        ----------
        code : int, optional
            WebSocket close code (default is 1000 for normal closure).

        Notes
        -----
        After calling close(), no further send() or recv() calls should be made.
        """
        await self._websocket.close(code)
