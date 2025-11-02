"""
Simple wrappers for websocket objects to provide a common interface.
"""

import json
from abc import ABC, abstractmethod
from typing import Any, Optional, TypeVar, Union

from .logger import get_logger
from .utils import pydantic_serialize

logger = get_logger(__name__)

# Type variable for the socket message type
T = TypeVar("T")


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
    """

    def __init__(self, websocket: Any) -> None:
        """
        Initialize the JSON serializing websocket.

        Args:
            websocket: A websocket object with send/recv/close methods.
                      This can be a websockets.WebSocketClientProtocol or any object
                      that provides these methods.
        """
        self._websocket = websocket
        self.messages = {
            "request_messages": {},
            "ack_messages": {},
        }

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

    def _deserialize(self, buffer: Union[str, bytes]) -> Any:
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

        Returns:
            The deserialized message.
        """
        logger.debug("Waiting for message...")
        message = await self._websocket.recv()
        logger.debug(f"Received raw message: {message}")

        deserialized = self._deserialize(message)
        logger.debug(f"Deserialized message: {deserialized}")

        return deserialized

    async def receive_text(self) -> Optional[dict]:
        """
        Legacy method for compatibility.

        Returns:
            dict: The message dictionary if available.
        """
        if self.messages is not None:
            return self.messages
        return None

    async def close(self, code: int = 1000) -> None:
        """
        Close the websocket connection.

        Args:
            code: The websocket close code.
        """
        await self._websocket.close(code)
