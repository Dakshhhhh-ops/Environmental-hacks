"""Bedrock Converse API service — generates structured bilingual proposals.

The agent receives ONLY the structured assessment data; it must not invent
rainfall measurements or alter computed risk classifications.
"""
from __future__ import annotations

import json
import asyncio
import re
import os
import math
from datetime import datetime
from typing import Optional

from ..models import RiskLevel, SpotAssessment, ProposalFlaggedSpot


PROMPT_TEMPLATE = """You are a flood-risk communication assistant for Delhi underpass waterlogging warnings.

You will receive structured risk-assessment data for an upcoming assessment window. Your task is to write a short, actionable bilingual warning draft for a transport dispatcher.

Rules:
- Use ONLY the assessment data provided. Do not invent rainfall values, risk levels, or locations.
- Flag ONLY the locations that are provided as flagged (amber or red). Do not add or remove locations.
- Keep recommended actions concise and generic. Do not assert that alternate routes are safe.
- Include the disclaimer: rainfall intensity is a proxy for potential waterlogging, not a confirmed flood prediction; all thresholds are initial estimates.

Return STRICT JSON (no markdown, no extra text) with this exact schema:
{{
  "english_text": "string — English warning draft (2-4 sentences plus a short bullet list of flagged spots)",
  "hindi_text": "string — Hindi translation of the same warning draft",
  "reasoning": "string — 1-2 sentences explaining why these locations were flagged",
  "flagged_spots": [
    {{
      "spot_id": "string — must exactly match one of the flagged spot IDs provided",
      "risk_level": "string — exactly amber or red as provided",
      "peak_mm_hr": 0.0,
      "peak_time": "string — ISO timestamp as provided",
      "recommended_action": "string — short action for this spot"
    }}
  ],
  "window_start": "string — ISO timestamp as provided",
  "window_end": "string — ISO timestamp as provided"
}}

Assessment window: {window_start} to {window_end}
Data source: {data_source}
Total spots assessed: {total_spots}
Flagged spots (amber/red only):
{flagged_json}

All thresholds are labelled as estimated and unverified (verified=false).
"""

FALLBACK_ENGLISH_TEMPLATE = (
    "Estimated Waterlogging Risk — {window_start} to {window_end}. "
    "Rainfall intensity is a proxy for potential waterlogging, not a confirmed flood prediction. "
    "All thresholds are initial estimates (verified=false). {flagged_summary} "
    "Dispatcher review required before any alert is distributed."
)
FALLBACK_HINDI_TEMPLATE = (
    "अनुमानित जलभराव जोखिम — {window_start} से {window_end} तक। "
    "वर्षा की तीव्रता संभावित जलभराव का संकेत है, पुष्ट बाढ़ की भविष्यवाणी नहीं। "
    "सभी सीमाएँ प्रारंभिक अनुमान हैं (verified=false)। {flagged_summary_hi} "
    "कोई चेतावनी भेजने से पहले डिस्पैचर की समीक्षा आवश्यक है।"
)


def _build_flagged_payload(assessments: list[SpotAssessment]) -> list[dict]:
    flagged: list[dict] = []
    for a in assessments:
        if a.risk_level in (RiskLevel.AMBER, RiskLevel.RED):
            flagged.append({
                "spot_id": a.spot_id,
                "spot_name": a.spot_name,
                "risk_level": a.risk_level.value,
                "peak_mm_hr": a.peak_precipitation_mm_hr,
                "peak_time": a.peak_time or a.assessment_window_start,
                "threshold_mm_hr": a.threshold_mm_hr,
                "risk_ratio": a.risk_ratio,
                "trigger": a.trigger,
                "recorded_peak_mm_hr": a.recent_peak_mm_hr if a.trigger == "recorded_rain" else None,
                "recorded_peak_time": a.recent_peak_time if a.trigger == "recorded_rain" else None,
                "alternate_route_hint": _get_hint(a.spot_id),
            })
    return flagged


def nice_time(iso: Optional[str], with_date: bool = False) -> str:
    """'2024-07-31T20:00' -> '20:00', or '31 Jul 2024, 20:00' with the date."""
    try:
        parsed = datetime.fromisoformat(iso)
    except (TypeError, ValueError):
        return iso or "unknown time"
    return parsed.strftime("%d %b %Y, %H:%M").lstrip("0") if with_date else parsed.strftime("%H:%M")


def describe_flag(a: SpotAssessment) -> str:
    """One computed clause per flagged spot, naming the signal that flagged it."""
    if a.trigger == "recorded_rain":
        return (f"{a.spot_name} ({a.risk_level.value}, {a.recent_peak_mm_hr} mm/hr already recorded at "
                f"{nice_time(a.recent_peak_time)}, threshold {a.threshold_mm_hr} mm/hr)")
    return (f"{a.spot_name} ({a.risk_level.value}, forecast peak {a.peak_precipitation_mm_hr} mm/hr at "
            f"{nice_time(a.peak_time)}, threshold {a.threshold_mm_hr} mm/hr)")


HINDI_LEVEL = {RiskLevel.RED: "उच्च जोखिम", RiskLevel.AMBER: "सतर्कता"}
HINDI_ACTION = {
    RiskLevel.RED: "इन रास्तों से अभी वाहन न भेजें; डिस्पैचर की पुष्टि का इंतज़ार करें।",
    RiskLevel.AMBER: "इन रास्तों पर वाहन भेजने से पहले स्थिति की जाँच करें।",
}


def hindi_summary(flagged: list[SpotAssessment]) -> str:
    """Hindi list of flagged spots by level, with the action for each level. Place names stay as written."""
    if not flagged:
        return "इस अवधि में किसी स्थान ने अनुमानित एहतियाती सीमा पार नहीं की।"
    parts: list[str] = []
    if any(a.trigger == "recorded_rain" for a in flagged):
        parts.append("बारिश अभी हो रही है; यह चेतावनी दर्ज की गई बारिश पर आधारित है।")
    for level in (RiskLevel.RED, RiskLevel.AMBER):
        names = [a.spot_name for a in flagged if a.risk_level == level]
        if names:
            parts.append(f"{HINDI_LEVEL[level]} ({len(names)}): " + ", ".join(names) + "। " + HINDI_ACTION[level])
    return " ".join(parts)


def _get_hint(spot_id: str) -> str:
    return "Review current road conditions and dispatcher-approved alternatives; no route is independently verified as safe."


def build_prompt(assessments: list[SpotAssessment], window_start: str, window_end: str, data_source: str) -> str:
    flagged = _build_flagged_payload(assessments)
    return PROMPT_TEMPLATE.format(
        window_start=window_start,
        window_end=window_end,
        data_source=data_source,
        total_spots=len(assessments),
        flagged_json=json.dumps(flagged, indent=2, ensure_ascii=False),
    )


def _extract_json(text: str) -> str:
    """Robustly extract JSON object from model output."""
    # Try direct parse first
    text = text.strip()
    # Strip markdown fences
    if text.startswith("```"):
        # Remove first fence line and last fence
        lines = text.split("\n")
        # Remove ```json or ``` line
        if lines[0].strip().startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    # Find outermost JSON object
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        return text[start:end + 1]
    return text


def validate_proposal_json(
    raw: dict,
    assessments: list[SpotAssessment],
    window_start: str,
    window_end: str,
) -> dict:
    """Validate that model output matches computed assessment. Raise ValueError on violation."""
    if not isinstance(raw, dict) or set(raw) != {"english_text", "hindi_text", "reasoning", "flagged_spots", "window_start", "window_end"}:
        raise ValueError("proposal JSON does not match the required schema")

    # Build lookup for flagged spots
    expected: dict[str, SpotAssessment] = {
        a.spot_id: a for a in assessments if a.risk_level in (RiskLevel.AMBER, RiskLevel.RED)
    }
    expected_ids = set(expected.keys())
    actual_ids: set[str] = set()

    flagged = raw.get("flagged_spots")
    if not isinstance(flagged, list):
        raise ValueError("flagged_spots must be a list")

    for item in flagged:
        if not isinstance(item, dict):
            raise ValueError("each flagged_spot must be an object")
        sid = item.get("spot_id")
        if sid not in expected_ids:
            raise ValueError(f"hallucinated or unexpected spot_id: {sid}")
        if sid in actual_ids:
            raise ValueError(f"duplicate spot_id: {sid}")
        actual_ids.add(sid)
        exp = expected[sid]
        # Validate risk_level matches computed
        rl = item.get("risk_level")
        if rl not in ("amber", "red"):
            raise ValueError(f"invalid risk_level: {rl}")
        if rl != exp.risk_level.value:
            raise ValueError(f"risk_level mismatch for {sid}: got {rl}, expected {exp.risk_level.value}")
        # Model text can describe a computed assessment, but may not change it.
        peak = item.get("peak_mm_hr")
        if isinstance(peak, bool) or not isinstance(peak, (int, float)):
            raise ValueError(f"peak_mm_hr must be a JSON number for {sid}")
        try:
            pf = float(peak)
        except Exception:
            raise ValueError(f"invalid peak_mm_hr for {sid}: {peak}")
        if not math.isfinite(pf) or abs(pf - exp.peak_precipitation_mm_hr) > 0.01:
            raise ValueError(f"peak_mm_hr mismatch for {sid}: got {pf}, expected {exp.peak_precipitation_mm_hr}")
        pt = item.get("peak_time")
        if not isinstance(pt, str) or pt != (exp.peak_time or exp.assessment_window_start):
            raise ValueError(f"peak_time mismatch for {sid}")
        try:
            datetime.fromisoformat(pt.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"invalid peak_time for {sid}") from exc
        try:
            ProposalFlaggedSpot.model_validate(item)
        except Exception as exc:
            raise ValueError(f"invalid flagged spot schema for {sid}: {exc}") from exc

    if actual_ids != expected_ids:
        raise ValueError("flagged_spots does not match the computed flagged locations")

    # Window validation
    if raw.get("window_start") != window_start:
        raise ValueError(f"window_start mismatch: got {raw.get('window_start')}, expected {window_start}")
    if raw.get("window_end") != window_end:
        raise ValueError(f"window_end mismatch: got {raw.get('window_end')}, expected {window_end}")
    for field, value in (("window_start", window_start), ("window_end", window_end)):
        try:
            datetime.fromisoformat(value.replace("Z", "+00:00"))
        except (ValueError, AttributeError) as exc:
            raise ValueError(f"invalid {field}") from exc

    # Text fields
    for field in ("english_text", "hindi_text", "reasoning"):
        v = raw.get(field)
        if not isinstance(v, str) or not v.strip():
            raise ValueError(f"missing or empty {field}")

    return raw


def build_fallback_proposal(
    assessments: list[SpotAssessment],
    window_start: str,
    window_end: str,
) -> dict:
    """Deterministic template proposal — never pretends to be AI-generated."""
    flagged_a = [a for a in assessments if a.risk_level in (RiskLevel.AMBER, RiskLevel.RED)]
    flagged_summary = (
        "Flagged: " + "; ".join(describe_flag(a) for a in flagged_a) + "."
        if flagged_a else "No locations exceeded the estimated precautionary threshold in this window."
    )
    start, end = nice_time(window_start, with_date=True), nice_time(window_end)
    english_text = FALLBACK_ENGLISH_TEMPLATE.format(window_start=start, window_end=end, flagged_summary=flagged_summary)
    hindi_text = FALLBACK_HINDI_TEMPLATE.format(window_start=start, window_end=end, flagged_summary_hi=hindi_summary(flagged_a))
    reasoning = (
        f"Flagged {len(flagged_a)} of {len(assessments)} assessed locations where forecast or already-recorded "
        f"rainfall reaches at least 60% of the location's estimated threshold. "
        f"{sum(a.trigger == 'recorded_rain' for a in flagged_a)} were flagged by rain already recorded. "
        f"Thresholds are initial estimates."
    )

    flagged_spots: list[dict] = []
    for a in flagged_a:
        flagged_spots.append({
            "spot_id": a.spot_id,
            "risk_level": a.risk_level.value,
            "peak_mm_hr": a.peak_precipitation_mm_hr,
            "peak_time": a.peak_time or window_start,
            "recommended_action": _get_hint(a.spot_id) + " — Dispatcher review required.",
        })

    return {
        "english_text": english_text,
        "hindi_text": hindi_text,
        "reasoning": reasoning,
        "flagged_spots": flagged_spots,
        "window_start": window_start,
        "window_end": window_end,
    }


async def generate_proposal(
    assessments: list[SpotAssessment],
    window_start: str,
    window_end: str,
    data_source: str,
    bedrock_model_id: Optional[str] = None,
    bedrock_region: Optional[str] = None,
    timeout_seconds: float = 30,
) -> tuple[dict, str, Optional[str]]:
    """
    Returns (proposal_json, generated_by, fallback_reason).
    generated_by is 'bedrock' or 'fallback'.
    """
    model_id = bedrock_model_id or os.environ.get("BEDROCK_MODEL_ID", "amazon.nova-lite-v1:0")
    region = bedrock_region or os.environ.get("BEDROCK_REGION", "us-east-1")

    prompt = build_prompt(assessments, window_start, window_end, data_source)

    # Attempt Bedrock call
    try:
        import boto3
        from botocore.config import Config

        def _call_bedrock() -> str:
            client = boto3.client(
                "bedrock-runtime",
                region_name=region,
                config=Config(read_timeout=timeout_seconds, connect_timeout=10, retries={"max_attempts": 1}),
            )
            resp = client.converse(
                modelId=model_id,
                messages=[{"role": "user", "content": [{"text": prompt}]}],
                inferenceConfig={"maxTokens": 2048, "temperature": 0.3},
            )
            # Extract text from response
            content = resp.get("output", {}).get("message", {}).get("content", [])
            texts: list[str] = []
            for block in content:
                if "text" in block:
                    texts.append(block["text"])
            return "\n".join(texts)

        loop = asyncio.get_running_loop()
        raw_text: str = await asyncio.wait_for(
            loop.run_in_executor(None, _call_bedrock),
            timeout=timeout_seconds + 5,
        )
        json_str = _extract_json(raw_text)
        raw_json = json.loads(json_str)
        validated = validate_proposal_json(raw_json, assessments, window_start, window_end)
        return validated, "bedrock", None

    except ImportError as e:
        fallback = build_fallback_proposal(assessments, window_start, window_end)
        return fallback, "fallback", f"boto3 unavailable: {e}"
    except Exception as e:
        # Any Bedrock failure -> fallback
        reason = str(e)[:500]
        fallback = build_fallback_proposal(assessments, window_start, window_end)
        return fallback, "fallback", f"bedrock unavailable: {reason}"
