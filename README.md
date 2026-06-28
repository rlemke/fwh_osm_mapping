# osm-mapping

A standalone [Facetwork](https://github.com/rlemke/facetwork) domain package that
builds an **OSM mapping-equity world map** — mapped **health-facility density per
capita** — surfacing where OpenStreetMap is **under-mapped** relative to
population (a recognised OSM "digital divide" / mapping-inequality signal).

- **Source** — `osm_mapping.sources.CountFacilities`: for each country (Natural
  Earth admin-0), a bounded Overpass `area` **count** of `amenity=hospital` /
  `amenity=clinic`. Runs a small, polite thread pool over an Overpass mirror,
  tolerant of per-country failures, and caches the tiny aggregate so re-renders
  never re-query Overpass. (Per-country counts, not one global query — a single
  global health-facility fetch is too large to be reliable; a count bounded to
  one country's `area` is fast. This is the platform's region-fan-out shape.)
- **Map** — `osm_mapping.maps.BuildMappingMap`: joins the counts onto Natural
  Earth geometry by ISO2, computes facilities-per-million from `POP_EST`, and
  renders a self-contained MapLibre choropleth (metric dropdown, legend, country
  search, click-for-values, provenance footer). **Dark = fewer per capita = more
  under-mapped.**
- **Workflow** — `osm_mapping.workflows.BuildMappingEquityMap`.
- **Storage** — cache + output follow `FW_STORAGE` (`local` / `hdfs` / `s3`);
  on the fleet they land in the shared MinIO at `cache/osm-mapping/`.

## OSM tag-quality (attribute-misuse) maps

The sibling of under-mapping ("where data is *missing*") is **mis-mapping**:
where the tags actually used on OSM entities **deviate from current valid
conventions**. `_tagquality.py` builds choropleths of that, to find cleanup
priorities by region:

- **Source** — `osm_mapping.sources.FetchTagIssues`: counts of **Osmose QA**
  issues — item **9002 "deprecated"** (tags superseded by newer conventions) +
  **3040 "incorrect tag"** — for every Osmose **leaf** region (only leaves carry
  issues; country/state parents return 0), aggregated upward. **This queries the
  Osmose QA API only — it never downloads OSM data** (no planet, no extracts, no
  Overpass) — and caches one small aggregate JSON; it is **cache-first with no
  TTL** (reuses the cache even if stale unless `force=true`).
- **Maps** — `BuildTagQualityWorld` (by country, Natural Earth) and
  `BuildTagQualityUsStates` (by state, TIGER), normalised **per 1,000 km²**
  (geodesic area), with absolute counts as a secondary metric and a region
  search. Workflows: `BuildTagQualityWorldMap` / `BuildTagQualityUsStatesMap`.
  (`BuildTagQualityUsCounties` exists but Osmose only subdivides ~1 US state to
  county level, so a national county map is mostly empty — held.)
- **Honest caveat** — Osmose analyzer coverage *and* OSM mapping age both
  confound a raw read (older areas carry more deprecated tags simply because they
  were tagged years ago), and per-area favours small dense regions. A
  **cleanup-prioritisation aid, not a quality verdict.**

## Honest caveat

A single feature class blends **mapping completeness** with **real-world
provision**: a low value means a country is under-*mapped* **or** under-*served*.
This is the reliable, no-API-key version. The rigorous "total OSM element density
per capita" metric needs a full-history backend (HeiGIT's OSHDB / ohsome), which
the public ohsome API can't serve at whole-world scale in reasonable time.

## Layout

```
src/osm_mapping/
├── __init__.py        # DomainPackage (facetwork.domains entry point) + runner_env
├── _lib.py            # download_facility_counts (Overpass fan-out) + build_map + render
├── storage.py         # backend-aware cache/output paths (MinIO on the fleet)
├── ffl/osm_mapping.ffl # CountFacilities / BuildMappingMap facets + BuildMappingEquityMap workflow
└── handlers/          # thin event-facet dispatchers over _lib
```

Data: OpenStreetMap contributors (via Overpass API, ODbL); country geometry +
population from Natural Earth (public domain).
