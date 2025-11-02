# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Breaking Changes
- **Dropped Python 3.7 and 3.8 support** - Minimum required version is now Python 3.9
- **Removed requirements.txt** - Project now uses `pyproject.toml` exclusively for dependency management

### Added
- Comprehensive type checking with mypy in strict mode
- pytest-cov for code coverage reporting with HTML and XML output
- Ruff configuration with modern Python linting rules
- Coverage configuration targeting fastapi_ws_rpc module
- GitHub Actions now use Poetry for dependency management

### Changed
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
