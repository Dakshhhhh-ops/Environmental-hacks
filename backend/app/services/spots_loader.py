"""Load and cache spots from data/spots.json."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional


def find_spots_file() -> Optional[Path]:
    """Search upwards for data/spots.json."""
    here = Path(__file__).resolve()
    for parent in [here] + list(here.parents):
        candidate = parent / "data" / "spots.json"
        if candidate.exists():
            return candidate
        candidate2 = parent.parent / "data" / "spots.json"
        if candidate2.exists():
            return candidate2
    # Fallback: relative to backend/app
    fallback = Path(__file__).resolve().parent.parent.parent.parent / "data" / "spots.json"
    if fallback.exists():
        return fallback
    return None


_cached: Optional[list[dict]] = None


def load_spots() -> list[dict]:
    global _cached
    if _cached is not None:
        return _cached
    path = find_spots_file()
    if path is None:
        raise FileNotFoundError("data/spots.json not found")
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    spots = data.get("spots", [])
    _cached = spots
    return spots


def get_spot(spot_id: str) -> Optional[dict]:
    for s in load_spots():
        if s["id"] == spot_id:
            return s
    return None


def reload_spots() -> list[dict]:
    global _cached
    _cached = None
    return load_spots()
