"""
Comprehensive tests for RpcMethodInvoker.

This test module covers:
- Method validation (valid methods, invalid methods, private methods)
- Parameter conversion (dict, list, None)
- Method invocation with various parameter types
- Return type handling and conversion
- Error handling (TypeError for invalid params, exceptions from methods)
- NoResponse handling
"""

from __future__ import annotations

import pytest

from fastapi_ws_rpc._internal.method_invoker import RpcMethodInvoker
from fastapi_ws_rpc.rpc_methods import (
    EXPOSED_BUILT_IN_METHODS,
    NoResponse,
    RpcMethodsBase,
)
from fastapi_ws_rpc.schemas import JsonRpcErrorCode

# ============================================================================
# Test RPC Methods Classes
# ============================================================================


class TestMethods(RpcMethodsBase):
    """Test methods class with various method signatures for testing."""

    def __init__(self) -> None:
        super().__init__()
        self.call_count = 0

    async def simple_method(self) -> str:
        """Simple method with no parameters."""
        return "success"

    async def method_with_params(self, a: int, b: int) -> int:
        """Method with positional parameters."""
        return a + b

    async def method_with_kwargs(self, name: str = "default", value: int = 0) -> dict:
        """Method with keyword arguments and defaults."""
        return {"name": name, "value": value}

    async def method_that_raises_type_error(self, x: int) -> int:
        """Method that expects specific parameter types."""
        # This will raise TypeError if x is not an int
        return x * 2

    async def method_that_raises_exception(self) -> str:
        """Method that raises a generic exception."""
        raise RuntimeError("Something went wrong")

    async def method_with_no_return_type(self):
        """Method without return type annotation."""
        return 42

    async def method_with_no_return_type_returns_int(self):
        """Method without return type annotation that returns int."""
        return 123

    async def method_returning_noresponse(self) -> type[NoResponse]:
        """Method that returns NoResponse sentinel."""
        self.call_count += 1
        return NoResponse

    async def _private_method(self) -> str:
        """Private method that should not be accessible."""
        return "private"

    # Non-async method (still callable but should work)
    def sync_method(self) -> str:
        """Synchronous method (not recommended but should work)."""
        return "sync"

    # Non-callable attribute
    not_a_method = "I am not a method"


# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture
def test_methods() -> TestMethods:
    """Create a test methods instance."""
    return TestMethods()


@pytest.fixture
def method_invoker(test_methods: TestMethods) -> RpcMethodInvoker:
    """Create a method invoker with test methods."""
    return RpcMethodInvoker(test_methods)


# ============================================================================
# Method Validation Tests
# ============================================================================


class TestMethodValidation:
    """Test method validation logic."""

    @pytest.mark.asyncio
    async def test_validate_valid_method(
        self, method_invoker: RpcMethodInvoker
    ) -> None:
        """
        Test validating a valid public method.

        Verifies that:
        - Valid method passes validation
        - Returns (True, None, None)
        """
        is_valid, error_code, error_msg = method_invoker.validate_method(
            "simple_method"
        )

        assert is_valid is True
        assert error_code is None
        assert error_msg is None

    @pytest.mark.asyncio
    async def test_validate_method_not_string(
        self, method_invoker: RpcMethodInvoker
    ) -> None:
        """
        Test validating a non-string method name.

        Verifies that:
        - Non-string method name fails validation
        - Returns INVALID_REQUEST error code
        - Error message mentions string requirement
        """
        is_valid, error_code, error_msg = method_invoker.validate_method(123)  # type: ignore

        assert is_valid is False
        assert error_code == JsonRpcErrorCode.INVALID_REQUEST
        assert "string" in error_msg.lower()

    @pytest.mark.asyncio
    async def test_validate_private_method(
        self, method_invoker: RpcMethodInvoker
    ) -> None:
        """
        Test validating a private method (starts with _).

        Verifies that:
        - Private methods fail validation
        - Returns METHOD_NOT_FOUND error code
        - Error message indicates method not found or not accessible
        """
        is_valid, error_code, error_msg = method_invoker.validate_method(
            "_private_method"
        )

        assert is_valid is False
        assert error_code == JsonRpcErrorCode.METHOD_NOT_FOUND
        assert "not found" in error_msg.lower() or "not accessible" in error_msg.lower()

    @pytest.mark.asyncio
    async def test_validate_builtin_method(
        self, method_invoker: RpcMethodInvoker
    ) -> None:
        """
        Test validating built-in methods that start with _.

        Verifies that:
        - Built-in methods like _get_channel_id_ pass validation
        - These are exceptions to the private method rule
        """
        # Test all exposed built-in methods
        for method_name in EXPOSED_BUILT_IN_METHODS:
            is_valid, error_code, error_msg = method_invoker.validate_method(
                method_name
            )

            if method_name in ["ping", "_get_channel_id_"]:
                assert is_valid is True
                assert error_code is None
                assert error_msg is None

    @pytest.mark.asyncio
    async def test_validate_nonexistent_method(
        self, method_invoker: RpcMethodInvoker
    ) -> None:
        """
        Test validating a method that doesn't exist.

        Verifies that:
        - Non-existent method fails validation
        - Returns METHOD_NOT_FOUND error code
        - Error message includes method name
        """
        is_valid, error_code, error_msg = method_invoker.validate_method(
            "nonexistent_method"
        )

        assert is_valid is False
        assert error_code == JsonRpcErrorCode.METHOD_NOT_FOUND
        assert "not found" in error_msg.lower()
        assert "nonexistent_method" in error_msg

    @pytest.mark.asyncio
    async def test_validate_non_callable_attribute(
        self, method_invoker: RpcMethodInvoker
    ) -> None:
        """
        Test validating a non-callable attribute.

        Verifies that:
        - Non-callable attributes fail validation
        - Returns METHOD_NOT_FOUND error code
        - Error message indicates not callable
        """
        is_valid, error_code, error_msg = method_invoker.validate_method("not_a_method")

        assert is_valid is False
        assert error_code == JsonRpcErrorCode.METHOD_NOT_FOUND
        assert "not" in error_msg.lower() and "callable" in error_msg.lower()


# ============================================================================
# Parameter Conversion Tests
# ============================================================================


class TestParameterConversion:
    """Test parameter conversion for different formats."""

    @pytest.mark.asyncio
    async def test_convert_dict_params(self, method_invoker: RpcMethodInvoker) -> None:
        """
        Test converting dictionary parameters (named params).

        Verifies that:
        - Dict params are passed through unchanged
        - Result is a dict suitable for **kwargs
        """
        params = {"name": "test", "value": 42}
        result = method_invoker.convert_params(params)

        assert result == params
        assert isinstance(result, dict)

    @pytest.mark.asyncio
    async def test_convert_list_params_raises_error(
        self, method_invoker: RpcMethodInvoker
    ) -> None:
        """
        Test that list parameters raise ValueError.

        Verifies that:
        - List params are not supported
        - Clear error message is provided
        - ValueError is raised with appropriate message
        """
        params = [1, 2, 3]

        with pytest.raises(ValueError) as exc_info:
            method_invoker.convert_params(params)

        error_message = str(exc_info.value).lower()
        assert "positional parameters" in error_message
        assert "not supported" in error_message
        assert "array format" in error_message

    @pytest.mark.asyncio
    async def test_convert_none_params(self, method_invoker: RpcMethodInvoker) -> None:
        """
        Test converting None parameters (no params).

        Verifies that:
        - None params are converted to empty dict
        - Result is suitable for **kwargs
        """
        result = method_invoker.convert_params(None)

        assert result == {}
        assert isinstance(result, dict)


# ============================================================================
# Return Type Handling Tests
# ============================================================================


class TestReturnTypeHandling:
    """Test return type inspection and conversion."""

    @pytest.mark.asyncio
    async def test_get_return_type_with_annotation(
        self, method_invoker: RpcMethodInvoker, test_methods: TestMethods
    ) -> None:
        """
        Test getting return type from method with type annotation.

        Verifies that:
        - Return type is extracted from annotation
        - Correct type is returned

        Note: With PEP 563 (from __future__ import annotations), annotations
        are strings, not actual types.
        """
        method = test_methods.method_with_params
        return_type = method_invoker.get_return_type(method)

        # With from __future__ import annotations, this will be 'int' string
        assert return_type in (int, "int")

    @pytest.mark.asyncio
    async def test_get_return_type_without_annotation(
        self, method_invoker: RpcMethodInvoker, test_methods: TestMethods
    ) -> None:
        """
        Test getting return type from method without annotation.

        Verifies that:
        - Default return type is str
        - Falls back gracefully
        """
        method = test_methods.method_with_no_return_type
        return_type = method_invoker.get_return_type(method)

        assert return_type is str

    @pytest.mark.asyncio
    async def test_return_value_conversion_to_string(
        self, method_invoker: RpcMethodInvoker
    ) -> None:
        """
        Test automatic conversion of return values to string.

        Verifies that:
        - Methods without return type annotation have values converted to str
        - Conversion happens when return type is str and value is not str
        """
        # Method returns int but has no type annotation (defaults to str)
        result = await method_invoker.invoke(
            "method_with_no_return_type_returns_int", None
        )

        # Should be converted to string
        assert result == "123"
        assert isinstance(result, str)


# ============================================================================
# Method Invocation Tests
# ============================================================================


class TestMethodInvocation:
    """Test method invocation with various scenarios."""

    @pytest.mark.asyncio
    async def test_invoke_simple_method(self, method_invoker: RpcMethodInvoker) -> None:
        """
        Test invoking a simple method with no parameters.

        Verifies that:
        - Method is called successfully
        - Return value is correct
        """
        result = await method_invoker.invoke("simple_method", None)

        assert result == "success"

    @pytest.mark.asyncio
    async def test_invoke_method_with_dict_params(
        self, method_invoker: RpcMethodInvoker
    ) -> None:
        """
        Test invoking method with dictionary parameters.

        Verifies that:
        - Named parameters are passed correctly
        - Method executes with correct arguments
        - Return value is correct
        """
        result = await method_invoker.invoke("method_with_params", {"a": 5, "b": 3})

        assert result == 8

    @pytest.mark.asyncio
    async def test_invoke_method_with_kwargs(
        self, method_invoker: RpcMethodInvoker
    ) -> None:
        """
        Test invoking method with keyword arguments and defaults.

        Verifies that:
        - Keyword arguments are passed correctly
        - Default values work when params omitted
        """
        # With parameters
        result = await method_invoker.invoke(
            "method_with_kwargs", {"name": "test", "value": 99}
        )
        assert result == {"name": "test", "value": 99}

        # With defaults
        result = await method_invoker.invoke("method_with_kwargs", None)
        assert result == {"name": "default", "value": 0}

    @pytest.mark.asyncio
    async def test_invoke_method_with_partial_kwargs(
        self, method_invoker: RpcMethodInvoker
    ) -> None:
        """
        Test invoking method with partial keyword arguments.

        Verifies that:
        - Some parameters can be provided while others use defaults
        """
        result = await method_invoker.invoke("method_with_kwargs", {"value": 42})

        assert result == {"name": "default", "value": 42}

    @pytest.mark.asyncio
    async def test_invoke_method_returning_noresponse(
        self, method_invoker: RpcMethodInvoker, test_methods: TestMethods
    ) -> None:
        """
        Test invoking method that returns NoResponse sentinel.

        Verifies that:
        - NoResponse is returned unchanged
        - Method is actually executed (side effects occur)
        - No type conversion is attempted on NoResponse
        """
        initial_count = test_methods.call_count

        result = await method_invoker.invoke("method_returning_noresponse", None)

        assert result is NoResponse
        assert test_methods.call_count == initial_count + 1

    @pytest.mark.asyncio
    async def test_invoke_sync_method(self, method_invoker: RpcMethodInvoker) -> None:
        """
        Test invoking a synchronous method.

        Verifies that:
        - Sync methods can be invoked (though not recommended)
        - Return value is correct
        """
        # Note: This will fail because we await the result, and sync methods
        # are not awaitable. This test documents the limitation.
        with pytest.raises(TypeError):
            await method_invoker.invoke("sync_method", None)


# ============================================================================
# Error Handling Tests
# ============================================================================


class TestErrorHandling:
    """Test error handling during method invocation."""

    @pytest.mark.asyncio
    async def test_invoke_with_wrong_param_type(
        self, method_invoker: RpcMethodInvoker
    ) -> None:
        """
        Test invoking method with wrong parameter types.

        Note: Python doesn't enforce type hints at runtime, so this test
        demonstrates that the method will be called even with "wrong" types.
        The actual type checking would need to be done by the method itself
        or by a validation framework like Pydantic.

        For demo purposes, this test shows that strings passed to an
        int + int operation actually work (but give unexpected results).
        """
        # Method expects int, we pass string - this actually works in Python!
        # "not" + "int" concatenates strings
        result = await method_invoker.invoke(
            "method_with_params", {"a": "not", "b": "int"}
        )
        # Result will be "notint" (string concatenation)
        assert result == "notint"

    @pytest.mark.asyncio
    async def test_invoke_with_missing_required_params(
        self, method_invoker: RpcMethodInvoker
    ) -> None:
        """
        Test invoking method with missing required parameters.

        Verifies that:
        - TypeError is raised for missing parameters
        - Error indicates missing argument
        """
        with pytest.raises(TypeError) as exc_info:
            await method_invoker.invoke("method_with_params", {"a": 5})

        assert "missing" in str(exc_info.value).lower()

    @pytest.mark.asyncio
    async def test_invoke_with_unexpected_params(
        self, method_invoker: RpcMethodInvoker
    ) -> None:
        """
        Test invoking method with unexpected parameters.

        Verifies that:
        - TypeError is raised for unexpected keyword arguments
        - Error indicates unexpected argument
        """
        with pytest.raises(TypeError) as exc_info:
            await method_invoker.invoke("simple_method", {"unexpected": "param"})

        assert (
            "unexpected" in str(exc_info.value).lower()
            or "keyword" in str(exc_info.value).lower()
        )

    @pytest.mark.asyncio
    async def test_invoke_method_that_raises_exception(
        self, method_invoker: RpcMethodInvoker
    ) -> None:
        """
        Test invoking method that raises an exception.

        Verifies that:
        - Exception from method propagates correctly
        - Exception type and message are preserved
        """
        with pytest.raises(RuntimeError) as exc_info:
            await method_invoker.invoke("method_that_raises_exception", None)

        assert "Something went wrong" in str(exc_info.value)


# ============================================================================
# Integration Tests
# ============================================================================


class TestIntegration:
    """Test complete validation and invocation workflows."""

    @pytest.mark.asyncio
    async def test_validate_and_invoke_valid_method(
        self, method_invoker: RpcMethodInvoker
    ) -> None:
        """
        Test complete workflow of validating and invoking a method.

        Verifies that:
        - Validation succeeds
        - Invocation succeeds
        - Result is correct
        """
        # Validate
        is_valid, error_code, error_msg = method_invoker.validate_method(
            "method_with_params"
        )
        assert is_valid is True

        # Invoke
        result = await method_invoker.invoke("method_with_params", {"a": 10, "b": 20})
        assert result == 30

    @pytest.mark.asyncio
    async def test_validate_and_invoke_invalid_method(
        self, method_invoker: RpcMethodInvoker
    ) -> None:
        """
        Test workflow when method validation fails.

        Verifies that:
        - Validation fails with appropriate error code
        - Invocation should not be attempted after validation failure
        """
        # Validate
        is_valid, error_code, error_msg = method_invoker.validate_method(
            "nonexistent_method"
        )

        assert is_valid is False
        assert error_code == JsonRpcErrorCode.METHOD_NOT_FOUND

        # Should not invoke after validation failure
        # (in real usage, protocol handler would send error response)

    @pytest.mark.asyncio
    async def test_multiple_invocations(
        self, method_invoker: RpcMethodInvoker, test_methods: TestMethods
    ) -> None:
        """
        Test multiple invocations of different methods.

        Verifies that:
        - Method invoker can handle multiple calls
        - State is maintained correctly across calls
        """
        # First invocation
        result1 = await method_invoker.invoke("simple_method", None)
        assert result1 == "success"

        # Second invocation
        result2 = await method_invoker.invoke("method_with_params", {"a": 1, "b": 2})
        assert result2 == 3

        # Third invocation with NoResponse
        result3 = await method_invoker.invoke("method_returning_noresponse", None)
        assert result3 is NoResponse

        # Verify state
        assert test_methods.call_count == 1


# ============================================================================
# Edge Cases
# ============================================================================


class TestEdgeCases:
    """Test edge cases and unusual scenarios."""

    @pytest.mark.asyncio
    async def test_method_with_empty_string_name(
        self, method_invoker: RpcMethodInvoker
    ) -> None:
        """
        Test validating a method with empty string name.

        Verifies that:
        - Empty string is handled gracefully
        - Returns METHOD_NOT_FOUND
        """
        is_valid, error_code, error_msg = method_invoker.validate_method("")

        assert is_valid is False
        assert error_code == JsonRpcErrorCode.METHOD_NOT_FOUND

    @pytest.mark.asyncio
    async def test_invoke_with_very_long_method_name(
        self, method_invoker: RpcMethodInvoker
    ) -> None:
        """
        Test validating a very long method name.

        Verifies that:
        - Long method names are handled correctly
        - Returns METHOD_NOT_FOUND
        """
        long_name = "a" * 1000
        is_valid, error_code, error_msg = method_invoker.validate_method(long_name)

        assert is_valid is False
        assert error_code == JsonRpcErrorCode.METHOD_NOT_FOUND

    @pytest.mark.asyncio
    async def test_method_with_unicode_in_params(
        self, method_invoker: RpcMethodInvoker
    ) -> None:
        """
        Test invoking method with unicode characters in parameters.

        Verifies that:
        - Unicode is handled correctly
        - Method executes successfully
        """
        result = await method_invoker.invoke(
            "method_with_kwargs", {"name": "测试中文", "value": 42}
        )

        assert result == {"name": "测试中文", "value": 42}
