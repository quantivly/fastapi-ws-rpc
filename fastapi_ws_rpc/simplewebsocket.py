"""
Simple wrappers for websocket objects to provide a common interface.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from typing import Any, TypeVar

from .exceptions import RpcMessageTooLargeError
from .logger import get_logger
from .utils import pydantic_serialize

logger = get_logger(__name__)

# Type variable for the socket message type
T = TypeVar("T")

# Default maximum message size: 10MB
# This prevents DoS attacks via extremely large JSON payloads
DEFAULT_MAX_MESSAGE_SIZE = 10 * 1024 * 1024  # 10 MB


class SimpleWebSocket(ABC):
    """
    Abstract base class for all websocket related wrappers.
    """

    @property
    def closed(self) -> bool:
        """
        Returns whether the socket is closed.
        This allows for a consistent interface for checking socket status.

        Returns:
            bool: True if the socket is closed, False otherwise.
        """
        return False

    @abstractmethod
    async def send(self, message: Any) -> None:
        """
        Send a message over the websocket.

        Args:
            message: The message to send.
        """
        pass

    @abstractmethod
    async def recv(self) -> Any:
        """
        Receive a message from the websocket.

        Returns:
            The received message.
        """
        pass

    @abstractmethod
    async def close(self, code: int = 1000) -> None:
        """
        Close the websocket connection.

        Args:
            code: The websocket close code.
        """
        pass


class JsonSerializingWebSocket(SimpleWebSocket):
    """
    A wrapper for websocket objects that automatically serializes and deserializes
    JSON messages.

    Provides message size validation to prevent denial-of-service attacks via
    extremely large payloads.
    """

    def __init__(
        self, websocket: Any, max_message_size: int = DEFAULT_MAX_MESSAGE_SIZE
    ) -> None:
        """
        Initialize the JSON serializing websocket.

        Args:
            websocket: A websocket object with send/recv/close methods.
                      This can be a websockets.WebSocketClientProtocol or any object
                      that provides these methods.
            max_message_size: Maximum allowed message size in bytes (default 10MB).
                             Messages exceeding this size will be rejected before
                             deserialization to prevent resource exhaustion.
        """
        self._websocket = websocket
        self._max_message_size = max_message_size

    @property
    def closed(self) -> bool:
        """
        Check if the underlying websocket is closed.

        Returns:
            bool: True if the websocket is closed, False otherwise.
        """
        return getattr(self._websocket, "closed", False)

    def _serialize(self, message: Any) -> str:
        """
        Serialize a message to JSON.

        Args:
            message: The message to serialize.

        Returns:
            str: The serialized message.
        """
        if isinstance(message, dict):
            return json.dumps(message)
        return pydantic_serialize(message)

    def _deserialize(self, buffer: str | bytes) -> Any:
        """
        Deserialize a JSON message.

        Args:
            buffer: The message to deserialize.

        Returns:
            The deserialized message.
        """
        if isinstance(buffer, bytes):
            buffer = buffer.decode("utf-8")

        logger.debug(f"Deserializing message: {buffer}")
        return json.loads(buffer)

    async def send(self, message: Any) -> None:
        """
        Serialize and send a message over the websocket.

        Args:
            message: The message to send.
        """
        await self._websocket.send(self._serialize(message))

    async def recv(self) -> Any:
        """
        Receive and deserialize a message from the websocket.

        Validates message size before deserialization to prevent DoS attacks
        via extremely large JSON payloads.

        Returns:
            The deserialized message.

        Raises:
            RpcMessageTooLargeError: If the message exceeds max_message_size.
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
        Close the websocket connection.

        Args:
            code: The websocket close code.
        """
        await self._websocket.close(code)
