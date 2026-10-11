"""Verification and threshold calibration — rainfall-based only."""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from ..models import DataSource
from .openmeteo import ARCHIVE_URL, _build_url, _fetch_json_async, _extract_window, OpenMeteoError


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def classify_outcome(
    is_flagged: bool,
    forecast_peak: float,
    observed_peak: Optional[float],
    threshold: float,
) -> str:
    """
    Rainfall-based classification:
      true_alarm: flagged and observed >= 0.6 * threshold
      false_alarm: flagged and observed < 0.6 * threshold
      missed_event: not flagged and observed >= threshold
      no_data: observed is None
    For non-flagged with observed < threshold -> no adjustment needed, return 'no_event' but
    caller should treat as no threshold change.
    """
    if observed_peak is None:
        return "no_data"
    if is_flagged:
        if observed_peak >= 0.6 * threshold:
            return "true_alarm"
        else:
            return "false_alarm"
    else:
        if observed_peak >= threshold:
            return "missed_event"
        else:
            return "no_event"


def adjust_threshold(
    outcome: str,
    current_threshold: float,
    original_threshold: float,
) -> tuple[float, bool]:
    """Return (new_threshold, adjusted)."""
    if outcome == "false_alarm":
        new_t = current_threshold * 1.05
        adjusted = True
    elif outcome == "missed_event":
        new_t = current_threshold * 0.90
        adjusted = True
    else:
        return current_threshold, False

    # Clamp to [0.5x, 2x] original
    lo = original_threshold * 0.5
    hi = original_threshold * 2.0
    new_t = max(lo, min(hi, new_t))
    new_t = round(new_t, 2)
    return new_t, adjusted
