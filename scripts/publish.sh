#!/bin/bash
set -e

# Clean previous builds
rm -rf dist/

python -m pip install --upgrade build twine

# Build the package
python -m build

# Upload to PyPI
python -m twine upload dist/*
