from enum import Enum
from typing import Any, Optional, TypeVar, Union

from pydantic import BaseModel

UUID = str


# JSON-RPC 2.0 Error Codes (as defined in the spec)
# https://www.jsonrpc.org/specification#error_object
class JsonRpcErrorCode(int, Enum):
    """Standard JSON-RPC 2.0 error codes."""

    PARSE_ERROR = -32700  # Invalid JSON was received by the server
    INVALID_REQUEST = -32600  # The JSON sent is not a valid Request object
    METHOD_NOT_FOUND = -32601  # The method does not exist / is not available
    INVALID_PARAMS = -32602  # Invalid method parameter(s)
    INTERNAL_ERROR = -32603  # Internal JSON-RPC error

    # Server errors (-32000 to -32099 are reserved for implementation-defined errors)
    # Can be extended for custom application errors


class JsonRpcRequest(BaseModel):
    """
    JSON-RPC 2.0 request format.

    Attributes:
        jsonrpc: Protocol version (always "2.0")
        id: Request identifier. If None, this is a notification (no response expected)
        method: Name of the method to call
        params: Method parameters. Can be:
            - dict: Named parameters (recommended) - passed as **kwargs to method
            - list: Positional parameters (LIMITED SUPPORT) - wrapped in params kwarg
            - None: No parameters

    Note on Positional Parameters:
        This implementation has limited support for positional parameters (list).
        When a list is provided, it's wrapped in a single "params" keyword argument
        rather than being unpacked as true positional arguments. This is a known
        limitation that would require method signature introspection to fix properly.

        Recommended: Always use named parameters (dict) for full compatibility.
    """

    jsonrpc: str = "2.0"
    id: Optional[Union[str, int]] = None
    method: str
    params: Optional[Union[dict[str, Any], list[Any]]] = None


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
