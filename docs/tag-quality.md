# Tag-Quality (Attribute-Misuse) Maps — Osmose QA by region

**Namespace(s):** `osm_mapping.sources` (`FetchTagIssues`) · `osm_mapping.maps`
(`BuildTagQualityWorld`, `BuildTagQualityUsStates`, `BuildTagQualityUsCounties`) ·
`osm_mapping.workflows` (the three `BuildTagQuality…Map` workflows) ·
**FFL:** `src/osm_mapping/ffl/osm_mapping.ffl` ·
**Handlers:** `src/osm_mapping/handlers/osm_mapping_handlers.py`
(`handle_fetch_tag_issues`, `_tq(...)` factory) ·
**Impl:** `src/osm_mapping/_tagquality.py`

## Overview

The sibling of under-mapping. Under-mapping asks "where is data *missing*";
tag-quality asks "where are the tags actually used **wrong** — deviating from
current valid conventions" (mis-mapping). It builds choropleths of **Osmose QA**
issue density so a mapper can prioritise cleanup by region. Three maps: world by
country, US by state, US by county. Darker = denser misuse.

The signal is two Osmose issue classes: item **9002 "deprecated"** (tags valid but
superseded by newer conventions, a global analyzer) + item **3040 "incorrect tag"**
(tag/value combinations flagged as wrong). **This feature queries only the Osmose QA
API — it never downloads OSM data** (no planet, no extracts, no Overpass).

## How it works

One cached fetch (`FetchTagIssues`) feeds all three maps:

1. **`fetch_osmose_counts(force=False)`** — Osmose exposes issues per **leaf**
   region only (country/state *parents* return 0: `usa_california`=0 but
   `usa_california_los_angeles`=N). So `leaf_codes()` enumerates the ~1,190 leaf
   codes from `/api/0.3/countries` (a code is a leaf if no other code starts with
   `code_`), and for each leaf × each item it takes the **count = `len` of the
   capped issue list** (`/issues?item=&country=<leaf>&limit=LIMIT`,
   `LIMIT=100000`; truncation is logged — there is no count endpoint). Result:
   `{leaf_code: {"dep": n, "inc": m}}`, cached and reused **with no TTL** unless
   `force=true`.
2. **`build_world` / `build_us_states` / `build_us_counties`** — each reads the
   cached leaf counts, maps every leaf code to a region (country / US state / US
   county), **aggregates the leaf counts upward** to that region, computes density,
   and renders a MapLibre choropleth (`_render`).

Leaf→region mapping is the hard part:
- **World:** `_country_of` matches a leaf's longest country prefix to a Natural
  Earth `NAME` (`_country_prefixes` builds `{normalised NAME: NAME}`, plus an
  explicit `COUNTRY_ALIASES` table for the ~30 roots that don't normalise cleanly —
  `usa` → "United States of America", `ivory_coast`/`cote_d_ivoire` → "Côte d'Ivoire",
  etc.).
- **US state/county:** `_us_state_county` splits `usa_<state>_<county>` using the
  known TIGER state-slug set (state slugs can be multi-word, e.g. `new_york`), so
  the state slug is matched longest-first.

## Fan-out

**Single distributed task, in-process thread pool — no fleet fan-out.**
`fetch_osmose_counts` parallelises the ~1,190 leaf fetches with a
`ThreadPoolExecutor(max_workers=6)` (`FETCH_WORKERS = 6`), inside one
`FetchTagIssues` handler task. Each map build is likewise a single task. Same
`timeout_ms=0` + raised global timeout as the other facets.

## Data & fields

- **Source (Osmose QA API only):** items `9002` (deprecated) + `3040`
  (incorrect tag) from `osmose.openstreetmap.fr/api/0.3`. No OSM element download.
- **Geometry:** world → Natural Earth `ne_110m_admin_0_countries.geojson`; US
  state/county → the census-us TIGER cache (`output/tiger/state/us_state.geojson`
  for states; `_us._assemble_counties` for counties). US state area prefers TIGER
  `ALAND` (→ km²), else geodesic.
- **Metric:** counts are normalised **per 1,000 km²** (geodesic area via
  `pyproj.Geod(ellps="WGS84").geometry_area_perimeter`): `m_total_density`
  (dep+inc), `m_dep_density`, `m_inc_density`, plus `m_total_abs` (absolute count)
  as a secondary metric. Geometry is simplified per map (world `0.08`, state `0.02`,
  county `0.01`) and coordinates rounded to 3 dp (`_gj`) to shrink the inlined
  GeoJSON.

## External libraries / binaries

- **`requests`** (pip) — all Osmose + Natural Earth HTTP.
- **`shapely`** (pip) — `shape`, `simplify`, `mapping` for geometry prep.
- **`pyproj`** (pip, `>=3.4`) — geodesic area for the per-1,000-km² normalisation
  (`_GEOD.geometry_area_perimeter`). Distinct from the world under-mapping map,
  which needs neither.
- **`boto3` / census-us cache** — the US state/county maps read TIGER geometry from
  the shared census-us MinIO cache (`_read_census_json`, `_us._assemble_counties`).
- **External HTTP services:** the **Osmose QA API** and **raw.githubusercontent.com**
  (Natural Earth); MapLibre + CARTO basemap at view time.

## Facets & workflows

All event facets; `Effect`/`Cost`/`Timeout` mixins are verbatim from the FFL.

| Facet / workflow | Kind | Effect / Cost / Timeout | Purpose (from FFL docstring) |
|---|---|---|---|
| `sources.FetchTagIssues(force=false) => (leaf_count: Int)` | event | external / expensive / 60 min | Fetch + cache Osmose counts (9002 deprecated + 3040 incorrect) for every leaf region worldwide; one small aggregate all tag-quality maps read from. |
| `maps.BuildTagQualityWorld(dependency_signal=0, force=false) => (region, html_path, feature_count, detail)` | event | io / moderate / 15 min | World choropleth by country, per 1,000 km². |
| `maps.BuildTagQualityUsStates(…) => (region, html_path, feature_count, detail)` | event | io / moderate / 15 min | US choropleth by state. |
| `maps.BuildTagQualityUsCounties(…) => (region, html_path, feature_count, detail)` | event | io / moderate / 20 min | US choropleth by county (reuses census-us county geometry). |
| `workflows.BuildTagQualityWorldMap` / `…UsStatesMap` / `…UsCountiesMap` | workflow | — | `issues = FetchTagIssues(force)` → `map = BuildTagQuality<scope>(dependency_signal = issues.leaf_count)` → yield. |

Handler wiring: `FetchTagIssues` → `handle_fetch_tag_issues`
(`{"leaf_count": len(counts)}`); the three map facets share a `_tq(fn, label)`
factory that adapts `build_world`/`build_us_states`/`build_us_counties` (returning
`region`/`html_path`/`feature_count`/`detail`).

## Cache / output

- **Cache** (`storage.cache_root()`): `osm-tagquality/osmose_leaf_counts.json`
  (`CACHE_REL`) — the `{leaf: {dep, inc}}` aggregate. **Cache-first with no TTL**:
  reused even if stale unless `force=true`.
- **Output** (`storage.output_root()` + `osm-tagquality/<world|us-states|us-counties>/`):
  one `index.html` per map (`_write`). Self-contained MapLibre HTML with a metric
  dropdown, region search, legend, "About this data" modal, and an attribution
  block citing Osmose + Natural Earth/TIGER.

## Gotchas & notes

- **Honest confounders (on the map).** Osmose analyzer coverage varies by region,
  AND older/heavily-mapped areas carry more deprecated tags simply because they
  were tagged years ago; per-area normalisation favours small dense regions. The
  `_NOTE` text calls this a **cleanup-prioritisation aid, not a quality verdict** —
  keep that framing.
- **The US-county map is held in practice.** `BuildTagQualityUsCounties` exists and
  is wired, but Osmose only subdivides ~1 US state to county level, so a national
  county map is mostly empty (README). County matching also slug-matches names
  (strip `County`/`Parish`/`Borough`/…), which is more fragile than the state
  match.
- **Truncation is silent-ish.** A leaf whose issue list hits `LIMIT=100000` is
  logged as truncated but still counted at the cap — extreme leaves are undercounted.
- **Stale offline tests (repo-wide, grounded in the code).** `tests/test_osm_mapping.py`
  only covers the under-mapping map; `test_dispatch_keys` asserts `_DISPATCH ==
  {CountFacilities, BuildMappingMap, BuildUsMap}` and `test_register_handlers_blocking`
  asserts `register_handler.call_count == 3`, but the actual `_DISPATCH` has **7**
  entries (the four tag-quality/`FetchTagIssues` facets were added after those
  tests). Those two assertions are out of date with the shipped dispatch table —
  the tag-quality facets are served but **untested**. See
  [packaging-and-storage](packaging-and-storage.md).

## Related specs

- [under-mapping](under-mapping.md) — the "missing data" sibling (this is the
  "wrong data" sibling); shares the renderer idiom and caveat framing.
- [us-health-mapping](us-health-mapping.md) — provides `_assemble_counties`, reused
  by the US-county tag-quality map.
- [packaging-and-storage](packaging-and-storage.md) — dispatch/registration, the
  census-us cache path resolution, and the stale-test note in full.
