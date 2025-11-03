"""
Method invocation component for validating and executing RPC methods.

This module handles method validation, parameter conversion, and execution
of local RPC methods.
"""

from __future__ import annotations

from inspect import _empty, signature
from typing import Any

from ..rpc_methods import EXPOSED_BUILT_IN_METHODS, NoResponse, RpcMethodsBase
from ..schemas import JsonRpcErrorCode


class RpcMethodInvoker:
    """
    Handles validation and invocation of RPC methods.

    This component:
    - Validates method accessibility (no private methods except built-ins)
    - Validates method existence and callable status
    - Converts parameters (dict/list) to appropriate arguments
    - Executes methods with proper error handling
    - Converts return values to expected types
    """

    def __init__(self, methods: RpcMethodsBase) -> None:
        """
        Initialize the method invoker.

        Args:
            methods: The RpcMethodsBase instance containing methods to invoke
        """
        self._methods = methods

    def validate_method(
        self, method_name: str
    ) -> tuple[bool, JsonRpcErrorCode | None, str | None]:
        """
        Validate that a method can be called.

        This method checks:
        1. Method name is a string
        2. Method is not protected (doesn't start with "_" unless built-in)
        3. Method exists on the methods object
        4. Method is callable

        Args:
            method_name: Name of the method to validate

        Returns:
            Tuple of (is_valid, error_code, error_message)
            - is_valid: True if method is valid
            - error_code: JsonRpcErrorCode if invalid, None if valid
            - error_message: Error message if invalid, None if valid

        Example:
            ```python
            is_valid, error_code, error_msg = invoker.validate_method("my_method")
            if not is_valid:
                await send_error(error_code, error_msg)
            ```
        """
        # Check method name type
        if not isinstance(method_name, str):
            return (
                False,
                JsonRpcErrorCode.INVALID_REQUEST,
                "Method name must be a string",
            )

        # Check protected methods
        if method_name.startswith("_") and method_name not in EXPOSED_BUILT_IN_METHODS:
            return (
                False,
                JsonRpcErrorCode.METHOD_NOT_FOUND,
                f"Method '{method_name}' not found or not accessible",
            )

        # Check existence
        if not hasattr(self._methods, method_name):
            return (
                False,
                JsonRpcErrorCode.METHOD_NOT_FOUND,
                f"Method '{method_name}' not found",
            )

        # Check callable
        method = getattr(self._methods, method_name)
        if not callable(method):
            return (
                False,
                JsonRpcErrorCode.METHOD_NOT_FOUND,
                f"'{method_name}' is not a callable method",
            )

        return (True, None, None)

    def convert_params(
        self, params: dict[str, Any] | list[Any] | None
    ) -> dict[str, Any]:
        """
        Convert JSON-RPC params to method arguments.

        JSON-RPC 2.0 supports three parameter formats:
        1. Named parameters (dict) - passed as **kwargs
        2. Positional parameters (list) - wrapped in "params" kwarg (limitation)
        3. No parameters (None) - empty dict

        Note: List params are wrapped in a "params" keyword due to lack of
        signature introspection. True positional args would require inspecting
        the method signature and matching list items to parameter positions.

        Args:
            params: JSON-RPC parameters (dict, list, or None)

        Returns:
            Dictionary of keyword arguments for method invocation

        Example:
            ```python
            # Named params
            convert_params({"a": 1, "b": 2})  # -> {"a": 1, "b": 2}

            # Positional params (wrapped)
            convert_params([1, 2])  # -> {"params": [1, 2]}

            # No params
            convert_params(None)  # -> {}
            ```
        """
        if isinstance(params, dict):
            return params
        elif isinstance(params, list):
            # For positional parameters, wrap in params dict
            # This is a limitation - true positional args would require
            # introspection of method signature
            return {"params": params}
        else:
            return {}

    def get_return_type(self, method: Any) -> Any:
        """
        Get the expected return type from method signature.

        If the method has a return type annotation, it's returned.
        Otherwise, defaults to str type.

        Args:
            method: The method to inspect

        Returns:
            The return type annotation, or str if not annotated

        Example:
            ```python
            async def my_method() -> int:
                return 42

            get_return_type(my_method)  # -> int
            ```
        """
        method_signature = signature(method)
        return (
            method_signature.return_annotation
            if method_signature.return_annotation is not _empty
            else str
        )

    async def invoke(
        self, method_name: str, params: dict[str, Any] | list[Any] | None
    ) -> Any:
        """
        Invoke a method with the given parameters.

        This method:
        1. Gets the method from the methods object
        2. Converts parameters to arguments dict
        3. Invokes the method with **kwargs
        4. Converts result to expected type if needed

        Args:
            method_name: Name of the method to invoke
            params: Method parameters (dict for named, list for positional, None for no params)

        Returns:
            The return value from the invoked method (or NoResponse sentinel)

        Raises:
            TypeError: If the parameters are invalid for the method signature
            Exception: Any exception raised by the invoked method

        Example:
            ```python
            result = await invoker.invoke("add", {"a": 1, "b": 2})
            ```
        """
        method = getattr(self._methods, method_name)
        arguments = self.convert_params(params)
        result = await method(**arguments)

        # Type conversion if needed
        if result is not NoResponse:
            result_type = self.get_return_type(method)
            # If no type given - try to convert to string
            if result_type is str and not isinstance(result, str):
                result = str(result)

        return result
