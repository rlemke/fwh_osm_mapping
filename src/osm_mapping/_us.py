"""US sub-national OSM mapping-equity map — health facilities per capita by
state AND county.

A United-States zoom of the world under-mapping map, at TIGER county resolution:

1. ``download_us_facilities`` — ONE US-wide Overpass fetch of
   ``amenity=hospital``/``clinic`` centroids (coords only — ~32k points, bounded
   to the US ``area``; per-county Overpass queries would be 3,000+ and infeasible).
   Cached.
2. ``build_us_map`` — assemble all counties from the census-us cache (TIGER
   geometry + ACS ``population`` + ``GEOID``/``STATEFP``, already in MinIO),
   spatial-join the facility points onto counties with a shapely STRtree, sum
   counties → states, compute facilities-per-100k at both levels, dissolve
   counties → state polygons, simplify geometry for the browser, and render a
   MapLibre choropleth with a **state/county level toggle** (dark = fewer per
   capita = more under-mapped; high outliers in a distinct colour).

Reuses the census-us domain's cached county GeoJSON (shared MinIO) so it needs no
TIGER/ACS re-fetch — run a census map first if the cache is cold.
"""

from __future__ import annotations

import json
import logging
import time
from collections import defaultdict
from dataclasses import dataclass

from . import storage as cstore
from ._lib import NODATA, OUTLIER, RAMP

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None

logger = logging.getLogger("osm-mapping.us")

USER_AGENT = "facetwork-osm-mapping/1.0 (+https://github.com/rlemke/facetwork)"
OVERPASS_ENDPOINTS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
)
US_FACILITIES_QUERY = (
    '[out:json][timeout:600];area["ISO3166-1"="US"][admin_level=2]->.a;'
    '(nwr["amenity"="hospital"](area.a);nwr["amenity"="clinic"](area.a););out center;'
)
# census-us cached per-state county GeoJSON (TIGER geometry + ACS population).
CENSUS_METRICS_PREFIX = "cache/census-us/output/metrics"
FFL_URL = "https://github.com/rlemke/fwh_osm_mapping/blob/main/src/osm_mapping/ffl/osm_mapping.ffl"

# Geometry simplification tolerances (degrees) — shrink the inlined GeoJSON so the
# ~3,143-county map loads fast in the browser without visibly degrading shapes.
COUNTY_SIMPLIFY = 0.01
STATE_SIMPLIFY = 0.02


@dataclass
class UsMapResult:
    output_path: str
    html_path: str
    facility_count: int
    county_count: int
    state_count: int


# ---------------------------------------------------------------------------
# Download US facility centroids.
# ---------------------------------------------------------------------------


def download_us_facilities(*, force: bool = False) -> list[list[float]]:
    """Return ``[[lon, lat], ...]`` for every US hospital/clinic, cached as JSON."""
    cache_key = cstore.join(cstore.cache_root(), "us-facilities.json")
    if not force and cstore.exists(cache_key):
        with cstore.open_read(cache_key) as f:
            return json.load(f)
    if requests is None:
        raise RuntimeError("requests is required to query Overpass")

    els = None
    for attempt in range(3):
        for ep in OVERPASS_ENDPOINTS:
            try:
                logger.info("US facilities fetch via %s (attempt %d)", ep, attempt + 1)
                r = requests.post(
                    ep, data={"data": US_FACILITIES_QUERY},
                    headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
                    timeout=(30, 650),
                )
                if r.status_code == 504 or r.status_code == 429:
                    time.sleep(5 + attempt * 5)
                    continue
                r.raise_for_status()
                els = r.json().get("elements") or []
                break
            except Exception as exc:
                logger.warning("US fetch %s failed: %s", ep, exc)
                time.sleep(5 + attempt * 5)
        if els is not None:
            break
    if els is None:
        raise RuntimeError("US facilities fetch failed on all endpoints/retries")

    pts: list[list[float]] = []
    for e in els:
        if e.get("lat") is not None and e.get("lon") is not None:
            pts.append([float(e["lon"]), float(e["lat"])])
        elif isinstance(e.get("center"), dict):
            c = e["center"]
            if c.get("lat") is not None and c.get("lon") is not None:
                pts.append([float(c["lon"]), float(c["lat"])])
    with cstore.open_write(cache_key, "w") as f:
        json.dump(pts, f)
    logger.info("US facilities: %d points cached", len(pts))
    return pts


# ---------------------------------------------------------------------------
# Assemble counties from the census-us cache.
# ---------------------------------------------------------------------------


def _assemble_counties() -> list[dict]:
    """Read every census-us per-state county GeoJSON → county records with
    geometry + population + GEOID + state name."""
    import boto3  # the census cache lives in MinIO; list per-state files

    # List state dirs under the census metrics prefix.
    root = cstore.cache_root()  # e.g. s3://afl-cache/cache/osm-mapping/cache
    counties: list[dict] = []
    # Read each state file via the storage layer (handles s3/local).
    states = _list_census_states()
    for state in states:
        path = _census_metrics_path(state)
        if not cstore.exists(path):
            continue
        with cstore.open_read(path) as f:
            fc = json.load(f)
        for ft in fc.get("features") or []:
            p = ft.get("properties") or {}
            pop = p.get("population") or p.get("B01003_001E")
            counties.append({
                "geoid": p.get("GEOID"),
                "name": p.get("NAME"),
                "state": state,
                "statefp": p.get("STATEFP"),
                "pop": float(pop) if pop else None,
                "geometry": ft.get("geometry"),
            })
    return counties


def _list_census_states() -> list[str]:
    """State names = the per-state dirs under the census metrics prefix."""
    import facetwork.runtime.storage as _fws  # noqa
    import boto3
    import os
    # Resolve the bucket from FW_DATA_ROOT (s3://bucket); local falls back to a scan.
    data_root = cstore._data_root()
    if cstore.is_remote(data_root):
        bucket = data_root.split("://", 1)[1].split("/", 1)[0]
        ep = os.environ.get("FW_S3_ENDPOINT")
        s3 = boto3.client(
            "s3", endpoint_url=ep,
            aws_access_key_id=os.environ.get("FW_S3_ACCESS_KEY"),
            aws_secret_access_key=os.environ.get("FW_S3_SECRET_KEY"),
        )
        states = set()
        for pg in s3.get_paginator("list_objects_v2").paginate(
            Bucket=bucket, Prefix=CENSUS_METRICS_PREFIX + "/"
        ):
            for o in pg.get("Contents", []):
                if o["Key"].endswith("/metrics.geojson"):
                    states.add(o["Key"].split("/")[-2])
        return sorted(states)
    # local backend: scan the directory
    base = cstore.join(data_root, CENSUS_METRICS_PREFIX)
    if os.path.isdir(base):
        return sorted(
            d for d in os.listdir(base)
            if os.path.exists(os.path.join(base, d, "metrics.geojson"))
        )
    return []


def _census_metrics_path(state: str) -> str:
    data_root = cstore._data_root()
    return cstore.join(data_root, CENSUS_METRICS_PREFIX, state, "metrics.geojson")


# ---------------------------------------------------------------------------
# Build map.
# ---------------------------------------------------------------------------


def build_us_map(*, force: bool = False) -> UsMapResult:
    from shapely.geometry import shape, Point
    from shapely.ops import unary_union
    from shapely.strtree import STRtree

    pts = download_us_facilities(force=force)
    counties = _assemble_counties()
    if not counties:
        raise RuntimeError(
            "no county geometry found in the census-us cache "
            f"({CENSUS_METRICS_PREFIX}) — run a census map first"
        )

    geoms = [shape(c["geometry"]) for c in counties]
    tree = STRtree(geoms)
    t0 = time.time()
    counts: dict[int, int] = defaultdict(int)
    for lon, lat in pts:
        pt = Point(lon, lat)
        for i in tree.query(pt):
            if geoms[i].covers(pt):
                counts[i] += 1
                break
    logger.info("spatial-joined %d facilities to counties in %.1fs", len(pts), time.time() - t0)

    # Per-county features (simplified geometry).
    county_feats = []
    by_state_count: dict[str, int] = defaultdict(int)
    by_state_pop: dict[str, float] = defaultdict(float)
    by_state_geoms: dict[str, list] = defaultdict(list)
    for i, c in enumerate(counties):
        n = counts.get(i, 0)
        pop = c["pop"]
        per100k = round(n / (pop / 100_000), 1) if (pop and pop > 0) else None
        simp = geoms[i].simplify(COUNTY_SIMPLIFY, preserve_topology=True)
        county_feats.append({
            "type": "Feature",
            "geometry": _geom_to_dict(simp),
            "properties": {
                "NAME": c["name"], "state": c["state"],
                "m_facilities": n, "m_population": int(pop) if pop else None,
                "m_per_100k": per100k,
            },
        })
        if c["state"]:
            by_state_count[c["state"]] += n
            if pop:
                by_state_pop[c["state"]] += pop
            by_state_geoms[c["state"]].append(geoms[i])

    # Per-state features (dissolve counties → state polygon, simplified).
    state_feats = []
    for state, gs in by_state_geoms.items():
        pop = by_state_pop.get(state) or 0
        n = by_state_count.get(state, 0)
        per100k = round(n / (pop / 100_000), 1) if pop > 0 else None
        union = unary_union(gs).simplify(STATE_SIMPLIFY, preserve_topology=True)
        state_feats.append({
            "type": "Feature",
            "geometry": _geom_to_dict(union),
            "properties": {
                "NAME": state,
                "m_facilities": n, "m_population": int(pop) if pop else None,
                "m_per_100k": per100k,
            },
        })

    county_fc = {"type": "FeatureCollection", "features": county_feats}
    state_fc = {"type": "FeatureCollection", "features": state_feats}
    html = _render_html(state_fc, county_fc)

    out_dir = cstore.join(cstore.output_root(), "us")
    html_path = cstore.join(out_dir, "index.html")
    with cstore.open_write(cstore.join(out_dir, "counties.geojson"), "w") as f:
        json.dump(county_fc, f, separators=(",", ":"))
    with cstore.open_write(html_path, "w") as f:
        f.write(html)
    return UsMapResult(html_path, html_path, len(pts), len(county_feats), len(state_feats))


def _geom_to_dict(geom) -> dict:
    import shapely.geometry as sg
    return sg.mapping(geom)


# ---------------------------------------------------------------------------
# Render — dual-layer (state/county) choropleth with a level toggle.
# ---------------------------------------------------------------------------


def _attribution() -> str:
    from datetime import UTC, datetime
    ts = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    call = "osm_mapping.workflows.BuildUsHealthMappingMap"
    repo = FFL_URL.split("/blob/")[0]
    from html import escape
    return (
        '<div style="position:fixed;bottom:10px;left:10px;z-index:9999;'
        "background:rgba(255,255,255,0.92);border-radius:6px;padding:6px 10px;"
        "box-shadow:0 1px 4px rgba(0,0,0,0.2);font:11px system-ui,sans-serif;color:#444;"
        'max-width:460px">Generated by Facetwork workflow '
        '<code style="background:#f0f0f0;padding:0 3px;border-radius:3px">'
        f"{escape(call)}</code> &middot; "
        f'<a href="{escape(FFL_URL)}" target="_blank" rel="noopener" '
        'style="color:#1565c0;text-decoration:none">view FFL</a>'
        f' &middot; <a href="{escape(repo)}" target="_blank" rel="noopener" '
        'style="color:#1565c0;text-decoration:none">source repo</a>'
        f" &middot; generated {ts}</div>"
    )


def _render_html(state_fc: dict, county_fc: dict) -> str:
    state_js = json.dumps(state_fc, separators=(",", ":"))
    county_js = json.dumps(county_fc, separators=(",", ":"))
    ramp_js = json.dumps(RAMP)
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>US OSM mapping equity - health facilities per capita</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<link href="https://unpkg.com/maplibre-gl@4.7.1/dist/maplibre-gl.css" rel="stylesheet">
<script src="https://unpkg.com/maplibre-gl@4.7.1/dist/maplibre-gl.js"></script>
<style>
  html,body,#map{{margin:0;height:100%;width:100%;font-family:system-ui,sans-serif}}
  .panel{{position:absolute;z-index:1;background:rgba(255,255,255,.94);padding:10px 12px;
    border-radius:6px;box-shadow:0 1px 4px rgba(0,0,0,.3);font-size:12px}}
  #ctl{{top:10px;left:10px;max-width:340px}}
  #ctl h3{{margin:0 0 6px;font-size:14px}}
  #legend{{bottom:18px;right:10px}} #legend .scale{{display:flex;margin-top:4px}}
  #legend .scale div{{display:flex;flex-direction:column;align-items:center;font-size:10px}}
  #legend .scale span{{width:30px;height:12px}}
  .maplibregl-popup-content{{max-width:300px;font-size:12px}}
  .maplibregl-popup-content h4{{margin:0 0 4px;font-size:13px}}
  table.m{{border-collapse:collapse;margin-top:4px}} table.m td{{padding:1px 6px 1px 0}}
  table.m td.v{{text-align:right}}
  .rsearch{{position:absolute;top:10px;left:50%;transform:translateX(-50%);z-index:6;width:300px;max-width:70%}}
  .rsearch input{{width:100%;box-sizing:border-box;padding:7px 11px;border:1px solid #aaa;border-radius:6px;font-size:13px;box-shadow:0 2px 6px rgba(0,0,0,.2)}}
  .rsearch .res{{background:#fff;border-radius:0 0 6px 6px;box-shadow:0 2px 6px rgba(0,0,0,.2);max-height:240px;overflow:auto}}
  .rsearch .res div{{padding:6px 11px;cursor:pointer;font-size:12px;border-top:1px solid #f0f0f0}}
  .rsearch .res div:hover{{background:#f3f3f3}}
</style></head>
<body>
<div id="map"></div>
<div class="rsearch"><input id="rsin" placeholder="Find a state or county..." autocomplete="off"><div class="res" id="rsres"></div></div>
<div id="ctl" class="panel">
  <h3>US health-facility mapping &middot; per capita</h3>
  <label><input type="radio" name="lvl" value="state" checked> By state</label>
  &nbsp; <label><input type="radio" name="lvl" value="county"> By county</label>
  <div style="margin-top:5px;color:#555">Mapped health facilities (OpenStreetMap
  <b>amenity=hospital / clinic</b>) per 100,000 people. <b>Dark = fewer per capita =
  more under-mapped</b>. Scale clamped at the 90th percentile; high outliers in
  <b style="color:#5e3c99">purple</b>. Click an area for its values.
  <b>Caveat:</b> blends mapping completeness with real provision (under-mapped
  <i>or</i> underserved). Data: OpenStreetMap via Overpass; geometry + population
  from US Census TIGER/ACS.</div>
</div>
<div id="legend" class="panel"><b id="lgttl"></b><div class="scale" id="lgscale"></div></div>
{_attribution()}
<script>
const STATE={state_js}, COUNTY={county_js}, RAMP={ramp_js};
const KEY='m_per_100k';
const fmt=v=>(v===null||v===undefined||v==='')?'—':(Math.round(v*10)/10).toLocaleString()+' /100k';
const fmtn=v=>(v===null||v===undefined||v==='')?'—':Math.round(v).toLocaleString();
function vals(fc){{return fc.features.map(f=>f.properties[KEY]).filter(v=>typeof v==='number'&&v>=0);}}
function quantile(s,q){{const i=(s.length-1)*q,lo=Math.floor(i),hi=Math.ceil(i);return lo===hi?s[lo]:s[lo]+(s[hi]-s[lo])*(i-lo);}}
function bounds(fc){{const a=vals(fc).slice().sort((x,y)=>x-y);if(!a.length)return null;let lo=a[0],hi=quantile(a,0.90);if(lo>=hi)hi=lo+1;return [lo,hi];}}
function colorExpr(fc){{
  const b=bounds(fc); if(!b) return '{NODATA}'; const lo=b[0],hi=b[1];
  const cols=RAMP.map(r=>r[1]).slice().reverse();  // low = dark = under-mapped
  const expr=['interpolate',['linear'],['get',KEY]];
  RAMP.forEach((r,i)=>expr.push(lo+(hi-lo)*r[0],cols[i]));
  return ['case',['==',['get',KEY],null],'{NODATA}',['>',['get',KEY],hi],'{OUTLIER}',expr];
}}
function legend(fc){{
  const b=bounds(fc); const sc=document.getElementById('lgscale'); sc.innerHTML='';
  document.getElementById('lgttl').textContent='Facilities /100k  (dark = under-mapped)';
  if(!b) return; const lo=b[0],hi=b[1]; const cols=RAMP.map(r=>r[1]).slice().reverse();
  RAMP.forEach((r,i)=>{{const d=document.createElement('div');
    d.innerHTML=`<span style="background:${{cols[i]}}"></span>${{fmt(lo+(hi-lo)*r[0])}}`;sc.appendChild(d);}});
  const o=document.createElement('div');o.innerHTML=`<span style="background:{OUTLIER}"></span>${{'>'+fmt(hi)}}`;sc.appendChild(o);
}}
const map=new maplibregl.Map({{container:'map',style:{{version:8,
  sources:{{bm:{{type:'raster',tiles:['https://a.basemaps.cartocdn.com/rastertiles/voyager/{{z}}/{{x}}/{{y}}.png','https://b.basemaps.cartocdn.com/rastertiles/voyager/{{z}}/{{x}}/{{y}}.png'],tileSize:256,attribution:'&copy; OpenStreetMap &copy; CARTO'}}}},
  layers:[{{id:'bm',type:'raster',source:'bm'}}]}},center:[-96,38],zoom:3.4}});
map.addControl(new maplibregl.NavigationControl());
let level='state';
function activeFc(){{return level==='state'?STATE:COUNTY;}}
function popup(e){{const p=e.features[0].properties||{{}};
  const rows=`<tr><td>Facilities /100k</td><td class="v">${{fmt(p[KEY])}}</td></tr>`
    +`<tr><td>Mapped facilities</td><td class="v">${{fmtn(p.m_facilities)}}</td></tr>`
    +`<tr><td>Population</td><td class="v">${{fmtn(p.m_population)}}</td></tr>`;
  new maplibregl.Popup({{closeButton:true,maxWidth:'300px'}}).setLngLat(e.lngLat)
    .setHTML(`<h4>${{p.NAME||''}}</h4><table class="m">${{rows}}</table>`).addTo(map);}}
map.on('load',()=>{{
  map.addSource('d',{{type:'geojson',data:STATE}});
  map.addLayer({{id:'fill',type:'fill',source:'d',paint:{{'fill-color':colorExpr(STATE),'fill-opacity':0.82}}}});
  map.addLayer({{id:'line',type:'line',source:'d',paint:{{'line-color':'#888','line-width':0.3}}}});
  legend(STATE);
  map.on('click','fill',popup);
  map.on('mouseenter','fill',()=>map.getCanvas().style.cursor='pointer');
  map.on('mouseleave','fill',()=>map.getCanvas().style.cursor='');
  document.querySelectorAll('input[name=lvl]').forEach(r=>r.addEventListener('change',()=>{{
    level=document.querySelector('input[name=lvl]:checked').value;
    const fc=activeFc();
    map.getSource('d').setData(fc);
    map.setPaintProperty('fill','fill-color',colorExpr(fc));
    map.setPaintProperty('line','line-width',level==='county'?0.15:0.3);
    legend(fc);
  }}));
  // search across the active level
  const inp=document.getElementById('rsin'),res=document.getElementById('rsres');
  function bbox(g){{let a=[180,90,-180,-90];const w=c=>{{if(typeof c[0]==='number'){{a[0]=Math.min(a[0],c[0]);a[1]=Math.min(a[1],c[1]);a[2]=Math.max(a[2],c[0]);a[3]=Math.max(a[3],c[1]);}}else c.forEach(w);}};w(g.coordinates);return a;}}
  inp.addEventListener('input',()=>{{const q=inp.value.trim().toLowerCase();res.innerHTML='';if(q.length<2)return;
    activeFc().features.map(f=>({{n:f.properties.NAME||'',f}})).filter(x=>x.n.toLowerCase().includes(q)).slice(0,12).forEach(x=>{{
      const d=document.createElement('div');d.textContent=x.n;
      d.addEventListener('click',()=>{{const b=bbox(x.f.geometry);
        map.fitBounds([[b[0],b[1]],[b[2],b[3]]],{{padding:40,maxZoom:9,duration:700}});res.innerHTML='';inp.value=x.n;
        new maplibregl.Popup().setLngLat([(b[0]+b[2])/2,(b[1]+b[3])/2]).setHTML('<h4>'+x.n+'</h4>').addTo(map);}});
      res.appendChild(d);}});}});
  document.addEventListener('click',e=>{{if(!e.target.closest('.rsearch'))res.innerHTML='';}});
}});
</script></body></html>"""
