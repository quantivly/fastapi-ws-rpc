"""
Method invocation component for validating and executing RPC methods.

This module handles method validation, parameter conversion, and execution
of local RPC methods.
"""

from __future__ import annotations

from inspect import Parameter, _empty, signature
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
        self, params: dict[str, Any] | list[Any] | None, method_name: str | None = None
    ) -> dict[str, Any]:
        """
        Convert JSON-RPC params to method arguments.

        This implementation supports both named and positional parameters per JSON-RPC 2.0 spec:
        - Named parameters (dict): Passed directly as keyword arguments
        - Positional parameters (list): Mapped to parameter names by position using method signature

        Parameters
        ----------
        params : dict[str, Any] | list[Any] | None
            JSON-RPC parameters as dict (named params), list (positional params), or None
        method_name : str | None, optional
            Method name for signature inspection (required for positional params)

        Returns
        -------
        dict[str, Any]
            Dictionary of keyword arguments for method invocation

        Raises
        ------
        ValueError
            - If positional params are used but method_name not provided
            - If positional params count exceeds method parameter count
            - If positional params count is insufficient (no defaults for missing params)
            - If method has keyword-only parameters (cannot use positional params)
            - If params is not dict, list, or None

        Examples
        --------
        Named params:

        >>> convert_params({"a": 1, "b": 2})
        {"a": 1, "b": 2}

        Positional params:

        >>> convert_params([1, 2], method_name="add")
        {"a": 1, "b": 2}

        No params:

        >>> convert_params(None)
        {}
        """
        if params is None:
            return {}

        if isinstance(params, dict):
            return params

        if isinstance(params, list):
            # Positional parameters - need to map to parameter names
            if method_name is None:
                raise ValueError(
                    "Cannot convert positional parameters without method name for signature inspection"
                )

            return self._convert_positional_params(params, method_name)

        raise ValueError(f"Invalid params type: {type(params).__name__}")

    def _convert_positional_params(
        self, params: list[Any], method_name: str
    ) -> dict[str, Any]:
        """
        Convert positional parameters (list) to named parameters (dict).

        Maps list items to method parameter names by position using inspect.signature().

        Parameters
        ----------
        params : list[Any]
            Positional parameters from JSON-RPC request
        method_name : str
            Name of the method to inspect for parameter names

        Returns
        -------
        dict[str, Any]
            Dictionary mapping parameter names to values

        Raises
        ------
        ValueError
            - If method has keyword-only parameters (incompatible with positional params)
            - If too many positional arguments provided
            - If too few positional arguments provided (and no defaults)
        """
        # Get the method
        method = getattr(self._methods, method_name)

        # Get method signature
        sig = signature(method)

        # Get parameters, excluding 'self' for bound methods
        method_params = [
            param
            for name, param in sig.parameters.items()
            if name != "self"
            and param.kind not in (Parameter.VAR_POSITIONAL, Parameter.VAR_KEYWORD)
        ]

        # Check for keyword-only parameters (after * separator)
        keyword_only = [p for p in method_params if p.kind == Parameter.KEYWORD_ONLY]
        if keyword_only:
            keyword_only_names = [p.name for p in keyword_only]
            raise ValueError(
                f"Method '{method_name}' has keyword-only parameters {keyword_only_names} "
                "and cannot be called with positional parameters. "
                "Use named parameters instead."
            )

        # Check parameter count
        required_count = sum(1 for p in method_params if p.default is Parameter.empty)
        max_count = len(method_params)

        if len(params) > max_count:
            raise ValueError(
                f"Too many positional arguments for method '{method_name}': "
                f"expected at most {max_count}, got {len(params)}"
            )

        if len(params) < required_count:
            raise ValueError(
                f"Too few positional arguments for method '{method_name}': "
                f"expected at least {required_count}, got {len(params)}"
            )

        # Map positional params to parameter names
        result = {}
        for i, value in enumerate(params):
            param_name = method_params[i].name
            result[param_name] = value

        return result

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
        2. Converts parameters to arguments dict (handles both named and positional params)
        3. Invokes the method with **kwargs
        4. Converts result to expected type if needed

        Args:
            method_name: Name of the method to invoke
            params: Method parameters (dict for named, list for positional, or None for no params)

        Returns:
            The return value from the invoked method (or NoResponse sentinel)

        Raises:
            TypeError: If the parameters are invalid for the method signature
            ValueError: If positional params are invalid for the method
            Exception: Any exception raised by the invoked method

        Example:
            ```python
            # Named parameters
            result = await invoker.invoke("add", {"a": 1, "b": 2})

            # Positional parameters
            result = await invoker.invoke("add", [1, 2])
            ```
        """
        method = getattr(self._methods, method_name)
        arguments = self.convert_params(params, method_name=method_name)
        result = await method(**arguments)

        # Type conversion if needed
        if result is not NoResponse:
            result_type = self.get_return_type(method)
            # If no type given - try to convert to string
            if result_type is str and not isinstance(result, str):
                result = str(result)

        return result
