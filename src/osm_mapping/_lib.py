"""OSM mapping-equity world map — health-facility density vs population.

A world country choropleth of **mapped health facilities per capita** in
OpenStreetMap: hospitals + clinics counted per country (via the Overpass API),
normalised by population. Low values surface **under-mapped** (and/or
underserved) countries — a recognised OSM mapping-equity / "digital divide"
signal, with the honest caveat that a single feature class blends *mapping
completeness* with *real-world provision*.

Pipeline (all backend-aware via :mod:`osm_mapping.storage`):

1. ``download_facility_counts`` — for each country (Natural Earth admin-0), an
   Overpass ``area`` count of ``amenity=hospital``/``amenity=clinic``. Runs a
   small thread pool against an Overpass mirror, tolerant of per-country
   failures (recorded as ``None`` and retried), and caches the tiny aggregate
   JSON so re-renders never re-query Overpass.
2. ``build_map`` — join the aggregate onto Natural Earth geometry by ISO2,
   compute facilities-per-million from ``POP_EST``, and render a MapLibre world
   choropleth with a metric dropdown (``dark = more under-mapped``) + a
   provenance footer.

Why per-country counts (not one global query): a single global Overpass fetch of
all health facilities is too large to be reliable, whereas a count bounded to one
country's ``area`` is fast and bounded — the platform's region-fan-out shape.
"""

from __future__ import annotations

import concurrent.futures
import json
import logging
import time
from dataclasses import dataclass
from html import escape

from . import storage as cstore

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None

logger = logging.getLogger("osm-mapping")

WORLD_GEOJSON_URL = (
    "https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/"
    "geojson/ne_110m_admin_0_countries.geojson"
)
FFL_URL = "https://github.com/rlemke/fwh_osm_mapping/blob/main/src/osm_mapping/ffl/osm_mapping.ffl"
USER_AGENT = "facetwork-osm-mapping/1.0 (+https://github.com/rlemke/facetwork)"

# Use the kumi mirror first for the bulk per-country job (spares the main
# instance, which the save-earth maps also use); fall back to the main instance.
OVERPASS_ENDPOINTS = (
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass-api.de/api/interpreter",
)
# Health facilities to count. Hospitals + clinics is the "mapped health
# facilities" basket; both as nodes/ways/relations (centroids not needed for a
# count). Kept as an Overpass filter fragment.
FACILITY_FILTER = '(nwr["amenity"="hospital"](area.a);nwr["amenity"="clinic"](area.a););'
MAX_WORKERS = 3          # polite concurrency against Overpass
PER_COUNTRY_TIMEOUT = 150
RETRIES = 2


# YlOrRd ramp (fraction → colour), light → dark.
RAMP = [
    [0.0, "#ffffb2"], [0.25, "#fecc5c"], [0.5, "#fd8d3c"],
    [0.75, "#f03b20"], [1.0, "#bd0026"],
]
NODATA = "#e0e0e0"


@dataclass
class Metric:
    key: str
    label: str
    fmt: str  # "count" | "rate"
    worse: str = "low"  # "low" → low values are dark (under-mapped); "high" → high dark


METRICS = [
    # Primary: facilities per million people. LOW = under-mapped/underserved → dark.
    Metric("per_million", "Health facilities per million people", "rate", worse="low"),
    # Context columns (informational; not direction-flipped on the scale).
    Metric("facilities", "Mapped health facilities (count)", "count", worse="high"),
    Metric("population", "Population (estimate)", "count", worse="high"),
]


@dataclass
class OsmMapResult:
    output_path: str
    html_path: str
    country_count: int
    matched: int


# ---------------------------------------------------------------------------
# Download — per-country Overpass facility counts.
# ---------------------------------------------------------------------------


def _country_iso2(props: dict) -> str | None:
    for k in ("ISO_A2_EH", "ISO_A2", "WB_A2"):
        v = props.get(k)
        if v and v not in ("-99", "-1", ""):
            return v
    return None


def _overpass_count(iso2: str) -> int | None:
    """Count hospital+clinic features in one country's Overpass ``area``.

    Returns the integer count, or ``None`` if every endpoint/retry failed (so the
    caller can record a gap rather than crashing the whole world build)."""
    if requests is None:
        raise RuntimeError("requests is required to query Overpass")
    query = (
        f"[out:json][timeout:{PER_COUNTRY_TIMEOUT}];"
        f'area["ISO3166-1"="{iso2}"][admin_level=2]->.a;'
        f"{FACILITY_FILTER}out count;"
    )
    for attempt in range(RETRIES + 1):
        for ep in OVERPASS_ENDPOINTS:
            try:
                r = requests.post(
                    ep, data={"data": query},
                    headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
                    timeout=(20, PER_COUNTRY_TIMEOUT + 30),
                )
                if r.status_code == 429:  # rate-limited — back off
                    time.sleep(5 + attempt * 5)
                    continue
                r.raise_for_status()
                els = r.json().get("elements") or []
                if els and "tags" in els[0]:
                    return int(els[0]["tags"].get("total", 0))
                # empty / remark (timeout server-side) → try next endpoint/attempt
            except Exception as exc:  # network / parse / timeout → next
                logger.debug("overpass %s %s failed: %s", iso2, ep, exc)
        time.sleep(2 + attempt * 3)
    logger.warning("overpass count failed for %s after retries", iso2)
    return None


def download_facility_counts(*, force: bool = False) -> dict[str, int]:
    """Return ``{iso2: facility_count}`` for every country, cached as JSON.

    Queries Overpass once per country in a small thread pool; per-country
    failures are tolerated (omitted from the result). Re-runs read the cache."""
    cache_key = cstore.join(cstore.cache_root(), "facility-counts.json")
    if not force and cstore.exists(cache_key):
        with cstore.open_read(cache_key) as f:
            return json.load(f)

    world = _world_geojson()
    isos = sorted({
        iso for ft in world["features"]
        if (iso := _country_iso2(ft.get("properties") or {}))
    })
    logger.info("counting OSM health facilities for %d countries via Overpass", len(isos))

    counts: dict[str, int] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futs = {ex.submit(_overpass_count, iso): iso for iso in isos}
        done = 0
        for fut in concurrent.futures.as_completed(futs):
            iso = futs[fut]
            done += 1
            try:
                c = fut.result()
            except Exception:
                c = None
            if c is not None:
                counts[iso] = c
            if done % 20 == 0:
                logger.info("  ... %d/%d countries counted", done, len(isos))

    with cstore.open_write(cache_key, "w") as f:
        json.dump(counts, f)
    logger.info("facility counts: %d/%d countries returned data", len(counts), len(isos))
    return counts


def _world_geojson() -> dict:
    cache_key = cstore.join(cstore.cache_root(), "world-countries.geojson")
    if cstore.exists(cache_key):
        with cstore.open_read(cache_key) as f:
            return json.load(f)
    if requests is None:
        raise RuntimeError("requests is required to download world geometry")
    resp = requests.get(WORLD_GEOJSON_URL, timeout=120, headers={"User-Agent": USER_AGENT})
    resp.raise_for_status()
    gj = resp.json()
    with cstore.open_write(cache_key, "w") as f:
        json.dump(gj, f, separators=(",", ":"))
    return gj


# ---------------------------------------------------------------------------
# Build map.
# ---------------------------------------------------------------------------


def build_map(*, force: bool = False) -> OsmMapResult:
    """Join facility counts onto world geometry + render the choropleth."""
    counts = download_facility_counts(force=force)
    world = _world_geojson()

    matched = 0
    feats = []
    for ft in world["features"]:
        props = ft.get("properties") or {}
        name = props.get("NAME", "")
        pop = props.get("POP_EST")
        iso = _country_iso2(props)
        fac = counts.get(iso) if iso else None
        if fac is not None:
            matched += 1
        per_m = (
            round(fac / (pop / 1_000_000), 1)
            if (fac is not None and pop and pop > 0) else None
        )
        feats.append({
            "type": "Feature",
            "geometry": ft.get("geometry"),
            "properties": {
                "NAME": name,
                "m_facilities": fac,
                "m_population": int(pop) if pop else None,
                "m_per_million": per_m,
            },
        })

    fc = {"type": "FeatureCollection", "features": feats}
    html = _render_html(fc)

    out_dir = cstore.output_root()
    geojson_path = cstore.join(out_dir, "osm-mapping.geojson")
    html_path = cstore.join(out_dir, "index.html")
    with cstore.open_write(geojson_path, "w") as f:
        json.dump(fc, f, separators=(",", ":"))
    with cstore.open_write(html_path, "w") as f:
        f.write(html)
    return OsmMapResult(geojson_path, html_path, len(world["features"]), matched)


# ---------------------------------------------------------------------------
# Render (choropleth — cloned from the conflict renderer, ramp direction added).
# ---------------------------------------------------------------------------


def _attribution() -> str:
    from datetime import UTC, datetime
    ts = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    call = "osm_mapping.workflows.BuildMappingEquityMap"
    repo = FFL_URL.split("/blob/")[0]
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


def _search_css() -> str:
    return (
        ".rsearch{position:absolute;top:10px;left:50%;transform:translateX(-50%);"
        "z-index:6;width:300px;max-width:70%}"
        ".rsearch input{width:100%;box-sizing:border-box;padding:7px 11px;border:1px solid #aaa;"
        "border-radius:6px;font-size:13px;box-shadow:0 2px 6px rgba(0,0,0,.2)}"
        ".rsearch .res{background:#fff;border-radius:0 0 6px 6px;box-shadow:0 2px 6px rgba(0,0,0,.2);"
        "max-height:240px;overflow:auto}"
        ".rsearch .res div{padding:6px 11px;cursor:pointer;font-size:12px;border-top:1px solid #f0f0f0}"
        ".rsearch .res div:hover{background:#f3f3f3}"
    )


def _search_box() -> str:
    return (
        '<div class="rsearch"><input id="rsin" placeholder="Find a country by name..." '
        'autocomplete="off"><div class="res" id="rsres"></div></div>'
    )


def _search_js() -> str:
    return (
        "(function(){"
        "function fbbox(g){let a=[180,90,-180,-90];const w=c=>{if(typeof c[0]==='number'){"
        "a[0]=Math.min(a[0],c[0]);a[1]=Math.min(a[1],c[1]);a[2]=Math.max(a[2],c[0]);a[3]=Math.max(a[3],c[1]);}"
        "else c.forEach(w);};w(g.coordinates);return a;}"
        "const idx=DATA.features.map(f=>({n:String((f.properties||{})['NAME']||''),f})).filter(x=>x.n);"
        "const inp=document.getElementById('rsin'),res=document.getElementById('rsres');if(!inp)return;"
        "inp.addEventListener('input',()=>{const q=inp.value.trim().toLowerCase();res.innerHTML='';"
        "if(q.length<2)return;"
        "idx.filter(x=>x.n.toLowerCase().includes(q)).slice(0,12).forEach(x=>{"
        "const d=document.createElement('div');d.textContent=x.n;"
        "d.addEventListener('click',()=>{const b=fbbox(x.f.geometry);"
        "map.fitBounds([[b[0],b[1]],[b[2],b[3]]],{padding:40,maxZoom:6,duration:700});"
        "res.innerHTML='';inp.value=x.n;"
        "new maplibregl.Popup().setLngLat([(b[0]+b[2])/2,(b[1]+b[3])/2])"
        ".setHTML('<h4>'+x.n+'</h4>').addTo(map);});res.appendChild(d);});});"
        "document.addEventListener('click',e=>{if(!e.target.closest('.rsearch'))res.innerHTML='';});"
        "})();"
    )


def _render_html(fc: dict) -> str:
    data_js = json.dumps(fc, separators=(",", ":"))
    metrics_js = json.dumps(
        [{"key": f"m_{m.key}", "label": m.label, "fmt": m.fmt, "worse": m.worse} for m in METRICS]
    )
    ramp_js = json.dumps(RAMP)
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>OSM mapping equity - health facilities per capita</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<link href="https://unpkg.com/maplibre-gl@4.7.1/dist/maplibre-gl.css" rel="stylesheet">
<script src="https://unpkg.com/maplibre-gl@4.7.1/dist/maplibre-gl.js"></script>
<style>
  html,body,#map{{margin:0;height:100%;width:100%;font-family:system-ui,sans-serif}}
  .panel{{position:absolute;z-index:1;background:rgba(255,255,255,.94);padding:10px 12px;
    border-radius:6px;box-shadow:0 1px 4px rgba(0,0,0,.3);font-size:12px}}
  #ctl{{top:10px;left:10px;max-width:340px}}
  #ctl h3{{margin:0 0 6px;font-size:14px}} #ctl select{{font-size:13px;padding:3px;width:100%}}
  #legend{{bottom:18px;right:10px}} #legend .scale{{display:flex;margin-top:4px}}
  #legend .scale div{{display:flex;flex-direction:column;align-items:center;font-size:10px}}
  #legend .scale span{{width:34px;height:12px}}
  .maplibregl-popup-content{{max-width:300px;font-size:12px}}
  .maplibregl-popup-content h4{{margin:0 0 4px;font-size:13px}}
  table.m{{border-collapse:collapse;margin-top:4px}} table.m td{{padding:1px 6px 1px 0}}
  table.m td.v{{text-align:right}} tr.sel td{{font-weight:700}}
  {_search_css()}
</style></head>
<body>
<div id="map"></div>
{_search_box()}
<div id="ctl" class="panel">
  <h3>OSM mapping equity &middot; health facilities per capita</h3>
  <select id="metric"></select>
  <div style="margin-top:5px;color:#555">Mapped health facilities (OpenStreetMap
  <b>amenity=hospital / clinic</b>) per million people, by country. On the primary
  metric, <b>dark = fewer per capita = more under-mapped</b>. Click a country for
  its values. <b>Caveat:</b> a single feature class blends mapping completeness with
  real-world provision; read low values as "under-mapped <i>or</i> underserved".
  Data: OpenStreetMap via Overpass; population from Natural Earth.</div>
</div>
<div id="legend" class="panel"><b id="lgttl"></b><div class="scale" id="lgscale"></div></div>
{_attribution()}
<script>
const DATA={data_js}, METRICS={metrics_js}, RAMP={ramp_js};
const fmt=(v,f)=>{{ if(v===null||v===undefined||v==='') return '—';
  if(f==='rate') return (Math.round(v*10)/10)+' /M';
  return Math.round(v).toLocaleString(); }};
const vals=k=>DATA.features.map(f=>f.properties[k]).filter(v=>typeof v==='number'&&v>=0);
function colorExpr(m){{
  const a=vals(m.key); if(!a.length) return '{NODATA}';
  let lo=Math.min(...a), hi=Math.max(...a); if(lo===hi) hi=lo+1;
  // worse==='low' → low values get the dark end (reverse the ramp colours).
  const cols=(m.worse==='low')?RAMP.map(r=>r[1]).slice().reverse():RAMP.map(r=>r[1]);
  const expr=['interpolate',['linear'],['get',m.key]];
  RAMP.forEach((r,i)=>expr.push(lo+(hi-lo)*r[0], cols[i]));
  return ['case',['==',['get',m.key],null],'{NODATA}',expr];
}}
function legend(m){{
  document.getElementById('lgttl').textContent=m.label+(m.worse==='low'?'  (dark = under-mapped)':'');
  const a=vals(m.key); const sc=document.getElementById('lgscale'); sc.innerHTML='';
  if(!a.length) return; let lo=Math.min(...a),hi=Math.max(...a);
  const cols=(m.worse==='low')?RAMP.map(r=>r[1]).slice().reverse():RAMP.map(r=>r[1]);
  RAMP.forEach((r,i)=>{{ const d=document.createElement('div');
    d.innerHTML=`<span style="background:${{cols[i]}}"></span>${{fmt(lo+(hi-lo)*r[0],m.fmt)}}`;
    sc.appendChild(d); }});
}}
const map=new maplibregl.Map({{container:'map',style:{{version:8,
  sources:{{bm:{{type:'raster',tiles:['https://a.basemaps.cartocdn.com/rastertiles/voyager/{{z}}/{{x}}/{{y}}.png','https://b.basemaps.cartocdn.com/rastertiles/voyager/{{z}}/{{x}}/{{y}}.png'],tileSize:256,attribution:'&copy; OpenStreetMap &copy; CARTO'}}}},
  layers:[{{id:'bm',type:'raster',source:'bm'}}]}},center:[14,18],zoom:1.4}});
map.addControl(new maplibregl.NavigationControl());
const sel=document.getElementById('metric');
METRICS.forEach((m,i)=>{{const o=document.createElement('option');o.value=i;o.textContent=m.label;sel.appendChild(o);}});
let cur=METRICS[0];
map.on('load',()=>{{
  map.addSource('c',{{type:'geojson',data:DATA}});
  map.addLayer({{id:'fill',type:'fill',source:'c',paint:{{'fill-color':colorExpr(cur),'fill-opacity':0.82}}}});
  map.addLayer({{id:'line',type:'line',source:'c',paint:{{'line-color':'#888','line-width':0.3}}}});
  legend(cur);
  sel.onchange=()=>{{cur=METRICS[+sel.value];map.setPaintProperty('fill','fill-color',colorExpr(cur));legend(cur);}};
  map.on('click','fill',e=>{{const p=e.features[0].properties||{{}};
    let rows=''; for(const m of METRICS){{ const v=p[m.key];
      rows+=`<tr class="${{m.key===cur.key?'sel':''}}"><td>${{m.label}}</td><td class="v">${{fmt(v,m.fmt)}}</td></tr>`; }}
    new maplibregl.Popup({{closeButton:true,maxWidth:'300px'}}).setLngLat(e.lngLat)
      .setHTML(`<h4>${{p.NAME||'Country'}}</h4><table class="m">${{rows}}</table>`).addTo(map);}});
  map.on('mouseenter','fill',()=>map.getCanvas().style.cursor='pointer');
  map.on('mouseleave','fill',()=>map.getCanvas().style.cursor='');
}});
{_search_js()}
</script></body></html>"""
