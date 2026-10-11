"""Open-Meteo service — Live forecast, Historical Forecast, and Archive APIs.

Window convention (documented):
- All timestamps are in Asia/Kolkata (UTC+05:30).
- Live mode: the assessment window is the next 12 forecast hours starting from
  the current local hour (inclusive). The API is called with forecast_days=2 and
  the 12-hour slice is extracted client-side by matching timestamps.
- Replay mode: the assessment window is 00:00–11:00 on the user-selected date
  (12 hours, timezone=Asia/Kolkata). Both historical-forecast and archive are
  queried for that single date; the 12-hour slice is extracted the same way.
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


def _extract_window(hourly: dict, window_start_str: str, window_end_str: str) -> tuple[list[str], list[float]]:
    """Extract timestamps and precipitation values within [window_start, window_end] inclusive."""
    times: list[str] = hourly.get("time", [])
    precips = hourly.get("precipitation", [])
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


def _replay_window(date_str: str) -> tuple[str, str]:
    """Return (window_start, window_end) for replay date (YYYY-MM-DD) 00:00-11:00."""
    return f"{date_str}T00:00", f"{date_str}T11:00"


async def fetch_live_spot(lat: float, lon: float, timeout: float = 15) -> tuple[list[str], list[float], str, str]:
    """Fetch live forecast for one spot. Returns (timestamps, values, window_start, window_end)."""
    w_start, w_end = _live_window()
    url = _build_url(LIVE_URL, lat, lon, {
        "hourly": "precipitation",
        "timezone": "Asia/Kolkata",
        "forecast_days": "2",
    })
    data = await _fetch_json_async(url, timeout)
    hourly = data.get("hourly", {})
    times, values = _extract_window(hourly, w_start, w_end)
    return times, values, w_start, w_end


async def fetch_replay_spot(
    lat: float, lon: float, date_str: str, timeout: float = 15
) -> dict:
    """
    Fetch replay data for one spot.
    Returns dict with keys:
      forecast_times, forecast_values, forecast_source,
      observed_times, observed_values, observation_source,
      window_start, window_end, error
    """
    w_start, w_end = _replay_window(date_str)

    forecast_times: list[str] = []
    forecast_values: list[float] = []
    forecast_source = "replay_historical_forecast"
    observed_times: list[str] = []
    observed_values: list[float] = []
    observation_source = "replay_archive"
    error: Optional[str] = None

    # 1. Historical forecast
    hf_url = _build_url(HIST_FORECAST_URL, lat, lon, {
        "hourly": "precipitation",
        "start_date": date_str,
        "end_date": date_str,
        "timezone": "Asia/Kolkata",
    })
    hf_ok = False
    try:
        hf_data = await _fetch_json_async(hf_url, timeout)
        hf_hourly = hf_data.get("hourly", {})
        forecast_times, forecast_values = _extract_window(hf_hourly, w_start, w_end)
        if forecast_times:
            hf_ok = True
        else:
            error = "historical forecast returned empty window"
    except OpenMeteoError as e:
        error = str(e)

    # 2. Archive (observed)
    arch_url = _build_url(ARCHIVE_URL, lat, lon, {
        "hourly": "precipitation",
        "start_date": date_str,
        "end_date": date_str,
        "timezone": "Asia/Kolkata",
    })
    try:
        arch_data = await _fetch_json_async(arch_url, timeout)
        arch_hourly = arch_data.get("hourly", {})
        observed_times, observed_values = _extract_window(arch_hourly, w_start, w_end)
    except OpenMeteoError as e:
        if error:
            error += f"; archive also failed: {e}"
        else:
            error = f"archive failed: {e}"

    # Fallback: if historical forecast empty/failed but archive has data, use archive as forecast fallback
    if not hf_ok and observed_times:
        forecast_times = list(observed_times)
        forecast_values = list(observed_values)
        forecast_source = "replay_archive_fallback"

    return {
        "forecast_times": forecast_times,
        "forecast_values": forecast_values,
        "forecast_source": forecast_source,
        "observed_times": observed_times,
        "observed_values": observed_values,
        "observation_source": observation_source,
        "window_start": w_start,
        "window_end": w_end,
        "error": error if not hf_ok and not observed_times else None,
        "fallback_used": forecast_source == "replay_archive_fallback",
    }


async def fetch_live_all(spots: list[dict], timeout: float = 15, max_concurrency: int = 6) -> list[dict]:
    """Fetch live forecast for all spots with concurrency limit."""
    sem = asyncio.Semaphore(max_concurrency)

    async def _one(spot: dict) -> dict:
        async with sem:
            try:
                times, values, w_start, w_end = await fetch_live_spot(spot["lat"], spot["lon"], timeout)
                return {
                    "spot": spot,
                    "times": times,
                    "values": values,
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


async def fetch_replay_all(spots: list[dict], date_str: str, timeout: float = 15, max_concurrency: int = 6) -> list[dict]:
    """Fetch replay data for all spots with concurrency limit."""
    sem = asyncio.Semaphore(max_concurrency)

    async def _one(spot: dict) -> dict:
        async with sem:
            result = await fetch_replay_spot(spot["lat"], spot["lon"], date_str, timeout)
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
