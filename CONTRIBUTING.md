# Contributing to fastapi_ws_rpc

Thank you for your interest in contributing to fastapi_ws_rpc! This document provides guidelines and instructions for contributing to the project.

## Table of Contents

- [Development Setup](#development-setup)
- [Code Style](#code-style)
- [Testing](#testing)
- [Making Changes](#making-changes)
- [Submitting Pull Requests](#submitting-pull-requests)
- [Reporting Issues](#reporting-issues)

## Development Setup

### Prerequisites

- Python 3.9 or higher
- [Poetry](https://python-poetry.org/) for dependency management
- Git

### Installation

1. Fork and clone the repository:
   ```bash
   git clone https://github.com/YOUR_USERNAME/fastapi_websocket_rpc.git
   cd fastapi_websocket_rpc
   ```

2. Install dependencies using Poetry:
   ```bash
   poetry install --with dev
   ```

3. Install pre-commit hooks:
   ```bash
   poetry run pre-commit install
   ```

## Code Style

We use several tools to maintain consistent code quality:

### Linting and Formatting

- **[Ruff](https://github.com/astral-sh/ruff)**: Fast Python linter
- **[Black](https://github.com/psf/black)**: Code formatter (88 character line length)
- **[isort](https://pycqa.github.io/isort/)**: Import sorting

Run all formatters and linters:
```bash
poetry run ruff check fastapi_ws_rpc --fix
poetry run black fastapi_ws_rpc
poetry run isort fastapi_ws_rpc
```

Or run pre-commit on all files:
```bash
poetry run pre-commit run --all-files
```

### Type Checking

We use **[mypy](https://mypy-lang.org/)** in strict mode for type checking:
```bash
poetry run mypy fastapi_ws_rpc
```

All new code should include comprehensive type hints. See `pyproject.toml` for our mypy configuration.

### Code Quality Guidelines

- Write clear, descriptive variable and function names
- Add docstrings to all public functions and classes (NumPy style preferred)
- Keep functions focused on a single responsibility
- Use type hints for all function parameters and return values
- Handle errors appropriately with specific exception types
- Add comments for complex logic

## Testing

We use **pytest** for testing with coverage reporting:

```bash
# Run all tests
poetry run pytest

# Run tests with verbose output
poetry run pytest -v

# Run specific test file
poetry run pytest tests/basic_rpc_test.py

# Run tests with coverage report
poetry run pytest --cov=fastapi_ws_rpc --cov-report=html
```

### Writing Tests

- Place tests in the `tests/` directory
- Name test files with `*_test.py` suffix
- Use descriptive test function names (e.g., `test_connection_handles_timeout`)
- Test both success and failure cases
- Use fixtures for common setup
- Mock external dependencies when appropriate

## Making Changes

### Workflow

1. Create a new branch for your feature or bugfix:
   ```bash
   git checkout -b feature/your-feature-name
   ```
   or
   ```bash
   git checkout -b fix/your-bugfix-name
   ```

2. Make your changes following the code style guidelines

3. Run the test suite and ensure all tests pass:
   ```bash
   poetry run pytest
   ```

4. Run linters and type checking:
   ```bash
   poetry run pre-commit run --all-files
   poetry run mypy fastapi_ws_rpc
   ```

5. Commit your changes with clear, descriptive commit messages:
   ```bash
   git commit -m "feat: Add support for custom websocket protocols"
   ```

### Commit Message Format

We follow conventional commits format:

- `feat:` New feature
- `fix:` Bug fix
- `doc:` Documentation changes
- `ref:` Refactoring
- `test:` Test additions or modifications
- `enh:` Enhancements to existing features

Example:
```
feat: Added support for connection timeouts

- Added timeout parameter to WebsocketRpcClient
- Updated documentation with timeout examples
- Added tests for timeout behavior
```

## Submitting Pull Requests

1. Push your changes to your fork:
   ```bash
   git push origin feature/your-feature-name
   ```

2. Create a Pull Request on GitHub with:
   - Clear title describing the change
   - Detailed description of what changed and why
   - Reference to related issues (if any)
   - Screenshots or examples (if applicable)

3. Ensure CI checks pass:
   - All tests must pass
   - Linting checks must pass
   - Code coverage should not decrease significantly

4. Respond to review feedback promptly

5. Once approved, a maintainer will merge your PR

### PR Checklist

Before submitting your PR, ensure:

- [ ] Code follows the project's style guidelines
- [ ] All tests pass locally
- [ ] New tests added for new functionality
- [ ] Type hints added for new code
- [ ] Docstrings added for new public functions/classes
- [ ] CHANGELOG.md updated (for significant changes)
- [ ] Pre-commit hooks pass
- [ ] No merge conflicts with main branch

## Reporting Issues

### Bug Reports

When reporting bugs, please include:

- Python version
- fastapi_ws_rpc version
- Operating system
- Minimal code example reproducing the issue
- Full error traceback
- Expected vs actual behavior

### Feature Requests

For feature requests, please describe:

- The problem you're trying to solve
- Proposed solution or API
- Alternative solutions considered
- Potential impact on existing functionality

## Questions?

If you have questions about contributing:

- Open a [GitHub Discussion](https://github.com/permitio/fastapi_websocket_rpc/discussions)
- Check existing [Issues](https://github.com/permitio/fastapi_websocket_rpc/issues)
- Review the [README](README.md) and [documentation](https://permitio.github.io/fastapi_websocket_rpc/)

## License

By contributing to fastapi_ws_rpc, you agree that your contributions will be licensed under the MIT License.

Thank you for contributing! 🎉
