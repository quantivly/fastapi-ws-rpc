from enum import Enum
from typing import Any, Dict, List, Optional, TypeVar, Union

from pydantic import BaseModel

UUID = str


class JsonRpcRequest(BaseModel):
    """JSON-RPC 2.0 request format"""

    jsonrpc: str = "2.0"
    id: Optional[Union[str, int]] = None
    method: str
    params: Optional[Union[Dict[str, Any], List[Any]]] = None


class JsonRpcError(BaseModel):
    """JSON-RPC 2.0 error format"""

    code: int
    message: str
    data: Optional[Any] = None


class JsonRpcResponse(BaseModel):
    """JSON-RPC 2.0 response format"""

    jsonrpc: str = "2.0"
    id: Optional[Union[str, int]] = None
    result: Optional[Any] = None
    error: Optional[JsonRpcError] = None
    compressed: Optional[bool] = None  # Extension for compression


ResponseT = TypeVar("ResponseT")


# Legacy classes removed - using JSON-RPC 2.0 only


class WebSocketFrameType(str, Enum):
    Text = "text"
    Binary = "binary"
