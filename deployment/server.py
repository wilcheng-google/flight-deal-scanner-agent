"""FastAPI + A2A Cloud Run server with `/scan/scheduled` endpoint invoked every 2 hours."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from flight_scanner_agent.agent import root_agent
from flight_scanner_agent.config import (
    ALLOWED_ORIGIN_AIRPORTS,
    DESTINATION_HUBS,
    SUPPORTED_FARE_PROVIDERS,
    get_settings,
)
from flight_scanner_agent.observability import configure_observability, trace_span
from flight_scanner_agent.tools import (
    compute_target_travel_window,
    evaluate_price_drop_thresholds,
    fetch_google_flights_quotes,
    fetch_ota_aggregator_quotes,
    record_fare_observation,
)

logger = configure_observability("flight-deal-scanner-server")


def execute_full_scheduled_matrix_scan(
    lead_days: int = 45,
    trip_duration_days: int = 8,
) -> dict[str, Any]:
    """Runs a deterministic sweep of SNA & LAX across all 6 destination regions and all 5 flight sites.

    Invoked automatically every 2 hours (`0 */2 * * *`) by Google Cloud Scheduler
    or GitHub Actions cron, ensuring continuous 24x7 monitoring even without manual chat input.
    """
    window = compute_target_travel_window(
        lead_days=lead_days,
        trip_duration_days=trip_duration_days,
    )
    if window.get("status") != "ok":
        return window

    departure_date = str(window["departure_date"])
    return_date = str(window["return_date"])
    substantial_deals: list[dict[str, Any]] = []
    routes_scanned = 0

    with trace_span(
        "scheduled_scan.full_matrix",
        {"departure_date": departure_date, "return_date": return_date},
    ):
        for origin in ALLOWED_ORIGIN_AIRPORTS:
            for region in DESTINATION_HUBS:
                gf_res = fetch_google_flights_quotes(
                    origin_iata=origin,  # type: ignore[arg-type]
                    destination_region=region,
                    departure_date=departure_date,
                    return_date=return_date,
                    trip_duration_days=trip_duration_days,
                )
                ota_res = fetch_ota_aggregator_quotes(
                    origin_iata=origin,  # type: ignore[arg-type]
                    destination_region=region,
                    departure_date=departure_date,
                    return_date=return_date,
                    trip_duration_days=trip_duration_days,
                )

                combined = list(gf_res.get("quotes", [])) + list(
                    ota_res.get("top_quotes", [])
                )
                best_by_hub: dict[str, dict[str, Any]] = {}
                for quote in combined:
                    hub = str(quote["destination_iata"])
                    if (
                        hub not in best_by_hub
                        or float(quote["price_usd"]) < float(best_by_hub[hub]["price_usd"])
                    ):
                        best_by_hub[hub] = quote

                for hub, best_quote in best_by_hub.items():
                    routes_scanned += 1
                    evaluation = evaluate_price_drop_thresholds(
                        origin_iata=origin,  # type: ignore[arg-type]
                        destination_iata=hub,
                        destination_region=region,
                        provider=str(best_quote["provider"]),
                        airline=str(best_quote["airline"]),
                        departure_date=departure_date,
                        return_date=return_date,
                        trip_duration_days=trip_duration_days,
                        current_price_usd=float(best_quote["price_usd"]),
                        deep_link_url=str(best_quote["deep_link_url"]),
                    )
                    # Record observation after baseline comparison so current flash sale does not dilute baseline first
                    record_fare_observation(
                        origin_iata=origin,  # type: ignore[arg-type]
                        destination_iata=hub,
                        destination_region=region,
                        provider=str(best_quote["provider"]),
                        airline=str(best_quote["airline"]),
                        departure_date=departure_date,
                        return_date=return_date,
                        trip_duration_days=trip_duration_days,
                        price_usd=float(best_quote["price_usd"]),
                    )
                    if evaluation.get("is_substantial_drop"):
                        substantial_deals.append(evaluation)

    substantial_deals.sort(key=lambda d: float(d["discount_pct"]), reverse=True)
    logger.info(
        "Completed 2-hour scheduled airfare scan across SNA and LAX",
        extra={
            "structured_data": {
                "event_phase": "scheduled_scan_completed",
                "routes_scanned_count": routes_scanned,
                "substantial_deals_found": len(substantial_deals),
            }
        },
    )
    return {
        "status": "ok",
        "scan_timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "target_departure_window": f"{departure_date} to {return_date} ({trip_duration_days} days)",
        "routes_scanned_count": routes_scanned,
        "providers_checked": list(SUPPORTED_FARE_PROVIDERS),
        "substantial_deals": substantial_deals,
    }


try:
    from fastapi import BackgroundTasks, FastAPI

    fastapi_app = FastAPI(
        title="Flight Deal Scanner Agent API",
        version="1.0.0",
        description="Automated 2-hour SNA/LAX flight price-drop monitor (Google Flights, Trip.com, Skyscanner, Kayak, Expedia).",
    )

    @fastapi_app.get("/healthz")
    async def healthcheck() -> dict[str, str]:
        return {"status": "healthy", "app": get_settings().app_name}

    @fastapi_app.post("/scan/scheduled")
    async def trigger_scheduled_scan(background_tasks: BackgroundTasks) -> dict[str, Any]:
        """Endpoint invoked every 2 hours (`0 */2 * * *`) by Cloud Scheduler."""
        cfg = get_settings()
        report = execute_full_scheduled_matrix_scan(
            lead_days=cfg.target_lead_days,
            trip_duration_days=max(7, cfg.min_trip_duration_days),
        )
        background_tasks.add_task(
            logger.info,
            "Async post-scan audit digest persisted",
            extra={"structured_data": {"deals_count": len(report.get("substantial_deals", []))}},
        )
        return report

    # Expose A2A Agent Card (`/.well-known/agent.json`) when ADK A2A helper is installed
    try:
        from google.adk.a2a.utils.agent_to_a2a import to_a2a

        a2a_app = to_a2a(root_agent)
        fastapi_app.mount("/a2a", a2a_app)
    except Exception:
        pass

except ImportError:  # pragma: no cover
    fastapi_app = None  # type: ignore[assignment]
