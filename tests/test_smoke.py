"""Smoke test: every module in the package must be importable.

This is the Phase-0 guardrail — the scaffold stays importable even before any
logic is implemented. As real modules gain logic, this keeps catching import-time
breakage (syntax errors, bad imports, circular imports).
"""

import importlib

import pytest

MODULES = [
    "bandiradar",
    "bandiradar.models",
    "bandiradar.sources",
    "bandiradar.sources.base",
    "bandiradar.sources.anac",
    "bandiradar.sources.ted",
    "bandiradar.matching",
    "bandiradar.matching.prefilter",
    "bandiradar.matching.relevance",
    "bandiradar.matching.llm",
    "bandiradar.matching.prompts",
    "bandiradar.storage",
    "bandiradar.core",
    "bandiradar.cli",
    "bandiradar.mcp_server",
]


@pytest.mark.parametrize("module_name", MODULES)
def test_module_imports(module_name: str) -> None:
    assert importlib.import_module(module_name) is not None
