"""Explicit JSON schemas and strict input/output validation models for airfare scanning."""

from __future__ import annotations

from enum import Enum
from typing import Any, Literal

try:
    from pydantic import BaseModel, Field
except ImportError:  # pragma: no cover - lightweight fallback for bare stdlib test runs

    def Field(default: Any = None, **kwargs: Any) -> Any:  # type: ignore[misc]
        return default

    class BaseModel:  # type: ignore[no-redef]
        """Minimal BaseModel shim when pydantic is not installed in bare Python."""

        def __init__(self, **data: Any) -> None:
            annotations = getattr(self.__class__, "__annotations__", {})
            for key in annotations:
                if key in data:
                    setattr(self, key, data[key])
                elif hasattr(self.__class__, key):
                    setattr(self, key, getattr(self.__class__, key))
            for key, value in data.items():
                setattr(self, key, value)

        def model_dump(self) -> dict[str, Any]:
            return dict(self.__dict__)


OriginAirport = Literal["SNA", "LAX"]


class DestinationRegion(str, Enum):
    """Supported international destination regions from SNA and LAX."""

    JAPAN = "JAPAN"
    LONDON = "LONDON"
    VANCOUVER = "VANCOUVER"
    TORONTO = "TORONTO"
    SPAIN = "SPAIN"
    ITALY = "ITALY"


class FareProvider(str, Enum):
    """Supported airfare search engines and online travel agencies (OTAs)."""

    GOOGLE_FLIGHTS = "google_flights"
    TRIP_COM = "trip_com"
    SKYSCANNER = "skyscanner"
    KAYAK = "kayak"
    EXPEDIA = "expedia"


class PriceDropTier(str, Enum):
    """Actionable airfare price-drop severity tiers relative to rolling average."""

    NO_SIGNIFICANT_DROP = "NO_SIGNIFICANT_DROP"
    DROP_30_PERCENT = "DROP_30_PERCENT"
    DROP_40_PERCENT = "DROP_40_PERCENT"
    DROP_50_PERCENT_PLUS = "DROP_50_PERCENT_PLUS"


class FlightSearchRequest(BaseModel):
    """Validated search parameters enforcing SNA/LAX origins, ~45d lead time, and >=7d stay."""

    origin_iata: OriginAirport = Field(
        description="Departure airport IATA code; restricted to SNA or LAX."
    )
    destination_region: DestinationRegion = Field(
        description="Target destination region (JAPAN, LONDON, VANCOUVER, TORONTO, SPAIN, ITALY)."
    )
    departure_date: str = Field(
        description="Outbound departure date in ISO-8601 YYYY-MM-DD format (~45 days out).",
        min_length=10,
        max_length=10,
        pattern=r"^\d{4}-\d{2}-\d{2}$",
    )
    return_date: str = Field(
        description="Inbound return date in ISO-8601 YYYY-MM-DD format (>=7 days after departure).",
        min_length=10,
        max_length=10,
        pattern=r"^\d{4}-\d{2}-\d{2}$",
    )
    trip_duration_days: int = Field(
        default=8,
        description="Total length of stay in days; must be at least 7 days (1 week).",
        ge=7,
        le=30,
    )
    cabin_class: Literal["ECONOMY", "PREMIUM_ECONOMY", "BUSINESS"] = Field(
        default="ECONOMY",
        description="Cabin class to compare consistently against historical route baselines.",
    )


class FlightQuote(BaseModel):
    """Normalized round-trip flight fare returned by a pricing provider."""

    origin_iata: OriginAirport = Field(description="Origin airport code (SNA or LAX).")
    destination_iata: str = Field(
        description="Destination hub IATA code (e.g. HND, LHR, YVR, YYZ, MAD, FCO).",
        min_length=3,
        max_length=3,
    )
    destination_region: DestinationRegion = Field(
        description="Canonical destination region."
    )
    provider: FareProvider = Field(
        description="Pricing platform that surfaced this fare (e.g. google_flights, trip_com)."
    )
    airline: str = Field(
        description="Operating or marketing carrier name.", min_length=2, max_length=80
    )
    departure_date: str = Field(
        description="Outbound ISO date YYYY-MM-DD.", pattern=r"^\d{4}-\d{2}-\d{2}$"
    )
    return_date: str = Field(
        description="Return ISO date YYYY-MM-DD.", pattern=r"^\d{4}-\d{2}-\d{2}$"
    )
    trip_duration_days: int = Field(
        description="Length of trip in days (>= 7).", ge=7, le=30
    )
    price_usd: float = Field(
        description="Total round-trip fare in USD including mandatory taxes/fees.",
        ge=50.0,
        le=25000.0,
    )
    stops: int = Field(default=0, description="Number of layover stops each way.", ge=0, le=3)
    deep_link_url: str = Field(
        description="Direct booking or verification URL on Google Flights, Trip.com, or OTA.",
        min_length=8,
        max_length=500,
    )


class PriceDropDealAlert(BaseModel):
    """Structured deal evaluation comparing a live quote against rolling route baseline."""

    origin_iata: OriginAirport = Field(description="Origin airport code (SNA or LAX).")
    destination_iata: str = Field(
        description="Destination airport IATA code.", min_length=3, max_length=3
    )
    destination_region: DestinationRegion = Field(
        description="Destination region label."
    )
    best_provider: FareProvider = Field(
        description="Cheapest website surfacing the deal."
    )
    airline: str = Field(description="Carrier offering the discounted fare.")
    departure_date: str = Field(description="Outbound date YYYY-MM-DD.")
    return_date: str = Field(description="Return date YYYY-MM-DD.")
    trip_duration_days: int = Field(description="Stay duration in days (>= 7).", ge=7)
    current_price_usd: float = Field(description="Current lowest round-trip USD fare.", ge=1.0)
    rolling_average_usd: float = Field(
        description="Historical 30-day rolling average USD fare for this route.", ge=1.0
    )
    discount_pct: float = Field(
        description="Percentage savings below rolling average (e.g. 34.5 for 34.5% off).",
        ge=0.0,
        le=99.0,
    )
    drop_tier: PriceDropTier = Field(
        description="Classified threshold tier (30%, 40%, or 50%+ drop)."
    )
    deep_link_url: str = Field(description="Booking verification link.")


class FlightScanSummaryReport(BaseModel):
    """Structured final output emitted by the Deal Report Synthesizer Agent."""

    scan_timestamp_utc: str = Field(
        description="UTC ISO-8601 timestamp when the 2-hour scan completed."
    )
    target_departure_window: str = Field(
        description="Evaluated outbound window (~45 days from today) and stay duration (>=7 days)."
    )
    routes_scanned_count: int = Field(
        description="Total (origin, destination) pairs checked across providers.", ge=1
    )
    providers_checked: list[str] = Field(
        description="List of flight websites queried (google_flights, trip_com, skyscanner, kayak, expedia)."
    )
    substantial_deals: list[PriceDropDealAlert] = Field(
        default_factory=list,
        description="All fares meeting or exceeding the 30%, 40%, or 50% drop thresholds.",
    )
    executive_summary: str = Field(
        description="Concise human-readable summary highlighting top 30%/40%/50%+ airfare drops."
    )
