"""Pydantic data models for Underpass Sentinel."""
from __future__ import annotations

import re
from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")


class RiskLevel(str, Enum):
    GREEN = "green"
    AMBER = "amber"
    RED = "red"


class DataSource(str, Enum):
    LIVE = "live"
    REPLAY_HISTORICAL_FORECAST = "replay_historical_forecast"
    REPLAY_ARCHIVE = "replay_archive"
    REPLAY_ARCHIVE_FALLBACK = "replay_archive_fallback"
    SYNTHETIC = "synthetic"


class Spot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    lat: float
    lon: float
    threshold_mm_hr: float
    original_threshold_mm_hr: float
    alternate_route_hint: str
    verified: bool = False

    @field_validator("threshold_mm_hr", "original_threshold_mm_hr")
    @classmethod
    def _positive_threshold(cls, v: float) -> float:
        if v <= 0:
            raise ValueError("threshold must be positive")
        return float(v)

    @field_validator("lat")
    @classmethod
    def _lat_range(cls, v: float) -> float:
        if not (-90.0 <= v <= 90.0):
            raise ValueError("latitude out of range")
        return float(v)

    @field_validator("lon")
    @classmethod
    def _lon_range(cls, v: float) -> float:
        if not (-180.0 <= v <= 180.0):
            raise ValueError("longitude out of range")
        return float(v)


class SpotAssessment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    spot_id: str
    spot_name: str
    threshold_mm_hr: float
    peak_precipitation_mm_hr: float
    peak_time: Optional[str] = None
    risk_ratio: float
    risk_level: RiskLevel
    hourly_series: list[float] = []
    hourly_timestamps: list[str] = []
    data_source: DataSource
    assessment_window_start: str
    assessment_window_end: str
    data_available: bool = True
    verified: bool = False

    @field_validator("risk_ratio")
    @classmethod
    def _ratio_nonneg(cls, v: float) -> float:
        if v < 0:
            raise ValueError("risk ratio must be non-negative")
        return float(v)


class AssessmentResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: str
    date: Optional[str] = None
    assessment_window_start: str
    assessment_window_end: str
    spots: list[SpotAssessment]
    generated_at: str
    provider_used: str
    error: Optional[str] = None


class ProposalFlaggedSpot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    spot_id: str
    risk_level: RiskLevel
    peak_mm_hr: float
    peak_time: str
    recommended_action: str


class Proposal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    status: str  # pending | approved | rejected | published
    generated_by: str  # bedrock | fallback
    fallback_reason: Optional[str] = None
    english_text: str
    hindi_text: str
    reasoning: str
    flagged_spots: list[ProposalFlaggedSpot]
    window_start: str
    window_end: str
    assessment_ids: list[str] = []
    created_at: str
    published_at: Optional[str] = None
    sns_message_id: Optional[str] = None
    delivery_state: Optional[str] = None  # published | local-skipped | failed
    delivery_error: Optional[str] = None


class VerificationRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    spot_id: str
    spot_name: str
    proposal_id: str
    assessment_window_start: str
    assessment_window_end: str
    forecast_peak_mm_hr: float
    forecast_peak_time: Optional[str]
    observed_peak_mm_hr: Optional[float]
    observed_peak_time: Optional[str]
    outcome: str  # true_alarm | false_alarm | missed_event | no_data | pending
    threshold_before: float
    threshold_after: float
    threshold_adjusted: bool
    forecast_source: str
    observation_source: str
    verified_at: str
    based_on_rainfall: bool = True
    based_on_flood_observations: bool = False


class SubscribeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: str

    @field_validator("email")
    @classmethod
    def _valid_email(cls, v: str) -> str:
        if not EMAIL_RE.match(v):
            raise ValueError("invalid email address")
        return v


class SubscribeResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: str
    subscribed: bool
    delivery_state: str
    message: str
    sns_subscription_arn: Optional[str] = None


class HealthResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: str
    storage_mode: str
    bedrock_configured: bool
    sns_configured: bool
    openmeteo_reachable: bool
    bedrock_model: Optional[str] = None
    bedrock_region: Optional[str] = None
    timestamp: str


class ApproveResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    proposal_id: str
    status: str
    delivery_state: str
    sns_message_id: Optional[str] = None
    delivery_error: Optional[str] = None
    message: str


class RejectResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    proposal_id: str
    status: str
    message: str


class VerifyResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    proposal_id: str
    records: list[VerificationRecord]
    message: str


class HistoryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    records: list[VerificationRecord]