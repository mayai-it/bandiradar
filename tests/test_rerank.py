"""Listwise rerank tests — OFFLINE, the LLM call is a deterministic fake."""

import re

from bandiradar.matching import rerank as rr
from bandiradar.matching.rerank import _parse_ranking, rerank
from bandiradar.models import Opportunity, Profile

PROFILE = Profile(name="p", keywords=["software"], capabilities="software per le PMI")


class FakeRankClient:
    """Reads the numbered candidate list from the prompt and ranks the one whose
    brief contains 'WINNER' first — so the test is independent of input order."""

    cache_id = "fake:rank"

    def __init__(self, omit_winner: bool = False) -> None:
        self.calls = 0
        self.omit_winner = omit_winner

    def score(self, system: str, user: str) -> dict:
        self.calls += 1
        ranking = []
        for line in user.splitlines():
            m = re.match(r"\[(\d+)\]", line.strip())
            if not m:
                continue
            n = int(m.group(1))
            is_winner = "WINNER" in line
            if is_winner and self.omit_winner:
                continue  # simulate the model dropping a candidate
            ranking.append({"n": n, "score": 100 if is_winner else 10})
        return {"ranking": ranking}


def _opp(oid: str, title: str) -> Opportunity:
    return Opportunity(
        id=oid,
        source="x",
        source_url="https://example.invalid/x",
        kind="incentive",
        title=title,
        geo_scope="national",
        status="open",
        raw_ref=oid,
    )


def test_parse_ranking_is_tolerant():
    raw = {"ranking": [{"n": 2, "score": 90}, {"n": 1, "score": 40}]}
    assert _parse_ranking(raw, 2) == {1: 90, 0: 40}  # 1-based -> 0-based
    # out-of-range, duplicate, malformed, and clamping
    raw2 = {
        "ranking": [
            {"n": 1, "score": 250},  # clamp to 100
            {"n": 9, "score": 50},  # out of range -> ignored
            {"n": 1, "score": 5},  # duplicate -> first wins
            {"bad": 1},  # malformed -> ignored
        ]
    }
    assert _parse_ranking(raw2, 2) == {0: 100}
    assert _parse_ranking("not a dict", 3) == {}


def test_rerank_orders_by_comparative_score():
    client = FakeRankClient()
    cands = [
        _opp("x:1", "Normale"),
        _opp("x:2", "WINNER software"),
        _opp("x:3", "Altro"),
    ]
    matches = rerank(PROFILE, cands, client)
    assert client.calls == 1  # ONE listwise call for the whole set
    assert matches[0].opportunity_id == "x:2"  # the comparative winner first
    assert matches[0].score == 100
    assert {m.opportunity_id for m in matches} == {"x:1", "x:2", "x:3"}  # all kept


def test_rerank_keeps_candidates_the_model_omits():
    client = FakeRankClient(omit_winner=True)
    cands = [_opp("x:1", "Normale"), _opp("x:2", "WINNER software")]
    matches = rerank(PROFILE, cands, client)
    # the omitted candidate is still returned (recall preserved), scored 0 / last
    assert {m.opportunity_id for m in matches} == {"x:1", "x:2"}
    assert matches[-1].opportunity_id == "x:2"
    assert matches[-1].score == 0


def test_rerank_truncates_to_top_n_but_keeps_tail():
    client = FakeRankClient()
    cands = [_opp(f"x:{i}", f"Bando {i}") for i in range(5)]
    matches = rerank(PROFILE, cands, client, top_n=2)
    assert len(matches) == 5  # tail kept (ranked last, score 0)
    assert client.calls == 1
    tail_scores = sorted(m.score for m in matches)
    assert tail_scores[:3] == [0, 0, 0]  # 3 tail items beyond the window


def test_rerank_empty():
    assert rerank(PROFILE, [], FakeRankClient()) == []


def test_build_prompt_lists_every_candidate():
    cands = [_opp("x:1", "Alpha"), _opp("x:2", "Beta")]
    prompt = rr.build_rerank_prompt(PROFILE, cands)
    assert "[1]" in prompt and "[2]" in prompt
    assert "Alpha" in prompt and "Beta" in prompt
