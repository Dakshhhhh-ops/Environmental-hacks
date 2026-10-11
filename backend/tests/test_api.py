import json

from fastapi.testclient import TestClient

from backend.app.main import app
from backend.app.services import storage as storage_module
from backend.app.services.spots_loader import load_spots as _load_spots

SPOT_COUNT = len(_load_spots())


def test_health_spots_and_invalid_inputs(tmp_path, monkeypatch):
    monkeypatch.setattr(storage_module.settings, "local_data_dir", str(tmp_path))
    monkeypatch.setattr(storage_module, "_storage", None)
    import urllib.request
    class Reachable:
        def __enter__(self): return self
        def __exit__(self, *_): pass
    monkeypatch.setattr(urllib.request, "urlopen", lambda *args, **kwargs: Reachable())
    client = TestClient(app)
    spots = client.get("/api/spots")
    assert spots.status_code == 200 and len(spots.json()["spots"]) == SPOT_COUNT
    assert client.get("/api/risk?mode=replay").status_code == 400
    assert client.get("/api/risk?mode=other").status_code == 422
    assert client.post("/api/subscribe", json={"email": "not-an-email"}).status_code == 400


def test_proposal_approval_rejection_and_local_skipped(tmp_path, monkeypatch):
    monkeypatch.setattr(storage_module.settings, "local_data_dir", str(tmp_path))
    monkeypatch.setattr(storage_module, "_storage", None)
    monkeypatch.delenv("SNS_TOPIC_ARN", raising=False)
    from backend.app.api import routes
    async def fallback(*args, **kwargs):
        return ({"english_text": "Review rainfall conditions.", "hindi_text": "वर्षा की समीक्षा करें।", "reasoning": "Estimated threshold comparison.", "flagged_spots": [], "window_start": args[1], "window_end": args[2]}, "fallback", "test stub")
    monkeypatch.setattr(routes, "generate_proposal", fallback)
    client = TestClient(app)
    created = client.post("/api/proposals", json={"mode": "replay", "date": "2025-05-01", "synthetic": True})
    assert created.status_code == 200, created.text
    proposal = created.json()
    assert proposal["generated_by"] in ("strands-agent", "fallback")
    assert len(proposal["assessments"]) == SPOT_COUNT
    published = client.post(f"/api/proposals/{proposal['id']}/approve")
    assert published.status_code == 200
    assert published.json()["delivery_state"] == "local-skipped"
    blocked = client.post(f"/api/proposals/{proposal['id']}/verify")
    assert blocked.status_code == 409
    assert "cannot support forecast verification" in blocked.json()["detail"]
    assert client.post(f"/api/proposals/{proposal['id']}/approve").json()["delivery_state"] == "local-skipped"
    rejected = client.post("/api/proposals", json={"mode": "replay", "date": "2025-05-01", "synthetic": True}).json()
    assert client.post(f"/api/proposals/{rejected['id']}/reject").json()["status"] == "rejected"
    assert client.post(f"/api/proposals/{rejected['id']}/approve").status_code == 409


def test_local_storage_survives_storage_recreation(tmp_path, monkeypatch):
    monkeypatch.setattr(storage_module.settings, "local_data_dir", str(tmp_path))
    monkeypatch.setattr(storage_module, "_storage", None)
    first = storage_module.get_storage()
    first.save_proposal({"id": "persisted", "status": "pending"})
    storage_module.reset_storage()
    assert storage_module.get_storage().get_proposal("persisted")["status"] == "pending"


def test_corrupt_local_state_is_reported_instead_of_reset(tmp_path, monkeypatch):
    monkeypatch.setattr(storage_module.settings, "local_data_dir", str(tmp_path))
    (tmp_path / "proposals.json").write_text("{broken", encoding="utf-8")
    with __import__("pytest").raises(storage_module.StorageError, match="invalid JSON"):
        storage_module.Storage().list_proposals()


def test_verification_is_rainfall_based_and_repeat_does_not_adjust_twice(tmp_path, monkeypatch):
    monkeypatch.setattr(storage_module.settings, "local_data_dir", str(tmp_path))
    monkeypatch.setattr(storage_module, "_storage", None)
    from backend.app.api import routes
    async def fallback(*args, **kwargs):
        from backend.app.services.bedrock import build_fallback_proposal
        return build_fallback_proposal(args[0], args[1], args[2]), "fallback", "test stub"
    async def replay(spots, date, **kwargs):
        times = [f"{date}T{hour:02}:00" for hour in range(12)]
        results = []
        for index, spot in enumerate(spots):
            values = [30.0] + [0.0] * 11 if index == 0 else [0.0] * 12
            results.append({"spot": spot, "forecast_times": times, "forecast_values": values,
                "forecast_source": "replay_historical_forecast", "observed_times": times,
                "observed_values": [0.0] * 12, "observation_source": "replay_archive",
                "window_start": f"{date}T00:00", "window_end": f"{date}T11:00", "error": None})
        return results
    monkeypatch.setattr(routes, "generate_proposal", fallback)
    monkeypatch.setattr(routes, "fetch_replay_all", replay)
    client = TestClient(app)
    proposal = client.post("/api/proposals", json={"mode": "replay", "date": "2025-05-01"}).json()
    assert proposal["generated_by"] == "fallback"
    result = client.post(f"/api/proposals/{proposal['id']}/verify")
    assert result.status_code == 200
    records = result.json()["records"]
    assert len(records) == SPOT_COUNT
    assert records[0]["outcome"] == "false_alarm"
    assert records[0]["based_on_flood_observations"] is False
    assert records[0]["threshold_after"] == 12.6
    repeated = client.post(f"/api/proposals/{proposal['id']}/verify").json()
    assert "already verified" in repeated["message"]
    thresholds = client.get("/api/spots").json()["spots"]
    assert thresholds[0]["threshold_mm_hr"] == 12.6


def test_sns_publication_retry_is_blocked_after_uncertain_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(storage_module.settings, "local_data_dir", str(tmp_path))
    monkeypatch.setattr(storage_module, "_storage", None)
    monkeypatch.setenv("SNS_TOPIC_ARN", "arn:aws:sns:us-east-1:123456789012:demo")
    from backend.app.api import routes
    async def fallback(*args, **kwargs):
        return ({"english_text": "Review", "hindi_text": "समीक्षा", "reasoning": "Estimate", "flagged_spots": [], "window_start": args[1], "window_end": args[2]}, "fallback", "test stub")
    calls = []
    async def failing_publish(*args, **kwargs):
        calls.append(1)
        raise TimeoutError("simulated timeout")
    monkeypatch.setattr(routes, "generate_proposal", fallback)
    monkeypatch.setattr(routes, "publish_alert", failing_publish)
    client = TestClient(app)
    proposal = client.post("/api/proposals", json={"mode": "replay", "date": "2025-05-01", "synthetic": True}).json()
    first = client.post(f"/api/proposals/{proposal['id']}/approve")
    assert first.status_code == 502
    second = client.post(f"/api/proposals/{proposal['id']}/approve")
    assert second.status_code == 409
    assert len(calls) == 1


def test_external_weather_failure_marks_data_unavailable_and_blocks_proposal(tmp_path, monkeypatch):
    monkeypatch.setattr(storage_module.settings, "local_data_dir", str(tmp_path))
    monkeypatch.setattr(storage_module, "_storage", None)
    from backend.app.api import routes
    from backend.app.services.spots_loader import load_spots
    async def failed_replay(spots, date, **kwargs):
        return [{"spot": spot, "forecast_times": [], "forecast_values": [], "forecast_source": "replay_historical_forecast",
                 "observed_times": [], "observed_values": [], "observation_source": "replay_archive",
                 "window_start": f"{date}T00:00", "window_end": f"{date}T11:00", "error": "provider unavailable"}
                for spot in spots]
    monkeypatch.setattr(routes, "fetch_replay_all", failed_replay)
    client = TestClient(app)
    result = client.get("/api/risk?mode=replay&date=2025-05-01").json()
    assert len(result["spots"]) == len(load_spots()) == SPOT_COUNT
    assert all(not spot["data_available"] for spot in result["spots"])
    assert client.post("/api/proposals", json={"mode": "replay", "date": "2025-05-01"}).status_code == 503


def test_driver_advisory_shows_only_approved_alerts(tmp_path, monkeypatch):
    monkeypatch.setattr(storage_module.settings, "local_data_dir", str(tmp_path))
    monkeypatch.setattr(storage_module, "_storage", None)
    monkeypatch.delenv("SNS_TOPIC_ARN", raising=False)
    from backend.app.api import routes
    from backend.app.services.bedrock import build_fallback_proposal
    async def template(assessments, window_start, window_end, *args, **kwargs):
        return build_fallback_proposal(assessments, window_start, window_end), "fallback", "test stub"
    monkeypatch.setattr(routes, "generate_proposal", template)
    client = TestClient(app)
    assert client.get("/api/advisory").json() == {"active": False}
    proposal = client.post("/api/proposals", json={"mode": "replay", "date": "2025-05-01", "synthetic": True}).json()
    assert proposal["flagged_spots"], "synthetic drill should flag spots"
    assert client.get("/api/advisory").json() == {"active": False}, "a draft must not reach drivers"
    assert client.post(f"/api/proposals/{proposal['id']}/approve").status_code == 200
    advisory = client.get("/api/advisory").json()
    assert advisory["active"] and advisory["kind"] == "drill"
    assert len(advisory["flagged"]) == len(proposal["flagged_spots"]) and advisory["flagged"][0]["name"]


def test_risk_endpoint_replays_a_day_hour_by_hour(tmp_path, monkeypatch):
    monkeypatch.setattr(storage_module.settings, "local_data_dir", str(tmp_path))
    monkeypatch.setattr(storage_module, "_storage", None)
    from backend.app.services import openmeteo
    hours = [f"2024-07-31T{h:02d}:00" for h in range(24)]
    async def fake_day(lat, lon, date, timeout):
        return {"day_ahead": {"time": hours, "precipitation_previous_day1": [1.0] * 24}, "historical": None,
                "archive": {"time": hours, "precipitation": [0.0] * 19 + [60.0] * 5}, "errors": []}
    async def no_prefetch(*args, **kwargs):
        return None
    monkeypatch.setattr(openmeteo, "_fetch_day", fake_day)
    monkeypatch.setattr(openmeteo, "_prefetch_days", no_prefetch)
    client = TestClient(app)
    morning = client.get("/api/risk?mode=replay&date=2024-07-31&as_of_hour=6").json()
    evening = client.get("/api/risk?mode=replay&date=2024-07-31&as_of_hour=20").json()
    assert all(s["risk_level"] == "green" for s in morning["spots"])
    assert all(s["risk_level"] == "red" and s["trigger"] == "recorded_rain" for s in evening["spots"])
    assert evening["assessment_window_start"] == "2024-07-31T20:00"
    assert client.get("/api/risk?mode=replay&date=2024-07-31&as_of_hour=24").status_code == 422


def test_same_window_verified_twice_counts_once(tmp_path, monkeypatch):
    monkeypatch.setattr(storage_module.settings, "local_data_dir", str(tmp_path))
    monkeypatch.setattr(storage_module, "_storage", None)
    monkeypatch.delenv("SNS_TOPIC_ARN", raising=False)
    from backend.app.api import routes
    from backend.app.services import openmeteo
    from backend.app.services.bedrock import build_fallback_proposal
    hours = [f"2025-08-14T{h:02d}:00" for h in range(24)]
    async def fake_day(lat, lon, date, timeout):
        return {"day_ahead": {"time": hours, "precipitation_previous_day1": [30.0] * 24}, "historical": None,
                "archive": {"time": hours, "precipitation": [0.5] * 24}, "errors": []}
    async def no_prefetch(*args, **kwargs):
        return None
    async def template(assessments, window_start, window_end, *args, **kwargs):
        return build_fallback_proposal(assessments, window_start, window_end), "fallback", "test stub"
    monkeypatch.setattr(openmeteo, "_fetch_day", fake_day)
    monkeypatch.setattr(openmeteo, "_prefetch_days", no_prefetch)
    monkeypatch.setattr(routes, "generate_proposal", template)
    client = TestClient(app)
    def replay_and_verify():
        p = client.post("/api/proposals", json={"mode": "replay", "date": "2025-08-14"}).json()
        client.post(f"/api/proposals/{p['id']}/approve")
        return client.post(f"/api/proposals/{p['id']}/verify").json()
    first = replay_and_verify()
    assert all(r["outcome"] == "false_alarm" and r["threshold_adjusted"] for r in first["records"])
    limits_after_first = {s["id"]: s["threshold_mm_hr"] for s in client.get("/api/spots").json()["spots"]}
    second = replay_and_verify()
    assert second.get("already_counted") is True
    assert len(client.get("/api/history").json()["records"]) == SPOT_COUNT
    assert {s["id"]: s["threshold_mm_hr"] for s in client.get("/api/spots").json()["spots"]} == limits_after_first
