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
    "bandiradar.sources.wordpress",
    "bandiradar.sources.anac",
    "bandiradar.sources.ted",
    "bandiradar.sources.incentivi",
    "bandiradar.sources.lombardia",
    "bandiradar.sources.lazio",
    "bandiradar.sources.toscana",
    "bandiradar.sources.llm_scraper",
    "bandiradar.matching",
    "bandiradar.matching.prefilter",
    "bandiradar.matching.relevance",
    "bandiradar.matching.llm",
    "bandiradar.matching.prompts",
    "bandiradar.config",
    "bandiradar.ocp",
    "bandiradar.documents",
    "bandiradar.storage",
    "bandiradar.exporters",
    "bandiradar.intelligence",
    "bandiradar.intelligence.anac_history",
    "bandiradar.intelligence.benchmarks",
    "bandiradar.intelligence.store",
    "bandiradar.intelligence.enrichment",
    "bandiradar.core",
    "bandiradar.cli",
    "bandiradar.mcp_server",
]


@pytest.mark.parametrize("module_name", MODULES)
def test_module_imports(module_name: str) -> None:
    assert importlib.import_module(module_name) is not None
