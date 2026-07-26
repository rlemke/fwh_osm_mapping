# Under-Mapping — World Health Facilities per Capita

**Namespace(s):** `osm_mapping.sources` (`CountFacilities`) · `osm_mapping.maps`
(`BuildMappingMap`) · `osm_mapping.workflows` (`BuildMappingEquityMap`) ·
**FFL:** `src/osm_mapping/ffl/osm_mapping.ffl` ·
**Handlers:** `src/osm_mapping/handlers/osm_mapping_handlers.py`
(`handle_count_facilities`, `handle_build_mapping_map`) ·
**Impl:** `src/osm_mapping/_lib.py` ·
**Tests:** `tests/test_osm_mapping.py`

## Overview

The flagship feature: a **world country choropleth of mapped health-facility
density per capita** in OpenStreetMap. For every country it counts OSM
`amenity=hospital` + `amenity=clinic`, normalises by population, and colours the map
so **dark = fewer per capita = more under-mapped**. It answers "where is OSM
under-mapped relative to population?" — a recognised OSM "digital divide" /
mapping-inequality signal.

It is deliberately the **no-API-key, reliable** version of that question. The
rigorous metric ("total OSM element density per capita") needs a full-history
backend (HeiGIT OSHDB / ohsome), which the public ohsome API can't serve at
whole-world scale in reasonable time (README "Honest caveat"). This feature trades
that rigour for a single, cheap, cacheable feature class.

Pipeline position: **Overpass count → cache → join onto Natural Earth geometry →
MapLibre HTML**. The count (`CountFacilities`) and the render (`BuildMappingMap`)
are two separate event facets, sequenced in the workflow by passing the count's
`country_count` as `BuildMappingMap`'s `dependency_signal`.

## How it works

Two functions in `_lib.py`, each fronted by one event facet:

1. **`download_facility_counts(force=False)`** — returns `{iso2: facility_count}`.
   It loads the Natural Earth world GeoJSON (`_world_geojson`), collects each
   country's ISO2 (`_country_iso2`, trying `ISO_A2_EH` → `ISO_A2` → `WB_A2` and
   rejecting the Natural Earth sentinels `-99` / `-1` / `""`), and for each ISO2
   issues one Overpass **`out count`** query bounded to that country's
   `area["ISO3166-1"=<iso2>"][admin_level=2]` (`_overpass_count`). Per-country
   failures return `None` and are simply omitted from the result (a gap, not a
   crash). The `{iso2: count}` aggregate is written to `facility-counts.json`; a
   non-`force` re-run reads that cache and never touches Overpass.
2. **`build_map(force=False)`** — joins the aggregate onto the Natural Earth
   features by ISO2, reads `POP_EST`, computes
   `per_million = round(fac / (pop / 1e6), 1)`, and emits a `FeatureCollection`
   with `m_facilities` / `m_population` / `m_per_million` per country. It renders a
   self-contained MapLibre choropleth (`_render_html`) and writes both the GeoJSON
   and `index.html`.

Data shape: `world GeoJSON + {iso2:count} → per-country FeatureCollection
(m_* props) → MapLibre HTML`. `BuildMappingMap` returns
`country_count = len(world features)` and `matched = # countries that had a count`.

## Fan-out

**Single distributed task, in-process thread pool — NOT a fleet `foreach`
fan-out.** There is no `foreach` in the FFL; the whole world is counted inside one
`CountFacilities` handler invocation, which parallelises the ~200 per-country
Overpass queries with a `concurrent.futures.ThreadPoolExecutor(max_workers=3)`
(`MAX_WORKERS = 3`, "polite concurrency against Overpass"). So the fleet sees a
single long-running task, not one task per country. This is why the domain raises
`FW_TASK_EXECUTION_TIMEOUT_MS` to 45 min and registers the handler with
`timeout_ms=0` (see [packaging-and-storage](packaging-and-storage.md)) — the pool
blocks with no heartbeat. The README's "region-fan-out shape" describes the
*bounded-per-country-query* idea (each query is scoped to one country's `area`),
not a distributed fan-out across runners.

## Data & fields

- **Source filter (Overpass):** `FACILITY_FILTER =
  '(nwr["amenity"="hospital"](area.a);nwr["amenity"="clinic"](area.a);)'` — nodes,
  ways and relations, `out count` (centroids not needed for a count). The country
  `area` is selected by `area["ISO3166-1"="<iso2>"][admin_level=2]`.
- **Geometry + population:** Natural Earth `ne_110m_admin_0_countries.geojson`
  (raw GitHub, `WORLD_GEOJSON_URL`). Join key is ISO2; population is `POP_EST`;
  label is `NAME`.
- **Metric:** `per_million` (primary, `worse="low"` → low is dark), plus context
  columns `facilities` (count) and `population` (estimate). Defined in the `METRICS`
  list of `Metric` dataclasses; the JS legend/colour honour `worse` to reverse the
  ramp for the primary metric.

## External libraries / binaries

- **`requests`** (pip) — the only network client, for Overpass POSTs and the
  Natural Earth GeoJSON GET. Imported defensively (`requests = None` if missing);
  the count/geometry helpers raise `RuntimeError` when it's absent.
- **No `shapely` / `pyproj` here** — the world map does a pure attribute join on
  ISO2 and inlines the Natural Earth geometry verbatim, so it needs no geometry
  library. (Those are used by the US map and the tag-quality maps instead.)
- **External HTTP services (not pip deps):** the **Overpass API** — main instance
  `overpass-api.de` first, `overpass.kumi.systems` as fallback (`OVERPASS_ENDPOINTS`;
  main-first because kumi-first was observed timing out and falling back ~10× slower)
  — and **raw.githubusercontent.com** for Natural Earth. The rendered HTML also
  pulls MapLibre GL (unpkg) and CARTO Voyager basemap tiles at view time.

## Facets & workflows

All facets are **event** facets (each needs a handler) and carry
`Effect`/`Cost`/`Timeout` mixins verbatim from the FFL:

| Facet / workflow | Kind | Effect / Cost / Timeout | Purpose (from FFL docstring) |
|---|---|---|---|
| `sources.CountFacilities(force=false) => (country_count: Int)` | event | external / expensive / 45 min | Count OSM health facilities (`amenity=hospital`/`clinic`) per country via Overpass and cache the per-country aggregate. |
| `maps.BuildMappingMap(dependency_signal=0) => (html_path, geojson_path, country_count, matched)` | event | io / cheap / 10 min | Join the cached counts onto Natural Earth geometry, compute per-million from population, render the world choropleth. `dependency_signal` sequences it after the count. |
| `workflows.BuildMappingEquityMap(force=false) => (status, html_path, country_count, matched)` | workflow | — | `counts = CountFacilities(force)` → `map = BuildMappingMap(dependency_signal = counts.country_count)` → yield. |

Handler wiring: `CountFacilities` → `handle_count_facilities` (returns
`{"country_count": len(counts)}`), `BuildMappingMap` → `handle_build_mapping_map`
(returns `html_path`, `geojson_path`, `country_count`, `matched`). Both are thin
wrappers over `_lib` that also emit `_step_log` success/error lines.

## Cache / output

- **Cache** (`storage.cache_root()`): `facility-counts.json` (the `{iso2:count}`
  aggregate) and `world-countries.geojson` (the downloaded Natural Earth file, so
  re-renders never re-download). On the fleet these live in MinIO under
  `cache/osm-mapping/cache/`; locally under `<data-root>/osm-mapping-cache/`.
- **Output** (`storage.output_root()` + `under-mapping/`): `osm-mapping.geojson`
  and `index.html`. **Note the dedicated `under-mapping/` subdir** — a comment in
  `build_map` records that using the bare `output_root()` made publishing this
  map's prefix drag every sibling map (US, tag-quality) into the destination, so
  each map family writes to its own subdir.
- Render specifics: YlOrRd `RAMP` reversed for the primary (low=dark); the scale is
  **clamped at the 90th percentile** so extreme outliers (e.g. Greenland ~600/M
  from its tiny population) don't compress the ramp — those high outliers get a
  distinct purple `OUTLIER = "#5e3c99"`. Self-contained HTML with a metric dropdown,
  legend, country search box, click-for-values popup, an "About this data" modal,
  and a provenance footer linking the FFL + source repo.

## Gotchas & notes

- **Honest caveat (baked into the map text):** a single feature class blends
  *mapping completeness* with *real-world provision* — a low value means a country
  is under-*mapped* **or** under-*served*. The map's side panel and modal both say
  so; don't present it as a pure data-quality verdict.
- **US slowness drove `PER_COUNTRY_TIMEOUT = 300`s.** The US alone took ~244s and
  Germany ~93s; an earlier 150s cap dropped the US to no-data (blank). Keep the
  per-country timeout above the slowest country or heavily-mapped countries go grey.
- **ISO2 sentinels.** Natural Earth uses `-99` / `-1` for missing ISO codes;
  `_country_iso2` rejects them and falls back across three fields — a country with
  all-sentinel codes silently gets no count.
- **Rate limiting:** `_overpass_count` backs off on HTTP 429 (`sleep 5 + attempt*5`)
  and retries `RETRIES=2` across both endpoints; `MAX_WORKERS=3` keeps concurrency
  polite. Don't raise the worker count.

## Related specs

- [us-health-mapping](us-health-mapping.md) — the US zoom of this exact question at
  TIGER county/state resolution (one US-wide Overpass fetch + a shapely spatial
  join, not per-country counts).
- [tag-quality](tag-quality.md) — the sibling "mis-mapping" (attribute-misuse)
  choropleths; shares the renderer idiom and the honest-caveat framing.
- [packaging-and-storage](packaging-and-storage.md) — the `DomainPackage` wiring,
  the backend-aware cache/output paths, the `runner_env` timeout bump, and the
  handler dispatch table these facets are served through.
