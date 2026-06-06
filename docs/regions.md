# Regional coverage map

Which Italian regional finanza-agevolata / bandi portals have been checked for a
usable **open-bandi API**, and where coverage is still needed. This is a
contributor map — if your region is "skipped", a community adapter is very
welcome (see [CONTRIBUTING.md](../CONTRIBUTING.md) and the `add-a-source` skill).

**What "viable" means here:** the portal must expose *open* calls (future
deadlines, not a retrospective beneficiary/awarded registry) over a *clean API*
with enough content to match on (title + requirements/eligibility text + a way to
tell open from closed). WordPress sites that expose a bandi post type over the WP
REST API with full `content` become a **config-only** entry on
`WordPressBandiSource` (see [ARCHITECTURE.md](../ARCHITECTURE.md) §5).

We deliberately **do not** ship half-working adapters: a region that's
unreachable, retrospective-only, or has no clean API is skipped honestly rather
than faked.

## Status

| Region | Portal probed | API | Status |
|---|---|---|---|
| **Lazio** | lazioinnova.it | WP-REST `bandi` (content-rich, parseable scadenza) | ✅ **Built** (`lazio`) |
| Toscana | sviluppo.toscana.it | WP-REST `bando` exists | ⏳ **Candidate** — REST `content` is empty + no deadline exposed; needs per-bando HTML detail scraping (a new pattern, not a WP config entry) |
| Sicilia | irfis.it | WordPress (Avada theme) | ⏭️ Skip — no bandi post type; bandi are pages/PDFs |
| Marche | regione.marche.it | reachable, not WP-REST | ⏭️ Skip — bespoke CMS, no clean bandi API |
| Piemonte | finpiemonte.it | not WordPress (404 `/wp-json`) | ⏭️ Skip — no clean API found |
| Veneto | venetosviluppo.it | not WordPress | ⏭️ Skip |
| Liguria | filse.it | not WordPress | ⏭️ Skip |
| Friuli-VG | regione.fvg.it | not WordPress | ⏭️ Skip |
| Emilia-Romagna | art-er.it | not WordPress | ⏭️ Skip (regional CKAN `dati.emilia-romagna.it` is retrospective) |
| Umbria | sviluppumbria.it | not WordPress | ⏭️ Skip |
| Abruzzo | regione.abruzzo.it | unreachable | ⏭️ Skip |
| Campania | sviluppocampania.it | 401 on `/wp-json` | ⏭️ Skip — not open/clean |
| Puglia | sistema.puglia.it | not WordPress | ⏭️ Skip (regional CKAN's active-gare dataset is empty) |
| Basilicata | sviluppobasilicata.it | not WordPress | ⏭️ Skip |
| Calabria | fincalabra.it | not WordPress | ⏭️ Skip |
| Sardegna | sardegnaimpresa.eu | not WordPress | ⏭️ Skip |
| Molise | regione.molise.it | not WordPress | ⏭️ Skip |
| Trento | provincia.tn.it | not WordPress (410) | ⏭️ Skip |
| Bolzano | provincia.bz.it | not WordPress | ⏭️ Skip |
| Valle d'Aosta | regione.vda.it | not WordPress | ⏭️ Skip |

**Reality check:** the clean WP-REST-with-content pattern that makes LazioInnova
easy is the exception, not the norm — most regional agency portals are bespoke
sites or have no public open-bandi API. Adding a region therefore usually means a
new adapter (CKAN/Socrata like `lombardia`, or HTML scraping), not a one-line
config. Both are welcome.

## Adding a WordPress region (config-only)

If a region's agency runs WordPress and exposes bandi with full content over the
WP REST API (`/wp-json/wp/v2/<type>`), it's a config entry — no new logic:

```python
# in a new sources/<region>.py
from bandiradar.sources.base import register
from bandiradar.sources.wordpress import WordPressBandiSource

register(WordPressBandiSource(
    id="<region>",
    region="<Region name>",
    data_url="https://<portal>/wp-json/wp/v2/<bandi-type>",
    issuer_name="<Agency>",
    kind="incentive",                       # or "tender"
    keyword_taxonomies=("tema-", "destinatari-"),  # the portal's taxonomy slugs
))
```

Then record a real `data/fixtures/<region>.json` (~10–15 records) and add a test —
see the `add-a-source` skill for the full template.
