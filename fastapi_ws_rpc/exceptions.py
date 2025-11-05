"""
Exception classes for fastapi_ws_rpc.

This module defines all custom exceptions raised by the library.
All exceptions inherit from RpcError, which inherits from Exception.
"""


class RpcError(Exception):
    """
    Base exception for all RPC-related errors.

    This is the parent class for all custom exceptions raised by the
    fastapi_ws_rpc library. Catching this exception will catch
    all library-specific errors.
    """


class RpcChannelClosedError(RpcError):
    """
    Raised when attempting to use a channel that has been closed.

    This exception indicates that an RPC operation was attempted after
    the channel was closed, either explicitly via close() or due to
    connection failure.
    """


class UnknownMethodError(RpcError):
    """
    Raised when an RPC call references a method that doesn't exist.

    This exception is raised when the remote side calls a method that
    is not registered in the local RpcMethodsBase instance.
    """


class RemoteValueError(RpcError):
    """
    Raised when a remote operation returns an error or invalid value.

    This exception is used to propagate errors from the remote side back
    to the caller, including cases where remote operations fail or return
    unexpected values.
    """


class RpcInvalidStateError(RpcError):
    """
    Raised when an RPC operation is attempted in an invalid state.

    This exception indicates that a method was called when the RPC
    channel or client was not in the expected state (e.g., calling
    RPC methods before connection is established, or after the
    connection has been closed).

    Examples of invalid states:
    - Calling RPC methods before __aenter__() or connect()
    - Calling methods after close() has been called
    - WebSocket not initialized
    - RPC channel not initialized
    """


class RpcMessageTooLargeError(RpcError):
    """
    Raised when a received message exceeds the configured size limit.

    This exception prevents denial-of-service attacks via extremely
    large JSON payloads that could exhaust memory or CPU during parsing.
    The size limit is checked before deserialization to protect against
    malicious or misconfigured clients.

    This is a production hardening feature to ensure system stability
    under resource constraints or attack scenarios.
    """


class RpcBackpressureError(RpcError):
    """
    Raised when the channel has too many pending requests.

    This indicates backpressure - the caller should slow down or wait
    for pending requests to complete before sending more. This prevents
    resource exhaustion from clients flooding the channel with requests
    faster than they can be processed.

    When this exception is raised, the caller should either:
    - Wait for some pending requests to complete
    - Implement exponential backoff before retrying
    - Handle the overload condition gracefully

    This is a production hardening feature to ensure system stability
    under high load or attack scenarios.
    """
