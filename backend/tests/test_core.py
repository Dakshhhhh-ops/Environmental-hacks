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
    assert "HIGH RISK (1): Spot a." in proposal["english_text"] and "not a confirmed flood" in proposal["english_text"]


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


def test_recorded_rain_raises_risk_when_forecast_missed_it():
    from backend.app.services.risk import apply_recorded_rain
    from backend.app.services.bedrock import describe_flag
    quiet = assess_spot(SPOT, [1.0, 2.0], ["2024-07-31T20:00", "2024-07-31T21:00"], "2024-07-31T20:00", "2024-07-31T23:00", DataSource.REPLAY_DAY_AHEAD_FORECAST)
    assert quiet.risk_level == RiskLevel.GREEN and quiet.trigger == "forecast"
    hit = apply_recorded_rain(quiet, [0.0, 1.8, SPOT["threshold_mm_hr"] * 2], ["2024-07-31T17:00", "2024-07-31T18:00", "2024-07-31T19:00"])
    assert hit.risk_level == RiskLevel.RED and hit.trigger == "recorded_rain"
    assert hit.recent_peak_time == "2024-07-31T19:00" and hit.peak_precipitation_mm_hr == 2.0
    assert "already recorded" in describe_flag(hit)


def test_recorded_rain_never_lowers_a_forecast_flag():
    from backend.app.services.risk import apply_recorded_rain
    wet = assess_spot(SPOT, [SPOT["threshold_mm_hr"] * 1.5], ["2024-09-13T06:00"], "2024-09-13T00:00", "2024-09-13T23:00", DataSource.REPLAY_DAY_AHEAD_FORECAST)
    same = apply_recorded_rain(wet, [0.0, 0.1], ["2024-09-12T22:00", "2024-09-12T23:00"])
    assert same.risk_level == RiskLevel.RED and same.trigger == "forecast" and same.recent_peak_mm_hr == 0.1


def test_replay_marks_forecast_identical_to_archive_as_stand_in(monkeypatch):
    from backend.app.services import openmeteo
    hours = [f"2023-07-09T{h:02d}:00" for h in range(24)]
    rain = [float(h % 5) for h in range(24)]
    async def fake_day(lat, lon, date, timeout):
        return {"day_ahead": None, "historical": {"time": hours, "precipitation": rain}, "archive": {"time": hours, "precipitation": rain}, "errors": []}
    monkeypatch.setattr(openmeteo, "_fetch_day", fake_day)
    result = asyncio.run(openmeteo.fetch_replay_spot(28.6, 77.2, "2023-07-09"))
    assert result["forecast_source"] == "replay_archive_fallback" and result["fallback_used"]


def test_replay_uses_day_ahead_forecast_and_recent_recorded_rain(monkeypatch):
    from backend.app.services import openmeteo
    hours = [f"2024-07-31T{h:02d}:00" for h in range(24)]
    async def fake_day(lat, lon, date, timeout):
        return {"day_ahead": {"time": hours, "precipitation_previous_day1": [1.0] * 24}, "historical": None,
                "archive": {"time": hours, "precipitation": [0.0] * 19 + [25.8, 36.0, 38.5, 58.1, 40.8]}, "errors": []}
    monkeypatch.setattr(openmeteo, "_fetch_day", fake_day)
    result = asyncio.run(openmeteo.fetch_replay_spot(28.6, 77.2, "2024-07-31", as_of_hour=20))
    assert result["forecast_source"] == "replay_day_ahead_forecast"
    assert result["window_start"] == "2024-07-31T20:00" and len(result["forecast_values"]) == 4
    assert result["recent_times"] == ["2024-07-31T17:00", "2024-07-31T18:00", "2024-07-31T19:00"] and max(result["recent_values"]) == 25.8


def test_batched_weather_request_maps_locations_in_order(monkeypatch):
    from backend.app.services import openmeteo
    seen = {}
    async def fake_json(url, timeout=15):
        seen["url"] = url
        return [{"hourly": {"time": ["2024-07-31T00:00"], "precipitation": [1.0]}}, {"hourly": {"time": ["2024-07-31T00:00"], "precipitation": [2.0]}}]
    monkeypatch.setattr(openmeteo, "_fetch_json_async", fake_json)
    spots = [{"lat": 28.6, "lon": 77.2}, {"lat": 28.7, "lon": 77.1}]
    hourly = asyncio.run(openmeteo._fetch_hourly_bulk(openmeteo.ARCHIVE_URL, spots, {"hourly": "precipitation"}, 5))
    assert "latitude=28.6,28.7" in seen["url"] and "longitude=77.2,77.1" in seen["url"]
    assert [h["precipitation"][0] for h in hourly] == [1.0, 2.0]
    async def short_json(url, timeout=15):
        return {"hourly": {}}
    monkeypatch.setattr(openmeteo, "_fetch_json_async", short_json)
    with pytest.raises(openmeteo.OpenMeteoError):
        asyncio.run(openmeteo._fetch_hourly_bulk(openmeteo.ARCHIVE_URL, spots, {"hourly": "precipitation"}, 5))


def test_agent_claims_of_flooding_or_safety_are_replaced_with_computed_text():
    from backend.app.services import agent
    assessments = [_assess_for_agent("a", 12.0, 18.4)]
    briefing = agent.Briefing(english_summary="Spot a is already experiencing high water levels.",
                              reasoning="The alternate road is safe to use.", actions={"a": "Evacuate the area"})
    proposal = agent._assemble(briefing, assessments, "2025-07-09T00:00", "2025-07-09T11:00")
    assert "water level" not in proposal["english_text"] and "1 low point(s) are at high risk" in proposal["english_text"]
    assert "Agent note" not in proposal["reasoning"]
    assert proposal["flagged_spots"][0]["recommended_action"].startswith("Heavy rain is forecast here")


def test_hindi_summary_groups_spots_by_level_and_notes_rain_already_falling():
    from backend.app.services.bedrock import hindi_summary
    from backend.app.services.risk import apply_recorded_rain
    red = _assess_for_agent("a", 12.0, 18.4)
    amber = apply_recorded_rain(_assess_for_agent("b", 10.0, 1.0), [7.0], ["2025-07-09T05:00"])
    text = hindi_summary([red, amber])
    assert "भारी बारिश का पूर्वानुमान (1): Spot a" in text and "सतर्कता (1): Spot b" in text
    now = apply_recorded_rain(_assess_for_agent("c", 10.0, 1.0), [30.0], ["2025-07-09T05:00"])
    assert "बारिश जारी, उच्च जोखिम (1): Spot c" in hindi_summary([now]) and "अभी वाहन न भेजें" in hindi_summary([now])
    assert hindi_summary([]).startswith("इस अवधि में")


def test_only_rain_already_falling_tells_dispatchers_to_hold_trips():
    from backend.app.services.bedrock import action_for
    from backend.app.services.risk import apply_recorded_rain
    forecast_red = _assess_for_agent("a", 12.0, 18.4)
    now_red = apply_recorded_rain(_assess_for_agent("b", 10.0, 1.0), [30.0], ["2025-07-09T05:00"])
    assert "Hold trips" not in action_for(forecast_red) and "Plan an alternative" in action_for(forecast_red)
    assert "Hold trips through this spot now" in action_for(now_red)


def _ask_assessments():
    from backend.app.services.spots_loader import load_spots
    out = []
    for i, s in enumerate(load_spots()):
        peak = s["threshold_mm_hr"] * (1.3 if s["id"] == "minto_road_bridge" else 0.7 if s["id"] == "moolchand_underpass" else 0.1)
        ratio = peak / s["threshold_mm_hr"]
        out.append({"spot_id": s["id"], "spot_name": s["name"], "threshold_mm_hr": s["threshold_mm_hr"], "peak_precipitation_mm_hr": round(peak, 1),
                    "risk_ratio": ratio, "risk_level": "red" if ratio >= 1 else "amber" if ratio >= 0.6 else "green", "trigger": "forecast", "data_available": True})
    return out


def test_ask_answers_are_computed_from_the_assessment():
    from backend.app.services.ask import Toolbox, keyword_route, summarize
    box = Toolbox(_ask_assessments())
    assert "1 low points are HIGH and 1 are on WATCH" in summarize(box.assessments)
    spot = box.spot("Minto Road")
    assert spot["spot_id"] == "minto_road_bridge" and "is HIGH" in spot["answer"] and "130%" in spot["answer"] and "Plan an alternative" in spot["answer"]
    assert "not one of the" in box.spot("Chandni Chowk")["answer"]
    assert "WATCH: Moolchand underpass" in box.zone("south")["answer"]
    result, label = keyword_route("How is the Moolchand underpass doing?", box)
    assert label.startswith("spot_status") and "is WATCH" in result["answer"]
    assert keyword_route("what is going on", box)[1] == "network_status"
    assert keyword_route("anything in North West Delhi?", box)[1] == "zone_status(North West)"


def test_ask_falls_back_to_keywords_when_the_agent_is_unavailable(monkeypatch):
    from backend.app.services import ask as ask_module
    def boom(question, assessments):
        raise RuntimeError("model offline")
    monkeypatch.setattr(ask_module, "_run", boom)
    result = asyncio.run(ask_module.ask("Is Minto Road okay?", _ask_assessments()))
    assert result["routed_by"] == "keywords" and "model offline" in result["reason"] and "Minto Road railway bridge is HIGH" in result["answer"]
    empty = asyncio.run(ask_module.ask("hello", []))
    assert empty["routed_by"] == "none" and "Run an assessment first" in empty["answer"]
