"""Exercise the running local app through HTTP without AWS credentials."""
from __future__ import annotations

import argparse
import json
import sys

import httpx

parser = argparse.ArgumentParser()
parser.add_argument("--base-url", default="http://127.0.0.1:8000")
parser.add_argument("--date", default="2025-05-01", help="completed replay date (YYYY-MM-DD)")
parser.add_argument("--synthetic", action="store_true", help="use the clearly labelled local demo fixture")
args = parser.parse_args()
base = args.base_url.rstrip("/")

with httpx.Client(base_url=base, timeout=45) as client:
    page = client.get("/")
    health = client.get("/api/health").json()
    spots_response = client.get("/api/spots")
    spots = spots_response.json()["spots"]
    print(json.dumps({"homepage_status": page.status_code, "health": health, "spot_count": len(spots)}, ensure_ascii=False))

    proposal_response = client.post("/api/proposals", json={"mode": "replay", "date": args.date, "synthetic": args.synthetic})
    if proposal_response.status_code != 200:
        print(json.dumps({"replay_status": proposal_response.status_code, "detail": proposal_response.text}, ensure_ascii=False))
        sys.exit(2)
    proposal_response.raise_for_status()
    proposal = proposal_response.json()
    print(f"{'Synthetic fixture' if args.synthetic else 'Historical provider'} assessments (actual API response):")
    for assessment in proposal["assessments"][:3]:
        print(json.dumps({key: assessment[key] for key in (
            "spot_id", "spot_name", "threshold_mm_hr", "peak_precipitation_mm_hr",
            "risk_ratio", "risk_level", "data_source", "assessment_window_start",
            "assessment_window_end", "data_available")}, ensure_ascii=False))
    print(json.dumps({"proposal_id": proposal["id"], "generated_by": proposal["generated_by"],
                      "fallback_reason": proposal.get("fallback_reason"), "status": proposal["status"]}, ensure_ascii=False))

    approved = client.post(f"/api/proposals/{proposal['id']}/approve")
    approved.raise_for_status()
    print(json.dumps({"approval": approved.json()}, ensure_ascii=False))

    if args.synthetic:
        verified = client.post(f"/api/proposals/{proposal['id']}/verify")
        print(json.dumps({"synthetic_verification_status": verified.status_code, "detail": verified.json().get("detail")}, ensure_ascii=False))
        records = []
    else:
        verified = client.post(f"/api/proposals/{proposal['id']}/verify")
        verified.raise_for_status()
        records = verified.json()["records"]
        history = client.get("/api/history").json()["records"]
        print(json.dumps({"verification_message": verified.json()["message"], "record_count": len(records),
                          "outcomes": sorted({r["outcome"] for r in records}), "history_count": len(history),
                          "observation_sources": sorted({r["observation_source"] for r in records})}, ensure_ascii=False))

if page.status_code != 200 or len(spots) < 8 or (not args.synthetic and len(records) != len(spots)):
    sys.exit("Local smoke workflow did not return all expected records.")
