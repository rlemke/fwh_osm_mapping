# Packaging, Handler Wiring & Backend-Aware Storage

**Cross-cutting.** ·
**Domain package:** `src/osm_mapping/__init__.py` ·
**Handlers/dispatch:** `src/osm_mapping/handlers/__init__.py`,
`src/osm_mapping/handlers/osm_mapping_handlers.py` ·
**Storage:** `src/osm_mapping/storage.py` ·
**Entry point:** `pyproject.toml` (`facetwork.domains`)

## Overview

This is the plumbing every map feature rides on: how the domain is discovered by a
runner, how facet names are dispatched to handlers, the timeout tuning that keeps
the long blocking fetches alive, and the backend-aware cache/output paths that put
artifacts on local disk in dev and in shared MinIO on the fleet. It also documents
the **shared census-us cache dependency** and one **honest wiring gap** (stale
tests) that a maintainer will otherwise trip on.

## How it works

**Discovery.** `pyproject.toml` declares
`[project.entry-points."facetwork.domains"] osm-mapping = "osm_mapping:domain"`.
`__init__.py` builds a `DomainPackage(name="osm-mapping", ffl_dir=…/ffl,
register_handlers=register_all_registry_handlers, runner_env={…})`. `fw runner
start --domain osm-mapping` and `fw ffl seed` find it through that entry point;
the FFL in `ffl/` is the capability surface.

**Dispatch.** `osm_mapping_handlers.py` holds a single `_DISPATCH` table keyed by
fully-qualified facet name → handler callable, and a `handle(payload)` RegistryRunner
entrypoint that looks up `payload["_facet_name"]`. `register_handlers(runner)`
registers **every** key in `_DISPATCH` with `module_uri=file://…`,
`entrypoint="handle"`, and `timeout_ms=0`. The current table has **7** entries:

| Facet | Handler |
|---|---|
| `osm_mapping.sources.CountFacilities` | `handle_count_facilities` |
| `osm_mapping.maps.BuildMappingMap` | `handle_build_mapping_map` |
| `osm_mapping.maps.BuildUsMap` | `handle_build_us_map` |
| `osm_mapping.sources.FetchTagIssues` | `handle_fetch_tag_issues` |
| `osm_mapping.maps.BuildTagQualityWorld` | `_tq(build_world, …)` |
| `osm_mapping.maps.BuildTagQualityUsStates` | `_tq(build_us_states, …)` |
| `osm_mapping.maps.BuildTagQualityUsCounties` | `_tq(build_us_counties, …)` |

`register_all_registry_handlers` (RegistryRunner) and `register_all_handlers`
(AgentPoller, via `register_poller`) are the two public registration shims.

**Timeout tuning.** The per-country / per-leaf fetches are long blocking I/O with no
heartbeat, so handlers register `timeout_ms=0` (opt out of the heartbeat watchdog)
and the `DomainPackage.runner_env` raises the *global* execution timeout:
`FW_TASK_EXECUTION_TIMEOUT_MS = 2700000` (45 min) and `FW_STUCK_TIMEOUT_MS =
3000000`. `fw runner start` applies these automatically. This is the correct pattern
for blocking handlers (CLAUDE.md "Runner resilience tuning").

## Fan-out

Not applicable — this is wiring, not a workflow. See each map spec; note that all
three families are **single-task + in-process `ThreadPoolExecutor`**, not fleet
`foreach` fan-outs.

## Data & fields

**Backend-aware paths (`storage.py`).** A thin wrapper over
`facetwork.runtime.storage`, the same shape census-us / conflict / save-earth use:

- `_data_root()` = `FW_DATA_ROOT` or `get_output_base()`.
- `cache_root()` = `FW_OSM_MAPPING_CACHE_DIR` override, else
  `<root>/cache/osm-mapping/cache` when the root is remote (`://`), else
  `<root>/osm-mapping-cache` locally.
- `output_root()` = `FW_OSM_MAPPING_OUTPUT_DIR` override, else
  `<root>/cache/osm-mapping/output` (remote) / `<root>/osm-mapping-output` (local).
- `is_remote` / `exists` / `localize` / `open_read` / `open_write` abstract
  local-fs vs object store. `open_write` on a remote path **stages to a temp file
  and finalises on close** into the backend (object stores don't do partial
  writes) — matching the platform's "stage-local, finalize-on-close" rule.

**Shared census-us cache.** The US maps read (never write) census-us artifacts under
the *data root*, not osm-mapping's own cache: `cache/census-us/output/metrics/<state>/metrics.geojson`
(county geometry+ACS) and `output/tiger/state/us_state.geojson` (states).
`_us._list_census_states` lists per-state files via **`boto3`** paginator when the
root is remote, else scans the local directory.

## External libraries / binaries

- **`facetwork`** — `DomainPackage`, `facetwork.config.get_output_base`,
  `facetwork.runtime.storage` (the platform, not a pip extra).
- **`requests`, `shapely>=2.0`, `pyproj>=3.4`** — the declared `pyproject.toml`
  dependencies (used by the map modules, not this wiring layer).
- **`boto3`** — imported lazily inside `_us` for MinIO bucket listing; not a hard
  declared dependency (present via the fleet's `[s3]` extra).

## Facets & workflows

None declared here — this layer *serves* the facets declared in
`ffl/osm_mapping.ffl` (see [under-mapping](under-mapping.md),
[us-health-mapping](us-health-mapping.md), [tag-quality](tag-quality.md)). The FFL
file's three namespaces (`sources`, `maps`, `workflows`) map 1:1 onto the dispatch
table above plus the six workflows.

## Cache / output

Covered above: one MinIO/local root, `cache/osm-mapping/cache/` for cached inputs
and `cache/osm-mapping/output/<subdir>/` for rendered maps, with each map family in
its **own output subdir** (`under-mapping/`, `us/`,
`osm-tagquality/<world|us-states|us-counties>/`) so publishing one map's prefix
doesn't drag the siblings along.

## Gotchas & notes

- **Stale offline tests — the dispatch table has drifted past its tests.**
  `tests/test_osm_mapping.py::test_dispatch_keys` asserts
  `set(_DISPATCH) == {CountFacilities, BuildMappingMap, BuildUsMap}` (3 entries) and
  `test_register_handlers_blocking` asserts `register_handler.call_count == 3`, but
  the shipped `_DISPATCH` has **7** entries (the tag-quality + `FetchTagIssues`
  facets were added later without updating the tests). As written, both assertions
  fail against current code, and the four newer facets have **no test coverage**.
  Treat the tests as covering only the under-mapping map. *(Docs are read-only here;
  this is reported, not fixed.)*
- **`runner_env` only takes effect via `fw runner start`** — running a handler
  directly won't inherit the 45-min timeout, so a bare invocation can hit the
  default watchdog on the slow US count.
- **The US / tag-quality-US maps hard-depend on the census-us cache** being warm in
  the same data root; a cold cache is a clean `RuntimeError` (US map) or an empty
  choropleth (tag-quality county). Build a census map first.
- **Overrides:** `FW_OSM_MAPPING_CACHE_DIR` / `FW_OSM_MAPPING_OUTPUT_DIR` fully
  replace the derived cache/output roots — useful for a one-off local render off the
  fleet.

## Related specs

- [under-mapping](under-mapping.md) · [us-health-mapping](us-health-mapping.md) ·
  [tag-quality](tag-quality.md) — the three feature families this layer serves.
