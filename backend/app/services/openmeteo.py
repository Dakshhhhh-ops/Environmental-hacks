"""Open-Meteo service — Live forecast, Historical Forecast, and Archive APIs.

Window convention (documented):
- All timestamps are in Asia/Kolkata (UTC+05:30).
- Live mode: the assessment window is the next 12 forecast hours starting from
  the current local hour (inclusive). The API is called with forecast_days=2 and
  the 12-hour slice is extracted client-side by matching timestamps.
- Replay mode: the assessment window runs from the chosen "as of" hour to 23:00
  on the selected date. The forecast is the day-ahead forecast from the Previous
  Runs API (what was predicted one day earlier), available from 2024. Recorded
  rain comes from the Archive API. The three hours before the "as of" hour are
  returned separately as rain already recorded.
- Before 2024 no day-ahead forecast exists. The Historical Forecast API returns
  the same series as the archive for those dates, so it is labelled as an
  archive stand-in and cannot be verified.
- Units: precipitation in millimetres per hour (mm/hr) as returned by Open-Meteo
  `hourly=precipitation`.
- Current hour: included. If the current hour is 14:37 IST, the window starts
  at 14:00 IST.
"""
from __future__ import annotations

import asyncio
import json
import urllib.request
import urllib.error
import urllib.parse
import math
from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")

LIVE_URL = "https://api.open-meteo.com/v1/forecast"
HIST_FORECAST_URL = "https://historical-forecast-api.open-meteo.com/v1/forecast"
ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
PREVIOUS_RUNS_URL = "https://previous-runs-api.open-meteo.com/v1/forecast"
DAY_AHEAD_KEY = "precipitation_previous_day1"
RECENT_HOURS = 3


class OpenMeteoError(Exception):
    pass


def _fetch_json(url: str, timeout: float = 15) -> dict:
    """Synchronous HTTP GET returning parsed JSON. Raises OpenMeteoError on failure."""
    req = urllib.request.Request(url, headers={"User-Agent": "Underpass-Sentinel/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
            return json.loads(body)
    except urllib.error.HTTPError as e:
        try:
            detail = e.read().decode()[:500]
        except Exception:
            detail = str(e)
        raise OpenMeteoError(f"Open-Meteo HTTP {e.code}: {detail}") from e
    except urllib.error.URLError as e:
        raise OpenMeteoError(f"Open-Meteo network error: {e.reason}") from e
    except TimeoutError as e:
        raise OpenMeteoError(f"Open-Meteo timeout after {timeout}s") from e
    except json.JSONDecodeError as e:
        raise OpenMeteoError(f"Open-Meteo invalid JSON: {e}") from e


async def _fetch_json_async(url: str, timeout: float = 15) -> dict:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, lambda: _fetch_json(url, timeout))


def _build_url(base: str, lat: float, lon: float, extra: dict) -> str:
    params = {"latitude": lat, "longitude": lon, **extra}
    return base + "?" + urllib.parse.urlencode(params)


def _extract_window(hourly: dict, window_start_str: str, window_end_str: str, key: str = "precipitation") -> tuple[list[str], list[float]]:
    """Extract timestamps and precipitation values within [window_start, window_end] inclusive."""
    times: list[str] = hourly.get("time", [])
    precips = hourly.get(key) or []
    # Ensure equal length
    n = min(len(times), len(precips))
    times = times[:n]
    precips = precips[:n]
    out_times: list[str] = []
    out_values: list[float] = []
    for t, p in zip(times, precips):
        if window_start_str <= t <= window_end_str:
            out_times.append(t)
            try:
                v = float(p)
                if not math.isfinite(v) or v < 0:
                    out_times.pop()
                    continue
                out_values.append(v)
            except (TypeError, ValueError, OverflowError):
                out_times.pop()
    return out_times, out_values


def _live_window() -> tuple[str, str]:
    """Return (window_start, window_end) as ISO strings in Asia/Kolkata for live mode."""
    now_ist = datetime.now(IST)
    # Floor to current hour
    start = now_ist.replace(minute=0, second=0, microsecond=0)
    end = start + timedelta(hours=11)  # 12 hours inclusive
    return start.strftime("%Y-%m-%dT%H:00"), end.strftime("%Y-%m-%dT%H:00")


def _replay_window(date_str: str, as_of_hour: int = 0) -> tuple[str, str]:
    """Return (window_start, window_end) for a replay date: the "as of" hour to 23:00."""
    return f"{date_str}T{as_of_hour:02d}:00", f"{date_str}T23:00"


def _recent_window(window_start: str) -> Optional[tuple[str, str]]:
    """The RECENT_HOURS hours before window_start, as (start, end) inclusive."""
    try:
        start = datetime.strptime(window_start, "%Y-%m-%dT%H:%M")
    except ValueError:
        return None
    return (start - timedelta(hours=RECENT_HOURS)).strftime("%Y-%m-%dT%H:00"), (start - timedelta(hours=1)).strftime("%Y-%m-%dT%H:00")


def _hours_between(window_start: str, window_end: str) -> int:
    fmt = "%Y-%m-%dT%H:%M"
    return int((datetime.strptime(window_end, fmt) - datetime.strptime(window_start, fmt)).total_seconds() // 3600) + 1


async def fetch_live_spot(lat: float, lon: float, timeout: float = 15) -> tuple[list[str], list[float], str, str, list[str], list[float]]:
    """Fetch live forecast for one spot.

    Returns (timestamps, values, window_start, window_end, recent_times, recent_values).
    The recent series is the model's rain for the hours just before now.
    """
    w_start, w_end = _live_window()
    url = _build_url(LIVE_URL, lat, lon, {
        "hourly": "precipitation",
        "timezone": "Asia/Kolkata",
        "forecast_days": "2",
        "past_days": "1",
    })
    data = await _fetch_json_async(url, timeout)
    hourly = data.get("hourly", {})
    times, values = _extract_window(hourly, w_start, w_end)
    recent = _recent_window(w_start)
    recent_times, recent_values = _extract_window(hourly, recent[0], recent[1]) if recent else ([], [])
    return times, values, w_start, w_end, recent_times, recent_values


async def _fetch_hourly_bulk(base: str, spots: list[dict], extra: dict, timeout: float) -> list[dict]:
    """One request for many locations. Returns the "hourly" block per spot, in order."""
    url = base + "?" + urllib.parse.urlencode({
        "latitude": ",".join(str(s["lat"]) for s in spots),
        "longitude": ",".join(str(s["lon"]) for s in spots),
        **extra,
    }, safe=",")
    data = await _fetch_json_async(url, timeout)
    items = data if isinstance(data, list) else [data]
    if len(items) != len(spots):
        raise OpenMeteoError(f"Open-Meteo returned {len(items)} locations for {len(spots)} requested")
    return [item.get("hourly", {}) for item in items]


# Past days never change, so one fetch per (spot, date) serves every "as of" hour.
_day_cache: dict[tuple[float, float, str], dict] = {}


def _day_key(lat: float, lon: float, date_str: str) -> tuple[float, float, str]:
    return (round(lat, 4), round(lon, 4), date_str)


async def _prefetch_days(spots: list[dict], date_str: str, timeout: float) -> None:
    """Fill the day cache for every spot with one request per data source.

    Any failure here is swallowed: fetch_replay_spot then fetches the spot on its own.
    """
    missing = [s for s in spots if _day_key(s["lat"], s["lon"], date_str) not in _day_cache]
    if not missing:
        return
    common = {"start_date": date_str, "end_date": date_str, "timezone": "Asia/Kolkata"}
    start, end = _replay_window(date_str)
    try:
        day_ahead, archive = await asyncio.gather(
            _fetch_hourly_bulk(PREVIOUS_RUNS_URL, missing, {"hourly": DAY_AHEAD_KEY, **common}, timeout),
            _fetch_hourly_bulk(ARCHIVE_URL, missing, {"hourly": "precipitation", **common}, timeout),
        )
        complete = [len(_extract_window(h, start, end, DAY_AHEAD_KEY)[0]) >= 24 for h in day_ahead]
        historical = [None] * len(missing)
        if not all(complete):
            historical = await _fetch_hourly_bulk(HIST_FORECAST_URL, missing, {"hourly": "precipitation", **common}, timeout)
        for i, s in enumerate(missing):
            _day_cache[_day_key(s["lat"], s["lon"], date_str)] = {
                "day_ahead": day_ahead[i] if complete[i] else None,
                "historical": historical[i],
                "archive": archive[i],
                "errors": [],
            }
    except OpenMeteoError:
        return


async def _fetch_day(lat: float, lon: float, date_str: str, timeout: float) -> dict:
    """Fetch the day-ahead forecast, the archive, and (only if needed) the historical forecast for one date."""
    key = _day_key(lat, lon, date_str)
    if key in _day_cache:
        return _day_cache[key]
    common = {"start_date": date_str, "end_date": date_str, "timezone": "Asia/Kolkata"}
    day: dict = {"day_ahead": None, "historical": None, "archive": None, "errors": []}

    async def _get(name: str, base: str, hourly: str) -> None:
        try:
            data = await _fetch_json_async(_build_url(base, lat, lon, {"hourly": hourly, **common}), timeout)
            day[name] = data.get("hourly", {})
        except OpenMeteoError as e:
            day["errors"].append(f"{name}: {e}")

    await asyncio.gather(_get("day_ahead", PREVIOUS_RUNS_URL, DAY_AHEAD_KEY), _get("archive", ARCHIVE_URL, "precipitation"))
    start, end = _replay_window(date_str)
    if len(_extract_window(day["day_ahead"] or {}, start, end, DAY_AHEAD_KEY)[0]) < 24:
        day["day_ahead"] = None
        await _get("historical", HIST_FORECAST_URL, "precipitation")
    if day["archive"] is not None:
        _day_cache[key] = day
    return day


async def fetch_replay_spot(
    lat: float, lon: float, date_str: str, timeout: float = 15, as_of_hour: int = 0
) -> dict:
    """
    Fetch replay data for one spot.
    Returns dict with keys:
      forecast_times, forecast_values, forecast_source,
      observed_times, observed_values, observation_source,
      recent_times, recent_values, window_start, window_end, error
    """
    w_start, w_end = _replay_window(date_str, as_of_hour)
    day = await _fetch_day(lat, lon, date_str, timeout)
    expected = _hours_between(w_start, w_end)

    observed_times, observed_values = _extract_window(day["archive"] or {}, w_start, w_end)
    recent = _recent_window(w_start)
    recent_times, recent_values = _extract_window(day["archive"] or {}, recent[0], recent[1]) if recent else ([], [])

    forecast_source = "replay_day_ahead_forecast"
    forecast_times, forecast_values = _extract_window(day["day_ahead"] or {}, w_start, w_end, DAY_AHEAD_KEY)
    if len(forecast_times) < expected:
        forecast_source = "replay_historical_forecast"
        forecast_times, forecast_values = _extract_window(day["historical"] or {}, w_start, w_end)
        # A "historical forecast" identical to the archive is the same analysis, not a forecast.
        if forecast_times and forecast_values == observed_values:
            forecast_source = "replay_archive_fallback"
    if not forecast_times and observed_times:
        forecast_times, forecast_values = list(observed_times), list(observed_values)
        forecast_source = "replay_archive_fallback"

    return {
        "forecast_times": forecast_times,
        "forecast_values": forecast_values,
        "forecast_source": forecast_source,
        "observed_times": observed_times,
        "observed_values": observed_values,
        "observation_source": "replay_archive",
        "recent_times": recent_times,
        "recent_values": recent_values,
        "window_start": w_start,
        "window_end": w_end,
        "error": "; ".join(day["errors"]) or "no rainfall data returned" if not forecast_times and not observed_times else None,
        "fallback_used": forecast_source == "replay_archive_fallback",
    }


async def fetch_live_all(spots: list[dict], timeout: float = 15, max_concurrency: int = 6) -> list[dict]:
    """Fetch live forecast for all spots: one batched request, per-spot requests as a fallback."""
    w_start, w_end = _live_window()
    recent = _recent_window(w_start)
    try:
        hourly = await _fetch_hourly_bulk(LIVE_URL, spots, {
            "hourly": "precipitation", "timezone": "Asia/Kolkata", "forecast_days": "2", "past_days": "1",
        }, timeout)
        results = []
        for spot, h in zip(spots, hourly):
            times, values = _extract_window(h, w_start, w_end)
            recent_times, recent_values = _extract_window(h, recent[0], recent[1]) if recent else ([], [])
            results.append({
                "spot": spot, "times": times, "values": values,
                "recent_times": recent_times, "recent_values": recent_values,
                "window_start": w_start, "window_end": w_end,
                "error": None if times else "no data in window",
            })
        return results
    except OpenMeteoError:
        pass
    sem = asyncio.Semaphore(max_concurrency)

    async def _one(spot: dict) -> dict:
        async with sem:
            try:
                times, values, w_start, w_end, recent_times, recent_values = await fetch_live_spot(spot["lat"], spot["lon"], timeout)
                return {
                    "spot": spot,
                    "times": times,
                    "values": values,
                    "recent_times": recent_times,
                    "recent_values": recent_values,
                    "window_start": w_start,
                    "window_end": w_end,
                    "error": None if times else "no data in window",
                }
            except OpenMeteoError as e:
                w_start, w_end = _live_window()
                return {
                    "spot": spot,
                    "times": [],
                    "values": [],
                    "window_start": w_start,
                    "window_end": w_end,
                    "error": str(e),
                }

    return await asyncio.gather(*[_one(s) for s in spots])


async def fetch_replay_all(spots: list[dict], date_str: str, timeout: float = 15, max_concurrency: int = 6, as_of_hour: int = 0) -> list[dict]:
    """Fetch replay data for all spots: one batched request per source, per-spot requests as a fallback."""
    await _prefetch_days(spots, date_str, timeout)
    sem = asyncio.Semaphore(max_concurrency)

    async def _one(spot: dict) -> dict:
        async with sem:
            result = await fetch_replay_spot(spot["lat"], spot["lon"], date_str, timeout, as_of_hour)
            result["spot"] = spot
            return result

    return await asyncio.gather(*[_one(s) for s in spots])


SYNTHETIC_FIXTURE: dict[str, list[float]] = {
    # Deterministic synthetic data for demo when provider is unavailable.
    # Clearly labelled as synthetic.
    "minto_road_bridge": [0.5, 1.2, 3.4, 8.9, 14.2, 6.1, 2.3, 0.8, 0.4, 0.2, 0.1, 0.0],
    "pul_prahladpur_underpass": [0.2, 0.8, 2.1, 5.5, 11.0, 16.3, 9.2, 4.1, 1.2, 0.6, 0.3, 0.1],
    "ito_area": [0.0, 0.3, 0.9, 1.8, 4.2, 7.5, 12.1, 19.8, 11.4, 3.2, 1.1, 0.4],
    "azadpur": [1.1, 2.3, 4.5, 9.8, 12.4, 8.7, 3.2, 1.5, 0.9, 0.4, 0.2, 0.1],
    "zakhira": [0.3, 0.7, 1.5, 3.8, 8.2, 13.5, 10.2, 5.4, 2.1, 0.9, 0.3, 0.1],
    "mathura_road": [0.1, 0.2, 0.5, 1.2, 2.8, 5.1, 9.3, 14.7, 22.1, 10.5, 4.2, 1.8],
    "dhula_kuan": [0.4, 1.1, 2.8, 6.2, 10.1, 17.8, 13.2, 6.8, 2.9, 1.2, 0.5, 0.2],
    "kashmere_gate": [0.8, 1.9, 3.8, 7.2, 11.5, 9.3, 4.8, 2.1, 1.0, 0.5, 0.2, 0.0],
}


def get_synthetic_window(date_str: str) -> tuple[str, str]:
    return f"{date_str}T00:00", f"{date_str}T11:00"
