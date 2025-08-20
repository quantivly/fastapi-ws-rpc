from enum import Enum
from typing import Any, Dict, Generic, List, Optional, TypeVar, Union

from pydantic import BaseModel

from .utils import is_pydantic_pre_v2

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


# Backward compatibility classes
class RpcRequest(BaseModel):
    method: str
    arguments: Optional[Dict] = {}
    call_id: Optional[UUID] = None


# Check pydantic version to handle deprecated GenericModel
if is_pydantic_pre_v2():
    from pydantic.generics import GenericModel

    class RpcResponse(GenericModel, Generic[ResponseT]):
        result: ResponseT
        result_type: Optional[str]
        call_id: Optional[UUID] = None

else:

    class RpcResponse(BaseModel, Generic[ResponseT]):
        result: ResponseT
        result_type: Optional[str]
        call_id: Optional[UUID] = None


class RpcMessage(BaseModel):
    request: Optional[RpcRequest] = None
    response: Optional[RpcResponse] = None


class WebSocketFrameType(str, Enum):
    Text = "text"
    Binary = "binary"
