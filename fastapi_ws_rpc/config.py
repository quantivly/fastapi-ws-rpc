"""Configuration dataclasses for FastAPI WebSocket RPC client.

This module provides immutable, validated configuration objects for all aspects
of the RPC client behavior including connection lifecycle, backpressure management,
keepalive behavior, and retry logic.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class RpcDebugConfig:
    """Configuration for RPC debugging and error disclosure.

    Controls how much error information is exposed to clients.
    In production, errors should be sanitized to prevent information disclosure.

    Parameters
    ----------
    debug_mode : bool, default False
        Whether to include full error details in responses. When False (default),
        errors are sanitized to generic messages to prevent leaking implementation
        details. When True, full error details including exception types and method
        names are included for debugging.

    Examples
    --------
    >>> # Production mode (default) - sanitized errors
    >>> config = RpcDebugConfig()
    >>> assert not config.debug_mode

    >>> # Development mode - full error details
    >>> config = RpcDebugConfig(debug_mode=True)
    >>> assert config.debug_mode
    """

    debug_mode: bool = False

    def validate(self) -> None:
        """Explicitly validate the configuration.

        This method exists for consistency with other config classes.
        No validation needed for boolean field.
        """


@dataclass(frozen=True)
class WebSocketConnectionConfig:
    """Configuration for WebSocket-specific connection settings.

    Controls WebSocket protocol-level features like subprotocol negotiation
    and message compression (permessage-deflate extension).

    Parameters
    ----------
    subprotocols : list[str] | None, default ["jsonrpc2.0"]
        List of WebSocket subprotocols to negotiate. Defaults to ["jsonrpc2.0"]
        for JSON-RPC 2.0 compliance. Set to None or empty list to disable
        subprotocol negotiation.
    compression : str | None, default "deflate"
        WebSocket compression method to use. Supports "deflate" for
        permessage-deflate extension (RFC 7692), or None to disable compression.
        Compression can reduce bandwidth by 20-50% for typical JSON payloads
        with minimal CPU overhead. Enabled by default for bandwidth optimization.
    compression_threshold : int, default 1024
        Minimum message size in bytes to trigger compression. Messages smaller
        than this threshold are sent uncompressed to avoid overhead. Default is
        1KB which is optimal for most use cases.
    ping_timeout : float | None, default None
        Timeout in seconds for WebSocket ping/pong frames. If None, uses the
        websockets library default of 20 seconds. This should typically be set
        to 2-3x the ping_interval (if using protocol-level pings) to account
        for network latency. For production with 30s ping_interval, recommend
        setting this to 40-60 seconds to prevent premature disconnections.

    Examples
    --------
    >>> # Default JSON-RPC 2.0 subprotocol with compression
    >>> config = WebSocketConnectionConfig()
    >>> assert config.subprotocols == ["jsonrpc2.0"]
    >>> assert config.compression == "deflate"

    >>> # Disable compression for low-latency scenarios
    >>> config = WebSocketConnectionConfig(compression=None)
    >>> assert config.compression is None

    >>> # Custom compression threshold (compress only large messages)
    >>> config = WebSocketConnectionConfig(compression_threshold=10240)  # 10KB
    >>> assert config.compression_threshold == 10240

    >>> # Disable subprotocol negotiation but keep compression
    >>> config = WebSocketConnectionConfig(subprotocols=None, compression="deflate")
    >>> assert config.subprotocols is None
    >>> assert config.compression == "deflate"

    >>> # Configure ping_timeout for production (60s timeout for 30s ping interval)
    >>> config = WebSocketConnectionConfig(ping_timeout=60.0)
    >>> assert config.ping_timeout == 60.0

    Notes
    -----
    Compression uses the permessage-deflate WebSocket extension which is widely
    supported by modern WebSocket implementations. The websockets library supports
    this natively with minimal configuration.

    Performance impact:
    - Bandwidth: 20-50% reduction for JSON (higher for repetitive data)
    - CPU: ~1-5% overhead for compression/decompression
    - Latency: Minimal impact (<1ms for typical messages)
    """

    subprotocols: list[str] | None = field(default_factory=lambda: ["jsonrpc2.0"])
    compression: str | None = "deflate"
    compression_threshold: int = 1024  # 1KB
    ping_timeout: float | None = None

    def validate(self) -> None:
        """Explicitly validate the configuration.

        Raises
        ------
        ValueError
            If compression is not None or "deflate", if compression_threshold
            is negative, or if ping_timeout is negative.
        """
        if self.compression is not None and self.compression != "deflate":
            raise ValueError(
                f"Invalid compression method: '{self.compression}'. "
                f"Supported values: None (disabled) or 'deflate' (permessage-deflate)"
            )

        if self.compression_threshold < 0:
            raise ValueError(
                f"compression_threshold must be non-negative, got {self.compression_threshold}"
            )

        if self.ping_timeout is not None and self.ping_timeout < 0:
            raise ValueError(
                f"ping_timeout must be non-negative, got {self.ping_timeout}"
            )


@dataclass(frozen=True)
class RpcConnectionConfig:
    """Configuration for RPC connection lifecycle and timeouts.

    Controls how long connections stay alive, how long to wait for responses,
    when to consider a connection idle, and automatic reconnection behavior.

    Parameters
    ----------
    default_response_timeout : float | None, default None
        Default timeout in seconds for RPC responses. If None, requests will
        wait indefinitely. Can be overridden per-request.
    max_connection_duration : float | None, default None
        Maximum time in seconds a connection can remain active before being
        gracefully closed. If None, connections can remain open indefinitely.
    idle_timeout : float | None, default None
        Time in seconds of inactivity after which the connection is considered
        idle and should be closed. If None, idle connections stay open.
        Replaces the old receive_timeout concept.
    message_receive_timeout : float | None, default None
        Maximum time in seconds to receive a complete WebSocket message.
        Originally designed for Slowloris attack protection (hardcoded at 60s).
        If None (default), no timeout is enforced on message reception.
        Use idle_timeout for general connection health monitoring instead.
        Only set this explicitly if you need protection against slow-read attacks.
    auto_reconnect : bool, default False
        Enable automatic reconnection when the connection is lost. When True,
        the client will attempt to reconnect with exponential backoff up to
        max_reconnect_attempts. Default is False for backward compatibility.
    reconnect_delay : float, default 5.0
        Initial delay in seconds before the first reconnection attempt.
        Subsequent attempts use exponential backoff (multiplied by 2).
        Maximum delay is capped at 60 seconds to prevent excessively long waits.
    max_reconnect_attempts : int, default 10
        Maximum number of reconnection attempts before giving up and closing
        the connection. Only used when auto_reconnect is True.
    debug : RpcDebugConfig, default RpcDebugConfig()
        Debug configuration controlling error disclosure. Defaults to
        production-safe mode (debug_mode=False) for v1.0.0 security.
    websocket : WebSocketConnectionConfig, default WebSocketConnectionConfig()
        WebSocket-specific connection configuration including subprotocol
        negotiation. Defaults to JSON-RPC 2.0 subprotocol.

    Examples
    --------
    >>> # Short timeouts for production
    >>> config = RpcConnectionConfig(
    ...     default_response_timeout=30.0,
    ...     max_connection_duration=3600.0,
    ...     idle_timeout=300.0
    ... )
    >>> config.validate()

    >>> # No timeouts for development
    >>> config = RpcConnectionConfig()
    >>> config.validate()

    >>> # Development with debug mode enabled
    >>> config = RpcConnectionConfig(
    ...     debug=RpcDebugConfig(debug_mode=True)
    ... )
    >>> assert config.debug.debug_mode

    >>> # Custom WebSocket subprotocol
    >>> config = RpcConnectionConfig(
    ...     websocket=WebSocketConnectionConfig(subprotocols=["custom.v1"])
    ... )
    >>> assert config.websocket.subprotocols == ["custom.v1"]

    >>> # Enable automatic reconnection with custom settings
    >>> config = RpcConnectionConfig(
    ...     auto_reconnect=True,
    ...     reconnect_delay=2.0,
    ...     max_reconnect_attempts=5
    ... )
    >>> assert config.auto_reconnect
    """

    default_response_timeout: float | None = None
    max_connection_duration: float | None = None
    idle_timeout: float | None = None
    message_receive_timeout: float | None = None
    auto_reconnect: bool = False
    reconnect_delay: float = 5.0
    max_reconnect_attempts: int = 10
    debug: RpcDebugConfig = field(default_factory=RpcDebugConfig)
    websocket: WebSocketConnectionConfig = field(
        default_factory=WebSocketConnectionConfig
    )

    def __post_init__(self) -> None:
        """Validate configuration after initialization.

        Raises
        ------
        ValueError
            If any timeout values are negative, if response_timeout
            is greater than or equal to max_connection_duration, or if
            reconnection parameters are invalid.
        """
        if (
            self.default_response_timeout is not None
            and self.default_response_timeout < 0
        ):
            raise ValueError(
                f"default_response_timeout must be non-negative, got {self.default_response_timeout}"
            )

        if (
            self.max_connection_duration is not None
            and self.max_connection_duration <= 0
        ):
            raise ValueError(
                f"max_connection_duration must be positive, got {self.max_connection_duration}"
            )

        if self.idle_timeout is not None and self.idle_timeout <= 0:
            raise ValueError(f"idle_timeout must be positive, got {self.idle_timeout}")

        # Ensure response timeout doesn't exceed connection duration
        if (
            self.default_response_timeout is not None
            and self.max_connection_duration is not None
            and self.default_response_timeout >= self.max_connection_duration
        ):
            raise ValueError(
                f"default_response_timeout ({self.default_response_timeout}s) must be "
                f"less than max_connection_duration ({self.max_connection_duration}s)"
            )

        # Validate reconnection parameters
        if self.reconnect_delay < 0:
            raise ValueError(
                f"reconnect_delay must be non-negative, got {self.reconnect_delay}"
            )

        if self.max_reconnect_attempts < 1:
            raise ValueError(
                f"max_reconnect_attempts must be at least 1, got {self.max_reconnect_attempts}"
            )

    def validate(self) -> None:
        """Explicitly validate the configuration.

        This method exists for consistency with other config classes and to
        allow external validation. Validation is automatically performed in
        __post_init__, so this is typically not needed.
        """
        # Validation is already done in __post_init__
        self.debug.validate()
        self.websocket.validate()


@dataclass(frozen=True)
class RpcBackpressureConfig:
    """Configuration for backpressure management and message limits.

    Controls how many pending requests can queue up, limits on outgoing messages,
    and the maximum size of individual messages to prevent resource exhaustion.

    Parameters
    ----------
    max_pending_requests : int, default 1000
        Maximum number of pending incoming RPC requests allowed before backpressure
        is applied. New requests will block or fail when this limit is reached.
        Prevents receive-side overwhelm.
    max_message_size : int, default 10485760
        Maximum size in bytes for a single message (default 10MB). Messages
        exceeding this size should be rejected or chunked.
    max_send_queue_size : int, default 1000
        Maximum number of pending outgoing messages before backpressure is applied.
        Prevents send-side overwhelm and memory exhaustion from queued messages.
        Set to 0 to disable send backpressure. Default: 1000.

    Examples
    --------
    >>> # Conservative limits for high-traffic production
    >>> config = RpcBackpressureConfig(
    ...     max_pending_requests=500,
    ...     max_message_size=5 * 1024 * 1024,  # 5MB
    ...     max_send_queue_size=500
    ... )
    >>> config.validate()

    >>> # Higher limits for development
    >>> config = RpcBackpressureConfig(
    ...     max_pending_requests=2000,
    ...     max_message_size=20 * 1024 * 1024,  # 20MB
    ...     max_send_queue_size=2000
    ... )
    >>> config.validate()

    >>> # Disable send backpressure
    >>> config = RpcBackpressureConfig(
    ...     max_pending_requests=1000,
    ...     max_send_queue_size=0  # Disabled
    ... )
    >>> config.validate()
    """

    max_pending_requests: int = 1000
    max_message_size: int = 10 * 1024 * 1024  # 10MB
    max_send_queue_size: int = 1000

    def __post_init__(self) -> None:
        """Validate configuration after initialization.

        Raises
        ------
        ValueError
            If max_pending_requests or max_message_size are not positive,
            or if max_send_queue_size is negative.
        """
        if self.max_pending_requests <= 0:
            raise ValueError(
                f"max_pending_requests must be positive, got {self.max_pending_requests}"
            )

        if self.max_message_size <= 0:
            raise ValueError(
                f"max_message_size must be positive, got {self.max_message_size}"
            )

        if self.max_send_queue_size < 0:
            raise ValueError(
                f"max_send_queue_size must be non-negative (0 to disable), got {self.max_send_queue_size}"
            )

    def validate(self) -> None:
        """Explicitly validate the configuration.

        This method exists for consistency with other config classes and to
        allow external validation. Validation is automatically performed in
        __post_init__, so this is typically not needed.
        """
        # Validation is already done in __post_init__


@dataclass(frozen=True)
class RpcKeepaliveConfig:
    """Configuration for connection keepalive behavior.

    Controls how frequently keepalive checks are performed and what type of
    ping mechanism to use (WebSocket protocol ping vs RPC-level ping).

    Parameters
    ----------
    interval : float, default 0
        Time in seconds between keepalive checks. Set to 0 to disable keepalive.
    max_consecutive_failures : int, default 3
        Maximum number of consecutive keepalive failures before considering
        the connection dead and closing it.
    use_protocol_ping : bool, default True
        If True, use WebSocket protocol-level ping/pong frames for keepalive.
        If False, use RPC-level ping messages. Protocol pings are more efficient
        but RPC pings test the full message processing pipeline.

    Examples
    --------
    >>> # Enable keepalive with 30-second interval
    >>> config = RpcKeepaliveConfig(interval=30.0)
    >>> assert config.enabled
    >>> config.validate()

    >>> # Disabled keepalive (default)
    >>> config = RpcKeepaliveConfig()
    >>> assert not config.enabled

    >>> # Use RPC-level pings instead of protocol pings
    >>> config = RpcKeepaliveConfig(
    ...     interval=30.0,
    ...     use_protocol_ping=False
    ... )
    >>> config.validate()
    """

    interval: float = 0
    max_consecutive_failures: int = 3
    use_protocol_ping: bool = True

    @property
    def enabled(self) -> bool:
        """Check if keepalive is enabled.

        Returns
        -------
        bool
            True if keepalive interval is greater than 0, False otherwise.
        """
        return self.interval > 0

    def __post_init__(self) -> None:
        """Validate configuration after initialization.

        Raises
        ------
        ValueError
            If interval is negative, or if keepalive is enabled but
            max_consecutive_failures is less than 1.
        """
        if self.interval < 0:
            raise ValueError(f"interval must be non-negative, got {self.interval}")

        if self.enabled and self.max_consecutive_failures < 1:
            raise ValueError(
                f"max_consecutive_failures must be at least 1 when keepalive is enabled, "
                f"got {self.max_consecutive_failures}"
            )

    def validate(self) -> None:
        """Explicitly validate the configuration.

        This method exists for consistency with other config classes and to
        allow external validation. Validation is automatically performed in
        __post_init__, so this is typically not needed.
        """
        # Validation is already done in __post_init__


@dataclass(frozen=True)
class RpcRetryConfig:
    """Configuration for automatic retry behavior with exponential backoff.

    Controls how failed RPC requests are retried, including retry limits,
    backoff timing, and jitter to prevent thundering herd problems.

    Parameters
    ----------
    enabled : bool, default True
        Whether automatic retries are enabled. When False, failed requests
        raise immediately without retry attempts.
    max_attempts : int, default 5
        Maximum number of retry attempts (including the initial attempt).
        Must be positive when retries are enabled.
    min_wait : float, default 0.1
        Minimum wait time in seconds before the first retry. Subsequent
        retries use exponential backoff from this base.
    max_wait : float, default 30.0
        Maximum wait time in seconds between retry attempts. Caps the
        exponential backoff to prevent excessively long waits.
    jitter : bool, default True
        If True, add random jitter to retry delays to prevent thundering herd
        problems when many clients retry simultaneously. Recommended for
        production use.

    Examples
    --------
    >>> # Standard retry configuration with jitter
    >>> config = RpcRetryConfig(
    ...     enabled=True,
    ...     max_attempts=5,
    ...     min_wait=0.1,
    ...     max_wait=30.0,
    ...     jitter=True
    ... )
    >>> config.validate()

    >>> # Aggressive retry for high-reliability scenarios
    >>> config = RpcRetryConfig(
    ...     max_attempts=10,
    ...     min_wait=0.05,
    ...     max_wait=60.0
    ... )
    >>> config.validate()

    >>> # Disable retries for testing
    >>> config = RpcRetryConfig(enabled=False)
    >>> config.validate()
    """

    enabled: bool = True
    max_attempts: int = 5
    min_wait: float = 0.1
    max_wait: float = 30.0
    jitter: bool = True

    def __post_init__(self) -> None:
        """Validate configuration after initialization.

        Raises
        ------
        ValueError
            If max_attempts is not positive, if min_wait or max_wait are negative,
            or if max_wait is less than min_wait.
        """
        if self.max_attempts <= 0:
            raise ValueError(f"max_attempts must be positive, got {self.max_attempts}")

        if self.min_wait < 0:
            raise ValueError(f"min_wait must be non-negative, got {self.min_wait}")

        if self.max_wait < 0:
            raise ValueError(f"max_wait must be non-negative, got {self.max_wait}")

        if self.max_wait < self.min_wait:
            raise ValueError(
                f"max_wait ({self.max_wait}s) must be >= min_wait ({self.min_wait}s)"
            )

    def validate(self) -> None:
        """Explicitly validate the configuration.

        This method exists for consistency with other config classes and to
        allow external validation. Validation is automatically performed in
        __post_init__, so this is typically not needed.
        """
        # Validation is already done in __post_init__


@dataclass(frozen=True)
class WebSocketRpcClientConfig:
    """Complete configuration for WebSocket RPC client behavior.

    Combines all configuration aspects (connection, backpressure, keepalive,
    retry) into a single immutable configuration object. Provides factory
    methods for common preset configurations.

    Parameters
    ----------
    connection : RpcConnectionConfig, default RpcConnectionConfig()
        Connection lifecycle and timeout configuration.
    backpressure : RpcBackpressureConfig, default RpcBackpressureConfig()
        Backpressure management and message size limits.
    keepalive : RpcKeepaliveConfig, default RpcKeepaliveConfig()
        Keepalive behavior and failure handling.
    retry : RpcRetryConfig, default RpcRetryConfig()
        Retry behavior with exponential backoff and jitter.
    websocket : WebSocketConnectionConfig, default WebSocketConnectionConfig()
        WebSocket-specific connection configuration including subprotocol
        negotiation. Defaults to JSON-RPC 2.0 subprotocol.
    websocket_kwargs : dict[str, Any], default {}
        Additional keyword arguments to pass to the underlying WebSocket
        connection. Can include proxy settings, headers, etc.

    Examples
    --------
    >>> # Create with default settings
    >>> config = WebSocketRpcClientConfig()
    >>> config.validate()

    >>> # Create with custom sub-configurations
    >>> config = WebSocketRpcClientConfig(
    ...     connection=RpcConnectionConfig(default_response_timeout=30.0),
    ...     keepalive=RpcKeepaliveConfig(interval=30.0),
    ...     websocket_kwargs={"ping_interval": None}
    ... )
    >>> config.validate()

    >>> # Use production presets
    >>> config = WebSocketRpcClientConfig.production_defaults()
    >>> assert config.connection.default_response_timeout == 30.0
    >>> assert config.keepalive.enabled

    >>> # Use development presets
    >>> config = WebSocketRpcClientConfig.development_defaults()
    >>> assert config.connection.default_response_timeout is None
    >>> assert not config.keepalive.enabled

    >>> # Custom WebSocket subprotocol
    >>> config = WebSocketRpcClientConfig(
    ...     websocket=WebSocketConnectionConfig(subprotocols=["custom.v1"])
    ... )
    >>> assert config.websocket.subprotocols == ["custom.v1"]
    """

    connection: RpcConnectionConfig = field(default_factory=RpcConnectionConfig)
    backpressure: RpcBackpressureConfig = field(default_factory=RpcBackpressureConfig)
    keepalive: RpcKeepaliveConfig = field(default_factory=RpcKeepaliveConfig)
    retry: RpcRetryConfig = field(default_factory=RpcRetryConfig)
    websocket: WebSocketConnectionConfig = field(
        default_factory=WebSocketConnectionConfig
    )
    websocket_kwargs: dict[str, Any] = field(default_factory=dict)

    def validate(self) -> None:
        """Validate all sub-configurations.

        Calls the validate() method on each sub-configuration to ensure
        all settings are valid and consistent.

        Raises
        ------
        ValueError
            If any sub-configuration validation fails.
        """
        self.connection.validate()
        self.backpressure.validate()
        self.keepalive.validate()
        self.retry.validate()
        self.websocket.validate()

    @classmethod
    def production_defaults(cls) -> WebSocketRpcClientConfig:
        """Create configuration with production-ready defaults.

        Production defaults prioritize reliability and resource management:
        - 30-second response timeout to fail fast
        - 1-hour maximum connection duration for graceful rotation
        - 5-minute idle timeout to clean up inactive connections
        - 30-second keepalive with protocol pings
        - 500 max pending requests for backpressure
        - Retries enabled with jitter

        Returns
        -------
        WebSocketRpcClientConfig
            Configuration object with production defaults.

        Examples
        --------
        >>> config = WebSocketRpcClientConfig.production_defaults()
        >>> assert config.connection.default_response_timeout == 30.0
        >>> assert config.connection.max_connection_duration == 3600.0
        >>> assert config.keepalive.interval == 30.0
        >>> assert config.backpressure.max_pending_requests == 500
        """
        return cls(
            connection=RpcConnectionConfig(
                default_response_timeout=30.0,
                max_connection_duration=3600.0,  # 1 hour
                idle_timeout=300.0,  # 5 minutes
                websocket=WebSocketConnectionConfig(
                    ping_timeout=60.0,  # 2x the keepalive interval to prevent false timeouts
                ),
            ),
            backpressure=RpcBackpressureConfig(
                max_pending_requests=500,
                max_message_size=10 * 1024 * 1024,  # 10MB
            ),
            keepalive=RpcKeepaliveConfig(
                interval=30.0,
                max_consecutive_failures=3,
                use_protocol_ping=True,
            ),
            retry=RpcRetryConfig(
                enabled=True,
                max_attempts=5,
                min_wait=0.1,
                max_wait=30.0,
                jitter=True,
            ),
            websocket_kwargs={},
        )

    @classmethod
    def development_defaults(cls) -> WebSocketRpcClientConfig:
        """Create configuration with development-friendly defaults.

        Development defaults prioritize debuggability and flexibility:
        - No timeouts to allow debugging
        - No keepalive to reduce noise in logs
        - Retries enabled but with defaults
        - Higher backpressure limits

        Returns
        -------
        WebSocketRpcClientConfig
            Configuration object with development defaults.

        Examples
        --------
        >>> config = WebSocketRpcClientConfig.development_defaults()
        >>> assert config.connection.default_response_timeout is None
        >>> assert not config.keepalive.enabled
        >>> assert config.retry.enabled
        >>> assert config.backpressure.max_pending_requests == 1000
        """
        return cls(
            connection=RpcConnectionConfig(
                default_response_timeout=None,
                max_connection_duration=None,
                idle_timeout=None,
            ),
            backpressure=RpcBackpressureConfig(
                max_pending_requests=1000,
                max_message_size=10 * 1024 * 1024,  # 10MB
            ),
            keepalive=RpcKeepaliveConfig(
                interval=0,  # Disabled
                max_consecutive_failures=3,
                use_protocol_ping=True,
            ),
            retry=RpcRetryConfig(
                enabled=True,
                max_attempts=5,
                min_wait=0.1,
                max_wait=30.0,
                jitter=True,
            ),
            websocket_kwargs={},
        )
