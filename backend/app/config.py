"""Application configuration loaded from environment variables with safe defaults."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parents[2] / ".env")
except ImportError:
    pass


def _env(name: str, default: Optional[str] = None) -> Optional[str]:
    val = os.environ.get(name)
    if val is None or val == "":
        return default
    return val


class Settings:
    def __init__(self) -> None:
        # Bedrock
        self.bedrock_model_id: str = _env("BEDROCK_MODEL_ID", "amazon.nova-lite-v1:0") or "amazon.nova-lite-v1:0"
        self.bedrock_region: str = _env("BEDROCK_REGION", "us-east-1") or "us-east-1"
        self.bedrock_timeout_seconds: float = float(_env("BEDROCK_TIMEOUT_SECONDS", "30") or "30")

        # AWS
        self.aws_region: str = _env("AWS_REGION", "us-east-1") or "us-east-1"
        self.dynamodb_table_proposals: str = _env("DYNAMODB_PROPOSALS_TABLE", "underpass-sentinel-proposals") or "underpass-sentinel-proposals"
        self.dynamodb_table_thresholds: str = _env("DYNAMODB_THRESHOLDS_TABLE", "underpass-sentinel-thresholds") or "underpass-sentinel-thresholds"
        self.dynamodb_table_history: str = _env("DYNAMODB_HISTORY_TABLE", "underpass-sentinel-history") or "underpass-sentinel-history"
        self.sns_topic_arn: str = _env("SNS_TOPIC_ARN", "") or ""
        self.use_dynamodb: bool = _env("USE_DYNAMODB", "").lower() in ("1", "true", "yes")

        # Open-Meteo
        self.openmeteo_timeout_seconds: float = float(_env("OPENMETEO_TIMEOUT_SECONDS", "15") or "15")
        self.openmeteo_max_concurrency: int = int(_env("OPENMETEO_MAX_CONCURRENCY", "8") or "8")
        self.live_forecast_url: str = "https://api.open-meteo.com/v1/forecast"
        self.historical_forecast_url: str = "https://historical-forecast-api.open-meteo.com/v1/forecast"
        self.archive_url: str = "https://archive-api.open-meteo.com/v1/archive"
        self.timezone: str = "Asia/Kolkata"
        self.forecast_hours: int = 12

        # Assessment window
        self.assessment_window_hours: int = int(_env("ASSESSMENT_WINDOW_HOURS", "12") or "12")

        # Risk thresholds
        self.green_ratio: float = 0.6
        self.amber_ratio: float = 1.0

        # Frontend origin for CORS
        self.cors_origins: str = _env("CORS_ORIGINS", "http://localhost:5173,http://localhost:8000") or "http://localhost:5173,http://localhost:8000"

        # Local data dir
        self.local_data_dir: str = _env("LOCAL_DATA_DIR", ".local_data") or ".local_data"

        # Bedrock availability flag (set at runtime after connectivity check)
        self.bedrock_available: bool = False


settings = Settings()
