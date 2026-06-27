"""Event facet handlers for the osm-mapping domain — thin layers over ``_lib``."""

from __future__ import annotations

import os
from typing import Any

from .._lib import build_map, download_facility_counts
from .._us import build_us_map
from .._tagquality import (
    build_us_counties,
    build_us_states,
    build_world,
    fetch_osmose_counts,
)

SRC = "osm_mapping.sources"
MAPS = "osm_mapping.maps"


def _tq(fn, label):
    def handler(params: dict[str, Any]) -> dict[str, Any]:
        step_log = params.get("_step_log")
        try:
            res = fn(force=bool(params.get("force")))
            if step_log:
                step_log(f"{label}: {res.detail} -> {res.html_path}", level="success")
            return {"region": res.region, "html_path": res.html_path,
                    "feature_count": res.feature_count, "detail": res.detail}
        except Exception as exc:
            if step_log:
                step_log(f"{label}: {exc}", level="error")
            raise
    return handler


def handle_fetch_tag_issues(params: dict[str, Any]) -> dict[str, Any]:
    """Fetch + cache Osmose tag-quality counts for every leaf region."""
    step_log = params.get("_step_log")
    try:
        counts = fetch_osmose_counts(force=bool(params.get("force")))
        if step_log:
            step_log(f"FetchTagIssues: {len(counts)} leaf regions cached", level="success")
        return {"leaf_count": len(counts)}
    except Exception as exc:
        if step_log:
            step_log(f"FetchTagIssues: {exc}", level="error")
        raise


def handle_count_facilities(params: dict[str, Any]) -> dict[str, Any]:
    """Count OSM health facilities per country (Overpass) + cache the aggregate."""
    step_log = params.get("_step_log")
    try:
        counts = download_facility_counts(force=bool(params.get("force")))
        if step_log:
            step_log(f"CountFacilities: {len(counts)} countries with data", level="success")
        return {"country_count": len(counts)}
    except Exception as exc:
        if step_log:
            step_log(f"CountFacilities: {exc}", level="error")
        raise


def handle_build_mapping_map(params: dict[str, Any]) -> dict[str, Any]:
    """Join the facility aggregate onto world geometry + render the choropleth."""
    step_log = params.get("_step_log")
    try:
        res = build_map()
        if step_log:
            step_log(
                f"BuildMappingMap: {res.matched}/{res.country_count} countries "
                f"-> {res.html_path}",
                level="success",
            )
        return {
            "html_path": res.html_path,
            "geojson_path": res.output_path,
            "country_count": res.country_count,
            "matched": res.matched,
        }
    except Exception as exc:
        if step_log:
            step_log(f"BuildMappingMap: {exc}", level="error")
        raise


def handle_build_us_map(params: dict[str, Any]) -> dict[str, Any]:
    """Build the US state+county health-facility-per-capita map (Overpass fetch +
    spatial join onto census-us county geometry)."""
    step_log = params.get("_step_log")
    try:
        res = build_us_map(force=bool(params.get("force")))
        if step_log:
            step_log(
                f"BuildUsMap: {res.facility_count} facilities -> "
                f"{res.county_count} counties / {res.state_count} states "
                f"-> {res.html_path}",
                level="success",
            )
        return {
            "html_path": res.html_path,
            "facility_count": res.facility_count,
            "county_count": res.county_count,
            "state_count": res.state_count,
        }
    except Exception as exc:
        if step_log:
            step_log(f"BuildUsMap: {exc}", level="error")
        raise


_DISPATCH: dict[str, Any] = {
    f"{SRC}.CountFacilities": handle_count_facilities,
    f"{MAPS}.BuildMappingMap": handle_build_mapping_map,
    f"{MAPS}.BuildUsMap": handle_build_us_map,
    f"{SRC}.FetchTagIssues": handle_fetch_tag_issues,
    f"{MAPS}.BuildTagQualityWorld": _tq(build_world, "BuildTagQualityWorld"),
    f"{MAPS}.BuildTagQualityUsStates": _tq(build_us_states, "BuildTagQualityUsStates"),
    f"{MAPS}.BuildTagQualityUsCounties": _tq(build_us_counties, "BuildTagQualityUsCounties"),
}


def handle(payload: dict) -> dict:
    """RegistryRunner entrypoint."""
    return _DISPATCH[payload["_facet_name"]](payload)


def register_handlers(runner) -> None:
    for facet_name in _DISPATCH:
        runner.register_handler(
            facet_name=facet_name,
            module_uri=f"file://{os.path.abspath(__file__)}",
            entrypoint="handle",
            # The per-country Overpass fan-out is long blocking I/O (no heartbeat);
            # rely on the global execution timeout (raised via runner_env).
            timeout_ms=0,
        )


def register_poller(poller) -> None:
    for facet_name, handler in _DISPATCH.items():
        poller.register(facet_name, handler)
