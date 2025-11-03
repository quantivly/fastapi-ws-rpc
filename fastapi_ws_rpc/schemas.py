from enum import Enum
from typing import Any, Optional, TypeVar, Union

from pydantic import BaseModel

UUID = str


# JSON-RPC 2.0 Error Codes (as defined in the spec)
# https://www.jsonrpc.org/specification#error_object
class JsonRpcErrorCode(int, Enum):
    """
    Standard JSON-RPC 2.0 error codes.

    These error codes are defined by the JSON-RPC 2.0 specification and
    indicate different types of failures during request processing.

    Attributes
    ----------
    PARSE_ERROR : int
        Invalid JSON was received (-32700). The server received invalid
        JSON that could not be parsed.
    INVALID_REQUEST : int
        The JSON sent is not a valid Request object (-32600). Missing
        required fields or malformed structure.
    METHOD_NOT_FOUND : int
        The method does not exist or is not available (-32601). Either
        the method name is unknown or it's a private method.
    INVALID_PARAMS : int
        Invalid method parameters (-32602). Wrong number of parameters,
        wrong types, or missing required parameters.
    INTERNAL_ERROR : int
        Internal JSON-RPC error (-32603). An exception occurred during
        method execution that wasn't a parameter validation error.

    Notes
    -----
    The range -32000 to -32099 is reserved for implementation-defined
    server errors. You can extend this enum with custom error codes
    in that range for application-specific errors.

    See Also
    --------
    JsonRpcError : The error object that uses these codes.

    Examples
    --------
    Create error responses:

    >>> error = JsonRpcError(
    ...     code=JsonRpcErrorCode.METHOD_NOT_FOUND,
    ...     message="Method 'unknown_method' not found"
    ... )

    >>> error = JsonRpcError(
    ...     code=JsonRpcErrorCode.INVALID_PARAMS,
    ...     message="Missing required parameter 'user_id'",
    ...     data={"missing": ["user_id"]}
    ... )

    Custom application errors:

    >>> class CustomErrorCode(int, Enum):
    ...     DATABASE_ERROR = -32000
    ...     AUTHENTICATION_FAILED = -32001
    """

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
    """
    JSON-RPC 2.0 error object format.

    Represents an error that occurred during request processing. This object
    is included in the error field of a JsonRpcResponse when method execution
    fails.

    Parameters
    ----------
    code : int
        Numeric error code indicating the error type. Should use values from
        JsonRpcErrorCode enum for standard errors, or values in range -32000
        to -32099 for application-specific errors.
    message : str
        Human-readable error message providing a short description of the error.
    data : Any, optional
        Additional error information (default is None). Can be any JSON-serializable
        type with context about the error (e.g., stack trace, validation details).

    Notes
    -----
    According to JSON-RPC 2.0 specification:
    - code MUST be an integer
    - message MUST be a string
    - data is optional and can be any JSON-serializable value

    See Also
    --------
    JsonRpcErrorCode : Standard error code values.
    JsonRpcResponse : Response format that contains error objects.

    Examples
    --------
    Method not found error:

    >>> error = JsonRpcError(
    ...     code=JsonRpcErrorCode.METHOD_NOT_FOUND,
    ...     message="Method 'calculate' not found"
    ... )

    Invalid parameters with details:

    >>> error = JsonRpcError(
    ...     code=JsonRpcErrorCode.INVALID_PARAMS,
    ...     message="Missing required parameters",
    ...     data={"missing": ["user_id", "action"], "provided": ["timestamp"]}
    ... )
    """

    code: int
    message: str
    data: Optional[Any] = None


class JsonRpcResponse(BaseModel):
    """
    JSON-RPC 2.0 response message format.

    Represents a response to a JSON-RPC request, containing either a result
    (success) or an error (failure), but never both. Responses must match
    the request ID to correlate with pending requests.

    Parameters
    ----------
    jsonrpc : str
        Protocol version, always "2.0" for JSON-RPC 2.0 compliance.
    id : str | int | None, optional
        Request identifier matching the original request (default is None).
        Required for responses to requests. May be None for malformed requests
        where the ID could not be determined.
    result : Any | None, optional
        The result data from successful method execution (default is None).
        Must be None if error is present. Can be any JSON-serializable type.
    error : JsonRpcError | None, optional
        Error information if method execution failed (default is None).
        Must be None if result is present. Contains code, message, and
        optional data.
    compressed : bool | None, optional
        Extension flag indicating if the payload was compressed (default is None).
        Not part of JSON-RPC 2.0 spec, used for large payload optimization.

    Notes
    -----
    According to JSON-RPC 2.0 specification:
    - MUST contain either result or error, never both
    - MUST contain id matching the request
    - result SHOULD NOT be included if error is present
    - error SHOULD NOT be included if result is present

    See Also
    --------
    JsonRpcRequest : The corresponding request format.
    JsonRpcError : Error object structure.

    Examples
    --------
    Success response:

    >>> response = JsonRpcResponse(id="123", result={"data": "success"})
    >>> response.model_dump(exclude_none=True)
    {'jsonrpc': '2.0', 'id': '123', 'result': {'data': 'success'}}

    Error response:

    >>> error = JsonRpcError(code=-32601, message="Method not found")
    >>> response = JsonRpcResponse(id="123", error=error)
    >>> response.model_dump(exclude_none=True)
    {'jsonrpc': '2.0', 'id': '123', 'error': {...}}
    """

    jsonrpc: str = "2.0"
    id: Optional[Union[str, int]] = None
    result: Optional[Any] = None
    error: Optional[JsonRpcError] = None
    compressed: Optional[bool] = None  # Extension for compression


ResponseT = TypeVar("ResponseT")


# Legacy classes removed - using JSON-RPC 2.0 only


class WebSocketFrameType(str, Enum):
    """
    WebSocket frame type for message transmission.

    Specifies whether WebSocket messages should be sent as text or binary frames.
    This affects how data is serialized and transmitted over the WebSocket connection.

    Attributes
    ----------
    Text : str
        Text frame type ("text"). Messages are sent as UTF-8 encoded text.
        Used for JSON messages in most cases.
    Binary : str
        Binary frame type ("binary"). Messages are sent as raw bytes.
        Can be used for custom binary protocols or compressed data.

    Examples
    --------
    Create endpoint with text frames (default):

    >>> endpoint = WebSocketRpcEndpoint(
    ...     methods,
    ...     frame_type=WebSocketFrameType.Text
    ... )

    Create endpoint with binary frames:

    >>> endpoint = WebSocketRpcEndpoint(
    ...     methods,
    ...     frame_type=WebSocketFrameType.Binary
    ... )
    """

    Text = "text"
    Binary = "binary"
