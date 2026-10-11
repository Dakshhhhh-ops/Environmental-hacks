import json
import asyncio
from pathlib import Path

import pytest

from backend.app.models import DataSource, RiskLevel
from backend.app.services.risk import assess_spot, classify_risk
from backend.app.services.verify import adjust_threshold, classify_outcome
from backend.app.services.bedrock import validate_proposal_json, build_fallback_proposal, generate_proposal

SPOT = {"id": "demo", "name": "Demo", "threshold_mm_hr": 10, "original_threshold_mm_hr": 10, "verified": False}


@pytest.mark.parametrize(("ratio", "expected"), [(.599, RiskLevel.GREEN), (.6, RiskLevel.AMBER), (.999, RiskLevel.AMBER), (1, RiskLevel.RED)])
def test_risk_boundaries(ratio, expected):
    assert classify_risk(ratio) == expected


def test_assessment_keeps_valid_rainfall_and_excludes_bad_values():
    result = assess_spot(SPOT, [None, -1, "bad", 6], ["0", "1", "2", "3"], "0", "3", DataSource.SYNTHETIC)
    assert result.data_available
    assert result.hourly_series == [6]
    assert result.hourly_timestamps == ["3"]
    assert result.risk_level == RiskLevel.AMBER


def test_assessment_missing_values_are_unavailable_not_dry():
    result = assess_spot(SPOT, [None, "invalid", -3], ["0", "1", "2"], "0", "2", DataSource.LIVE)
    assert not result.data_available
    assert result.hourly_series == []


@pytest.mark.parametrize(("outcome", "before", "expected"), [("false_alarm", 19, 19.95), ("missed_event", 9, 8.1), ("true_alarm", 10, 10)])
def test_threshold_calibration(outcome, before, expected):
    after, adjusted = adjust_threshold(outcome, before, 10)
    assert after == expected
    assert adjusted is (outcome != "true_alarm")


def test_threshold_calibration_clamps_to_original_bounds():
    assert adjust_threshold("missed_event", 5.01, 10) == (5, True)
    assert adjust_threshold("false_alarm", 19.99, 10) == (20, True)


def test_rainfall_outcomes_do_not_claim_flooding():
    assert classify_outcome(True, 15, 6, 10) == "true_alarm"
    assert classify_outcome(True, 15, 5.9, 10) == "false_alarm"
    assert classify_outcome(False, 4, 10, 10) == "missed_event"
    assert classify_outcome(False, 4, None, 10) == "no_data"


def test_proposal_validator_rejects_unknown_or_inconsistent_spots():
    assessment = assess_spot(SPOT, [12], ["2026-10-10T00:00"], "2026-10-10T00:00", "2026-10-10T11:00", DataSource.SYNTHETIC)
    proposal = {"english_text": "Review", "hindi_text": "समीक्षा", "reasoning": "Threshold", "flagged_spots": [{"spot_id": "demo", "risk_level": "red", "peak_mm_hr": 12, "peak_time": "2026-10-10T00:00", "recommended_action": "Review current conditions."}], "window_start": "2026-10-10T00:00", "window_end": "2026-10-10T11:00"}
    assert validate_proposal_json(proposal, [assessment], proposal["window_start"], proposal["window_end"])
    proposal["flagged_spots"][0]["spot_id"] = "made_up"
    with pytest.raises(ValueError):
        validate_proposal_json(proposal, [assessment], proposal["window_start"], proposal["window_end"])


def test_all_candidate_locations_are_unverified():
    data = json.loads((Path(__file__).resolve().parents[2] / "data" / "spots.json").read_text(encoding="utf-8"))
    assert len(data["spots"]) >= 8
    assert all(s["verified"] is False for s in data["spots"])


def test_fallback_proposal_contains_readable_hindi_and_honest_disclaimer():
    assessment = assess_spot(SPOT, [1], ["2025-05-01T00:00"], "2025-05-01T00:00", "2025-05-01T11:00", DataSource.SYNTHETIC)
    proposal = build_fallback_proposal([assessment], "2025-05-01T00:00", "2025-05-01T11:00")
    assert "अनुमानित जलभराव जोखिम" in proposal["hindi_text"]
    assert "पुष्ट बाढ़ की भविष्यवाणी नहीं" in proposal["hindi_text"]
    assert "à¤" not in proposal["hindi_text"]


def test_malformed_bedrock_response_uses_explicit_fallback(monkeypatch):
    import boto3
    class FakeClient:
        def converse(self, **kwargs):
            return {"output": {"message": {"content": [{"text": "not valid JSON"}]}}}
    monkeypatch.setattr(boto3, "client", lambda *args, **kwargs: FakeClient())
    assessment = assess_spot(SPOT, [7], ["2025-05-01T00:00"], "2025-05-01T00:00", "2025-05-01T11:00", DataSource.SYNTHETIC)
    proposal, generated_by, reason = asyncio.run(generate_proposal(
        [assessment], "2025-05-01T00:00", "2025-05-01T11:00", "synthetic",
        bedrock_model_id="test-model", bedrock_region="us-east-1", timeout_seconds=2))
    assert generated_by == "fallback"
    assert reason.startswith("bedrock unavailable:")
    assert "Estimated Waterlogging Risk" in proposal["english_text"]


def test_agent_reply_parsing_ignores_unknown_spots_and_keeps_computed_values():
    from backend.app.services import agent
    assessments = [_assess_for_agent("a", 12.0, 18.4), _assess_for_agent("b", 15.0, 3.0)]
    reply = (
        "**SUMMARY:** Spot a is red.\nIt needs review.\n"
        "REASONING: Peak 99 mm/hr exceeded the threshold.\n"
        "ACTION a: hold trips until confirmed\n"
        "ACTION invented_spot: close the road\n"
    )
    briefing = agent.parse_briefing(reply, {"a"})
    assert briefing.english_summary == "Spot a is red. It needs review."
    assert briefing.actions == {"a": "hold trips until confirmed"}
    proposal = agent._assemble(briefing, assessments, "2025-07-09T00:00", "2025-07-09T11:00")
    assert [f["spot_id"] for f in proposal["flagged_spots"]] == ["a"]
    assert proposal["flagged_spots"][0]["peak_mm_hr"] == 18.4
    assert proposal["flagged_spots"][0]["risk_level"] == "red"
    assert "18.4 mm/hr" in proposal["english_text"] and "not a confirmed flood" in proposal["english_text"]


def test_agent_failure_and_no_flags_use_labelled_fallback(monkeypatch):
    from backend.app.services import agent
    def boom(*args, **kwargs):
        raise RuntimeError("model offline")
    monkeypatch.setattr(agent, "_run_agent", boom)
    flagged = [_assess_for_agent("a", 12.0, 18.4)]
    _, generated_by, reason = asyncio.run(agent.generate_proposal(flagged, "2025-07-09T00:00", "2025-07-09T11:00", "synthetic"))
    assert generated_by == "fallback" and "model offline" in reason
    calm = [_assess_for_agent("a", 12.0, 1.0)]
    proposal, generated_by, reason = asyncio.run(agent.generate_proposal(calm, "2025-07-09T00:00", "2025-07-09T11:00", "synthetic"))
    assert generated_by == "fallback" and proposal["flagged_spots"] == [] and "no flagged" in reason


def _assess_for_agent(spot_id, threshold, peak):
    from backend.app.models import SpotAssessment
    ratio = peak / threshold
    return SpotAssessment.model_validate({
        "spot_id": spot_id, "spot_name": f"Spot {spot_id}", "threshold_mm_hr": threshold,
        "peak_precipitation_mm_hr": peak, "peak_time": "2025-07-09T07:00", "risk_ratio": ratio,
        "risk_level": "red" if ratio >= 1 else "amber" if ratio >= 0.6 else "green",
        "data_source": "synthetic", "assessment_window_start": "2025-07-09T00:00",
        "assessment_window_end": "2025-07-09T11:00",
    })


def test_route_scoring_counts_only_flagged_spots_near_the_path():
    from backend.app.services.routing import distance_to_path_m, score_route
    path = [[28.60, 77.20], [28.60, 77.30]]
    spots = [
        {"id": "on_red", "name": "On route red", "lat": 28.6005, "lon": 77.25},
        {"id": "on_green", "name": "On route green", "lat": 28.6005, "lon": 77.26},
        {"id": "far_red", "name": "Far red", "lat": 28.65, "lon": 77.25},
    ]
    assert 40 < distance_to_path_m(28.6005, 77.25, path) < 70
    hits, score = score_route(path, spots, {"on_red": "red", "on_green": "green", "far_red": "red"})
    assert [h["spot_id"] for h in hits] == ["on_red"] and score == 10
