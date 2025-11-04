"""
Pytest configuration and shared fixtures for fastapi_websocket_rpc tests.

This module provides:
- Custom pytest markers for test categorization
- Shared fixtures that can be reused across test modules
- Test configuration and setup
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import pytest


def pytest_configure(config: pytest.Config) -> None:
    """
    Register custom pytest markers.

    This function is called during pytest initialization to register
    custom markers that can be used to categorize and filter tests.
    """
    config.addinivalue_line(
        "markers",
        "slow: mark test as slow (stress tests, integration tests with delays)",
    )
    config.addinivalue_line(
        "markers",
        "integration: mark test as integration test (requires external resources)",
    )
    config.addinivalue_line(
        "markers",
        "network: mark test as network test (simulates network failures)",
    )


# ============================================================================
# Shared Fixtures
# ============================================================================
# Add shared fixtures here that are used across multiple test modules
# Currently, each test module defines its own fixtures for clarity
# This section can be expanded as common patterns emerge
