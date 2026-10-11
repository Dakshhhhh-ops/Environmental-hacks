"""Strands Agents SDK briefing agent — drafts the dispatcher proposal.

The agent reads the computed assessment through tools and writes only prose:
an English briefing and its reasoning. Every
number, risk level, spot id, and timestamp in the stored proposal is copied
from the deterministic assessment, never from model output. The Hindi text is
the vetted template unless a Bedrock model is configured, because small local
models do not produce reliable Hindi.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
from typing import Optional

from pydantic import BaseModel

from ..models import RiskLevel, SpotAssessment
from .bedrock import _build_flagged_payload, action_for, build_fallback_proposal, describe_flag, validate_proposal_json
from .storage import get_storage


SYSTEM_PROMPT = """You brief a Delhi transport dispatcher on rain-related waterlogging risk at underpasses.
First call get_flagged_spots. Then call get_calibration_history for the worst one or two spots.
Then reply in exactly this format and nothing else:
SUMMARY: <2-3 plain English sentences: how many spots are high risk and how many are on watch, then the worst two or three by spot_name>
REASONING: <1-2 sentences on why they were flagged and how past alerts for them performed>

Rules:
- In SUMMARY and REASONING write the spot_name, never the spot_id.
- Use only values returned by the tools. Never invent rainfall values, risk levels, or locations.
- Rainfall is a proxy for possible waterlogging, not a confirmed flood. Never say a place is flooded, under water, or has high water levels: you only know about rain.
- If high_risk_from_recorded_rain or watch_from_recorded_rain is above zero, say first that rain is already falling and the forecast did not predict it.
- Each spot has a "status" field. Repeat it as given: amber means the forecast peak is close to but still below the threshold; red means it reached or passed the threshold.
- Do not give instructions or actions; those are set by the rule engine. Never order evacuations and never say a route is safe."""

DISCLAIMER = (
    "Rainfall intensity is a proxy for potential waterlogging, not a confirmed flood prediction. "
    "All thresholds are initial estimates (verified=false). "
    "Dispatcher review required before any alert is distributed."
)


class Briefing(BaseModel):
    """Prose parsed from the agent reply."""

    english_summary: str
    reasoning: str = ""
    actions: dict[str, str] = {}


_LINE = re.compile(r"^[\s*#-]*(SUMMARY|REASONING|ACTION)[\s*]*([\w-]*)[\s*]*:\s*(.*)$", re.IGNORECASE)


def parse_briefing(text: str, flagged_ids: set[str]) -> Briefing:
    """Parse the SUMMARY / REASONING / ACTION lines; ignore actions for unknown spots."""
    summary: list[str] = []
    reasoning: list[str] = []
    actions: dict[str, str] = {}
    current: Optional[list[str]] = None
    for line in text.splitlines():
        m = _LINE.match(line)
        if not m:
            if current is not None and line.strip():
                current.append(line.strip())
            continue
        kind, spot_id, rest = m.group(1).upper(), m.group(2), m.group(3).strip(" *")
        if kind == "ACTION":
            current = None
            if spot_id in flagged_ids and rest and spot_id not in actions:
                actions[spot_id] = rest
        else:
            current = summary if kind == "SUMMARY" else reasoning
            if rest:
                current.append(rest)
    if not summary:
        raise ValueError("agent reply had no SUMMARY line")
    return Briefing(english_summary=" ".join(summary), reasoning=" ".join(reasoning), actions=actions)


# A small model loses the thread on long lists, so it sees only the worst few spots.
MAX_SPOTS_FOR_MODEL = 5
MAX_HISTORY_LOOKUPS = 2

# Claims the data cannot support. Rainfall is the only input: nothing here observes water on the road.
UNSUPPORTED_CLAIMS = ("water level", "flooded", "is flooding", "are flooding", "submerged", "inundated",
                      "is safe", "are safe", "safe to", "safely", "evacuat", "confirmed flood", "guarantee")


def makes_unsupported_claim(text: str) -> bool:
    lowered = text.lower()
    return any(phrase in lowered for phrase in UNSUPPORTED_CLAIMS)


FLAGGED_LEVELS = (RiskLevel.RED, RiskLevel.AMBER)


def _status(a: SpotAssessment) -> str:
    pct = round(a.risk_ratio * 100)
    if a.trigger == "recorded_rain":
        return (f"rain ALREADY RECORDED in the last hours reached {pct}% of the estimated threshold; "
                f"the forecast alone did not flag this spot")
    if a.risk_level == RiskLevel.RED:
        return f"forecast peak reached {pct}% of the estimated threshold (at or above it)"
    return f"forecast peak reached {pct}% of the estimated threshold (close to it, still below)"


def agent_provider() -> str:
    return (os.environ.get("AGENT_PROVIDER") or "ollama").strip().lower()


def agent_model_id() -> str:
    if agent_provider() == "bedrock":
        return os.environ.get("BEDROCK_MODEL_ID", "amazon.nova-lite-v1:0")
    return os.environ.get("OLLAMA_MODEL_ID", "qwen3-vl:2b-instruct")


def _build_model():
    if agent_provider() == "bedrock":
        from strands.models import BedrockModel
        return BedrockModel(
            model_id=agent_model_id(),
            region_name=os.environ.get("BEDROCK_REGION", "us-east-1"),
            temperature=0.2,
            max_tokens=600,
        )
    from strands.models.ollama import OllamaModel
    return OllamaModel(
        host=os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434"),
        model_id=agent_model_id(),
        temperature=0.2,
        max_tokens=600,
    )


def _history_for(spot_id: str) -> dict:
    """Summarise past rainfall verifications for one spot."""
    try:
        records = [r for r in get_storage().list_history() if r.get("spot_id") == spot_id]
    except Exception as exc:
        return {"spot_id": spot_id, "verified_alerts": 0, "note": f"history unavailable: {exc}"}
    counts: dict[str, int] = {}
    for r in records:
        counts[r.get("outcome", "unknown")] = counts.get(r.get("outcome", "unknown"), 0) + 1
    return {
        "spot_id": spot_id,
        "verified_alerts": len(records),
        "outcomes": counts,
        "note": "Outcomes compare forecast with observed rainfall only; no flood observations exist.",
    }


def _run_agent(flagged: list[dict], window_start: str, window_end: str, data_source: str, total: int) -> tuple[Briefing, list[str]]:
    from strands import Agent, tool

    flagged_ids = {f["spot_id"] for f in flagged}
    tools_called: list[str] = []

    @tool
    def get_flagged_spots() -> str:
        """Return the computed assessment: every flagged spot with its risk level, peak rainfall, peak time and estimated threshold."""
        tools_called.append("get_flagged_spots")
        worst = sorted(flagged, key=lambda f: -f["risk_ratio"])[:MAX_SPOTS_FOR_MODEL]
        return json.dumps({
            "window_start": window_start,
            "window_end": window_end,
            "data_source": data_source,
            "total_spots_assessed": total,
            "high_risk_count": sum(f["risk_level"] == "red" for f in flagged),
            "watch_count": sum(f["risk_level"] == "amber" for f in flagged),
            "high_risk_from_recorded_rain": sum(f["risk_level"] == "red" and f["trigger"] == "recorded_rain" for f in flagged),
            "watch_from_recorded_rain": sum(f["risk_level"] == "amber" and f["trigger"] == "recorded_rain" for f in flagged),
            "worst_spots": worst,
        }, ensure_ascii=False)

    @tool
    def get_calibration_history(spot_id: str) -> str:
        """Return how past alerts for one spot compared with observed rainfall (true alarms, false alarms, missed events).

        Args:
            spot_id: A spot_id returned by get_flagged_spots.
        """
        # Two lookups are enough for a briefing and keep the request inside API Gateway's 30 second limit.
        if sum(t.startswith("get_calibration_history") for t in tools_called) >= MAX_HISTORY_LOOKUPS:
            return json.dumps({"note": "history lookup limit reached; write the briefing now"})
        tools_called.append(f"get_calibration_history:{spot_id}")
        if spot_id not in flagged_ids:
            return json.dumps({"error": f"{spot_id} is not a flagged spot"})
        return json.dumps(_history_for(spot_id))

    agent = Agent(
        model=_build_model(),
        tools=[get_flagged_spots, get_calibration_history],
        system_prompt=SYSTEM_PROMPT,
        callback_handler=None,
    )
    result = agent(
        f"Draft the dispatcher briefing for the window {window_start} to {window_end}. "
        f"{len(flagged)} of {total} assessed spots are flagged."
    )
    if "get_flagged_spots" not in tools_called:
        raise ValueError("agent drafted a briefing without reading the assessment")
    return parse_briefing(str(result), flagged_ids), tools_called


def _assemble(briefing: Briefing, assessments: list[SpotAssessment], window_start: str, window_end: str) -> dict:
    """Merge agent prose with computed values. Numbers come from the assessment only."""
    template = build_fallback_proposal(assessments, window_start, window_end)
    flagged_a = [a for a in assessments if a.risk_level in (RiskLevel.AMBER, RiskLevel.RED)]

    red = [a.spot_name for a in flagged_a if a.risk_level == RiskLevel.RED]
    amber = [a.spot_name for a in flagged_a if a.risk_level == RiskLevel.AMBER]
    english = briefing.english_summary.strip()
    # Reject a summary that leaked raw spot ids or claims flooding or safety; use the computed sentence.
    if not english or makes_unsupported_claim(english) or sum(a.spot_id in english for a in flagged_a if "_" in a.spot_id) >= 2:
        worst = max(flagged_a, key=lambda a: a.risk_ratio)
        english = (
            f"{len(red)} low point(s) are at high risk and {len(amber)} are on watch in this window. "
            f"The worst is {describe_flag(worst)}."
        )
    recorded = sum(a.trigger == "recorded_rain" for a in flagged_a)
    if recorded:
        english += f" {recorded} of these were flagged by rain already recorded, not by the forecast."
    lists = "\n".join(part for part in (
        f"HIGH RISK ({len(red)}): " + ", ".join(red) + "." if red else "",
        f"WATCH ({len(amber)}): " + ", ".join(amber) + "." if amber else "",
    ) if part)
    english = f"{english}\n\n{lists}\n\n{DISCLAIMER}"

    proposal = {
        "english_text": english,
        "hindi_text": template["hindi_text"],
        "reasoning": template["reasoning"] + (
            f" Agent note: {briefing.reasoning.strip()}"
            if briefing.reasoning.strip() and not makes_unsupported_claim(briefing.reasoning) else ""
        ),
        "flagged_spots": [
            {
                "spot_id": a.spot_id,
                "risk_level": a.risk_level.value,
                "peak_mm_hr": a.peak_precipitation_mm_hr,
                "peak_time": a.peak_time or window_start,
                "recommended_action": action_for(a),
            }
            for a in flagged_a
        ],
        "window_start": window_start,
        "window_end": window_end,
    }
    return validate_proposal_json(proposal, assessments, window_start, window_end)


async def generate_proposal(
    assessments: list[SpotAssessment],
    window_start: str,
    window_end: str,
    data_source: str,
    timeout_seconds: Optional[float] = None,
) -> tuple[dict, str, Optional[str]]:
    """Returns (proposal_json, generated_by, fallback_reason).

    generated_by is 'strands-agent' or 'fallback'.
    """
    flagged = _build_flagged_payload(assessments)
    status = {a.spot_id: _status(a) for a in assessments if a.risk_level in FLAGGED_LEVELS}
    for f in flagged:
        f["status"] = status[f["spot_id"]]
        f.pop("alternate_route_hint", None)
    if not flagged:
        # Nothing to brief on; the template states that plainly without a model call.
        return build_fallback_proposal(assessments, window_start, window_end), "fallback", "no flagged locations; agent not needed"

    timeout = timeout_seconds or float(os.environ.get("AGENT_TIMEOUT_SECONDS", "90"))
    try:
        loop = asyncio.get_running_loop()
        briefing, tools_called = await asyncio.wait_for(
            loop.run_in_executor(None, _run_agent, flagged, window_start, window_end, data_source, len(assessments)),
            timeout=timeout,
        )
        proposal = _assemble(briefing, assessments, window_start, window_end)
        proposal["agent_trace"] = {
            "framework": "Strands Agents SDK",
            "provider": agent_provider(),
            "model": agent_model_id(),
            "tools_called": tools_called,
        }
        return proposal, "strands-agent", None
    except Exception as e:
        reason = (str(e) or type(e).__name__)[:500]
        return build_fallback_proposal(assessments, window_start, window_end), "fallback", f"strands agent unavailable: {reason}"
