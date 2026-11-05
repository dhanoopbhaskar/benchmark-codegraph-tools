"""Small plugin loader for custom parsers, runners and scorers.

A plugin is referenced from JSON as ``"module:function"``. Modules are imported
from the normal Python path, plus any directory listed in the ``plugins``
section of ``config/tools.json`` and the optional ``<home>/plugins`` directory.
No entry points or installs are required.
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path


def add_plugin_paths(paths: list[str]) -> None:
    for raw in paths:
        path = Path(raw).expanduser()
        if path.is_dir() and str(path) not in sys.path:
            sys.path.insert(0, str(path))


def resolve(spec: str):
    if not spec or ":" not in spec:
        return None
    module_name, function_name = spec.split(":", 1)
    module = importlib.import_module(module_name)
    function = getattr(module, function_name, None)
    if not callable(function):
        raise SystemExit(f"plugin '{spec}' does not resolve to a callable")
    return function
