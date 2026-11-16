# Architectural Specification (`SPEC.md`) — Flight Deal Scanner Agent

## 1. Objective & Domain Invariants
Build a production-grade, multi-agent airfare scanner using the **Google Agent Development Kit (ADK)** that monitors round-trip flight prices **every 2 hours (12x daily)** and alerts the traveler whenever fares drop substantially (**30%**, **40%**, or **50%+**) below the 30-day rolling historical average.

### Non-Negotiable Domain Rules
* **Allowed Origins**: `SNA` (John Wayne Airport, Orange County) and `LAX` (Los Angeles International Airport). Any other origin is deterministically rejected by tool & callback guardrails.
* **Target Destination Regions (11 Major Hubs)**:
  * `JAPAN`: `HND` (Tokyo Haneda), `NRT` (Tokyo Narita), `KIX` (Osaka Kansai)
  * `LONDON`: `LHR` (London Heathrow), `LGW` (London Gatwick)
  * `VANCOUVER`: `YVR` (Vancouver International)
  * `TORONTO`: `YYZ` (Toronto Pearson)
  * `SPAIN`: `MAD` (Madrid-Barajas), `BCN` (Barcelona-El Prat)
  * `ITALY`: `FCO` (Rome Fiumicino), `MXP` (Milan Malpensa)
* **Travel Date & Minimum Duration**:
  * Outbound departure date: **~45 days from the current date** (`lead_days = 45`, +/- 3 days flex).
  * Trip length: **Minimum 7 days (1 full week)** (`trip_duration_days >= 7`).
* **Multi-Website Coverage**: Every scan checks **Google Flights** (`google_flights`), **Trip.com** (`trip_com`), **Skyscanner** (`skyscanner`), **Kayak** (`kayak`), and **Expedia** (`expedia`).
* **Substantial Drop Alert Tiers**:
  * `DROP_30_PERCENT`: 30.0% – 39.9% below rolling route baseline
  * `DROP_40_PERCENT`: 40.0% – 49.9% below rolling route baseline
  * `DROP_50_PERCENT_PLUS`: >= 50.0% below rolling route baseline

## 2. Multi-Agent Architecture & Strategic Model Routing
* **`flight_concierge_coordinator` (`gemini-2.5-pro`)**: Top-level interactive coordinator equipped with `PreloadMemoryTool`, `AgentTool(scheduled_deal_pipeline)`, and Human-in-the-Loop (`require_confirmation=True`) tools (`dispatch_price_drop_alert`, `place_refundable_fare_hold`).
* **`scheduled_deal_pipeline` (`SequentialAgent`)**:
  1. `travel_window_planner_agent` (`gemini-2.5-flash`): Deterministically computes the ~45-day outbound and >=7-day return dates.
  2. `parallel_provider_scanners` (`ParallelAgent`): Concurrently executes `google_flights_scanner_agent` (`gemini-2.5-flash`) and `ota_aggregator_scanner_agent` (`gemini-2.5-flash`, checking Trip.com, Skyscanner, Kayak, Expedia) with `include_contents="none"` to isolate sub-agent context.
  3. `price_drop_analyzer_agent` (`gemini-2.5-pro`): Compares live fares against persistent 30-day rolling SQL baselines (`FareHistoryRepository`).
  4. `deal_report_synthesizer_agent` (`gemini-2.5-pro`): Emits a schema-validated `FlightScanSummaryReport` (`output_schema=FlightScanSummaryReport`).
* **Compaction Summarizer (`gemini-2.5-flash-lite`)**: Powers `EventsCompactionConfig` via `LlmEventSummarizer`.

## 3. Security, Privacy & AgentOps Invariants
* **Zero Hardcoded Secrets**: Loaded exclusively via Google Cloud Secret Manager (`SecretManagerServiceClient.access_secret_version`) or gitignored `.env`.
* **Active PII Scrubbing**: `redact_sensitive_pii` integrates Google Cloud Sensitive Data Protection (`DlpServiceClient.deidentify_content`) + deterministic regex masking prior to LLM calls, structured JSON logs, and async Memory Bank writes.
* **Human-in-the-Loop Gates**: External alert dispatch and 24-hour refundable fare holds require explicit human confirmation (`FunctionTool(..., require_confirmation=True)`).
* **Do Not**: Never use `print()` for runtime logging, never use ephemeral in-memory session stores in production `Runner`, and never commit `.env` files.
