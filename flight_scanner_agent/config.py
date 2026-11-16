"""Centralized configuration and strategic model routing for the Flight Deal Scanner Agent."""

from __future__ import annotations

from dataclasses import dataclass, field
import os
from typing import Final

ALLOWED_ORIGIN_AIRPORTS: Final[tuple[str, ...]] = ("SNA", "LAX")

DESTINATION_HUBS: Final[dict[str, tuple[str, ...]]] = {
    "JAPAN": ("HND", "NRT", "KIX"),
    "LONDON": ("LHR", "LGW"),
    "VANCOUVER": ("YVR",),
    "TORONTO": ("YYZ",),
    "SPAIN": ("MAD", "BCN"),
    "ITALY": ("FCO", "MXP"),
}

# Seeded historical round-trip USD economy baselines (7-14 day stays, ~45 days out)
# used to bootstrap rolling averages before 30 days of live observations accumulate.
SEEDED_ROUTE_BASELINES_USD: Final[dict[tuple[str, str], float]] = {
    ("LAX", "HND"): 1080.0,
    ("LAX", "NRT"): 1020.0,
    ("LAX", "KIX"): 1095.0,
    ("SNA", "HND"): 1240.0,
    ("SNA", "NRT"): 1190.0,
    ("SNA", "KIX"): 1265.0,
    ("LAX", "LHR"): 890.0,
    ("LAX", "LGW"): 820.0,
    ("SNA", "LHR"): 1010.0,
    ("SNA", "LGW"): 950.0,
    ("LAX", "YVR"): 295.0,
    ("SNA", "YVR"): 340.0,
    ("LAX", "YYZ"): 460.0,
    ("SNA", "YYZ"): 515.0,
    ("LAX", "MAD"): 870.0,
    ("LAX", "BCN"): 885.0,
    ("SNA", "MAD"): 995.0,
    ("SNA", "BCN"): 1015.0,
    ("LAX", "FCO"): 940.0,
    ("LAX", "MXP"): 910.0,
    ("SNA", "FCO"): 1085.0,
    ("SNA", "MXP"): 1050.0,
}

SUPPORTED_FARE_PROVIDERS: Final[tuple[str, ...]] = (
    "google_flights",
    "trip_com",
    "skyscanner",
    "kayak",
    "expedia",
)

PRICE_DROP_TIERS: Final[tuple[float, ...]] = (0.30, 0.40, 0.50)


@dataclass(frozen=True)
class ModelRoutingConfig:
    """Strategic model routing across task complexity tiers."""

    fast_scanner_model: str = field(
        default_factory=lambda: os.getenv("FAST_SCANNER_MODEL", "gemini-2.5-flash")
    )
    compaction_summarizer_model: str = field(
        default_factory=lambda: os.getenv(
            "COMPACTION_SUMMARIZER_MODEL", "gemini-2.5-flash-lite"
        )
    )
    reasoning_coordinator_model: str = field(
        default_factory=lambda: os.getenv(
            "REASONING_COORDINATOR_MODEL", "gemini-2.5-pro"
        )
    )


@dataclass(frozen=True)
class FlightScannerSettings:
    """Runtime settings for scheduled and interactive airfare scanning."""

    app_name: str = "flight_deal_scanner_agent"
    gcp_project_id: str = field(
        default_factory=lambda: os.getenv("GOOGLE_CLOUD_PROJECT", "")
    )
    gcp_location: str = field(
        default_factory=lambda: os.getenv("GOOGLE_CLOUD_LOCATION", "us-central1")
    )
    session_service_uri: str = field(
        default_factory=lambda: os.getenv(
            "SESSION_SERVICE_URI", "sqlite:///./data/flight_sessions.db"
        )
    )
    fare_history_db_path: str = field(
        default_factory=lambda: os.getenv(
            "FARE_HISTORY_DB_PATH", "./data/fare_history.db"
        )
    )
    memory_bank_agent_engine_id: str = field(
        default_factory=lambda: os.getenv("VERTEX_MEMORY_BANK_AGENT_ENGINE_ID", "")
    )
    target_lead_days: int = field(
        default_factory=lambda: int(os.getenv("TARGET_LEAD_DAYS", "45"))
    )
    lead_days_flex_window: int = field(
        default_factory=lambda: int(os.getenv("LEAD_DAYS_FLEX_WINDOW", "3"))
    )
    min_trip_duration_days: int = field(
        default_factory=lambda: int(os.getenv("MIN_TRIP_DURATION_DAYS", "7"))
    )
    max_trip_duration_days: int = field(
        default_factory=lambda: int(os.getenv("MAX_TRIP_DURATION_DAYS", "14"))
    )
    scan_interval_hours: int = field(
        default_factory=lambda: int(os.getenv("SCAN_INTERVAL_HOURS", "2"))
    )
    enable_cloud_trace: bool = field(
        default_factory=lambda: os.getenv("OTEL_EXPORT_TO_CLOUD", "false").lower()
        == "true"
    )
    models: ModelRoutingConfig = field(default_factory=ModelRoutingConfig)


def get_settings() -> FlightScannerSettings:
    """Returns a validated snapshot of runtime settings."""
    return FlightScannerSettings()
