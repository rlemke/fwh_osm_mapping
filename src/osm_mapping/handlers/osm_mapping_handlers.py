"""Event facet handlers for the osm-mapping domain — thin layers over ``_lib``."""

from __future__ import annotations

import os
from typing import Any

from .._lib import build_map, download_facility_counts

SRC = "osm_mapping.sources"
MAPS = "osm_mapping.maps"


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


_DISPATCH: dict[str, Any] = {
    f"{SRC}.CountFacilities": handle_count_facilities,
    f"{MAPS}.BuildMappingMap": handle_build_mapping_map,
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
