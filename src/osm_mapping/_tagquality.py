"""OSM tag-quality (attribute-misuse) choropleths from Osmose QA, by region.

Compares the tags actually used on OSM entities against current valid tagging
conventions and maps where the deviation is densest — "which regions need
cleanup attention". The signal is the **Osmose QA** issue feed:

- item **9002 "deprecated"** — tags that were valid but have been superseded
  (the global analyzer; runs worldwide).
- item **3040 "incorrect tag"** — tag/value combinations flagged as wrong.

Data-model facts (established empirically, see module tests):

- Osmose issues are exposed per **leaf** region code only (country/state parents
  return 0): ``usa_california``\\=0 but ``usa_california_los_angeles``\\=N;
  ``germany``\\=0 but ``germany_baden_wuerttemberg_…``\\=N. So we enumerate the
  ~1,190 leaf codes from ``/api/0.3/countries`` and **aggregate upward**.
- There is no count endpoint: a region's count is ``len`` of
  ``/issues?item=&country=<leaf>&limit=N`` (capped at ``LIMIT``; truncation
  logged).
- Counts are normalised **per 1,000 km²** (area via pyproj geodesic); absolute
  counts are kept as a secondary metric. Per-area favours small dense regions —
  noted on the map.

Three maps: world by country (Natural Earth), US by state and US by county
(reusing the census-us TIGER cache). Honest caveats (in each map's note):
Osmose analyzer coverage and OSM mapping age both confound a raw read, so this is
a *prioritisation aid*, not a quality verdict.
"""
from __future__ import annotations

import concurrent.futures
import json
import logging
import re
from dataclasses import dataclass

import pyproj
from shapely.geometry import shape

from . import storage as cstore

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None

logger = logging.getLogger("osm-tagquality")

OSMOSE = "https://osmose.openstreetmap.fr/api/0.3"
USER_AGENT = "facetwork-osm-tagquality/1.0 (+https://github.com/rlemke/facetwork)"
ITEMS = {"dep": 9002, "inc": 3040}   # deprecated (global) + incorrect tag
LIMIT = 100000                        # per-leaf issue cap (logged if hit)
FETCH_WORKERS = 6
CACHE_REL = "osm-tagquality/osmose_leaf_counts.json"
NE_URL = ("https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/"
          "geojson/ne_110m_admin_0_countries.geojson")
CENSUS_PREFIX = "cache/census-us"     # shared census-us TIGER cache in MinIO
FFL_URL = "https://github.com/rlemke/fwh_osm_mapping/blob/main/src/osm_mapping/ffl/osm_mapping.ffl"

_GEOD = pyproj.Geod(ellps="WGS84")


@dataclass
class TagQualityResult:
    region: str
    html_path: str
    feature_count: int
    detail: str


# ---------------------------------------------------------------------------
# Osmose leaf fetch (cached aggregate so re-renders never re-hit Osmose)
# ---------------------------------------------------------------------------

def _get_json(url: str, timeout: int = 120):
    r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=timeout)
    r.raise_for_status()
    return r.json()


def leaf_codes() -> list[str]:
    """Osmose region codes that have no child (only leaves carry issues)."""
    codes = set(_get_json(f"{OSMOSE}/countries")["countries"])
    return sorted(c for c in codes if not any(o != c and o.startswith(c + "_") for o in codes))


def _leaf_count(code: str, item: int) -> tuple[int, bool]:
    """(#issues, truncated?) for one leaf+item — len of the capped issue list."""
    d = _get_json(f"{OSMOSE}/issues?item={item}&country={code}&limit={LIMIT}", timeout=180)
    n = len(d.get("issues", []))
    return n, n >= LIMIT


def fetch_osmose_counts(*, force: bool = False) -> dict[str, dict[str, int]]:
    """``{leaf_code: {"dep": n, "inc": m}}`` for every leaf, cached in MinIO."""
    cache_path = cstore.join(cstore.cache_root(), CACHE_REL)
    if not force and cstore.exists(cache_path):
        with cstore.open_read(cache_path) as f:
            return json.load(f)
    if requests is None:
        raise RuntimeError("requests not installed")
    leaves = leaf_codes()
    logger.info("Osmose: fetching %d leaf codes x %d items", len(leaves), len(ITEMS))
    out: dict[str, dict[str, int]] = {}
    truncated = 0

    def one(code: str):
        rec = {}
        for key, item in ITEMS.items():
            n, trunc = _leaf_count(code, item)
            rec[key] = n
            if trunc:
                logger.warning("Osmose leaf %s item %s truncated at %d", code, item, LIMIT)
        return code, rec

    with concurrent.futures.ThreadPoolExecutor(max_workers=FETCH_WORKERS) as ex:
        for code, rec in ex.map(one, leaves):
            out[code] = rec
            truncated += sum(1 for v in rec.values() if v >= LIMIT)
    logger.info("Osmose: %d leaves fetched (%d truncated)", len(out), truncated)
    with cstore.open_write(cache_path) as f:
        json.dump(out, f)
    return out


# ---------------------------------------------------------------------------
# Geometry + area helpers
# ---------------------------------------------------------------------------

def _area_km2(geom: dict) -> float:
    """Geodesic land area in km² (sign-independent)."""
    try:
        return abs(_GEOD.geometry_area_perimeter(shape(geom))[0]) / 1e6
    except Exception:  # noqa: BLE001
        return 0.0


def _read_census_json(rel: str) -> dict:
    # census-us cache lives under the data root, not osm-mapping's own cache.
    path = cstore.join(cstore._data_root(), CENSUS_PREFIX, rel)
    with cstore.open_read(cstore.localize(path) if cstore.is_remote(path) else path) as f:
        return json.load(f)


def _norm(s: str) -> str:
    """Lowercase, non-alphanumeric -> underscore, collapse — Osmose code style."""
    return re.sub(r"_+", "_", re.sub(r"[^a-z0-9]+", "_", (s or "").lower())).strip("_")


# ---------------------------------------------------------------------------
# Region matchers (leaf code -> country / US state / US county)
# ---------------------------------------------------------------------------

# Osmose country prefixes whose bare code is absent or differs from the Natural
# Earth name. Maps an Osmose prefix -> Natural Earth NAME for the ~country roots
# that don't normalise cleanly. (Most countries match by normalised NAME.)
COUNTRY_ALIASES = {
    "usa": "United States of America", "united_kingdom": "United Kingdom",
    "bosnia_herzegovina": "Bosnia and Herz.", "czechia": "Czechia",
    "south_korea": "South Korea", "north_korea": "North Korea",
    "russia": "Russia", "iran": "Iran", "laos": "Laos", "syria": "Syria",
    "tanzania": "Tanzania", "venezuela": "Venezuela", "bolivia": "Bolivia",
    "moldova": "Moldova", "vietnam": "Vietnam", "brunei": "Brunei",
    "ivory_coast": "Côte d'Ivoire", "cote_d_ivoire": "Côte d'Ivoire",
    "democratic_republic_of_the_congo": "Dem. Rep. Congo",
    "republic_of_the_congo": "Congo", "macedonia": "Macedonia",
    "czech_republic": "Czechia", "central_african_republic": "Central African Rep.",
    "dominican_republic": "Dominican Rep.", "south_sudan": "S. Sudan",
    "equatorial_guinea": "Eq. Guinea", "western_sahara": "W. Sahara",
}


def _country_prefixes(ne_features: list[dict]) -> dict[str, str]:
    """Build {osmose_prefix: NE NAME}. Longest-match wins at lookup time."""
    pref: dict[str, str] = {}
    for f in ne_features:
        name = (f["properties"].get("NAME") or "").strip()
        if name:
            pref.setdefault(_norm(name), name)
    for a, name in COUNTRY_ALIASES.items():  # explicit aliases win
        pref[a] = name
    return pref


def _country_of(code: str, prefixes: dict[str, str]) -> str | None:
    """Map a leaf code to a NE country NAME by the longest matching prefix."""
    best = None
    for p, name in prefixes.items():
        if code == p or code.startswith(p + "_"):
            if best is None or len(p) > best[0]:
                best = (len(p), name)
    return best[1] if best else None


def _us_state_slug(code: str) -> str | None:
    """For ``usa_<state>[...]`` return the state slug (``california``)."""
    if not code.startswith("usa_"):
        return None
    return code[4:].split("_")[0] if "_" not in code[4:] else _us_state_county(code)[0]


_US_STATE_SLUGS: list[str] = []  # filled from TIGER state names at build time


def _us_state_county(code: str) -> tuple[str | None, str | None]:
    """Split ``usa_<state_slug>_<county_slug>`` using the known state-slug set
    (state slugs can be multi-word, e.g. ``new_york``)."""
    if not code.startswith("usa_"):
        return None, None
    rest = code[4:]
    for slug in sorted(_US_STATE_SLUGS, key=len, reverse=True):
        if rest == slug:
            return slug, None
        if rest.startswith(slug + "_"):
            return slug, rest[len(slug) + 1:]
    return None, None


# ---------------------------------------------------------------------------
# Map builders
# ---------------------------------------------------------------------------

def _metrics_props(dep: int, inc: int, area_km2: float) -> dict:
    a = area_km2 / 1000.0  # per 1,000 km²
    den = (lambda n: round(n / a, 3) if a > 0 else None)
    return {"m_total_density": den(dep + inc), "m_dep_density": den(dep),
            "m_inc_density": den(inc), "m_total_abs": dep + inc}


_METRICS = [
    {"key": "m_total_density", "label": "All misuse / 1,000 km² (deprecated + incorrect)"},
    {"key": "m_dep_density",   "label": "Deprecated tags / 1,000 km²"},
    {"key": "m_inc_density",   "label": "Incorrect tags / 1,000 km²"},
    {"key": "m_total_abs",     "label": "All misuse — absolute count"},
]

_NOTE = ("Counts are Osmose QA issues — <b>deprecated</b> tags (superseded by newer conventions) and "
         "<b>incorrect</b> tag/value combinations — aggregated from Osmose's per-region data. Darker = denser "
         "misuse. Two big confounders: Osmose analyzer coverage varies by region, and older / more-heavily-mapped "
         "areas carry more deprecated tags simply because they were tagged years ago. Per-area normalisation favours "
         "small dense regions. Treat this as a <b>cleanup-prioritisation aid, not a quality verdict</b>.")

_ATTR = ('Data: <a href="https://osmose.openstreetmap.fr">Osmose QA</a> (items 9002 deprecated + 3040 incorrect tag), '
         'aggregated by region. Geometry: Natural Earth / US Census TIGER. Built by an FFL workflow on '
         '<a href="https://github.com/rlemke/facetwork">Facetwork</a> '
         '(<a href="https://github.com/rlemke/fwh_osm_mapping">fwh_osm_mapping</a>).')


def _write(name: str, html: str) -> str:
    path = cstore.join(cstore.output_root(), "osm-tagquality", name, "index.html")
    with cstore.open_write(path) as f:
        f.write(html)
    return path


def build_world(*, force: bool = False) -> TagQualityResult:
    counts = fetch_osmose_counts(force=force)
    ne = _get_json(NE_URL)
    prefixes = _country_prefixes(ne["features"])
    agg: dict[str, dict[str, int]] = {}
    matched_leaves = 0
    for code, rec in counts.items():
        country = _country_of(code, prefixes)
        if not country:
            continue
        matched_leaves += 1
        a = agg.setdefault(country, {"dep": 0, "inc": 0})
        a["dep"] += rec.get("dep", 0)
        a["inc"] += rec.get("inc", 0)
    feats = []
    for f in ne["features"]:
        name = (f["properties"].get("NAME") or "").strip()
        rec = agg.get(name)
        if not rec or not f.get("geometry"):
            continue
        geom = shape(f["geometry"]).simplify(0.08, preserve_topology=True)
        if geom.is_empty:
            continue
        props = {"name": name, **_metrics_props(rec["dep"], rec["inc"], _area_km2(f["geometry"]))}
        feats.append({"type": "Feature", "geometry": _gj(geom), "properties": props})
    html = _render(feats, "World OSM tag-quality by country",
                   "Where OSM tags deviate from current valid conventions (Osmose QA). Pick a metric; use search:",
                   _NOTE + f" Country match: {matched_leaves}/{len(counts)} Osmose leaf regions mapped to a country.",
                   center=[10, 25], zoom=1.4)
    path = _write("world", html)
    logger.info("world tag-quality: %d countries, %d/%d leaves matched", len(feats), matched_leaves, len(counts))
    return TagQualityResult("world", path, len(feats), f"{len(feats)} countries, {matched_leaves}/{len(counts)} leaves matched")


def build_us_states(*, force: bool = False) -> TagQualityResult:
    counts = fetch_osmose_counts(force=force)
    states = _read_census_json("output/tiger/state/us_state.geojson")
    global _US_STATE_SLUGS
    _US_STATE_SLUGS = [_norm(f["properties"].get("NAME", "")) for f in states["features"]]
    agg: dict[str, dict[str, int]] = {}
    for code, rec in counts.items():
        slug, _county = _us_state_county(code)
        if not slug:
            continue
        a = agg.setdefault(slug, {"dep": 0, "inc": 0})
        a["dep"] += rec.get("dep", 0)
        a["inc"] += rec.get("inc", 0)
    feats = []
    for f in states["features"]:
        p = f["properties"]
        slug = _norm(p.get("NAME", ""))
        rec = agg.get(slug)
        if not rec or not f.get("geometry"):
            continue
        aland = float(p.get("ALAND") or 0) / 1e6 or _area_km2(f["geometry"])
        geom = shape(f["geometry"]).simplify(0.02, preserve_topology=True)
        props = {"name": p.get("NAME"), **_metrics_props(rec["dep"], rec["inc"], aland)}
        feats.append({"type": "Feature", "geometry": _gj(geom), "properties": props})
    html = _render(feats, "US OSM tag-quality by state",
                   "Where OSM tags deviate from valid conventions (Osmose QA), by state. Pick a metric:",
                   _NOTE, center=[-96, 38], zoom=3.4)
    path = _write("us-states", html)
    return TagQualityResult("us-states", path, len(feats), f"{len(feats)} states")


def build_us_counties(*, force: bool = False) -> TagQualityResult:
    from ._us import _assemble_counties  # reuse the census-us county loader
    counts = fetch_osmose_counts(force=force)
    states = _read_census_json("output/tiger/state/us_state.geojson")
    global _US_STATE_SLUGS
    _US_STATE_SLUGS = [_norm(f["properties"].get("NAME", "")) for f in states["features"]]
    # leaf -> (state_slug, county_slug) county counts
    cagg: dict[tuple, dict[str, int]] = {}
    for code, rec in counts.items():
        slug, county = _us_state_county(code)
        if not slug or not county:
            continue
        a = cagg.setdefault((slug, county), {"dep": 0, "inc": 0})
        a["dep"] += rec.get("dep", 0)
        a["inc"] += rec.get("inc", 0)
    feats = []
    matched = 0
    for c in _assemble_counties():
        # census metrics NAME is a display name ("Los Angeles County, California");
        # osmose county slugs are the bare core ("los_angeles").
        core = re.split(r",", c.get("name", ""))[0]
        core = re.sub(r"\s+(County|Parish|Borough|Census Area|Municipality|"
                      r"City and Borough|Municipio|city)$", "", core, flags=re.I)
        key = (_norm(c.get("state", "")), _norm(core))
        rec = cagg.get(key)
        geom = c.get("geometry")
        if not geom:
            continue
        if rec:
            matched += 1
        dep, inc = (rec or {}).get("dep", 0), (rec or {}).get("inc", 0)
        gj = _gj(shape(geom).simplify(0.01, preserve_topology=True))  # browser-light
        props = {"name": c.get("name"), **_metrics_props(dep, inc, _area_km2(geom))}
        if not rec:  # leave unmatched counties grey
            props = {k: (None if k.startswith("m_") else v) for k, v in props.items()}
        feats.append({"type": "Feature", "geometry": gj, "properties": props})
    html = _render(feats, "US OSM tag-quality by county",
                   "Where OSM tags deviate from valid conventions (Osmose QA), by county. Pick a metric:",
                   _NOTE + f" County match: {matched} counties mapped to an Osmose region.",
                   center=[-96, 38], zoom=3.6)
    path = _write("us-counties", html)
    return TagQualityResult("us-counties", path, len(feats), f"{len(feats)} counties, {matched} with data")


def _gj(geom) -> dict:
    from shapely.geometry import mapping
    g = mapping(geom)
    def rnd(o, n=3):
        if isinstance(o, float): return round(o, n)
        if isinstance(o, (list, tuple)): return [rnd(x, n) for x in o]
        return o
    return {"type": g["type"], "coordinates": rnd(g["coordinates"])}


# ---------------------------------------------------------------------------
# Renderer — choropleth, metric dropdown + region search, dark = more misuse
# ---------------------------------------------------------------------------

def _render(feats: list[dict], title: str, subtitle: str, note: str, *,
            center: list[float], zoom: float) -> str:
    fc = {"type": "FeatureCollection", "features": feats}
    data_js = json.dumps(fc, separators=(",", ":"))
    metrics_js = json.dumps(_METRICS)
    first = _METRICS[0]["key"]
    return f"""<!DOCTYPE html><html><head><meta charset="utf-8"><title>{title}</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<link href="https://unpkg.com/maplibre-gl@4.7.1/dist/maplibre-gl.css" rel="stylesheet">
<script src="https://unpkg.com/maplibre-gl@4.7.1/dist/maplibre-gl.js"></script>
<style>
 html,body,#map{{height:100%;margin:0}}
 .panel{{position:absolute;top:10px;left:10px;z-index:2;background:#fff;padding:12px 14px;border-radius:8px;
  box-shadow:0 1px 6px rgba(0,0,0,.3);font:14px/1.4 system-ui,sans-serif;max-width:350px}}
 .panel h1{{font-size:15px;margin:0 0 6px}} .panel p{{margin:0 0 8px;color:#555;font-size:12px}}
 .panel .note{{margin:8px 0 0;padding:6px 8px;background:#fff8e1;border-left:3px solid #f6c343;
  color:#5d4b00;font-size:11px;line-height:1.35;border-radius:3px}}
 select,#search{{width:100%;padding:6px;font-size:14px;box-sizing:border-box}} #search{{margin-top:6px}}
 .legend{{position:absolute;bottom:24px;left:10px;z-index:2;background:#fff;padding:8px 10px;border-radius:8px;
  box-shadow:0 1px 6px rgba(0,0,0,.3);font:12px system-ui,sans-serif}}
 .legend i{{display:inline-block;width:14px;height:14px;margin-right:6px;vertical-align:-2px}}
 .maplibregl-popup-content{{font:13px system-ui,sans-serif}}
 .attribution{{position:absolute;bottom:0;right:0;z-index:2;background:rgba(255,255,255,.85);
  padding:4px 8px;font:11px system-ui,sans-serif;color:#444;max-width:440px}}
 .attribution a{{color:#1565c0;text-decoration:none}}
</style></head><body>
<div id="map"></div>
<div class="panel"><h1>{title}</h1><p>{subtitle}</p>
 <select id="metric"></select>
 <input id="search" placeholder="Find a region by name…" autocomplete="off">
 <div class="note">{note}</div></div>
<div class="legend" id="legend"></div>
<div class="attribution">{_ATTR}</div>
<script>
const DATA={data_js}, METRICS={metrics_js};
const map=new maplibregl.Map({{container:'map',style:{{version:8,sources:{{c:{{type:'raster',
 tiles:['https://cartodb-basemaps-a.global.ssl.fastly.net/light_all/{{z}}/{{x}}/{{y}}.png'],tileSize:256,
 attribution:'© OpenStreetMap © CARTO'}}}},layers:[{{id:'bg',type:'raster',source:'c'}}]}},
 center:{json.dumps(center)},zoom:{zoom}}});
const RAMP=['#fee5d9','#fcae91','#fb6a4a','#de2d26','#a50f15'];  // light->dark = more misuse
const NODATA='#e0e0e0';
function vals(k){{return DATA.features.map(f=>f.properties[k]).filter(v=>v!=null).sort((a,b)=>a-b);}}
function breaks(k){{const v=vals(k);if(!v.length)return[1,2,3,4];
 const raw=[0.2,0.4,0.6,0.8].map(q=>v[Math.floor(q*v.length)]);
 const o=[];for(const x of raw){{if(!o.length||x>o[o.length-1])o.push(x);}}return o;}}
function colorExpr(k){{const b=breaks(k);const e=['step',['coalesce',['get',k],-1],NODATA,-0.5,RAMP[0]];
 b.forEach((bk,i)=>e.push(bk,RAMP[Math.min(i+1,RAMP.length-1)]));return e;}}
function legend(k){{const v=vals(k),b=breaks(k);const m=METRICS.find(x=>x.key===k);
 let h='<b>'+m.label+'</b><br><div><i style="background:'+NODATA+'"></i>no data</div>';
 const lo=[v.length?v[0]:0,...b],hi=[...b,(v.length?v[v.length-1]:0)];
 for(let i=0;i<Math.min(b.length+1,RAMP.length);i++)
  h+='<div><i style="background:'+RAMP[i]+'"></i>'+(+lo[i]).toLocaleString()+' – '+(+hi[i]).toLocaleString()+'</div>';
 document.getElementById('legend').innerHTML=h;}}
map.on('load',()=>{{
 map.addSource('s',{{type:'geojson',data:DATA}});
 map.addLayer({{id:'fill',type:'fill',source:'s',paint:{{'fill-color':colorExpr('{first}'),'fill-opacity':0.85}}}});
 map.addLayer({{id:'line',type:'line',source:'s',paint:{{'line-color':'#fff','line-width':0.4}}}});
 const sel=document.getElementById('metric');
 METRICS.forEach(m=>{{const o=document.createElement('option');o.value=m.key;o.textContent=m.label;sel.appendChild(o);}});
 function apply(k){{map.setPaintProperty('fill','fill-color',colorExpr(k));legend(k);}}
 sel.onchange=()=>apply(sel.value); apply('{first}');
 map.on('click','fill',e=>{{const p=e.features[0].properties;let h='<b>'+p.name+'</b><br>';
  METRICS.forEach(m=>{{const v=p[m.key];h+=m.label.split(' /')[0].split(' —')[0]+': '+(v!=null?(+v).toLocaleString():'n/a')+'<br>';}});
  new maplibregl.Popup({{maxWidth:'280px'}}).setLngLat(e.lngLat).setHTML(h).addTo(map);}});
 map.on('mouseenter','fill',()=>map.getCanvas().style.cursor='pointer');
 map.on('mouseleave','fill',()=>map.getCanvas().style.cursor='');
 const search=document.getElementById('search');
 search.onkeydown=ev=>{{if(ev.key!=='Enter')return;const q=search.value.trim().toLowerCase();
  const f=DATA.features.find(x=>String(x.properties.name||'').toLowerCase().includes(q));
  if(!f)return; const b=new maplibregl.LngLatBounds();
  (function walk(c){{if(typeof c[0]==='number')b.extend(c);else c.forEach(walk);}})(f.geometry.coordinates);
  map.fitBounds(b,{{padding:40,maxZoom:7}});}};
}});
</script></body></html>"""
