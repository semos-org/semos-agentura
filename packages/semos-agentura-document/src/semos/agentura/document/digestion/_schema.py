"""Schema loading for structured annotation extraction."""

from __future__ import annotations

import importlib.util
import inspect
from pathlib import Path

from pydantic import BaseModel

from ..exceptions import DocumentAgentError


def load_schema(schema_path: str) -> type[BaseModel]:
    """Load the first BaseModel subclass from a Python file."""
    path = Path(schema_path).resolve()
    if not path.exists():
        raise DocumentAgentError(f"Schema file not found: {path}")

    spec = importlib.util.spec_from_file_location("_schema", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    for _name, obj in inspect.getmembers(module, inspect.isclass):
        if issubclass(obj, BaseModel) and obj is not BaseModel:
            return obj

    raise DocumentAgentError(f"No BaseModel subclass found in {path}")
