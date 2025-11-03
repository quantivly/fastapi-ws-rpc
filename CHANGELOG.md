# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Breaking Changes
- **Renamed class** - `WebsocketRPCEndpoint` → `WebSocketRpcEndpoint` for naming consistency with `WebSocketRpcClient`
- **Exception hierarchy** - `RpcChannelClosedError` and `RemoteValueError` now inherit from `RpcError` base class instead of `Exception` and `ValueError` respectively
- **Removed dead code** - Removed unused `messages` dict and `receive_text()` method from `JsonSerializingWebSocket`
- **Removed legacy file** - Deleted `tests/requirements.txt` (dependencies managed in `pyproject.toml`)
- **Dropped Python 3.7 and 3.8 support** - Minimum required version is now Python 3.9

### Added
- **JSON-RPC 2.0 compliance**:
  - `JsonRpcErrorCode` enum with all standard error codes (PARSE_ERROR, INVALID_REQUEST, METHOD_NOT_FOUND, INVALID_PARAMS, INTERNAL_ERROR)
  - Method validation with proper error code returns for protected methods, missing methods, and invalid parameters
  - `notify()` method for fire-and-forget notifications (requests without id)
  - Request ID collision detection in `async_call()` to prevent undefined behavior
- **Production hardening features**:
  - Message size limits (default 10MB) to prevent DoS attacks via large JSON payloads
  - Rate limiting with max pending requests (default 1000) to prevent resource exhaustion
  - Connection duration limits with graceful closure after max age
  - Enhanced keepalive with consecutive failure detection (3 failures → auto-close)
  - Connection state validation replacing assertions with `RpcInvalidStateError`
- **New exceptions**:
  - `RpcInvalidStateError` - For connection state validation
  - `RpcMessageTooLargeError` - For message size limit violations
  - `RpcBackpressureError` - For rate limit violations
- **Internal architecture**:
  - `_internal/` package with focused components (`RpcPromiseManager`, `RpcMethodInvoker`, `RpcProtocolHandler`, `RpcCaller`)
  - Protocol-based architecture (`RpcCallable`, `MethodInvokable`) to break circular dependencies
  - Dedicated `exceptions.py` module for all exception classes
- **ConnectionManager enhancements**:
  - `get_connection_count()` helper method for observability
  - Comprehensive NumPy-style docstrings with usage examples
- **Documentation**:
  - NumPy-style docstrings for all public APIs with Parameters, Returns, Raises, Notes, Examples, and See Also sections
  - ~590 lines of professional-grade documentation added to core classes
  - Complete API reference documentation for all exported classes and methods
- **Comprehensive type checking** with mypy in strict mode
- pytest-cov for code coverage reporting with HTML and XML output
- Ruff configuration with modern Python linting rules
- Coverage configuration targeting fastapi_ws_rpc module
- GitHub Actions now use Poetry for dependency management

### Changed
- **Architecture refactoring**:
  - Decomposed `RpcChannel` from 717 → 430 lines (40% reduction)
  - Extracted 4 specialized components for better separation of concerns
  - Improved testability and maintainability with focused modules
- **Exception handling**:
  - Replaced 8 bare `except Exception` handlers with specific exception types throughout codebase
  - Added comprehensive docstrings to all custom exceptions
  - Improved error messages and logging context
- **Async cleanup**:
  - Made `cancel_tasks()`, `cancel_reader_task()`, and `_cancel_keep_alive_task()` properly async
  - Tasks now await cancellation completion to prevent warnings
- **Public API expansion**:
  - Defined comprehensive public API in `__init__.py` with 18+ exports
  - Added exports for previously internal but useful classes
  - All imports backwards compatible (additions only, no removals)
- **Dependencies updated**:
  - FastAPI: 0.115.14 → 0.120.1
  - uvicorn: ^0.34.1 → ^0.38.0
  - packaging: ^24.2 → ^25.0
  - Pydantic: 2.11.7 → 2.12.3
  - Starlette: 0.46.2 → 0.49.1
  - anyio: 4.9.0 → 4.11.0
  - pytest: 8.4.1 → 8.4.2
- **Pre-commit hooks updated**:
  - pre-commit-hooks: v4.4.0 → v5.0.0
  - ruff: v0.0.264 → v0.8.5
  - black: 23.3.0 → 24.10.0
  - isort: 5.12.0 → 5.13.2
- **Modernized type hints**: Replaced deprecated typing constructs (`List` → `list`, `Dict` → `dict`, `Optional[X]` → `X | None`)
- **Removed Python 2 legacy code**: Modernized class declarations (removed `object` inheritance)
- GitHub Actions now tests Python 3.9, 3.10, 3.11, and 3.12 (previously tested 3.7-3.11)
- README updated to reflect Python 3.9+ requirement

### Fixed
- **ConnectionManager race condition** - Added proper error handling for `ValueError` in `disconnect()` method
- **Memory leaks** - Added cleanup for pending requests when channel closes
- **Async task cancellation** - Properly await task cancellation to prevent un-awaited coroutine warnings
- **JSON-RPC 2.0 compliance** - Added proper method validation and error codes
- **Critical bug fix**: Replaced Python 2 `.encode("hex")` with Python 3 `.hex()` method in utils.py
- Fixed integer division in `gen_token` to use `//` instead of `/` for Python 3 compatibility

### Infrastructure
- Added mypy type checking to development workflow
- Configured ruff with comprehensive rule sets (pyupgrade, bugbear, comprehensions, etc.)
- Set up coverage reporting with term-missing, HTML, and XML outputs
- Updated GitHub Actions to use latest action versions (checkout@v4, setup-python@v5)

## [0.2.0] - 2024-10-30

### Changed
- Added connection closed event for immediate reconnection detection
- Updated FastAPI to ^0.120.0 for Starlette CVE-2025-62727 security fix

### Fixed
- Handle dictionaries in _serialize method for send_raw compatibility
- Renamed ping method

### Removed
- Legacy RPC format removed, now uses JSON-RPC 2.0 only

---

[Unreleased]: https://github.com/permitio/fastapi_websocket_rpc/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/permitio/fastapi_websocket_rpc/releases/tag/v0.2.0
