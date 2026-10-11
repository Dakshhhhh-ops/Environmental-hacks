"""Underpass Sentinel FastAPI application and Lambda handler."""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from .api.routes import router
from .config import settings

app = FastAPI(
    title="Underpass Sentinel",
    description=("AI-assisted estimated waterlogging risk decision support. "
                 "Rainfall is a proxy, not a flood observation; thresholds are unverified estimates."),
    version="1.0.0",
)
origins = [origin.strip() for origin in settings.cors_origins.split(",") if origin.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type"],
)
app.include_router(router, prefix="/api")

_frontend = Path(__file__).resolve().parents[2] / "frontend"
_frontend = _frontend.resolve()
if _frontend.is_dir():
    app.mount("/", StaticFiles(directory=str(_frontend), html=True), name="frontend")

try:
    from mangum import Mangum
    handler = Mangum(app, lifespan="off")
except ImportError:  # Local API remains usable if Lambda adapter is not installed.
    handler = None
