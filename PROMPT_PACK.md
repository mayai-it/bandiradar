# BandiRadar — Prompt Pack per Claude Code

Questo è il copione operativo: una sequenza di prompt da incollare in **Claude
Code**, uno slice verticale alla volta. Discutiamo l'architettura qui; costruisci
là.

## Come si usa
1. Crea il repo e mettici dentro `ARCHITECTURE.md` e `CLAUDE.md` **prima** di
   lanciare i prompt. Sono la memoria: Claude Code li leggerà a ogni sessione.
2. Incolla i prompt **in ordine**. Non passare al successivo finché i test non
   sono verdi.
3. Regola d'oro per ogni prompt: chiudi sempre con
   *"Run `uv run pytest` and show me the output. Do not stop until green."*
4. Slice verticale, non orizzontale: prima un filo che gira end-to-end, poi
   allarghi.

## Setup iniziale (manuale, una volta)
```bash
mkdir bandiradar && cd bandiradar
git init
# copia qui ARCHITECTURE.md e CLAUDE.md
claude            # avvia Claude Code in questa cartella
```

---

## Prompt 0 — Bootstrap dello scheletro
```
Read ARCHITECTURE.md and CLAUDE.md first. Scaffold the project exactly as the
module map in CLAUDE.md describes, using uv, Python 3.12, pydantic v2, ruff,
pytest. Create:
- pyproject.toml (package name "bandiradar", console script "bandiradar"),
  ruff + pytest config, deps: pydantic, httpx, typer, mcp (FastMCP).
- the full src/bandiradar/ tree with EMPTY but importable modules
  (docstrings + TODOs, no logic yet).
- data/fixtures/ and data/profiles/ dirs.
- .gitignore (.env, .venv, __pycache__, *.db), .env.example, MIT LICENSE
  (holder: MayAI), a stub README.
- tests/test_smoke.py that just imports every module.
Run `uv sync` and `uv run pytest`. Show me green before stopping.
```

## Prompt 1 — Modello canonico + fixtures (il contratto)
```
Implement src/bandiradar/models.py exactly per ARCHITECTURE.md §4: Opportunity,
RawDoc, Profile, Match as pydantic v2 models with full type hints and sensible
validators (e.g. status derives from deadline; content_hash helper).
Add data/fixtures/anac_sample.json with 5-8 realistic ANAC-style tender records.
Add data/profiles/mayai.yaml and data/profiles/manifattura.yaml per §7.
Write tests/test_models.py covering validation, content_hash stability, and
status derivation. Run pytest, show me green.
```

## Prompt 2 — Source framework + adapter ANAC
```
Implement src/bandiradar/sources/base.py: the Source Protocol and a registry
(register/get/list) per ARCHITECTURE.md §5.
Implement src/bandiradar/sources/anac.py:
- to_opportunities(raw): PURE mapping from an ANAC/OCDS record to Opportunity[].
- fetch(since): for now read data/fixtures/anac_sample.json when --sample mode;
  leave a clearly-marked TODO + config constant for the live PNCP/ANAC open-data
  endpoint (do NOT invent a URL — mark it to confirm against live docs).
Register "anac". Write tests/test_anac.py asserting the mapper output against the
fixture (offline, no network). Run pytest, show me green.
```

## Prompt 3 — Matching Stage 1 (prefiltro deterministico)
```
Implement src/bandiradar/matching/prefilter.py: a PURE function
prefilter(opportunities, profile) -> list[Opportunity] filtering on region/geo,
cpv ∩ ateco, value range, deadline > now, keyword overlap (per ARCHITECTURE.md §6
Stage 1). No LLM, no I/O. Write thorough tests/test_prefilter.py with edge cases
(no deadline, empty cpv, region mismatch). Run pytest, show me green.
```

## Prompt 4 — Matching Stage 2 (LLM + fallback offline + cache)
```
Implement the LLM relevance scorer per ARCHITECTURE.md §6 Stage 2:
- matching/llm.py: provider-agnostic client. Provider chosen by env
  (BANDIRADAR_LLM_PROVIDER=anthropic|openai|none). Default to "none".
- matching/prompts.py: the scoring prompt (minimal opportunity text + compact
  profile summary; never raw dumps).
- matching/relevance.py: score(opportunity, profile) -> Match with
  score/reasons/matched_capabilities/eligibility_flags/risk_notes (structured).
  Cache by hash(profile.version + opportunity.content_hash).
  When provider == none OR no API key, use a DETERMINISTIC heuristic fallback
  (keyword/overlap based) so it runs with zero secrets.
Write tests/test_relevance.py exercising the OFFLINE fallback only. Run pytest,
show me green.
```

## Prompt 5 — Storage SQLite + dedupe / change detection
```
Implement src/bandiradar/storage.py using stdlib sqlite3 per ARCHITECTURE.md §8:
tables opportunities, raw_docs, matches, runs. upsert_opportunity must dedupe by
id and detect changes via content_hash → bump version, set status "amended",
flag as re-notifiable. Add list_new(since) and save_match/get_matches.
Write tests/test_storage.py (insert, re-insert unchanged = no-op, re-insert
changed = version bump + amended). Run pytest, show me green.
```

## Prompt 6 — Core service layer + CLI
```
Implement src/bandiradar/core.py as the orchestration layer (fetch→normalize→
store→match) with NO presentation logic.
Implement src/bandiradar/cli.py with Typer (THIN — calls core only):
  profile show|validate ; sources list ; fetch --source --sample ;
  match --profile --sample ; watch (loop stub) ; mcp (launch server).
--json on every command. Wire it end-to-end so
`uv run bandiradar match --profile data/profiles/mayai.yaml --sample`
prints ranked opportunities with reasons. Add tests/test_cli.py (typer runner,
--sample). Run pytest, show me green, then show me real --sample output.
```

## Prompt 7 — MCP server (dogfood)
```
Implement src/bandiradar/mcp_server.py with FastMCP, exposing tools per
ARCHITECTURE.md §9: list_sources, fetch_opportunities, search_opportunities,
score_opportunity, get_matches, get_profile. THIN — each tool calls core.
Add a short docs/MCP.md on how to register the server with Claude. Smoke-test
that the server starts and tools are listed. Run pytest, show me green.
```

## Prompt 8 — README + skill `bandiradar` + install-skill.sh
```
Write a marketing-grade README.md in English (badges, one-liner, "what it does",
quickstart that runs OFFLINE on --sample, architecture diagram from
ARCHITECTURE.md, roadmap = the phases, "open core vs pro" note, MIT).
Create skills/bandiradar/SKILL.md teaching an agent to drive the CLI/MCP.
Create install-skill.sh (freecode style) that installs the skill into
~/.claude/skills and ${CODEX_HOME:-~/.codex}/skills. Run pytest, show me green.
```

## Prompt 9 — Skill `add-a-source` + CONTRIBUTING (apri ai contributi)
```
Create skills/add-a-source/SKILL.md codifying the "How to add a new Source"
playbook from CLAUDE.md as a step-by-step template (new file, fetch, pure
to_opportunities, fixture, test, register).
Write CONTRIBUTING.md inviting community source adapters (esp. regional bandi),
explaining the Source contract and the fixture+test requirement.
This is how we reach "integrate everything later" without funding every scraper.
```

---

## Lavorare con i subagent (parallelo)
Una volta che il contratto (`models.py`) è verde, puoi parallelizzare con i
subagent di Claude Code. Esempio:
> "Spawn three subagents in parallel: (1) implement sources/anac.py + its test,
> (2) implement matching/prefilter.py + its test, (3) implement storage.py + its
> test. Each must follow CLAUDE.md, ship a test, and report `pytest` green for
> its own files. Then a 4th 'reviewer' subagent checks all three against
> ARCHITECTURE.md and the guardrails."
Regola: parallelizza **solo dopo** che il modello canonico è stabile, altrimenti
i subagent collidono sul contratto.

## Quando un cambiamento tocca il contratto
Se modifichi `Opportunity`, aggiorna nello stesso commit: `models.py`, tutti gli
adapter, i test, e `ARCHITECTURE.md §4`. Dillo esplicitamente nel prompt.

## Checklist "slice fatto"
- [ ] `uv run pytest` verde **offline** (zero segreti)
- [ ] `uv run ruff check .` pulito
- [ ] gira end-to-end con `--sample`
- [ ] doc aggiornati se è cambiato un contratto

## Phase 1 / 2 (dopo la v1)
- Phase 1: adapter `incentivi` (incentivi.gov.it), `watch`/scheduling, export
  JSON/RSS, prefiltro a embedding.
- Phase 2: adapter regionali (community), repo privato `bandiradar-pro`
  (dashboard, delivery WhatsApp/email, multi-tenant).
