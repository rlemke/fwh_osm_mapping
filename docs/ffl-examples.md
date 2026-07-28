# FFL Examples — `osm-mapping`

Every numbered scenario is a **complete, compilable FFL file**. Copy one into
`my.ffl` and run it:

```bash
fw ffl run --primary my.ffl \
  --library ~/fw_handlers/fwh_osm_mapping/src/osm_mapping/ffl/osm_mapping.ffl \
  --workflow my.osm_mapping.<WorkflowName>
```

A runner serving the `osm_mapping` namespace must be up
(`fw runner start --domain osm-mapping`). Every block below is compile-checked
against `src/osm_mapping/ffl/osm_mapping.ffl`.

New to the language? Start with the
[FFL grammar](https://github.com/rlemke/facetwork/blob/main/docs/reference/language/grammar.md)
and the [canonical examples](https://github.com/rlemke/facetwork/tree/main/examples/canonical).

---

## The facets at a glance

Two **source** facets that fetch-and-cache, and four **map** facets that render off
those caches. That split is the pattern to copy: expensive external fetch once,
cheap renders many times.

| Declaration | Signature | Does |
|---|---|---|
| `osm_mapping.sources.CountFacilities` | `(force = false) => (country_count: Int)` | Per-country Overpass health-facility counts → cached aggregate |
| `osm_mapping.sources.FetchTagIssues` | `(force = false) => (leaf_count: Int)` | Osmose QA tag-issue counts for every leaf region → one cached aggregate |
| `osm_mapping.maps.BuildMappingMap` | `(dependency_signal: Int = 0) => (html_path, geojson_path, country_count, matched)` | Facilities-per-million world choropleth |
| `osm_mapping.maps.BuildUsMap` | `(force = false) => (html_path, facility_count, county_count, state_count)` | US state+county per-capita choropleth |
| `osm_mapping.maps.BuildTagQualityWorld` | `(dependency_signal = 0, force = false) => (region, html_path, feature_count, detail)` | Tag-quality by country |
| `osm_mapping.maps.BuildTagQualityUsStates` | same shape | Tag-quality by US state |
| `osm_mapping.maps.BuildTagQualityUsCounties` | same shape | Tag-quality by US county |

Workflows: `BuildMappingEquityMap`, `BuildUsHealthMappingMap`,
`BuildTagQualityWorldMap`, `BuildTagQualityUsStatesMap`,
`BuildTagQualityUsCountiesMap`.

> ⚠️ Both source facets hit rate-limited public endpoints (Overpass, Osmose).
> Fetch **once** into the shared cache and render from it — do not fan a fetch out
> across the fleet.

---

## 1. Run what ships — no FFL to write

```bash
fw ffl seed --include osm-mapping

fw ffl run --primary ~/fw_handlers/fwh_osm_mapping/src/osm_mapping/ffl/osm_mapping.ffl \
  --workflow osm_mapping.workflows.BuildMappingEquityMap \
  --inputs '{"force": false}'
```

Write FFL when you want a different *shape* of run — one fetch feeding several
renders, your own error handling, or publishing the family in one commit.

## 2. The smallest workflow you can write

Every FFL workflow needs a `namespace`, a `use` per namespace it calls into, and a
`yield` back to itself.

```ffl
namespace my.osm_mapping {

    use osm_mapping.sources
    use osm_mapping.maps

    /** Count facilities, then render the world equity map. */
    workflow MyEquityMap() => (html_path: String, countries: Int) andThen {

        counts = osm_mapping.sources.CountFacilities(force = false)

        map = osm_mapping.maps.BuildMappingMap(dependency_signal = counts.country_count)

        yield MyEquityMap(html_path = map.html_path, countries = map.country_count)
    }
}
```

Rules visible above: `=>` sits on the **same line** as the closing `)`; references
are always `step.field`; `$.x` reads the container's parameters.

## 3. Sequencing steps that share no data

`BuildMappingMap` reads the *cache* that `CountFacilities` wrote — it needs no
value from it. Steps with no reference between them may run in **any** order (and
concurrently), so the dependency is made explicit by passing an upstream field into
`dependency_signal`:

```ffl
namespace my.osm_mapping {

    use osm_mapping.sources
    use osm_mapping.maps

    /** Ordering comes from the reference, not from line order. */
    workflow OrderedEquityMap(force: Boolean = false) => (html_path: String) andThen {

        counts = osm_mapping.sources.CountFacilities(force = $.force)

        // referencing counts.country_count is what makes this run second
        map = osm_mapping.maps.BuildMappingMap(dependency_signal = counts.country_count)

        yield OrderedEquityMap(html_path = map.html_path)
    }
}
```

## 4. One fetch, three renders — fan out from a shared cache

All three tag-quality maps read the same Osmose aggregate. Fetch it once, then let
the three renders run **concurrently** (they reference the fetch, but not each
other).

```ffl
namespace my.osm_mapping {

    use osm_mapping.sources
    use osm_mapping.maps

    /** One Osmose fetch → three renders in parallel. */
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

Each render waits on `issues` (it references it) but not on its siblings — so the
three land on three runners at once.

## 5. Call-time mixins — timeouts and retries

`CountFacilities` ships `with Timeout(minutes = 45)` and `FetchTagIssues`
`with Timeout(minutes = 60)`. The **call site** can override for one particular use.

```ffl
namespace my.osm_mapping {

    use osm_mapping.sources
    use osm_mapping.maps

    /** Overpass throttles: be patient and back off hard between attempts. */
    workflow PatientCounts() => (html_path: String) andThen {

        counts = osm_mapping.sources.CountFacilities(force = true) with Timeout(minutes = 180) with Retry(maxAttempts = 3, backoffSeconds = 300)

        map = osm_mapping.maps.BuildMappingMap(dependency_signal = counts.country_count) with Timeout(minutes = 20)

        yield PatientCounts(html_path = map.html_path)
    }
}
```

## 6. Render from the cache even if the refresh fails — `catch`

`catch` runs when its step errors after retries are exhausted. Because the renders
read the cache rather than the fetch's return value, a failed refresh doesn't have
to mean no map.

```ffl
namespace my.osm_mapping {

    use osm_mapping.sources
    use osm_mapping.maps

    /** Refresh best-effort; if Overpass is down, report it. */
    workflow BestEffortEquityMap() => (status: String, html_path: String) andThen {

        counts = osm_mapping.sources.CountFacilities(force = true) catch {
            yield BestEffortEquityMap(status = "overpass_failed", html_path = "")
        }

        map = osm_mapping.maps.BuildMappingMap(dependency_signal = counts.country_count)

        yield BestEffortEquityMap(status = "completed", html_path = map.html_path)
    }
}
```

## 7. Branch on a result — `when`

A `when` block hangs off the step it inspects: inside a case `$` is that step and
`$$` reaches the workflow. Every `when` needs a default case, last.

```ffl
namespace my.osm_mapping {

    use osm_mapping.sources
    use osm_mapping.maps

    /** A thin country count means Overpass throttled us — don't publish that map. */
    workflow GuardedEquityMap(min_countries: Int = 150) => (status: String, html_path: String) andThen {

        counts = osm_mapping.sources.CountFacilities() andThen when {
            case $.country_count >= $$.min_countries => {
                map = osm_mapping.maps.BuildMappingMap(dependency_signal = $.country_count)
                yield GuardedEquityMap(status = "completed", html_path = map.html_path)
            }
            case _ => {
                yield GuardedEquityMap(status = "throttled_partial_counts", html_path = "")
            }
        }
    }
}
```

## 8. Compose across domains — publish the family in one commit

`census.Publish` is the generic publisher the map domains share; one call can push
several prefixes in a single commit.

```ffl
namespace my.osm_mapping {

    use osm_mapping.maps
    use census.Publish

    /** Render two maps, publish both at once. */
    workflow MappingPublish(repo: String = "rlemke/facetwork-maps") => (pages_url: String, files: Long) andThen {

        world = osm_mapping.maps.BuildMappingMap(dependency_signal = 0)
        us = osm_mapping.maps.BuildUsMap(force = false)

        published = census.Publish.PublishWebBundle(
            repo = $.repo,
            prefixes = ["osm_mapping/output/world", "osm_mapping/output/us"],
            dests = ["osm/mapping-equity", "osm/mapping-equity-us"],
            labels = ["OSM mapping equity (world)", "OSM mapping equity (US)"],
            landing_title = "Facetwork maps")

        yield MappingPublish(pages_url = published.pages_url, files = published.file_count)
    }
}
```

Compile that one with `--library ~/fw_handlers/fwh_census_us/src/census_us/ffl/census.ffl`
as well.

---

## Cheat sheet

| You want to… | Write |
|---|---|
| Read a workflow/step parameter | `$.name` (`$$.name` one level out) |
| Read a previous step's result | `stepname.field` |
| Order two independent steps | pass an upstream field as `dependency_signal` |
| Run steps in parallel | write them with no reference between them |
| More time / retries for one call | `… with Timeout(minutes = 180) with Retry(maxAttempts = 3, backoffSeconds = 300)` |
| Handle a step failure | `step = Facet(…) catch { yield … }` |
| Branch | `step = Facet(…) andThen when { case <bool> => { … } case _ => { … } }` |
| Fan out over a list | `workflow W(items: Json) … andThen foreach i in $.items { … }` |
| Concatenate strings | `a ++ b` |

**Validate before you run:** `afl my.ffl --check` or MCP `fw_validate`. Every error
carries a `rule_id` — fetch `fw://docs/rules/{rule_id}` for a wrong/right pair.

## See also

- [`docs/README.md`](README.md) — per-feature specs for this domain
- [FFL grammar](https://github.com/rlemke/facetwork/blob/main/docs/reference/language/grammar.md) ·
  [canonical examples](https://github.com/rlemke/facetwork/tree/main/examples/canonical) ·
  [relative `$`-scoping](https://github.com/rlemke/facetwork/blob/main/docs/architecture/ffl-relative-scoping.md)
- `src/osm_mapping/ffl/osm_mapping.ffl` — the source of truth for every signature above
