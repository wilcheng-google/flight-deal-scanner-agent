"""Production ADK Runner factory backed by persistent Database / Vertex AI Session & Memory Bank services."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from .agent import app
from .config import FlightScannerSettings, get_settings
from .observability import configure_observability

try:
    from google.adk import Runner
    from google.adk.memory import VertexAiMemoryBankService
    from google.adk.sessions import DatabaseSessionService, VertexAiSessionService
except ImportError:  # pragma: no cover
    Runner = None  # type: ignore[assignment,misc]
    VertexAiMemoryBankService = None  # type: ignore[assignment,misc]
    DatabaseSessionService = None  # type: ignore[assignment,misc]
    VertexAiSessionService = None  # type: ignore[assignment,misc]


def build_persistent_session_service(settings: FlightScannerSettings) -> Any:
    """Creates a persistent session store (Vertex AI Agent Engine or SQL DatabaseSessionService)."""
    use_vertex = os.getenv("USE_VERTEX_AGENT_ENGINE_SESSIONS", "false").lower() == "true"
    if (
        use_vertex
        and settings.gcp_project_id
        and settings.memory_bank_agent_engine_id
        and VertexAiSessionService is not None
    ):
        return VertexAiSessionService(
            project=settings.gcp_project_id,
            location=settings.gcp_location,
            agent_engine_id=settings.memory_bank_agent_engine_id,
        )

    if settings.session_service_uri.startswith("sqlite:///"):
        sqlite_file = settings.session_service_uri.replace("sqlite:///", "", 1)
        Path(os.path.dirname(os.path.abspath(sqlite_file))).mkdir(
            parents=True, exist_ok=True
        )

    if DatabaseSessionService is None:
        raise RuntimeError(
            "google-adk[db] is required to instantiate DatabaseSessionService."
        )
    return DatabaseSessionService(db_url=settings.session_service_uri)


def build_memory_bank_service(settings: FlightScannerSettings) -> Any | None:
    """Creates a long-term VertexAiMemoryBankService when Agent Engine ID is configured."""
    if (
        settings.gcp_project_id
        and settings.memory_bank_agent_engine_id
        and VertexAiMemoryBankService is not None
    ):
        return VertexAiMemoryBankService(
            project=settings.gcp_project_id,
            location=settings.gcp_location,
            agent_engine_id=settings.memory_bank_agent_engine_id,
        )
    return None


def create_production_runner(
    custom_settings: FlightScannerSettings | None = None,
) -> Any:
    """Wires `app`, persistent `DatabaseSessionService`/`VertexAiSessionService`, and `VertexAiMemoryBankService`."""
    configure_observability()
    cfg = custom_settings or get_settings()
    if Runner is None:
        raise RuntimeError("google-adk is required to instantiate Runner.")

    session_service = build_persistent_session_service(cfg)
    memory_service = build_memory_bank_service(cfg)
    return Runner(
        app=app,
        session_service=session_service,
        memory_service=memory_service,
    )
