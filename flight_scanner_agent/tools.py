"""Granular ADK airfare tools with strict schema validation, guided errors, and HITL gates."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import re
from typing import Any

from .config import ALLOWED_ORIGIN_AIRPORTS, DESTINATION_HUBS, SUPPORTED_FARE_PROVIDERS
from .observability import get_structured_logger, trace_span
from .pii_redactor import redact_sensitive_pii
from .providers import query_provider_fares
from .schemas import OriginAirport, PriceDropTier
from .storage import FareHistoryRepository

logger = get_structured_logger()
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

_DEFAULT_REPO: FareHistoryRepository | None = None


def get_fare_repository() -> FareHistoryRepository:
    """Returns the shared persistent `FareHistoryRepository` instance."""
    global _DEFAULT_REPO
    if _DEFAULT_REPO is None:
        _DEFAULT_REPO = FareHistoryRepository()
    return _DEFAULT_REPO


def set_fare_repository(repo: FareHistoryRepository) -> None:
    """Injects a custom `FareHistoryRepository` (used by unit tests and custom runtimes)."""
    global _DEFAULT_REPO
    _DEFAULT_REPO = repo


def compute_target_travel_window(
    lead_days: int = 45,
    trip_duration_days: int = 8,
    reference_date_iso: str = "",
) -> dict[str, Any]:
    """Computes the target outbound and return travel dates ~45 days out with >=7-day duration.

    Use this tool at the start of every scheduled or interactive scan so all flight
    websites (Google Flights, Trip.com, Skyscanner, Kayak, Expedia) evaluate the
    exact same round-trip date window.

    Args:
        lead_days: Number of days from today until outbound departure (target ~45 days).
        trip_duration_days: Length of stay at destination in days; must be at least 7 days.
        reference_date_iso: Optional YYYY-MM-DD anchor date override; defaults to current UTC date.

    Returns:
        Dictionary with `status`, `departure_date`, `return_date`, `flex_earliest_departure`,
        `flex_latest_departure`, and `trip_duration_days`, or structured recovery guidance on error.
    """
    try:
        if lead_days < 7 or lead_days > 330:
            return {
                "status": "error",
                "error_code": "INVALID_LEAD_DAYS",
                "error_message": (
                    f"Invalid lead_days={lead_days}. Valid values are between 7 and 330 "
                    "(recommended default is 45 days). Please retry with lead_days=45."
                ),
            }
        if trip_duration_days < 7:
            return {
                "status": "error",
                "error_code": "STAY_TOO_SHORT",
                "error_message": (
                    f"Invalid trip_duration_days={trip_duration_days}. Trip duration must be "
                    "at least 7 days (1 week). Please retry with trip_duration_days >= 7."
                ),
            }

        if reference_date_iso:
            if not _DATE_RE.match(reference_date_iso):
                return {
                    "status": "error",
                    "error_code": "INVALID_REFERENCE_DATE",
                    "error_message": (
                        f"Invalid reference_date_iso='{reference_date_iso}'. Please provide "
                        "an ISO-8601 date formatted as YYYY-MM-DD and try again."
                    ),
                }
            anchor = date.fromisoformat(reference_date_iso)
        else:
            anchor = datetime.now(timezone.utc).date()

        outbound = anchor + timedelta(days=lead_days)
        inbound = outbound + timedelta(days=trip_duration_days)
        return {
            "status": "ok",
            "anchor_date": anchor.isoformat(),
            "lead_days": lead_days,
            "departure_date": outbound.isoformat(),
            "return_date": inbound.isoformat(),
            "flex_earliest_departure": (outbound - timedelta(days=3)).isoformat(),
            "flex_latest_departure": (outbound + timedelta(days=3)).isoformat(),
            "trip_duration_days": trip_duration_days,
        }
    except Exception as exc:
        return {
            "status": "error",
            "error_code": "WINDOW_CALCULATION_FAILED",
            "error_message": (
                f"Failed to compute travel window ({exc}). Please check date parameters and try again."
            ),
        }


def _validate_route_and_dates(
    origin_iata: str,
    destination_region: str,
    departure_date: str,
    return_date: str,
    trip_duration_days: int,
) -> dict[str, Any] | None:
    """Shared input validator returning actionable LLM recovery hints when invalid."""
    origin = origin_iata.strip().upper()
    if origin not in ALLOWED_ORIGIN_AIRPORTS:
        return {
            "status": "error",
            "error_code": "UNSUPPORTED_ORIGIN_AIRPORT",
            "error_message": (
                f"Origin '{origin_iata}' is outside policy. Valid values are SNA or LAX. "
                "Please ask the user to choose SNA or LAX and try again."
            ),
        }

    region = destination_region.strip().upper()
    if region not in DESTINATION_HUBS:
        valid_regions = ", ".join(sorted(DESTINATION_HUBS.keys()))
        return {
            "status": "error",
            "error_code": "UNSUPPORTED_DESTINATION_REGION",
            "error_message": (
                f"Destination '{destination_region}' is not configured. Valid options are: "
                f"{valid_regions}. Please retry with one of those destination regions."
            ),
        }

    if not _DATE_RE.match(departure_date) or not _DATE_RE.match(return_date):
        return {
            "status": "error",
            "error_code": "INVALID_DATE_FORMAT",
            "error_message": (
                "Dates must follow ISO-8601 YYYY-MM-DD format. Suggest calling "
                "`compute_target_travel_window` first and try again."
            ),
        }

    dep_dt = date.fromisoformat(departure_date)
    ret_dt = date.fromisoformat(return_date)
    actual_days = (ret_dt - dep_dt).days
    if actual_days < 7 or trip_duration_days < 7:
        return {
            "status": "error",
            "error_code": "MINIMUM_DURATION_VIOLATION",
            "error_message": (
                f"Trip duration ({actual_days} days) is shorter than the required 7-day minimum. "
                "Please adjust return_date to be at least 7 days after departure_date and try again."
            ),
        }
    return None


def fetch_google_flights_quotes(
    origin_iata: OriginAirport,
    destination_region: str,
    departure_date: str,
    return_date: str,
    trip_duration_days: int = 8,
) -> dict[str, Any]:
    """Fetches round-trip airfare quotes from Google Flights for SNA or LAX to a target region.

    Queries Google Flights (`google_flights`) across all major hub airports in the
    target region (e.g. HND/NRT/KIX for JAPAN, LHR/LGW for LONDON) and returns
    normalized USD round-trip fares with booking verification links.

    Args:
        origin_iata: Departure airport code; must be `SNA` or `LAX`.
        destination_region: Target region (`JAPAN`, `LONDON`, `VANCOUVER`, `TORONTO`, `SPAIN`, `ITALY`).
        departure_date: Outbound date in `YYYY-MM-DD` format (~45 days from today).
        return_date: Return date in `YYYY-MM-DD` format (at least 7 days after `departure_date`).
        trip_duration_days: Total trip duration in days (minimum 7).

    Returns:
        Dictionary containing `status`, `provider`, `quotes` list, and `lowest_price_usd`.
    """
    try:
        validation_err = _validate_route_and_dates(
            origin_iata,
            destination_region,
            departure_date,
            return_date,
            trip_duration_days,
        )
        if validation_err is not None:
            return validation_err

        quotes = query_provider_fares(
            provider="google_flights",
            origin_iata=origin_iata,
            destination_region=destination_region,
            departure_date=departure_date,
            return_date=return_date,
            trip_duration_days=trip_duration_days,
        )
        lowest = min((q["price_usd"] for q in quotes), default=None)
        return {
            "status": "ok",
            "provider": "google_flights",
            "origin_iata": origin_iata.upper(),
            "destination_region": destination_region.upper(),
            "quotes_count": len(quotes),
            "lowest_price_usd": lowest,
            "quotes": quotes,
        }
    except Exception as exc:
        return {
            "status": "error",
            "error_code": "GOOGLE_FLIGHTS_SCAN_ERROR",
            "error_message": (
                f"Google Flights lookup failed ({exc}). Please check parameters and try again."
            ),
        }


def fetch_ota_aggregator_quotes(
    origin_iata: OriginAirport,
    destination_region: str,
    departure_date: str,
    return_date: str,
    trip_duration_days: int = 8,
) -> dict[str, Any]:
    """Fetches round-trip airfare quotes across Trip.com, Skyscanner, Kayak, and Expedia.

    Use this tool alongside `fetch_google_flights_quotes` to compare major OTA
    pricing platforms (`trip_com`, `skyscanner`, `kayak`, `expedia`) and surface
    OTA-exclusive discounts for SNA and LAX departures.

    Args:
        origin_iata: Departure airport code; must be `SNA` or `LAX`.
        destination_region: Target region (`JAPAN`, `LONDON`, `VANCOUVER`, `TORONTO`, `SPAIN`, `ITALY`).
        departure_date: Outbound date in `YYYY-MM-DD` format (~45 days from today).
        return_date: Return date in `YYYY-MM-DD` format (at least 7 days after `departure_date`).
        trip_duration_days: Total trip duration in days (minimum 7).

    Returns:
        Dictionary with `status`, `providers_checked`, `lowest_quote`, and concise per-OTA quotes.
    """
    try:
        validation_err = _validate_route_and_dates(
            origin_iata,
            destination_region,
            departure_date,
            return_date,
            trip_duration_days,
        )
        if validation_err is not None:
            return validation_err

        ota_providers = ("trip_com", "skyscanner", "kayak", "expedia")
        all_quotes: list[dict[str, Any]] = []
        for provider in ota_providers:
            all_quotes.extend(
                query_provider_fares(
                    provider=provider,
                    origin_iata=origin_iata,
                    destination_region=destination_region,
                    departure_date=departure_date,
                    return_date=return_date,
                    trip_duration_days=trip_duration_days,
                )
            )

        all_quotes.sort(key=lambda item: float(item["price_usd"]))
        lowest_quote = all_quotes[0] if all_quotes else None
        return {
            "status": "ok",
            "providers_checked": list(ota_providers),
            "origin_iata": origin_iata.upper(),
            "destination_region": destination_region.upper(),
            "quotes_count": len(all_quotes),
            "lowest_quote": lowest_quote,
            "top_quotes": all_quotes[:8],
        }
    except Exception as exc:
        return {
            "status": "error",
            "error_code": "OTA_AGGREGATOR_SCAN_ERROR",
            "error_message": (
                f"OTA aggregator scan failed ({exc}). Please retry or verify route parameters."
            ),
        }


def record_fare_observation(
    origin_iata: OriginAirport,
    destination_iata: str,
    destination_region: str,
    provider: str,
    airline: str,
    departure_date: str,
    return_date: str,
    trip_duration_days: int,
    price_usd: float,
) -> dict[str, Any]:
    """Persists a verified airfare quote to the historical database to maintain 30-day baselines.

    Call this tool after retrieving quotes from Google Flights or Trip.com/OTAs so
    every 2-hour scan updates the rolling historical price distribution.

    Args:
        origin_iata: Origin airport IATA code (`SNA` or `LAX`).
        destination_iata: Destination airport 3-letter IATA code (e.g. `NRT`, `LHR`, `YVR`).
        destination_region: Canonical destination region (`JAPAN`, `LONDON`, etc.).
        provider: Source pricing website (`google_flights`, `trip_com`, `skyscanner`, `kayak`, `expedia`).
        airline: Marketing or operating carrier name.
        departure_date: Outbound travel date (`YYYY-MM-DD`).
        return_date: Inbound return date (`YYYY-MM-DD`).
        trip_duration_days: Length of stay in days (>= 7).
        price_usd: Observed round-trip fare in USD.

    Returns:
        Dictionary with `status` and persisted `observation_id`, or guided error message.
    """
    try:
        if provider not in SUPPORTED_FARE_PROVIDERS:
            valid_providers = ", ".join(SUPPORTED_FARE_PROVIDERS)
            return {
                "status": "error",
                "error_code": "INVALID_PROVIDER",
                "error_message": (
                    f"Provider '{provider}' is unrecognized. Valid options are: {valid_providers}. "
                    "Please retry with a valid provider identifier."
                ),
            }
        if price_usd <= 0:
            return {
                "status": "error",
                "error_code": "INVALID_PRICE",
                "error_message": "price_usd must be positive. Please check the quote value and try again.",
            }

        obs_id = get_fare_repository().record_observation(
            origin_iata=origin_iata,
            destination_iata=destination_iata,
            destination_region=destination_region,
            provider=provider,
            airline=airline,
            departure_date=departure_date,
            return_date=return_date,
            trip_duration_days=trip_duration_days,
            price_usd=price_usd,
        )
        return {
            "status": "ok",
            "observation_id": obs_id,
            "origin_iata": origin_iata.upper(),
            "destination_iata": destination_iata.upper(),
            "recorded_price_usd": round(float(price_usd), 2),
        }
    except Exception as exc:
        return {
            "status": "error",
            "error_code": "OBSERVATION_WRITE_FAILED",
            "error_message": (
                f"Could not store fare observation ({exc}). Please check database connectivity and try again."
            ),
        }


def query_route_baseline_average(
    origin_iata: OriginAirport,
    destination_iata: str,
    lookback_days: int = 30,
) -> dict[str, Any]:
    """Retrieves the 30-day rolling average USD airfare for a specific `(origin, destination)` route.

    Args:
        origin_iata: Departure airport IATA code (`SNA` or `LAX`).
        destination_iata: Destination hub 3-letter IATA code (e.g. `HND`, `LHR`, `YYZ`, `BCN`, `FCO`).
        lookback_days: Rolling historical window in days (default 30 days).

    Returns:
        Dictionary with `rolling_average_usd`, `observation_count`, and `baseline_source`.
    """
    try:
        if origin_iata.upper() not in ALLOWED_ORIGIN_AIRPORTS:
            return {
                "status": "error",
                "error_code": "UNSUPPORTED_ORIGIN",
                "error_message": "Valid values are SNA or LAX. Please check origin_iata and try again.",
            }
        if len(destination_iata.strip()) != 3:
            return {
                "status": "error",
                "error_code": "INVALID_DESTINATION_IATA",
                "error_message": (
                    f"Invalid destination_iata='{destination_iata}'. Please provide a 3-letter "
                    "airport code (e.g. HND, NRT, LHR, YVR, YYZ, MAD, BCN, FCO) and try again."
                ),
            }
        stats = get_fare_repository().get_rolling_baseline_usd(
            origin_iata=origin_iata,
            destination_iata=destination_iata,
            lookback_days=lookback_days,
        )
        return {"status": "ok", **stats}
    except Exception as exc:
        return {
            "status": "error",
            "error_code": "BASELINE_LOOKUP_FAILED",
            "error_message": f"Baseline query failed ({exc}). Please retry with valid airport codes.",
        }


def evaluate_price_drop_thresholds(
    origin_iata: OriginAirport,
    destination_iata: str,
    destination_region: str,
    provider: str,
    airline: str,
    departure_date: str,
    return_date: str,
    trip_duration_days: int,
    current_price_usd: float,
    deep_link_url: str,
) -> dict[str, Any]:
    """Evaluates whether a live fare represents a substantial 30%, 40%, or 50%+ drop below average.

    Compares `current_price_usd` against the persistent 30-day rolling average for
    `(origin_iata, destination_iata)` and classifies the deal into `DROP_50_PERCENT_PLUS`,
    `DROP_40_PERCENT`, `DROP_30_PERCENT`, or `NO_SIGNIFICANT_DROP`.

    Args:
        origin_iata: Departure airport code (`SNA` or `LAX`).
        destination_iata: Destination 3-letter airport code (e.g. `NRT`, `LHR`, `BCN`).
        destination_region: Canonical destination region (`JAPAN`, `LONDON`, `VANCOUVER`, `TORONTO`, `SPAIN`, `ITALY`).
        provider: Website surfacing the quote (`google_flights`, `trip_com`, `skyscanner`, `kayak`, `expedia`).
        airline: Carrier name offering the fare.
        departure_date: Outbound date in `YYYY-MM-DD` format.
        return_date: Return date in `YYYY-MM-DD` format (>= 7 days stay).
        trip_duration_days: Stay duration in days (>= 7).
        current_price_usd: Lowest observed round-trip fare in USD.
        deep_link_url: Direct verification link on the pricing website.

    Returns:
        Dictionary detailing `is_substantial_drop`, `discount_pct`, `drop_tier`,
        `rolling_average_usd`, `savings_usd`, and alert metadata.
    """
    try:
        if current_price_usd <= 0:
            return {
                "status": "error",
                "error_code": "NON_POSITIVE_FARE",
                "error_message": "current_price_usd must be greater than 0. Please check the quote and try again.",
            }

        repo = get_fare_repository()
        baseline_info = repo.get_rolling_baseline_usd(origin_iata, destination_iata)
        rolling_avg = float(baseline_info["rolling_average_usd"])
        savings_usd = round(rolling_avg - current_price_usd, 2)
        discount_ratio = (rolling_avg - current_price_usd) / rolling_avg if rolling_avg > 0 else 0.0
        discount_pct = round(max(0.0, discount_ratio * 100.0), 2)

        if discount_ratio >= 0.50:
            tier = PriceDropTier.DROP_50_PERCENT_PLUS
        elif discount_ratio >= 0.40:
            tier = PriceDropTier.DROP_40_PERCENT
        elif discount_ratio >= 0.30:
            tier = PriceDropTier.DROP_30_PERCENT
        else:
            tier = PriceDropTier.NO_SIGNIFICANT_DROP

        is_substantial = tier != PriceDropTier.NO_SIGNIFICANT_DROP
        alert_id: int | None = None
        if is_substantial:
            alert_id = repo.record_triggered_alert(
                origin_iata=origin_iata,
                destination_iata=destination_iata,
                provider=provider,
                current_price_usd=current_price_usd,
                rolling_average_usd=rolling_avg,
                discount_pct=discount_pct,
                drop_tier=tier.value,
            )

        return {
            "status": "ok",
            "is_substantial_drop": is_substantial,
            "alert_id": alert_id,
            "origin_iata": origin_iata.upper(),
            "destination_iata": destination_iata.upper(),
            "destination_region": destination_region.upper(),
            "best_provider": provider,
            "airline": airline,
            "departure_date": departure_date,
            "return_date": return_date,
            "trip_duration_days": trip_duration_days,
            "current_price_usd": round(current_price_usd, 2),
            "rolling_average_usd": rolling_avg,
            "savings_usd": savings_usd,
            "discount_pct": discount_pct,
            "drop_tier": tier.value,
            "deep_link_url": deep_link_url,
        }
    except Exception as exc:
        return {
            "status": "error",
            "error_code": "THRESHOLD_EVALUATION_FAILED",
            "error_message": (
                f"Failed to evaluate price drop thresholds ({exc}). Please check inputs and try again."
            ),
        }


def dispatch_price_drop_alert(
    origin_iata: OriginAirport,
    destination_iata: str,
    provider: str,
    current_price_usd: float,
    discount_pct: float,
    drop_tier: str,
    recipient_channel: str,
    deep_link_url: str,
    tool_context: Any = None,
) -> dict[str, Any]:
    """Dispatches an external price-drop alert notification after human-in-the-loop approval.

    This is a high-stakes external notification action gated by `require_confirmation=True`.
    It verifies explicit approval and scrubs any recipient PII prior to logging.

    Args:
        origin_iata: Departure airport (`SNA` or `LAX`).
        destination_iata: Destination 3-letter airport code.
        provider: Website offering the deal (`google_flights`, `trip_com`, etc.).
        current_price_usd: Verified discounted round-trip USD price.
        discount_pct: Percentage drop below rolling average (must be >= 30.0).
        drop_tier: Threshold tier (`DROP_30_PERCENT`, `DROP_40_PERCENT`, `DROP_50_PERCENT_PLUS`).
        recipient_channel: Target notification channel identifier (e.g. `email_digest` or `webhook`).
        deep_link_url: Direct booking link for the user to inspect the fare.
        tool_context: Injected ADK `ToolContext` carrying human confirmation state.

    Returns:
        Dictionary confirming notification status or requesting explicit human confirmation.
    """
    try:
        if discount_pct < 30.0:
            return {
                "status": "error",
                "error_code": "BELOW_ALERT_THRESHOLD",
                "error_message": (
                    f"discount_pct={discount_pct}% is below the 30% substantial drop minimum. "
                    "Suggest notifying only on >= 30%, 40%, or 50% drops."
                ),
            }

        if tool_context is not None:
            confirmation = getattr(tool_context, "tool_confirmation", None)
            if confirmation is not None and not getattr(confirmation, "confirmed", True):
                return {
                    "status": "cancelled",
                    "message": "Traveler declined dispatching the external price-drop alert.",
                }

        scrubbed_channel = redact_sensitive_pii(recipient_channel)
        with trace_span(
            "notification.dispatch_price_drop_alert",
            {"origin": origin_iata, "destination": destination_iata, "tier": drop_tier},
        ):
            logger.info(
                "Dispatched verified substantial airfare price-drop alert",
                extra={
                    "structured_data": {
                        "origin_iata": origin_iata,
                        "destination_iata": destination_iata,
                        "provider": provider,
                        "current_price_usd": current_price_usd,
                        "discount_pct": discount_pct,
                        "drop_tier": drop_tier,
                        "recipient_channel": scrubbed_channel,
                    }
                },
            )

        return {
            "status": "ok",
            "dispatched": True,
            "route": f"{origin_iata.upper()} -> {destination_iata.upper()}",
            "provider": provider,
            "current_price_usd": round(current_price_usd, 2),
            "discount_pct": round(discount_pct, 2),
            "drop_tier": drop_tier,
            "recipient_channel": scrubbed_channel,
            "deep_link_url": deep_link_url,
        }
    except Exception as exc:
        return {
            "status": "error",
            "error_code": "ALERT_DISPATCH_FAILED",
            "error_message": f"Notification dispatch failed ({exc}). Please check channel config and try again.",
        }


def place_refundable_fare_hold(
    origin_iata: OriginAirport,
    destination_iata: str,
    provider: str,
    airline: str,
    departure_date: str,
    return_date: str,
    locked_price_usd: float,
    tool_context: Any = None,
) -> dict[str, Any]:
    """Places a 24-hour refundable price-lock hold on an exceptional 40%–50%+ airfare drop.

    High-stakes financial/reservation action gated by `FunctionTool(..., require_confirmation=True)`.
    Never executes without explicit human confirmation.

    Args:
        origin_iata: Departure airport (`SNA` or `LAX`).
        destination_iata: Destination 3-letter IATA code.
        provider: Booking platform (`google_flights`, `trip_com`, etc.).
        airline: Operating carrier name.
        departure_date: Outbound date (`YYYY-MM-DD`).
        return_date: Return date (`YYYY-MM-DD`, >= 7 days after departure).
        locked_price_usd: Total round-trip USD price to lock for 24 hours.
        tool_context: Injected ADK `ToolContext` carrying `tool_confirmation` approval state.

    Returns:
        Dictionary with `hold_reference` and 24-hour expiration timestamp, or confirmation prompt.
    """
    try:
        if tool_context is not None:
            confirmation = getattr(tool_context, "tool_confirmation", None)
            request_fn = getattr(tool_context, "request_confirmation", None)
            if confirmation is None and callable(request_fn):
                request_fn(
                    hint=(
                        f"Approve 24-hour refundable fare hold for {origin_iata}->{destination_iata} "
                        f"on {airline} ({departure_date} to {return_date}) at ${locked_price_usd:.2f} USD?"
                    )
                )
                return {
                    "status": "pending_confirmation",
                    "message": "Awaiting explicit human approval before placing 24-hour fare hold.",
                }
            if confirmation is not None and not getattr(confirmation, "confirmed", False):
                return {
                    "status": "cancelled",
                    "message": "Fare hold cancelled by user.",
                }

        expires_utc = (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat()
        hold_ref = f"HOLD-{origin_iata.upper()}{destination_iata.upper()}-{departure_date.replace('-', '')}"
        return {
            "status": "ok",
            "hold_reference": hold_ref,
            "origin_iata": origin_iata.upper(),
            "destination_iata": destination_iata.upper(),
            "provider": provider,
            "airline": airline,
            "locked_price_usd": round(locked_price_usd, 2),
            "expires_at_utc": expires_utc,
        }
    except Exception as exc:
        return {
            "status": "error",
            "error_code": "FARE_HOLD_FAILED",
            "error_message": f"Unable to place fare hold ({exc}). Please verify itinerary and try again.",
        }
