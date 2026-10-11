"""Backtest the alert rules against two Delhi monsoon seasons.

For every monitored spot and every day, compares the day-ahead forecast (Open-Meteo
Previous Runs API) with recorded rain (Open-Meteo Archive API) using the same rules as
the app: a spot is flagged when the peak reaches 60% of its threshold, and a heavy-rain
event is a recorded peak at or above the threshold.

Recorded rain here is a weather-model analysis, not a rain gauge, and none of this
measures flooding. Writes data/backtest.json, which the dashboard shows.

Usage: python scripts/backtest.py
"""
from __future__ import annotations

import json
import sys
import time
import urllib.parse
import urllib.request
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SEASONS = [("2024-06-15", "2024-09-30"), ("2025-06-15", "2025-09-30")]
PREVIOUS_RUNS_URL = "https://previous-runs-api.open-meteo.com/v1/forecast"
ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
WATCH_RATIO = 0.6


def fetch(base: str, spots: list[dict], start: str, end: str, hourly: str) -> list[dict]:
    url = base + "?" + urllib.parse.urlencode({
        "latitude": ",".join(str(s["lat"]) for s in spots),
        "longitude": ",".join(str(s["lon"]) for s in spots),
        "start_date": start, "end_date": end, "timezone": "Asia/Kolkata", "hourly": hourly,
    }, safe=",")
    for attempt in range(3):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "Underpass-Sentinel/1.0"}), timeout=120) as resp:
                data = json.loads(resp.read())
            return [item["hourly"] for item in (data if isinstance(data, list) else [data])]
        except Exception as exc:  # retry transient network errors
            if attempt == 2:
                raise
            print(f"retrying after: {exc}", file=sys.stderr)
            time.sleep(5)
    return []


def main() -> None:
    spots = json.loads((ROOT / "data" / "spots.json").read_text(encoding="utf-8"))["spots"]
    totals = {"spot_days": 0, "events": 0, "caught_by_forecast": 0, "missed_by_forecast": 0,
              "false_alarms": 0, "forecast_flags": 0, "missed_with_earlier_watch_level_rain": 0}
    days_with_event: set[str] = set()
    days_total: set[str] = set()
    worst = {"date": None, "spot": None, "recorded_mm_hr": 0.0, "forecast_mm_hr": 0.0}

    for start, end in SEASONS:
        forecast = fetch(PREVIOUS_RUNS_URL, spots, start, end, "precipitation_previous_day1")
        recorded = fetch(ARCHIVE_URL, spots, start, end, "precipitation")
        for spot, f, r in zip(spots, forecast, recorded):
            threshold = float(spot["original_threshold_mm_hr"])
            for i in range(0, len(f["time"]), 24):
                fc = f["precipitation_previous_day1"][i:i + 24]
                ob = r["precipitation"][i:i + 24]
                if len(fc) < 24 or len(ob) < 24 or any(v is None for v in fc) or any(v is None for v in ob):
                    continue
                day = f["time"][i][:10]
                days_total.add(day)
                totals["spot_days"] += 1
                flagged = max(fc) >= WATCH_RATIO * threshold
                event = max(ob) >= threshold
                totals["forecast_flags"] += flagged
                if flagged and max(ob) < WATCH_RATIO * threshold:
                    totals["false_alarms"] += 1
                if not event:
                    continue
                totals["events"] += 1
                days_with_event.add(day)
                if max(ob) > worst["recorded_mm_hr"]:
                    worst = {"date": day, "spot": spot["name"], "recorded_mm_hr": max(ob), "forecast_mm_hr": max(fc)}
                if flagged:
                    totals["caught_by_forecast"] += 1
                    continue
                totals["missed_by_forecast"] += 1
                first_event_hour = next(h for h, v in enumerate(ob) if v >= threshold)
                if any(v >= WATCH_RATIO * threshold for v in ob[:first_event_hour]):
                    totals["missed_with_earlier_watch_level_rain"] += 1

    result = {
        "generated_on": date.today().isoformat(),
        "seasons": [f"{a} to {b}" for a, b in SEASONS],
        "spots": len(spots),
        "days": len(days_total),
        "days_with_any_event": len(days_with_event),
        **totals,
        "worst_event": worst,
        "method": ("A spot-day is flagged when the day-ahead forecast peak reaches 60% of the spot's threshold. "
                   "An event is a recorded hourly peak at or above the threshold. A false alarm is a flag on a day "
                   "whose recorded peak stayed under 60% of the threshold. Recorded rain is a weather-model analysis, "
                   "not a gauge reading. Thresholds are unverified estimates. Flooding is not measured."),
    }
    out = ROOT / "data" / "backtest.json"
    out.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
