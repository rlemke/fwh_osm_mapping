"""Offline tests for the osm-mapping domain (no network)."""

import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

SRC = str(Path(__file__).resolve().parent.parent / "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from osm_mapping import _lib  # noqa: E402
from osm_mapping.handlers import osm_mapping_handlers as oh  # noqa: E402


_WORLD = {
    "type": "FeatureCollection",
    "features": [
        {"type": "Feature",
         "geometry": {"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 0]]]},
         "properties": {"NAME": "Germany", "ISO_A2_EH": "DE", "POP_EST": 83_000_000}},
        {"type": "Feature",
         "geometry": {"type": "Polygon", "coordinates": [[[2, 2], [3, 2], [3, 3], [2, 2]]]},
         "properties": {"NAME": "Chad", "ISO_A2_EH": "TD", "POP_EST": 16_000_000}},
    ],
}


def test_metric_registry():
    keys = [m.key for m in _lib.METRICS]
    assert keys == ["per_million", "facilities", "population"]
    # the primary metric flips the colour ramp (low = under-mapped = dark)
    assert _lib.METRICS[0].worse == "low"
    assert all(m.fmt in ("count", "rate") for m in _lib.METRICS)


def test_dispatch_keys():
    assert set(oh._DISPATCH) == {
        "osm_mapping.sources.CountFacilities",
        "osm_mapping.maps.BuildMappingMap",
        "osm_mapping.maps.BuildUsMap",
    }


def test_register_handlers_blocking():
    runner = MagicMock()
    oh.register_handlers(runner)
    assert runner.register_handler.call_count == 3
    # long blocking fan-out → registered with timeout_ms=0
    for c in runner.register_handler.call_args_list:
        assert c.kwargs.get("timeout_ms") == 0


def test_country_iso2_fallback():
    assert _lib._country_iso2({"ISO_A2": "KE"}) == "KE"
    assert _lib._country_iso2({"ISO_A2": "-99", "ISO_A2_EH": "FR"}) == "FR"
    assert _lib._country_iso2({"ISO_A2": "-99"}) is None


def test_build_map_joins_and_renders(monkeypatch, tmp_path):
    monkeypatch.setenv("AFL_STORAGE", "local")
    monkeypatch.setenv("AFL_DATA_ROOT", str(tmp_path))
    # stub the two network calls
    monkeypatch.setattr(_lib, "_world_geojson", lambda: json.loads(json.dumps(_WORLD)))
    monkeypatch.setattr(_lib, "download_facility_counts",
                        lambda *, force=False: {"DE": 5000, "TD": 300})

    res = _lib.build_map()
    assert res.country_count == 2 and res.matched == 2

    from osm_mapping import storage as cstore
    with cstore.open_read(res.output_path) as f:
        fc = json.load(f)
    by_name = {ft["properties"]["NAME"]: ft["properties"] for ft in fc["features"]}
    # Germany: 5000 / 83M people = ~60.2 per million; Chad: 300/16M = ~18.8
    assert by_name["Germany"]["m_facilities"] == 5000
    assert by_name["Germany"]["m_per_million"] == round(5000 / 83, 1)
    assert by_name["Chad"]["m_per_million"] == round(300 / 16, 1)
    # Chad is more under-mapped (fewer per capita) than Germany
    assert by_name["Chad"]["m_per_million"] < by_name["Germany"]["m_per_million"]

    with cstore.open_read(res.html_path) as f:
        html = f.read()
    for probe in ('id="rsin"', "colorExpr", "source repo", "dark = fewer per capita",
                  "amenity=hospital", "m_per_million"):
        assert probe in html, probe
    # the primary metric reverses the ramp for low=dark
    assert "m.worse==='low'" in html
