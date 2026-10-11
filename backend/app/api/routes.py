"""API routes."""
from __future__ import annotations

import asyncio
from pathlib import Path
import json
import os
import re
import uuid
from datetime import datetime, timezone, timedelta
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Body
from pydantic import BaseModel, Field

from ..models import (
    RiskLevel, DataSource, SpotAssessment, AssessmentResponse,
    SubscribeResponse, HealthResponse, ApproveResponse, RejectResponse,
    VerifyResponse, HistoryResponse,
)
from ..services.spots_loader import load_spots
from ..services.risk import assess_spot, apply_recorded_rain
from ..services.routing import PLACES, check_route
from ..services.agent import generate_proposal, agent_provider, agent_model_id
from ..services.storage import get_storage
from ..services.sns import is_sns_configured, get_sns_topic_arn, publish_alert, subscribe_email, SnsError
from ..services.verify import classify_outcome, adjust_threshold, _now_iso
from ..services.openmeteo import (
    fetch_live_all, fetch_replay_all, get_synthetic_window, SYNTHETIC_FIXTURE, OpenMeteoError,
    _replay_window, _live_window,
)

router = APIRouter()

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# Used to prevent duplicate assessment side effects — not needed for assessment itself
# but proposal creation is deduplicated via id.


def _parse_date(date_str: str) -> None:
    if not DATE_RE.match(date_str):
        raise HTTPException(status_code=400, detail="date must be YYYY-MM-DD")
    try:
        d = datetime.strptime(date_str, "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid date")
    today = datetime.now(timezone.utc).date()
    # Allow historical dates only (not future beyond today)
    if d > today:
        raise HTTPException(status_code=400, detail="replay date cannot be in the future")
    # Open-Meteo historical forecast coverage starts around 2021-2022
    if d < datetime(2021, 1, 1).date():
        raise HTTPException(status_code=400, detail="replay date must be 2021-01-01 or later")


@router.get("/health")
async def health():
    from ..config import settings
    # Check Open-Meteo reachability (quick check)
    import urllib.request

    def _reachable() -> bool:
        try:
            urllib.request.urlopen("https://api.open-meteo.com/v1/forecast?latitude=28.6&longitude=77.2&hourly=precipitation&forecast_days=1", timeout=8)
            return True
        except Exception:
            return False

    # Off the event loop, so a slow weather provider cannot stall other requests.
    openmeteo_ok = await asyncio.get_running_loop().run_in_executor(None, _reachable)

    bedrock_cfg = False
    if os.environ.get("BEDROCK_MODEL_ID"):
        try:
            import boto3
            bedrock_cfg = boto3.Session(region_name=settings.bedrock_region).get_credentials() is not None
        except Exception:
            bedrock_cfg = False
    return {
        "status": "ok",
        "storage_mode": get_storage().mode,
        "bedrock_configured": bedrock_cfg,
        "sns_configured": is_sns_configured(),
        "openmeteo_reachable": openmeteo_ok,
        "bedrock_model": os.environ.get("BEDROCK_MODEL_ID", settings.bedrock_model_id),
        "bedrock_region": os.environ.get("BEDROCK_REGION", settings.bedrock_region),
        "agent_framework": "Strands Agents SDK",
        "agent_provider": agent_provider(),
        "agent_model": agent_model_id(),
        "timestamp": _now_iso(),
    }


@router.get("/spots")
async def list_spots():
    spots = load_spots()
    # Apply calibrated thresholds
    storage = get_storage()
    thresholds = storage.get_thresholds()
    out = []
    for s in spots:
        item = dict(s)
        if s["id"] in thresholds:
            item["threshold_mm_hr"] = thresholds[s["id"]]
        out.append(item)
    return {"spots": out}


async def _run_assessment(mode: str, date: Optional[str], use_synthetic: bool = False, as_of_hour: int = 0):
    """Shared assessment logic. Returns (assessments, window_start, window_end, provider_used, errors)."""
    from ..config import settings
    spots = load_spots()
    storage = get_storage()
    thresholds = storage.get_thresholds()

    if use_synthetic:
        synthetic_date = date or datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if mode == "replay":
            _parse_date(synthetic_date)
        w_start, w_end = get_synthetic_window(synthetic_date)
        assessments: list[SpotAssessment] = []
        for spot in spots:
            cal = thresholds.get(spot["id"])
            vals = SYNTHETIC_FIXTURE.get(spot["id"])
            if vals is None:
                # Spots without a hand-written fixture reuse one, scaled, so the demo covers the whole network.
                base = list(SYNTHETIC_FIXTURE.values())[len(assessments) % len(SYNTHETIC_FIXTURE)]
                vals = [round(v * (0.35 + 0.15 * (len(assessments) % 5)), 1) for v in base]
            times = [f"{synthetic_date}T{str(h).zfill(2)}:00" for h in range(12)]
            a = assess_spot(spot, vals, times, w_start, w_end, DataSource.SYNTHETIC, cal)
            assessments.append(a)
        return assessments, w_start, w_end, "synthetic", None

    if mode == "live":
        results = await fetch_live_all(spots, timeout=settings.openmeteo_timeout_seconds, max_concurrency=settings.openmeteo_max_concurrency)
        # Derive window from first result
        w_start = results[0]["window_start"] if results else ""
        w_end = results[0]["window_end"] if results else ""
        assessments = []
        errors: list[str] = []
        for r in results:
            spot = r["spot"]
            cal = thresholds.get(spot["id"])
            if r["error"]:
                errors.append(f"{spot['id']}: {r['error']}")
                a = assess_spot(spot, [], [], r["window_start"], r["window_end"], DataSource.LIVE, cal)
                # Mark unavailable
                a.data_available = False
            else:
                a = assess_spot(spot, r["values"], r["times"], r["window_start"], r["window_end"], DataSource.LIVE, cal)
                apply_recorded_rain(a, r.get("recent_values", []), r.get("recent_times", []))
            assessments.append(a)
        err_msg = "; ".join(errors) if errors else None
        return assessments, w_start, w_end, "open-meteo", err_msg

    else:  # replay
        assert date is not None
        _parse_date(date)
        results = await fetch_replay_all(spots, date, timeout=settings.openmeteo_timeout_seconds, max_concurrency=settings.openmeteo_max_concurrency, as_of_hour=as_of_hour)
        w_start, w_end = _replay_window(date, as_of_hour)
        assessments = []
        errors = []
        # Determine dominant data source
        fallback_count = sum(1 for r in results if r.get("fallback_used"))
        has_historical = any(not r.get("fallback_used") and r.get("forecast_times") for r in results)
        for r in results:
            spot = r["spot"]
            cal = thresholds.get(spot["id"])
            # Use forecast series for assessment
            ft = r.get("forecast_times", [])
            fv = r.get("forecast_values", [])
            src_str = r.get("forecast_source", "replay_historical_forecast")
            try:
                ds = DataSource(src_str)
            except ValueError:
                ds = DataSource.REPLAY_ARCHIVE_FALLBACK
            if r.get("error") and not ft:
                errors.append(f"{spot['id']}: {r['error']}")
            a = assess_spot(spot, fv, ft, r.get("window_start", w_start), r.get("window_end", w_end), ds, cal)
            # Attach observed for later verification display
            # Store observed on assessment via extra field — use model_extra or just keep in memory
            # For API response we include forecast assessment only; observed is used in verification
            if not ft:
                a.data_available = False
            apply_recorded_rain(a, r.get("recent_values", []), r.get("recent_times", []))
            # Stash observed data for verification use (not part of SpotAssessment model, but keep as attribute)
            # We'll store it separately in proposal if needed
            assessments.append(a)
            # Keep observed alongside for later (attach as hidden attr via __dict__)
            a.__dict__["_observed_times"] = r.get("observed_times", [])
            a.__dict__["_observed_values"] = r.get("observed_values", [])
            a.__dict__["_observation_source"] = r.get("observation_source", "replay_archive")

        err_msg = "; ".join(errors) if errors else None
        provider = "open-meteo"
        if fallback_count > 0 and not has_historical:
            provider = "open-meteo (archive fallback)"
        return assessments, w_start, w_end, provider, err_msg


@router.get("/risk")
async def get_risk(
    mode: str = Query("live", pattern="^(live|replay)$"),
    date: Optional[str] = Query(None),
    synthetic: bool = Query(False),
):
    if mode == "replay" and not date and not synthetic:
        raise HTTPException(status_code=400, detail="date is required in replay mode (YYYY-MM-DD)")
    if mode == "live" and date:
        raise HTTPException(status_code=400, detail="date is only valid in replay mode")
    if synthetic:
        if not date:
            date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        assessments, w_start, w_end, provider, err = await _run_assessment(mode, date, use_synthetic=True)
    else:
        assessments, w_start, w_end, provider, err = await _run_assessment(mode, date, use_synthetic=False)

    return {
        "mode": mode,
        "date": date,
        "assessment_window_start": w_start,
        "assessment_window_end": w_end,
        "spots": [a.model_dump() for a in assessments],
        "generated_at": _now_iso(),
        "provider_used": provider,
        "error": err,
    }


# --- Driver advisory ---

@router.get("/advisory")
async def get_advisory():
    """The most recently approved alert, reduced to what a driver needs. Unapproved drafts never appear here."""
    published = [p for p in get_storage().list_proposals() if p.get("status") == "published" and p.get("published_at")]
    if not published:
        return {"active": False}
    latest = max(published, key=lambda p: p["published_at"])
    spots = {s["id"]: s for s in load_spots()}
    assessments = {a.get("spot_id"): a for a in latest.get("assessments", [])}
    source = next(iter(assessments.values()), {}).get("data_source")
    return {
        "active": True,
        "kind": "drill" if source == "synthetic" else "replay" if latest.get("mode") == "replay" else "live",
        "published_at": latest["published_at"],
        "window_start": latest.get("window_start"),
        "window_end": latest.get("window_end"),
        "flagged": [
            {
                "name": spots.get(f["spot_id"], {}).get("name", f["spot_id"]),
                "zone": spots.get(f["spot_id"], {}).get("zone"),
                "risk_level": f["risk_level"],
                "rain_now": assessments.get(f["spot_id"], {}).get("trigger") == "recorded_rain",
            }
            for f in latest.get("flagged_spots", [])
        ],
    }


# --- Backtest ---

@router.get("/backtest")
async def get_backtest():
    """Two-season comparison of day-ahead forecasts with recorded rain, produced by scripts/backtest.py."""
    path = Path(__file__).resolve().parents[3] / "data" / "backtest.json"
    if not path.exists():
        raise HTTPException(status_code=404, detail="backtest has not been generated; run scripts/backtest.py")
    return json.loads(path.read_text(encoding="utf-8"))


# --- Route check ---

class LatLon(BaseModel):
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)


class RouteCheckBody(BaseModel):
    origin: LatLon
    destination: LatLon
    risk: dict[str, str] = {}


@router.get("/places")
async def list_places():
    return {"places": PLACES}


@router.post("/route-check")
async def route_check(body: RouteCheckBody):
    """Score driving alternatives by the flagged spots they pass. Risk levels come from the caller's current assessment."""
    try:
        return await check_route(
            (body.origin.lat, body.origin.lon), (body.destination.lat, body.destination.lon), load_spots(), body.risk
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc))


# --- Proposals ---

class ProposalCreateBody(BaseModel):
    mode: str = "live"
    date: Optional[str] = None
    synthetic: bool = False
    # Replay only: the hour of the day the dispatcher is "standing at".
    as_of_hour: int = Field(0, ge=0, le=23)


@router.post("/proposals")
async def create_proposal(body: ProposalCreateBody):
    mode = body.mode
    date = body.date
    synthetic = body.synthetic
    if mode not in ("live", "replay"):
        raise HTTPException(status_code=400, detail="mode must be live or replay")
    if mode == "replay" and not date and not synthetic:
        raise HTTPException(status_code=400, detail="date is required in replay mode")
    if mode == "live" and date and not synthetic:
        raise HTTPException(status_code=400, detail="date only valid in replay mode")
    if synthetic and not date:
        date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    assessments, w_start, w_end, provider, err = await _run_assessment(mode, date, use_synthetic=synthetic, as_of_hour=body.as_of_hour if mode == "replay" else 0)
    if not any(a.data_available for a in assessments):
        raise HTTPException(status_code=503, detail=err or "No usable rainfall data was available. Retry or explicitly select the synthetic demo fixture.")

    # Determine data source label for proposal
    ds_label = assessments[0].data_source.value if assessments else "unknown"

    # Draft the proposal with the Strands agent, or the labelled template fallback
    proposal_json, generated_by, fallback_reason = await generate_proposal(
        assessments, w_start, w_end, ds_label
    )

    # Build proposal record
    pid = str(uuid.uuid4())
    now = _now_iso()
    # Store observed data for verification
    observed_map: dict[str, dict] = {}
    for a in assessments:
        observed_map[a.spot_id] = {
            "observed_times": a.__dict__.get("_observed_times", []),
            "observed_values": a.__dict__.get("_observed_values", []),
            "observation_source": a.__dict__.get("_observation_source", "replay_archive"),
        }

    record = {
        "id": pid,
        "status": "pending",
        "generated_by": generated_by,
        "fallback_reason": fallback_reason,
        "agent_trace": proposal_json.get("agent_trace"),
        "english_text": proposal_json["english_text"],
        "hindi_text": proposal_json["hindi_text"],
        "reasoning": proposal_json["reasoning"],
        "flagged_spots": proposal_json["flagged_spots"],
        "window_start": proposal_json["window_start"],
        "window_end": proposal_json["window_end"],
        "assessment_ids": [a.spot_id for a in assessments],
        "assessments": [a.model_dump() for a in assessments],
        "observed_map": observed_map,
        "mode": mode,
        "date": date,
        "as_of_hour": body.as_of_hour if mode == "replay" else None,
        "provider_used": provider,
        "created_at": now,
        "published_at": None,
        "sns_message_id": None,
        "delivery_state": None,
        "delivery_error": None,
    }

    storage = get_storage()
    storage.save_proposal(record)

    # Return proposal (without internal observed_map)
    out = {k: v for k, v in record.items() if k not in ("observed_map", "assessments")}
    out["assessments"] = record["assessments"]
    if fallback_reason:
        out["fallback_reason"] = fallback_reason
    return out


@router.get("/proposals")
async def list_proposals():
    storage = get_storage()
    items = storage.list_proposals()
    # Strip internal fields for list view
    out = []
    for item in items:
        entry = {k: v for k, v in item.items() if k not in ("observed_map",)}
        out.append(entry)
    return {"proposals": out}


@router.post("/proposals/{proposal_id}/approve")
async def approve_proposal(proposal_id: str):
    storage = get_storage()
    proposal = storage.get_proposal(proposal_id)
    if not proposal:
        raise HTTPException(status_code=404, detail="proposal not found")
    status = proposal.get("status")
    if status == "published":
        # Idempotent — already published, return existing
        return {
            "proposal_id": proposal_id,
            "status": "published",
            "delivery_state": proposal.get("delivery_state", "published"),
            "sns_message_id": proposal.get("sns_message_id"),
            "delivery_error": proposal.get("delivery_error"),
            "message": "already published — duplicate approval ignored",
        }
    if status == "rejected":
        raise HTTPException(status_code=409, detail="proposal was rejected and cannot be approved")
    if status not in ("pending", "approved"):
        raise HTTPException(status_code=409, detail=f"proposal in state '{status}' cannot be approved")
    if status == "approved":
        # Already approved but not yet published — proceed to publish (idempotent approve)
        pass
    else:
        # pending -> approved
        storage.update_proposal(proposal_id, {"status": "approved"})
        proposal["status"] = "approved"

    # Publish via SNS
    topic_arn = get_sns_topic_arn()
    subject = f"Underpass Sentinel — Estimated Waterlogging Risk {proposal.get('window_start', '')}"
    # Build bilingual message
    message = (
        f"Underpass Sentinel — Estimated Waterlogging Risk\n"
        f"Window: {proposal.get('window_start')} to {proposal.get('window_end')}\n"
        f"Status: Approved by dispatcher\n\n"
        f"ENGLISH:\n{proposal.get('english_text', '')}\n\n"
        f"HINDI:\n{proposal.get('hindi_text', '')}\n\n"
        f"Reasoning: {proposal.get('reasoning', '')}\n\n"
        f"DISCLAIMER: Rainfall intensity is a proxy for potential waterlogging, not a confirmed flood prediction. "
        f"All thresholds are initial estimates (verified=false). Alternate routes are suggestions requiring dispatcher review.\n"
        f"Generated by: {proposal.get('generated_by')}\n"
    )

    if not topic_arn:
        # Local-skipped
        now = _now_iso()
        storage.update_proposal(proposal_id, {
            "status": "published",
            "published_at": now,
            "delivery_state": "local-skipped",
            "sns_message_id": None,
            "delivery_error": None,
        })
        return {
            "proposal_id": proposal_id,
            "status": "published",
            "delivery_state": "local-skipped",
            "sns_message_id": None,
            "delivery_error": None,
            "message": "approved and recorded locally — SNS not configured (local-skipped)",
        }

    if not storage.claim_publication(proposal_id):
        raise HTTPException(status_code=409, detail="an SNS publication attempt has already started or completed; duplicate publish blocked")

    try:
        msg_id = await publish_alert(topic_arn, subject, message)
        now = _now_iso()
        storage.update_proposal(proposal_id, {
            "status": "published",
            "published_at": now,
            "delivery_state": "published",
            "sns_message_id": msg_id,
            "delivery_error": None,
        })
        return {
            "proposal_id": proposal_id,
            "status": "published",
            "delivery_state": "published",
            "sns_message_id": msg_id,
            "delivery_error": None,
            "message": "approved and published via SNS",
        }
    except Exception as e:
        err = str(e)[:500]
        storage.update_proposal(proposal_id, {
            "delivery_state": "failed",
            "delivery_error": err,
        })
        raise HTTPException(status_code=502, detail=f"SNS publish attempt failed or its result is uncertain: {err}. Duplicate retries are blocked; inspect SNS before taking further action.")


@router.post("/proposals/{proposal_id}/reject")
async def reject_proposal(proposal_id: str):
    storage = get_storage()
    proposal = storage.get_proposal(proposal_id)
    if not proposal:
        raise HTTPException(status_code=404, detail="proposal not found")
    status = proposal.get("status")
    if status == "published":
        raise HTTPException(status_code=409, detail="already published — cannot reject")
    if status == "rejected":
        return {"proposal_id": proposal_id, "status": "rejected", "message": "already rejected"}
    if status not in ("pending", "approved"):
        raise HTTPException(status_code=409, detail=f"proposal in state '{status}' cannot be rejected")
    storage.update_proposal(proposal_id, {"status": "rejected"})
    return {"proposal_id": proposal_id, "status": "rejected", "message": "proposal rejected — no alert will be published"}


# --- Verification ---

@router.post("/proposals/{proposal_id}/verify")
async def verify_proposal(proposal_id: str):
    storage = get_storage()
    # Idempotency: if already verified, return existing records
    if storage.has_verification(proposal_id):
        records = [r for r in storage.list_history() if r.get("proposal_id") == proposal_id]
        return {"proposal_id": proposal_id, "records": records, "message": "already verified — no threshold re-adjustment"}

    proposal = storage.get_proposal(proposal_id)
    if not proposal:
        raise HTTPException(status_code=404, detail="proposal not found")

    invalid_forecast_sources = {"synthetic", "replay_archive_fallback"}
    if any(a.get("data_source") in invalid_forecast_sources for a in proposal.get("assessments", [])):
        raise HTTPException(status_code=409, detail="this assessment used a synthetic or archive demonstration forecast; it cannot support forecast verification or threshold calibration")

    mode = proposal.get("mode", "replay")
    date = proposal.get("date")
    w_start = proposal.get("window_start", "")
    w_end = proposal.get("window_end", "")
    assessments = proposal.get("assessments", [])
    observed_map = proposal.get("observed_map", {})

    # If live mode or synthetic, try to fetch archive for verification; otherwise use stored observed
    # For replay proposals, we already have observed_map from initial assessment
    # For live proposals, we need to fetch archive for the window date

    from ..services.spots_loader import load_spots as _load_spots
    spots = _load_spots()
    spot_thresholds: dict[str, float] = {}
    spot_thresholds = storage.get_thresholds()

    # Build observed lookup: if we have stored observed_map, use it; else fetch
    observed_lookup: dict[str, dict] = {}
    observed_lookup = dict(observed_map)
    # Synthetic forecast fixtures never stand in for observed rainfall.
    v_date = w_start[:10] if w_start else None
    if v_date and DATE_RE.match(v_date):
        from ..services.openmeteo import ARCHIVE_URL, _build_url, _fetch_json_async, _extract_window
        sem = asyncio.Semaphore(4)
        async def load_observation(s):
            prior = observed_lookup.get(s["id"], {})
            if prior.get("observed_values"):
                return s["id"], prior
            async with sem:
                url = _build_url(ARCHIVE_URL, s["lat"], s["lon"], {
                    "hourly": "precipitation", "start_date": v_date,
                    "end_date": v_date, "timezone": "Asia/Kolkata",
                })
                try:
                    data = await _fetch_json_async(url, timeout=15)
                    times, vals = _extract_window(data.get("hourly", {}), w_start, w_end)
                    return s["id"], {"observed_times": times, "observed_values": vals, "observation_source": "replay_archive"}
                except Exception:
                    return s["id"], {"observed_times": [], "observed_values": [], "observation_source": "replay_archive"}
        observed_lookup.update(dict(await asyncio.gather(*(load_observation(s) for s in spots))))

    previous = {r.get("spot_id"): r for r in storage.list_history() if r.get("proposal_id") == proposal_id}

    # Determine which spots were flagged
    flagged_ids = {fs.get("spot_id") for fs in proposal.get("flagged_spots", [])}

    records: list[dict] = []
    for a in assessments:
        sid = a.get("spot_id", "")
        old_record = previous.get(sid)
        if old_record and old_record.get("outcome") != "pending":
            records.append(old_record)
            continue
        spot_cfg = next((s for s in spots if s["id"] == sid), None)
        if not spot_cfg:
            continue
        original = float(spot_cfg.get("original_threshold_mm_hr", spot_cfg.get("threshold_mm_hr", 10)))
        current_threshold = spot_thresholds.get(sid, float(spot_cfg.get("threshold_mm_hr", 10)))
        forecast_peak = float(a.get("peak_precipitation_mm_hr", 0))
        forecast_time = a.get("peak_time")
        is_flagged = sid in flagged_ids

        obs_entry = observed_lookup.get(sid, {})
        obs_vals: list[float] = obs_entry.get("observed_values", [])
        obs_times: list[str] = obs_entry.get("observed_times", [])
        obs_source: str = obs_entry.get("observation_source", "replay_archive")

        # Determine observed peak
        observed_peak: Optional[float] = None
        observed_time: Optional[str] = None
        if obs_vals and obs_times:
            # Sanitize
            clean_vals: list[float] = []
            for v in obs_vals:
                try:
                    fv = float(v) if v is not None else 0.0
                    if fv < 0 or fv != fv:
                        fv = 0.0
                    clean_vals.append(fv)
                except Exception:
                    clean_vals.append(0.0)
            if clean_vals:
                observed_peak = max(clean_vals)
                observed_time = obs_times[clean_vals.index(observed_peak)] if clean_vals else None
        elif obs_vals:
            # No times but have values — use max
            try:
                observed_peak = max(float(v) if v is not None else 0.0 for v in obs_vals)
            except Exception:
                observed_peak = None

        outcome = classify_outcome(is_flagged, forecast_peak, observed_peak, current_threshold)
        # Map no_event and no_data appropriately
        if outcome == "no_event":
            # No threshold change needed — still record but no adjustment
            outcome_record = "no_event"
            new_threshold = current_threshold
            adjusted = False
        elif outcome == "no_data":
            outcome_record = "pending"
            new_threshold = current_threshold
            adjusted = False
        else:
            outcome_record = outcome
            new_threshold, adjusted = adjust_threshold(outcome, current_threshold, original)
            if adjusted:
                storage.set_threshold(sid, new_threshold, original)

        rec = {
            "id": f"{proposal_id}:{sid}",
            "spot_id": sid,
            "spot_name": a.get("spot_name", sid),
            "proposal_id": proposal_id,
            "assessment_window_start": w_start,
            "assessment_window_end": w_end,
            "forecast_peak_mm_hr": forecast_peak,
            "forecast_peak_time": forecast_time,
            "observed_peak_mm_hr": observed_peak,
            "observed_peak_time": observed_time,
            "outcome": outcome_record,
            "threshold_before": current_threshold,
            "threshold_after": new_threshold,
            "threshold_adjusted": adjusted,
            "forecast_source": a.get("data_source", "unknown"),
            "observation_source": obs_source,
            "verified_at": _now_iso(),
            "based_on_rainfall": True,
            "based_on_flood_observations": False,
        }
        records.append(rec)

    storage.save_verification_records(records)
    return {"proposal_id": proposal_id, "records": records, "message": f"verified {len(records)} spots"}


@router.get("/history")
async def get_history():
    storage = get_storage()
    records = storage.list_history()
    return {"records": records}


class SubscribeBody(BaseModel):
    email: str


@router.post("/subscribe")
async def subscribe(body: SubscribeBody):
    import re as _re
    email = body.email.strip()
    if not _re.match(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$", email):
        raise HTTPException(status_code=400, detail="invalid email address")

    topic_arn = get_sns_topic_arn()
    if not topic_arn:
        get_storage().save_local_subscription(email)
        return {
            "email": email,
            "subscribed": False,
            "delivery_state": "local-skipped",
            "message": "SNS not configured — subscription recorded locally; no email will be sent until SNS is configured. Configure SNS_TOPIC_ARN and retry.",
            "sns_subscription_arn": None,
        }
    try:
        arn = await subscribe_email(topic_arn, email)
        is_pending = arn is None or "pending" in str(arn).lower() or arn == "pending confirmation"
        return {
            "email": email,
            "subscribed": True,
            "delivery_state": "pending_confirmation" if is_pending else "subscribed",
            "message": "Subscription request sent — please check your email and confirm the subscription to receive alerts." if is_pending else "Subscribed successfully.",
            "sns_subscription_arn": arn,
        }
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"SNS subscription failed: {str(e)[:500]}")
