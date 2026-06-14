# Self-healing for HTML-listing scrapers — design (Phase 2)

> Status: **Phase 2 essentially COMPLETE — 9 of 10 scrapers auto-heal (v0.16.0).**
> Phase 1 (v0.14.0) extended the gated self-heal from `toscana` to the JSON scrapers
> (`calabria`, `basilicata`). Phase 2a (v0.15.0) built the **regex-template HTML
> recipe** (Approach A below, stdlib `re`, ReDoS-guarded) for `veneto`, `sardegna`,
> `piemonte`. v0.16.0 added `HtmlCrawlRecipe.title_template` and — crucially — leaned
> on the recipe's **fetch/parse split** (the bespoke FETCH lives in `_listing_html`,
> only the PARSE is the healable regex) to bring `campania` (label from slug), `fvg`
> (the `#contributi` filter is in the regex; multi-page fetch) and `liguria`
> (POST+CSRF fetch, regex parse) into auto-heal too.
>
> **Outcome:** the fetch/parse split made the proposed **assisted-heal (Approach B)
> unnecessary for everything except `puglia`** — whose "Bando aperto" badge filter is a
> conditional on a sibling element (not one item regex) AND whose host is CI-blocked.
> So `puglia` stays detect-only (a documented skip); assisted-heal remains the option
> if it is ever both unblocked and worth a regex with a bounded lookahead.

## 1. The problem

The fragile part of an LLM scraper is the **crawl** (the listing it walks), not the
per-page extraction (the LLM adapts to HTML changes already). For a **JSON listing**
the crawl is modelled as a `CrawlRecipe` — DATA (dotted field paths + url/params) —
so when it drifts an LLM can re-derive the paths and the deterministic golden gate
(`recipe_reproduces_golden`) adopts the candidate **only if it reproduces the
last-good refs exactly**. The LLM proposes DATA; a deterministic socket disposes.

The 7 **HTML-listing** scrapers don't fit this: their listing is parsed by bespoke
per-source code (a `re` pattern over the page HTML), e.g.

```python
# veneto: server-rendered landing anchors
_DETAIL_RE = re.compile(r'<a[^>]+href="(?:/Public/)?Dettaglio\?idAtto=(\d+)"[^>]*>(.*?)</a>', re.S | re.I)
```

A parser is **code, not re-derivable DATA**, so today drift is only **detected**
(`validate_refs` → broken) and **flagged for a human**. That is honest but leaves
auto-maintenance wired on 3 of 10 scrapers.

## 2. Invariants any solution must preserve

1. **Propose/dispose.** The LLM proposes DATA only; a pure deterministic gate
   (golden-exact reproduction) disposes. The model can never bypass the gate.
2. **No execution of LLM-authored code.** A healed artifact must be interpreted by a
   fixed, audited engine — never `eval`/`exec` of model output.
3. **Stage-1 / golden gate stays pure** and offline-testable.
4. **Every source keeps its fixture + offline test** (guardrail 5).
5. **Stdlib-first.** `crawl.py` is dependency-free; new deps are a real cost.

## 3. The 7 HTML sources, by listing shape

| Source | Listing | Heal-ability |
|---|---|---|
| `sardegna` | single-anchor regex (`<a href="/it/agevolazioni/…">title</a>`) | clean |
| `veneto` | single-anchor regex on the server-rendered landing | clean |
| `campania` | open-bandi widget anchors (regex) | clean |
| `piemonte` | Drupal Views listing (regex, multi-page) | mostly clean |
| `fvg` | listing + a `contributi` filter (params + regex) | filtered |
| `puglia` | Liferay news-list fragment + a **"Bando aperto" badge** filter | bespoke |
| `liguria` | **POST + per-session CSRF token**, two requests, then regex | bespoke |

So ~4 are a single regex over one fetched page; 3 carry extra logic (a filter or a
multi-step authenticated fetch) that a single DATA recipe can't fully express.

## 4. Two approaches

### A. Declarative HTML recipe (true auto-heal)

Model the HTML parse as DATA, exactly like the JSON `CrawlRecipe`:

```python
@dataclass(frozen=True)
class HtmlCrawlRecipe:
    listing_url: str
    params: dict[str, Any] = field(default_factory=dict)
    item_regex: str = ""     # a regex with named groups: (?P<post_id>…)(?P<url>…)(?P<title>…)
    filter_regex: str | None = None  # optional: keep only items whose block matches
```

`apply_html_recipe(recipe, page_html) -> list[DetailRef]` is **pure**: it
`re.finditer`s `item_regex` over the HTML and reads the named groups. On drift the
LLM re-derives `item_regex` (a DATA string), and the **existing** golden gate decides
— a candidate is adopted only if `apply_html_recipe(candidate, golden_html)` equals
the last-good refs exactly. This reuses `recipe_store`, the golden, `heal_crawl`'s
philosophy, and the heal CLI surface.

- **Why a regex, not CSS selectors?** Stdlib has no CSS engine; CSS would mean a new
  parser dependency (`lxml`/`selectolax`/`bs4`), violating invariant 5. The current
  parses are already regexes, so a regex-template recipe is the lowest-friction,
  zero-dep fit. (CSS selectors would be *cleaner* recipes — an explicit open question
  below.)
- **Is a re-derived regex still "DATA, not code"?** A regex is interpreted by the
  fixed, audited `re` engine — no arbitrary code runs — and the golden-exact gate is
  the socket it cannot bypass (invariants 1–2 hold). The one NEW risk is **ReDoS**:
  a pathological pattern causing catastrophic backtracking *during evaluation*. This
  is a DoS, not a correctness/injection hole, and is mitigable (see §6).
- **Fits:** `sardegna`, `veneto`, `campania`, `piemonte` (the single-regex sources).
- **Does NOT fit:** `puglia` (badge filter is semantic, not a stable regex of the
  anchor), `liguria` (POST+CSRF multi-step fetch is code, not a URL+params recipe),
  `fvg` (filter logic) — unless `filter_regex` happens to capture their filter, which
  is brittle.

### B. Assisted-heal (LLM proposes, golden pre-validates, human one-click)

Keep the parse as code. On drift, the LLM proposes a candidate parse (a regex recipe
or selector set); the golden gate **pre-validates** it (proves it reproduces the
golden exactly) and surfaces it as a high-confidence candidate for **one-click human
adoption** (e.g. `bandiradar heal --review <source>` prints the proposed recipe + a
green "reproduces golden" check; the human confirms).

- Keeps the **"code = human"** boundary (no code auto-adopted), but removes ~all the
  diagnosis toil — the human just confirms a pre-proven fix.
- **Fits all 7**, including the bespoke ones: for `liguria`/`puglia` the human still
  applies the actual code edit, but starts from a validated proposal, not a blank
  page.
- Honest framing: this is *assisted* maintenance, not fully autonomous — arguably the
  correct ceiling for a code parse.

## 5. Recommendation — hybrid, phased

- **Phase 2a (true auto-heal):** introduce `HtmlCrawlRecipe` + pure
  `apply_html_recipe` (regex-template, stdlib `re`, ReDoS-guarded) and migrate the 4
  single-regex sources (`sardegna` first — cleanest — then `veneto`, `campania`,
  `piemonte`). Reuses the golden gate + `heal_crawl` philosophy. **Auto-heal 3 → 7.**
- **Phase 2b (assisted-heal):** for the 3 bespoke sources (`puglia`, `liguria`,
  `fvg`), ship the `heal --review` flow (golden-pre-validated proposal, human
  confirm). If even a proposal doesn't generalize, they stay detect-only — documented,
  not pretended (the project's "skip = success" ethos).

This keeps every step measurable and never auto-adopts code, while taking autonomous
auto-heal from 3/10 to 7/10 scrapers and giving the rest a pre-validated assist.

## 6. Open questions for the decision

1. **ReDoS guard.** Acceptable mitigations for an LLM-proposed `item_regex`: a length
   cap on the pattern, a complexity lint (reject nested quantifiers like `(a+)+`), and
   a wall-clock bound on `finditer` over the golden. Is a `re`-only guard enough, or
   do we want the `regex` module's timeout (a new dep) for hard safety?
2. **Regex vs CSS recipe.** Stay stdlib `re` (zero-dep, uglier recipes, ReDoS to
   guard) — recommended — or accept a small HTML-parser dep for CSS-selector recipes
   (cleaner, no ReDoS, but a dependency and a new failure surface)?
3. **Assisted-heal as the honest ceiling.** Is a human one-click confirm acceptable
   for the bespoke sources, or do we want to push for full auto everywhere (higher
   risk, and `liguria`'s POST+CSRF likely can't be a pure recipe anyway)?

## 7. What does NOT change

The golden gate, the propose/dispose invariant, `recipe_store`, fixtures + offline
tests, and the keyless monitor's detect-only fallback all stay exactly as they are.
Phase 2 only adds a second *kind* of re-derivable recipe (HTML) behind the same gate.
