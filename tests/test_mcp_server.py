"""MCP server tests (ARCHITECTURE.md §9 / Prompt 7). Offline."""

import anyio
import pytest
from mcp.server.fastmcp import FastMCP

from bandiradar import mcp_server

EXPECTED_TOOLS = {
    "list_sources",
    "fetch_opportunities",
    "search_opportunities",
    "score_opportunity",
    "get_matches",
    "get_profile",
}


def test_module_exposes_named_fastmcp():
    assert isinstance(mcp_server.mcp, FastMCP)
    assert mcp_server.mcp.name == "bandiradar"
    assert callable(mcp_server.main)
    assert callable(mcp_server.run)


def test_expected_tools_registered():
    tools = anyio.run(mcp_server.mcp.list_tools)
    names = {t.name for t in tools}
    assert EXPECTED_TOOLS <= names


def test_fetch_then_search_offline(tmp_path):
    db = str(tmp_path / "mcp.db")

    counts = mcp_server.fetch_opportunities(source="synthetic", sample=True, db=db)
    assert counts == {"fetched": 6, "new": 6, "amended": 0}

    ranked = mcp_server.search_opportunities(
        profile_path="data/profiles/mayai.yaml", source="synthetic", sample=True, db=db
    )
    ids = {row["opportunity_id"] for row in ranked}
    assert ids == {
        "synthetic:ocds-bandi-0001",
        "synthetic:ocds-bandi-0002",
        "synthetic:ocds-bandi-0004",
    }
    scores = [row["score"] for row in ranked]
    assert scores == sorted(scores, reverse=True)
    # Canonical view shape; never raw payloads.
    assert set(ranked[0]) == {
        "opportunity_id",
        "score",
        "status",
        "title",
        "issuer",
        "region",
        "deadline",
        "reasons",
        "matched_capabilities",
        "source_url",
    }


def test_list_sources_and_get_profile():
    assert {"id": "anac", "kind": "tender"} in mcp_server.list_sources()
    profile = mcp_server.get_profile("data/profiles/mayai.yaml")
    assert profile["name"] == "MayAI"


def test_score_and_get_matches(tmp_path):
    db = str(tmp_path / "s.db")
    mcp_server.fetch_opportunities(source="synthetic", sample=True, db=db)

    match = mcp_server.score_opportunity(
        "synthetic:ocds-bandi-0001", profile_path="data/profiles/mayai.yaml", db=db
    )
    assert match["opportunity_id"] == "synthetic:ocds-bandi-0001"
    assert 0 <= match["score"] <= 100

    persisted = mcp_server.get_matches(profile_path="data/profiles/mayai.yaml", db=db)
    assert any(m["opportunity_id"] == "synthetic:ocds-bandi-0001" for m in persisted)


def test_profile_arg_validation(tmp_path):
    db = str(tmp_path / "v.db")
    # Neither provided.
    with pytest.raises(ValueError):
        mcp_server.search_opportunities(sample=True, db=db)
    # Both provided.
    with pytest.raises(ValueError):
        mcp_server.search_opportunities(
            profile_path="data/profiles/mayai.yaml",
            profile={"name": "X"},
            sample=True,
            db=db,
        )


def test_score_opportunity_missing_is_error(tmp_path):
    db = str(tmp_path / "missing.db")
    mcp_server.fetch_opportunities(source="synthetic", sample=True, db=db)
    with pytest.raises(ValueError):
        mcp_server.score_opportunity(
            "anac:does-not-exist", profile_path="data/profiles/mayai.yaml", db=db
        )
