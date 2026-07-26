# US Health-Facility Mapping per Capita (state + county)

**Namespace(s):** `osm_mapping.maps` (`BuildUsMap`) · `osm_mapping.workflows`
(`BuildUsHealthMappingMap`) ·
**FFL:** `src/osm_mapping/ffl/osm_mapping.ffl` ·
**Handlers:** `src/osm_mapping/handlers/osm_mapping_handlers.py`
(`handle_build_us_map`) ·
**Impl:** `src/osm_mapping/_us.py`

## Overview

A United-States **zoom** of the world under-mapping map, at TIGER county
resolution with a **state/county level toggle**. It maps mapped health facilities
(`amenity=hospital`/`clinic`) per 100,000 people, again with **dark = fewer per
capita = more under-mapped**. Where the world map does a country-level attribute
join, this one does a real **spatial join** of facility points onto county polygons,
then dissolves counties up to states.

It is a **consumer of the census-us domain's cache**: it reuses the already-cached
TIGER county geometry + ACS population in the shared MinIO store rather than
re-fetching TIGER/ACS, so a census map must have been built first (or the run
raises with a clear message).

## How it works

Two functions in `_us.py`, fronted by one event facet (`BuildUsMap`):

1. **`download_us_facilities(force=False)`** — **one** US-wide Overpass fetch of
   `amenity=hospital`/`clinic` (`US_FACILITIES_QUERY`, `out center`), bounded to the
   US `area["ISO3166-1"="US"][admin_level=2]`, returning `[[lon, lat], ...]`
   (~32k points; per-county Overpass queries would be 3,000+ and infeasible).
   Cached as `us-facilities.json`.
2. **`build_us_map(force=False)`** — assembles every county from the census-us cache
   (`_assemble_counties`: TIGER geometry + ACS `population` + `GEOID`/`STATEFP`),
   builds a shapely **`STRtree`** over county geometries, and spatial-joins each
   facility point (`tree.query` + `covers`) to its county. It sums county counts up
   to states (`by_state_*`), computes facilities-per-100k at both levels, dissolves
   county polygons to state polygons (`unary_union`), simplifies geometry for the
   browser, and renders a dual-layer MapLibre choropleth with a level toggle.

Data shape: `US Overpass points + census-us county GeoJSON → per-county counts
(STRtree join) → per-state rollup → simplified dual-layer FeatureCollection →
MapLibre HTML`.

## Fan-out

**Single distributed task — no fan-out.** No `foreach`; one Overpass fetch and one
in-process spatial join per `BuildUsMap` invocation. The whole US is a single fetch
by design ("per-county Overpass queries would be 3,000+ and infeasible"). Like the
world count, it is registered `timeout_ms=0` and relies on the raised global
execution timeout.

## Data & fields

- **Source filter (Overpass):** `amenity=hospital` + `amenity=clinic`, `out center`,
  scoped to the US `area`. Only the centroid `[lon, lat]` is kept.
- **Geometry + population (census-us cache):** per-state `metrics.geojson` under
  `cache/census-us/output/metrics/<state>/metrics.geojson`. Each county feature
  yields `GEOID`, `NAME`, `STATEFP`, and population from `population` (falling back
  to the raw ACS field `B01003_001E`).
- **Metric:** `m_per_100k = round(n / (pop / 100_000), 1)` per county and per state,
  plus `m_facilities` (count) and `m_population`. Same 90th-percentile clamp +
  purple `OUTLIER` idiom as the world map (the `RAMP`/`NODATA`/`OUTLIER` constants
  are imported straight from `_lib`).

## External libraries / binaries

- **`requests`** (pip) — the US Overpass fetch.
- **`shapely`** (pip, `>=2.0`) — `shape`, `Point`, `STRtree` (spatial join),
  `unary_union` (county→state dissolve), `simplify`/`mapping` (browser-light
  geometry). This is the geometry engine the world map doesn't need.
- **`boto3`** (via the `[s3]`-style path) — `_list_census_states` /
  `_assemble_counties` list the per-state census files in the MinIO bucket
  (`s3.get_paginator("list_objects_v2")`) when the data root is remote; a local data
  root falls back to an `os.listdir` scan.
- **External HTTP services:** Overpass (same two-endpoint list as `_lib`). Geometry
  and population come entirely from the **census-us MinIO cache**, not a fresh
  TIGER/ACS download.

## Facets & workflows

| Facet / workflow | Kind | Effect / Cost / Timeout | Purpose (from FFL docstring) |
|---|---|---|---|
| `maps.BuildUsMap(force=false) => (html_path, facility_count, county_count, state_count)` | event | external / expensive / 20 min | US zoom: fetch US health facilities (Overpass), spatial-join onto census county geometry, render a state+county per-capita choropleth with a level toggle. Reuses the census-us cached county GeoJSON. |
| `workflows.BuildUsHealthMappingMap(force=false) => (status, html_path, facility_count, county_count)` | workflow | — | `map = BuildUsMap(force)` → yield. |

`BuildUsMap` → `handle_build_us_map`, a thin wrapper over `build_us_map` that logs
`facility_count -> county_count / state_count`.

## Cache / output

- **Cache** (`storage.cache_root()`): `us-facilities.json` (the `[[lon,lat],…]`
  point list). Reads (does not write) the census-us cache under
  `cache/census-us/output/metrics/`.
- **Output** (`storage.output_root()` + `us/`): `counties.geojson` and `index.html`.
  Its own `us/` subdir (same "don't drag siblings into the publish prefix" reason
  as the world map).
- Render: dual GeoJSON sources (state + county) inlined into one self-contained
  MapLibre HTML; a radio `state`/`county` level toggle swaps the source data and
  recolours; region search across the active level; per-area popup; "About this
  data" modal; provenance footer citing `BuildUsHealthMappingMap`.

## Gotchas & notes

- **Cold census cache = hard failure.** `build_us_map` raises
  `"no county geometry found in the census-us cache … run a census map first"` if
  `_assemble_counties` returns nothing. This map has a hard dependency on the
  census-us domain having populated MinIO; it is not self-contained.
- **County-name matching is downstream's problem, not this map's** — this map joins
  spatially (points-in-polygon), so it does not depend on county-name slugs. (The
  tag-quality US-county map *does* slug-match names and inherits that fragility;
  see [tag-quality](tag-quality.md).)
- **Same honest caveat** as the world map: per-capita blends completeness with real
  provision; the map text says "under-mapped *or* underserved".
- **~3,143 counties inlined** — geometry is simplified (`COUNTY_SIMPLIFY=0.01`,
  `STATE_SIMPLIFY=0.02`) specifically to keep the single-file HTML browser-fast.

## Related specs

- [under-mapping](under-mapping.md) — the world version of the same question
  (country attribute join instead of a point-in-polygon spatial join).
- [tag-quality](tag-quality.md) — reuses `_us._assemble_counties` for its US-county
  map, and the same census-us cache dependency.
- [packaging-and-storage](packaging-and-storage.md) — the census-us cache path
  resolution, `boto3` bucket listing, and handler/timeout wiring.
