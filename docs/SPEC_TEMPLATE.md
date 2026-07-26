<!-- SPEC TEMPLATE — every docs/<feature>.md follows this shape so the set reads
consistently. Delete this comment in real specs. Keep sections in this order;
omit a section only if it genuinely does not apply (say so in one line rather
than dropping the heading silently). Ground every claim in the actual FFL
docstrings / handler code (_lib.py / _us.py / _tagquality.py) / storage.py — do
not invent behaviour. -->

# <Feature Name>

**Namespace(s):** `osm_mapping.<ns>` · **FFL:** `src/osm_mapping/ffl/osm_mapping.ffl` ·
**Handlers:** `src/osm_mapping/handlers/osm_mapping_handlers.py` ·
**Impl:** `src/osm_mapping/<module>.py`

## Overview
One or two paragraphs: what this feature is for, the request it answers, and where
it sits in the pipeline (Overpass/Osmose fetch → cache → join onto geometry →
render → publish).

## How it works
The algorithm / data flow, step by step. Name the concrete functions and the shape
of the data at each (Overpass `area` count → `{iso2: count}` JSON → join onto
Natural Earth features → MapLibre HTML, etc.). If the count and the render are two
separate facets sequenced by a `dependency_signal`, say so.

## Fan-out
Does it fan out across the fleet? **Be precise:** this domain's "per-country" /
"per-leaf" work is an **in-process `ThreadPoolExecutor`** inside a *single* handler
task — it is NOT a distributed FFL `foreach` fan-out across runners. Say which
(single-task + in-process pool, or true fleet fan-out) and why.

## Data & fields
What it fetches and on which attributes — be specific (`amenity=hospital` /
`amenity=clinic` via Overpass `area["ISO3166-1"=..]`, Osmose items `9002`
deprecated + `3040` incorrect tag, Natural Earth `NAME`/`POP_EST`/`ISO_A2_EH`,
TIGER `GEOID`/`STATEFP`/`ALAND`). Name the join key and the normalised metric
(per million, per 100k, per 1,000 km²). If it does no attribute filtering, say so.

## External libraries / binaries
Every non-stdlib dependency this feature relies on and what for — `requests`
(Overpass/Osmose HTTP), `shapely` (geometry, STRtree spatial join), `pyproj`
(geodesic area), `boto3` (listing the census-us MinIO cache). Distinguish a
**pip** dependency from an external **HTTP service** (Overpass mirror, Osmose QA
API, Natural Earth raw GitHub).

## Facets & workflows
The key event facets and workflows, with signatures and a one-line purpose taken
from the FFL `/** … */` docstrings. Mark event facets (need a handler) vs pure,
and note the `Effect`/`Cost`/`Timeout` mixins that are present on every facet here.

## Cache / output
The cache namespace under the backend-aware root (`storage.cache_root()` →
`$FW_DATA_ROOT/cache/osm-mapping/cache/` on the fleet) and the cached artifact(s)
(`facility-counts.json`, `world-countries.geojson`, `us-facilities.json`,
`osm-tagquality/osmose_leaf_counts.json`), plus the rendered output(s) under
`storage.output_root()` (per-map subdirs) and their format (self-contained
MapLibre HTML + a `.geojson`). Note local vs MinIO/S3.

## Gotchas & notes
Known pitfalls, rate limits, sensitivity caveats, honest confounders (the
mapping-completeness-vs-provision caveat; Osmose analyzer coverage; per-area bias),
and anything a future maintainer would trip on (stale tests, cold census cache).

## Related specs
Links to the specs this feature composes with or depends on.
