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

## FFL at a glance

The domain is driven from [FFL](https://github.com/rlemke/facetwork/blob/main/docs/reference/language/grammar.md),
Facetwork's workflow language. A step is `name = Facet(args)`; steps that
reference each other are ordered, steps that don't run in parallel — so one cached
fetch can feed three renders at once:

```ffl
namespace my.osm_mapping {

    use osm_mapping.sources
    use osm_mapping.maps

    /** One Osmose fetch → three tag-quality renders in parallel. */
    workflow TagQualityFamily(force: Boolean = false) => (world: String, states: String, counties: String) andThen {

        issues = osm_mapping.sources.FetchTagIssues(force = $.force)

        world = osm_mapping.maps.BuildTagQualityWorld(dependency_signal = issues.leaf_count)
        states = osm_mapping.maps.BuildTagQualityUsStates(dependency_signal = issues.leaf_count)
        counties = osm_mapping.maps.BuildTagQualityUsCounties(dependency_signal = issues.leaf_count)

        yield TagQualityFamily(
            world = world.html_path,
            states = states.html_path,
            counties = counties.html_path)
    }
}
```

```bash
fw ffl run --primary my.ffl --library src/osm_mapping/ffl/osm_mapping.ffl \
  --workflow my.osm_mapping.TagQualityFamily
```

📖 **[docs/ffl-examples.md](docs/ffl-examples.md)** — the full example gallery:
`dependency_signal` sequencing, one-fetch-many-renders, call-time mixins for
throttled endpoints, `catch`, `when` guards against partial Overpass counts, and
publishing several maps in one commit. Every snippet there is compile-checked.

## Feature specifications

Every feature has a spec in [**`docs/`**](docs/README.md) — how it works,
whether/how it **fans out** (these are single-task + in-process thread pools, not
fleet fan-outs), what it **fetches** and on which **attributes**, the **external
libraries/services** it uses, its **facets & workflows**, and its **cache/output**.
Start with the flagship [**Under-Mapping**](docs/under-mapping.md) map; the full
index is in [`docs/README.md`](docs/README.md).

| Feature | Spec |
|---------|------|
| **Under-mapping** (flagship) — world health facilities per capita | [docs/under-mapping.md](docs/under-mapping.md) |
| **US health-mapping** — state+county per capita (spatial join) | [docs/us-health-mapping.md](docs/us-health-mapping.md) |
| **Tag-quality** — Osmose attribute-misuse maps (world / US) | [docs/tag-quality.md](docs/tag-quality.md) |
| **Packaging & storage** (cross-cutting) — dispatch, timeouts, MinIO paths | [docs/packaging-and-storage.md](docs/packaging-and-storage.md) |

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
