"""osm-mapping domain — Facetwork workflows + handlers for an OSM mapping-equity map.

Builds a world country choropleth of mapped health-facility density per capita
(an OSM under-mapping / digital-divide signal). Discovered by the Facetwork
runner via the ``facetwork.domains`` entry point in pyproject.toml::

    [project.entry-points."facetwork.domains"]
    osm-mapping = "osm_mapping:domain"
"""

from __future__ import annotations

from pathlib import Path

from facetwork.domains import DomainPackage

from .handlers import register_all_registry_handlers

# The per-country Overpass fan-out is a long one-time job; give it room before
# the global execution-timeout watchdog fires (counts are cached afterwards).
domain = DomainPackage(
    name="osm-mapping",
    ffl_dir=Path(__file__).parent / "ffl",
    register_handlers=register_all_registry_handlers,
    runner_env={
        "AFL_TASK_EXECUTION_TIMEOUT_MS": "2700000",  # 45 min
        "AFL_STUCK_TIMEOUT_MS": "3000000",
    },
)
