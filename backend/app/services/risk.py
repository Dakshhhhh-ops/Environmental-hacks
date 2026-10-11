"""Deterministic risk assessment engine.

Classification is computed by backend code, never invented by the LLM.
"""
from __future__ import annotations

from typing import Optional
import math

from ..models import RiskLevel, DataSource, SpotAssessment


def classify_risk(ratio: float, green_threshold: float = 0.6, amber_threshold: float = 1.0) -> RiskLevel:
    """Return risk level from risk ratio."""
    if ratio < green_threshold:
        return RiskLevel.GREEN
    if ratio < amber_threshold:
        return RiskLevel.AMBER
    return RiskLevel.RED


def assess_spot(
    spot: dict,
    precip_values: list[float],
    precip_times: list[str],
    window_start: str,
    window_end: str,
    data_source: DataSource,
    threshold_override: Optional[float] = None,
) -> SpotAssessment:
    """
    Deterministic assessment for one spot.
    - precip_values/times: hourly series in the assessment window.
    - threshold_override: calibrated threshold if available, else spot threshold.
    """
    threshold = threshold_override if threshold_override is not None else float(spot["threshold_mm_hr"])
    verified = bool(spot.get("verified", False))

    # Handle unavailable data
    if not precip_values or not precip_times or threshold <= 0:
        return SpotAssessment(
            spot_id=spot["id"],
            spot_name=spot["name"],
            threshold_mm_hr=threshold,
            peak_precipitation_mm_hr=0.0,
            peak_time=None,
            risk_ratio=0.0,
            risk_level=RiskLevel.GREEN,
            hourly_series=[],
            hourly_timestamps=[],
            data_source=data_source,
            assessment_window_start=window_start,
            assessment_window_end=window_end,
            data_available=False,
            verified=verified,
        )

    # Missing or malformed values are excluded, never treated as dry weather.
    clean: list[float] = []
    clean_times: list[str] = []
    for index, v in enumerate(precip_values):
        try:
            fv = float(v)
            if not math.isfinite(fv) or fv < 0:
                continue
            clean.append(fv)
            if index < len(precip_times):
                clean_times.append(precip_times[index])
        except (TypeError, ValueError, OverflowError):
            continue

    if not clean:
        return SpotAssessment(
            spot_id=spot["id"],
            spot_name=spot["name"],
            threshold_mm_hr=threshold,
            peak_precipitation_mm_hr=0.0,
            peak_time=None,
            risk_ratio=0.0,
            risk_level=RiskLevel.GREEN,
            hourly_series=[],
            hourly_timestamps=[],
            data_source=data_source,
            assessment_window_start=window_start,
            assessment_window_end=window_end,
            data_available=False,
            verified=verified,
        )

    peak = max(clean)
    peak_idx = clean.index(peak)
    peak_time = clean_times[peak_idx] if peak_idx < len(clean_times) else None

    # Avoid division by zero (threshold is validated positive, but be safe)
    ratio = peak / threshold

    level = classify_risk(ratio)

    return SpotAssessment(
        spot_id=spot["id"],
        spot_name=spot["name"],
        threshold_mm_hr=threshold,
        peak_precipitation_mm_hr=round(float(peak), 2),
        peak_time=peak_time,
        risk_ratio=round(float(ratio), 4),
        risk_level=level,
        hourly_series=[round(float(x), 2) for x in clean],
        hourly_timestamps=clean_times,
        data_source=data_source,
        assessment_window_start=window_start,
        assessment_window_end=window_end,
        data_available=True,
        verified=verified,
    )
