"""Route check — which flagged low points does a driving route pass through.

Driving routes come from the public OSRM demo servers (OpenStreetMap data).
Each candidate route is scored by the flagged spots within PROXIMITY_M of its
geometry. A route with no flagged spot nearby is "clear of flagged spots",
which is not a statement that the road is safe.
"""
from __future__ import annotations

import math
from typing import Optional

import httpx

OSRM_URLS = (
    "https://router.project-osrm.org/route/v1/driving",
    "https://routing.openstreetmap.de/routed-car/route/v1/driving",
)
PROXIMITY_M = 300.0
RISK_WEIGHT = {"red": 10, "amber": 3}

# Common dispatch origins and destinations. Coordinates are approximate.
PLACES = [
    {"id": "aiims", "name": "AIIMS Hospital", "kind": "hospital", "lat": 28.5672, "lon": 77.2100},
    {"id": "safdarjung", "name": "Safdarjung Hospital", "kind": "hospital", "lat": 28.5680, "lon": 77.2060},
    {"id": "lnjp", "name": "LNJP Hospital", "kind": "hospital", "lat": 28.6385, "lon": 77.2385},
    {"id": "gtb", "name": "GTB Hospital", "kind": "hospital", "lat": 28.6840, "lon": 77.3100},
    {"id": "ndls", "name": "New Delhi Railway Station", "kind": "station", "lat": 28.6425, "lon": 77.2200},
    {"id": "nzm", "name": "Hazrat Nizamuddin Station", "kind": "station", "lat": 28.5880, "lon": 77.2530},
    {"id": "isbt_kg", "name": "ISBT Kashmere Gate", "kind": "bus", "lat": 28.6680, "lon": 77.2290},
    {"id": "isbt_av", "name": "ISBT Anand Vihar", "kind": "bus", "lat": 28.6470, "lon": 77.3160},
    {"id": "igi_t3", "name": "IGI Airport Terminal 3", "kind": "airport", "lat": 28.5550, "lon": 77.0850},
    {"id": "cp", "name": "Connaught Place", "kind": "area", "lat": 28.6315, "lon": 77.2167},
    {"id": "dtu", "name": "Delhi Technological University", "kind": "school", "lat": 28.7500, "lon": 77.1170},
    {"id": "du_north", "name": "Delhi University North Campus", "kind": "school", "lat": 28.6880, "lon": 77.2100},
    {"id": "dwarka21", "name": "Dwarka Sector 21", "kind": "area", "lat": 28.5520, "lon": 77.0580},
    {"id": "okhla", "name": "Okhla Industrial Area", "kind": "area", "lat": 28.5300, "lon": 77.2710},
    {"id": "azadpur_mandi", "name": "Azadpur Mandi", "kind": "market", "lat": 28.7130, "lon": 77.1720},
    {"id": "saket", "name": "Saket (Max Hospital)", "kind": "hospital", "lat": 28.5275, "lon": 77.2120},
]


def _to_xy(lat: float, lon: float, lat0: float) -> tuple[float, float]:
    """Equirectangular projection in metres; accurate enough at city scale."""
    return (math.radians(lon) * 6371000.0 * math.cos(math.radians(lat0)), math.radians(lat) * 6371000.0)


def distance_to_path_m(lat: float, lon: float, path: list[list[float]]) -> float:
    """Shortest distance in metres from a point to a [[lat, lon], ...] polyline."""
    if not path:
        return float("inf")
    px, py = _to_xy(lat, lon, lat)
    pts = [_to_xy(p[0], p[1], lat) for p in path]
    if len(pts) == 1:
        return math.hypot(px - pts[0][0], py - pts[0][1])
    best = float("inf")
    for (ax, ay), (bx, by) in zip(pts, pts[1:]):
        dx, dy = bx - ax, by - ay
        seg = dx * dx + dy * dy
        t = 0.0 if seg == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / seg))
        best = min(best, math.hypot(px - (ax + t * dx), py - (ay + t * dy)))
    return best


def score_route(path: list[list[float]], spots: list[dict], risk: dict[str, str]) -> tuple[list[dict], int]:
    """Return (flagged spots near the route, risk score)."""
    hits: list[dict] = []
    score = 0
    for s in spots:
        level = risk.get(s["id"])
        if level not in RISK_WEIGHT:
            continue
        d = distance_to_path_m(s["lat"], s["lon"], path)
        if d <= PROXIMITY_M:
            hits.append({"spot_id": s["id"], "name": s["name"], "risk_level": level, "distance_m": round(d)})
            score += RISK_WEIGHT[level]
    hits.sort(key=lambda h: (-RISK_WEIGHT[h["risk_level"]], h["distance_m"]))
    return hits, score


async def fetch_routes(origin: tuple[float, float], dest: tuple[float, float], timeout: float = 12.0) -> list[dict]:
    """Fetch up to three driving alternatives. origin/dest are (lat, lon)."""
    coords = f"{origin[1]},{origin[0]};{dest[1]},{dest[0]}"
    params = {"alternatives": "3", "overview": "full", "geometries": "geojson"}
    last_error: Optional[str] = None
    async with httpx.AsyncClient(timeout=timeout, headers={"User-Agent": "underpass-sentinel-demo"}) as client:
        for base in OSRM_URLS:
            try:
                resp = await client.get(f"{base}/{coords}", params=params)
                resp.raise_for_status()
                data = resp.json()
                if data.get("code") != "Ok" or not data.get("routes"):
                    last_error = f"routing service returned {data.get('code')}"
                    continue
                return [
                    {
                        "distance_km": round(r["distance"] / 1000.0, 1),
                        "duration_min": round(r["duration"] / 60.0),
                        "path": [[c[1], c[0]] for c in r["geometry"]["coordinates"]],
                    }
                    for r in data["routes"]
                ]
            except Exception as exc:
                last_error = str(exc) or type(exc).__name__
    raise RuntimeError(f"routing service unavailable: {last_error}")


async def check_route(origin: tuple[float, float], dest: tuple[float, float], spots: list[dict], risk: dict[str, str]) -> dict:
    routes = await fetch_routes(origin, dest)
    for r in routes:
        r["hits"], r["score"] = score_route(r["path"], spots, risk)
    fastest = min(range(len(routes)), key=lambda i: routes[i]["duration_min"])
    recommended = min(range(len(routes)), key=lambda i: (routes[i]["score"], routes[i]["duration_min"]))
    return {
        "routes": routes,
        "fastest_index": fastest,
        "recommended_index": recommended,
        "extra_minutes": routes[recommended]["duration_min"] - routes[fastest]["duration_min"],
        "all_routes_flagged": all(r["score"] > 0 for r in routes),
        "proximity_m": PROXIMITY_M,
    }
