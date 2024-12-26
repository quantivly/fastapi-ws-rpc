import json
from abc import ABC, abstractmethod

from .logger import get_logger
from .utils import pydantic_serialize

logger = get_logger("fastapi_ws_rpc.simplewebsocket")


class SimpleWebSocket(ABC):
    """
    Abstract base class for all websocket related wrappers.
    """

    @abstractmethod
    def send(self, message):
        pass

    @abstractmethod
    def recv(self):
        pass

    @abstractmethod
    def close(self, code: int = 1000):
        pass


class JsonSerializingWebSocket(SimpleWebSocket):
    def __init__(self, websocket: SimpleWebSocket):
        self._websocket = websocket
        self.messages = {
            "request_messages": {},
            "ack_messages": {},
        }

    def _serialize(self, message):
        return pydantic_serialize(message)

    def _deserialize(self, buffer):
        logger.debug(f"Deserializing message: {buffer}")
        return json.loads(buffer)

    async def send(self, message):
        await self._websocket.send(self._serialize(message))

    async def recv(self):
        logger.debug("Waiting for message...")
        message = await self._websocket.recv()
        logger.debug(f"Received message: {message}")
        message = self._deserialize(message)
        if "request" in message and isinstance(message.get("request"), dict):
            method_name = message["request"].get("method")
            call_id = message["request"].get("call_id")
            if method_name and call_id:
                self.messages["request_messages"][method_name] = call_id
        else:
            self.messages["ack_messages"] = message
        return message

    async def receive_text(self):
        if self.messages is not None:
            return self.messages

    async def close(self, code: int = 1000):
        await self._websocket.close(code)
