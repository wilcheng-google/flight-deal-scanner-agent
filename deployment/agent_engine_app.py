"""Vertex AI Agent Engine deployment entrypoint with Cloud Trace enabled."""

from __future__ import annotations

import os
from typing import Any

from flight_scanner_agent.agent import root_agent
from flight_scanner_agent.config import get_settings

try:
    import vertexai
    from vertexai import agent_engines
    from vertexai.preview.reasoning_engines import AdkApp
except ImportError:  # pragma: no cover
    vertexai = None  # type: ignore[assignment]
    agent_engines = None  # type: ignore[assignment]
    AdkApp = None  # type: ignore[assignment,misc]


def build_agent_engine_app() -> Any:
    """Wraps `root_agent` in `AdkApp(enable_tracing=True)` for managed Vertex AI Agent Engine."""
    if AdkApp is None:
        raise RuntimeError(
            "google-cloud-aiplatform[agent_engines] is required to build AdkApp."
        )
    return AdkApp(
        agent=root_agent,
        enable_tracing=True,
    )


def deploy_to_vertex_agent_engine(staging_bucket: str) -> Any:
    """Deploys the Flight Deal Scanner Agent to managed Vertex AI Agent Engine."""
    cfg = get_settings()
    if vertexai is None or agent_engines is None:
        raise RuntimeError("google-cloud-aiplatform[agent_engines] is not installed.")

    vertexai.init(
        project=cfg.gcp_project_id,
        location=cfg.gcp_location,
        staging_bucket=staging_bucket,
    )
    adk_app = build_agent_engine_app()
    return agent_engines.create(
        agent_engine=adk_app,
        display_name="flight-deal-scanner-agent",
        description="Automated 2-hour SNA/LAX international airfare scanner (30%/40%/50%+ drop alerts).",
        requirements="./requirements.txt",
        extra_packages=["./flight_scanner_agent"],
        env_vars={
            "GOOGLE_CLOUD_AGENT_ENGINE_ENABLE_TELEMETRY": "true",
            "OTEL_EXPORT_TO_CLOUD": "true",
            "TARGET_LEAD_DAYS": str(cfg.target_lead_days),
            "MIN_TRIP_DURATION_DAYS": str(cfg.min_trip_duration_days),
            "SCAN_INTERVAL_HOURS": os.getenv("SCAN_INTERVAL_HOURS", "2"),
        },
    )
