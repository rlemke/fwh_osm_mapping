# osm-mapping — Feature Specifications

This directory holds one **spec per feature** of the `osm-mapping` domain. Each
document follows a common shape ([`SPEC_TEMPLATE.md`](SPEC_TEMPLATE.md)) and states,
for that feature: how it works, whether and how it **fans out** (spoiler: these are
single-task + in-process thread pools, *not* fleet `foreach` fan-outs), what it
**fetches** and on which **attributes** (Overpass `amenity=hospital/clinic`, Osmose
items `9002`/`3040`, Natural Earth / TIGER fields), the **external
libraries/services** it relies on, its **facets & workflows**, and its
**cache/output**. Claims are grounded in the FFL `/** … */` docstrings
(`src/osm_mapping/ffl/osm_mapping.ffl`), the handler dispatch
(`handlers/osm_mapping_handlers.py`), and the three impl modules
(`_lib.py` / `_us.py` / `_tagquality.py`) — the source of truth for each facet
remains its FFL docstring; these specs are the feature-level narrative over them.

**Start here:** [**Under-Mapping**](under-mapping.md) — the flagship map (world
health-facility density per capita; "dark = more under-mapped") and the deepest
write-up.

## Maps (the domain's three feature families)

| Spec | What it covers |
|------|----------------|
| [under-mapping.md](under-mapping.md) | **Flagship.** World country choropleth of mapped health facilities (`amenity=hospital/clinic`) per capita via Overpass per-country `out count` → Natural Earth join → MapLibre HTML; `CountFacilities` + `BuildMappingMap` + `BuildMappingEquityMap`. |
| [us-health-mapping.md](us-health-mapping.md) | US zoom at TIGER county/state resolution: one US-wide Overpass fetch → shapely `STRtree` point-in-polygon join → county→state dissolve → dual-layer choropleth with a level toggle; `BuildUsMap` + `BuildUsHealthMappingMap`. Reuses the census-us cache. |
| [tag-quality.md](tag-quality.md) | The "mis-mapping" sibling: Osmose QA attribute-misuse choropleths (deprecated + incorrect tags per 1,000 km²) for world / US-state / US-county; one cached `FetchTagIssues` leaf fetch feeds three `BuildTagQuality…` maps. |

## Cross-cutting

| Spec | What it covers |
|------|----------------|
| [packaging-and-storage.md](packaging-and-storage.md) | The `DomainPackage` entry point, the 7-entry handler dispatch table, the `runner_env` timeout tuning (`timeout_ms=0` + 45-min global), the backend-aware cache/output paths (local vs MinIO), the shared census-us cache dependency, and the **stale-test** honesty note. |
| [ffl-examples.md](ffl-examples.md) | **Usage patterns.** A gallery of complete, compile-checked FFL examples over these facets — `after` sequencing, one fetch feeding parallel renders, call-time mixins, `catch`, `when` guards, multi-prefix publish. |

---

*See also the repo [`README.md`](../README.md) (domain overview + honest caveats)
and the FFL capability surface at
[`src/osm_mapping/ffl/osm_mapping.ffl`](../src/osm_mapping/ffl/osm_mapping.ffl).
The live/queryable interface is the MCP `fw_capabilities` /
`fw_describe_handler` tools.*
