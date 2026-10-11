"""Strands Agents SDK briefing agent — drafts the dispatcher proposal.

The agent reads the computed assessment through tools and writes only prose:
an English briefing, the reasoning, and one action per flagged spot. Every
number, risk level, spot id, and timestamp in the stored proposal is copied
from the deterministic assessment, never from model output. The Hindi text is
the vetted template unless a Bedrock model is configured, because small local
models do not produce reliable Hindi.
"""
from __future__ import annotations

import asyncio
import json
import os
from typing import Optional

from pydantic import BaseModel, Field

from ..models import RiskLevel, SpotAssessment
from .bedrock import _build_flagged_payload, _get_hint, build_fallback_proposal, validate_proposal_json
from .storage import get_storage


SYSTEM_PROMPT = """You are a briefing assistant for a Delhi transport dispatcher who must decide whether to warn drivers about rain-related waterlogging at underpasses.

Call get_flagged_spots to read the computed assessment. Call get_calibration_history for a flagged spot to see how past alerts for that spot compared with observed rainfall.

Rules:
- Use only values returned by the tools. Never invent rainfall values, risk levels, or locations.
- Rainfall intensity is a proxy for potential waterlogging, not a confirmed flood. Say so.
- Do not claim any alternate route is safe.
- Write plain English a busy dispatcher can read in ten seconds.
- Give exactly one short action for every flagged spot, using its spot_id."""

DISCLAIMER = (
    "Rainfall intensity is a proxy for potential waterlogging, not a confirmed flood prediction. "
    "All thresholds are initial estimates (verified=false). "
    "Dispatcher review required before any alert is distributed."
)


class SpotAction(BaseModel):
    spot_id: str = Field(description="Exactly one of the flagged spot_id values")
    recommended_action: str = Field(description="One short action for the dispatcher at this spot")


class Briefing(BaseModel):
    """Dispatcher briefing drafted from the computed assessment."""

    english_summary: str = Field(description="2-4 sentence English briefing naming the flagged spots, worst first")
    reasoning: str = Field(description="1-2 sentences on why these spots were flagged and how much to trust it")
    actions: list[SpotAction] = Field(default_factory=list, description="One entry per flagged spot")
    hindi_summary: Optional[str] = Field(default=None, description="Hindi translation of english_summary, if you can write fluent Hindi")


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
        )
    from strands.models.ollama import OllamaModel
    return OllamaModel(
        host=os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434"),
        model_id=agent_model_id(),
        temperature=0.2,
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
        return json.dumps({
            "window_start": window_start,
            "window_end": window_end,
            "data_source": data_source,
            "total_spots_assessed": total,
            "flagged_spots": flagged,
        }, ensure_ascii=False)

    @tool
    def get_calibration_history(spot_id: str) -> str:
        """Return how past alerts for one spot compared with observed rainfall (true alarms, false alarms, missed events).

        Args:
            spot_id: A spot_id returned by get_flagged_spots.
        """
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
        f"{len(flagged)} of {total} spots are flagged. Read the assessment with your tools first.",
        structured_output_model=Briefing,
    )
    briefing = result.structured_output
    if not isinstance(briefing, Briefing):
        raise ValueError("agent returned no structured briefing")
    return briefing, tools_called


def _has_devanagari(text: Optional[str]) -> bool:
    return bool(text) and sum("ऀ" <= ch <= "ॿ" for ch in text) >= 10


def _assemble(briefing: Briefing, assessments: list[SpotAssessment], window_start: str, window_end: str) -> dict:
    """Merge agent prose with computed values. Numbers come from the assessment only."""
    template = build_fallback_proposal(assessments, window_start, window_end)
    flagged_a = [a for a in assessments if a.risk_level in (RiskLevel.AMBER, RiskLevel.RED)]
    actions = {a.spot_id: a.recommended_action.strip() for a in briefing.actions if a.recommended_action.strip()}

    facts = "; ".join(
        f"{a.spot_name} ({a.risk_level.value}, peak {a.peak_precipitation_mm_hr} mm/hr at {a.peak_time}, threshold {a.threshold_mm_hr} mm/hr)"
        for a in flagged_a
    )
    english = briefing.english_summary.strip()
    if not english:
        raise ValueError("agent returned an empty briefing")
    english = f"{english}\n\nComputed values: {facts}.\n\n{DISCLAIMER}"

    use_model_hindi = agent_provider() == "bedrock" and _has_devanagari(briefing.hindi_summary)
    proposal = {
        "english_text": english,
        "hindi_text": briefing.hindi_summary.strip() if use_model_hindi else template["hindi_text"],
        "reasoning": briefing.reasoning.strip() or template["reasoning"],
        "flagged_spots": [
            {
                "spot_id": a.spot_id,
                "risk_level": a.risk_level.value,
                "peak_mm_hr": a.peak_precipitation_mm_hr,
                "peak_time": a.peak_time or window_start,
                "recommended_action": actions.get(a.spot_id) or _get_hint(a.spot_id),
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
