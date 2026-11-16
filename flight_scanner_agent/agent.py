"""Multi-agent ADK orchestration and App definition for the Flight Deal Scanner Agent."""

from __future__ import annotations

from typing import Any

from .callbacks import (
    FlightPolicyGuardrailPlugin,
    capture_tool_outcome_metrics,
    enforce_input_security_guardrails,
    handle_tool_error_with_guidance,
    schedule_async_memory_consolidation,
    validate_and_log_tool_intent,
)
from .config import get_settings
from .prompts import (
    COORDINATOR_SYSTEM_PROMPT,
    DEAL_REPORT_SYNTHESIZER_PROMPT,
    GLOBAL_CONSTITUTION_INSTRUCTION,
    GOOGLE_FLIGHTS_SCANNER_PROMPT,
    OTA_AGGREGATOR_SCANNER_PROMPT,
    PRICE_DROP_ANALYZER_PROMPT,
    TRAVEL_WINDOW_PLANNER_PROMPT,
)
from .schemas import FlightScanSummaryReport
from .tools import (
    compute_target_travel_window,
    dispatch_price_drop_alert,
    evaluate_price_drop_thresholds,
    fetch_google_flights_quotes,
    fetch_ota_aggregator_quotes,
    place_refundable_fare_hold,
    query_route_baseline_average,
    record_fare_observation,
)

try:
    from google.adk.agents import LlmAgent, ParallelAgent, SequentialAgent
    from google.adk.agents.context_cache_config import ContextCacheConfig
    from google.adk.apps import App, EventsCompactionConfig, ResumabilityConfig
    from google.adk.apps.llm_event_summarizer import LlmEventSummarizer
    from google.adk.models import Gemini
    from google.adk.plugins.auto_tracing_plugin import AutoTracingPlugin
    from google.adk.plugins.context_filter_plugin import ContextFilterPlugin
    from google.adk.plugins.global_instruction_plugin import GlobalInstructionPlugin
    from google.adk.plugins.logging_plugin import LoggingPlugin
    from google.adk.plugins.reflect_retry_tool_plugin import ReflectAndRetryToolPlugin
    from google.adk.tools import AgentTool, FunctionTool
    from google.adk.tools.preload_memory_tool import PreloadMemoryTool
    from google.genai import types as genai_types

    _ADK_AVAILABLE = True
except ImportError:  # pragma: no cover - lightweight structural shim for bare stdlib test runs
    _ADK_AVAILABLE = False

    class _ShimObj:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self.args = args
            for key, value in kwargs.items():
                setattr(self, key, value)

    LlmAgent = ParallelAgent = SequentialAgent = App = _ShimObj  # type: ignore[misc,assignment]
    EventsCompactionConfig = ContextCacheConfig = ResumabilityConfig = _ShimObj  # type: ignore[misc,assignment]
    LlmEventSummarizer = Gemini = _ShimObj  # type: ignore[misc,assignment]
    AutoTracingPlugin = ContextFilterPlugin = GlobalInstructionPlugin = _ShimObj  # type: ignore[misc,assignment]
    LoggingPlugin = ReflectAndRetryToolPlugin = _ShimObj  # type: ignore[misc,assignment]
    AgentTool = FunctionTool = PreloadMemoryTool = _ShimObj  # type: ignore[misc,assignment]
    genai_types = None  # type: ignore[assignment]


settings = get_settings()


def _build_safety_generate_config() -> Any:
    """Configures explicit Gemini HarmCategory safety thresholds."""
    if not _ADK_AVAILABLE or genai_types is None:
        return None
    return genai_types.GenerateContentConfig(
        temperature=0.1,
        safety_settings=[
            genai_types.SafetySetting(
                category=genai_types.HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT,
                threshold=genai_types.HarmBlockThreshold.BLOCK_LOW_AND_ABOVE,
            ),
            genai_types.SafetySetting(
                category=genai_types.HarmCategory.HARM_CATEGORY_HARASSMENT,
                threshold=genai_types.HarmBlockThreshold.BLOCK_LOW_AND_ABOVE,
            ),
            genai_types.SafetySetting(
                category=genai_types.HarmCategory.HARM_CATEGORY_HATE_SPEECH,
                threshold=genai_types.HarmBlockThreshold.BLOCK_LOW_AND_ABOVE,
            ),
        ],
    )


# 1. Fast model (`gemini-2.5-flash`) computes exact ~45-day outbound & >=7-day return dates
travel_window_planner_agent = LlmAgent(
    name="travel_window_planner_agent",
    model=settings.models.fast_scanner_model,
    description="Computes the canonical ~45-day departure window and >=7-day trip duration.",
    instruction=TRAVEL_WINDOW_PLANNER_PROMPT,
    tools=[compute_target_travel_window],
    output_key="planned_travel_window",
    generate_content_config=_build_safety_generate_config(),
    before_model_callback=enforce_input_security_guardrails,
    before_tool_callback=validate_and_log_tool_intent,
    after_tool_callback=capture_tool_outcome_metrics,
    on_tool_error_callback=handle_tool_error_with_guidance,
)

# 2a. Parallel Worker 1 (`gemini-2.5-flash`): Scans Google Flights across SNA & LAX
google_flights_scanner_agent = LlmAgent(
    name="google_flights_scanner_agent",
    model=settings.models.fast_scanner_model,
    description="Scans Google Flights fares from SNA and LAX to Japan, London, Vancouver, Toronto, Spain, and Italy.",
    instruction=GOOGLE_FLIGHTS_SCANNER_PROMPT,
    include_contents="none",
    tools=[fetch_google_flights_quotes, record_fare_observation],
    output_key="google_flights_findings",
    generate_content_config=_build_safety_generate_config(),
    before_model_callback=enforce_input_security_guardrails,
    before_tool_callback=validate_and_log_tool_intent,
    after_tool_callback=capture_tool_outcome_metrics,
    on_tool_error_callback=handle_tool_error_with_guidance,
)

# 2b. Parallel Worker 2 (`gemini-2.5-flash`): Scans Trip.com, Skyscanner, Kayak, and Expedia
ota_aggregator_scanner_agent = LlmAgent(
    name="ota_aggregator_scanner_agent",
    model=settings.models.fast_scanner_model,
    description="Scans Trip.com, Skyscanner, Kayak, and Expedia in parallel for OTA flash sales from SNA and LAX.",
    instruction=OTA_AGGREGATOR_SCANNER_PROMPT,
    include_contents="none",
    tools=[fetch_ota_aggregator_quotes, record_fare_observation],
    output_key="ota_aggregator_findings",
    generate_content_config=_build_safety_generate_config(),
    before_model_callback=enforce_input_security_guardrails,
    before_tool_callback=validate_and_log_tool_intent,
    after_tool_callback=capture_tool_outcome_metrics,
    on_tool_error_callback=handle_tool_error_with_guidance,
)

# Fan-out / Fan-in ParallelAgent running Google Flights and Multi-OTA scanners concurrently
parallel_provider_scanners = ParallelAgent(
    name="parallel_provider_scanners",
    description="Executes Google Flights and Trip.com/Skyscanner/Kayak/Expedia fare scans concurrently.",
    sub_agents=[google_flights_scanner_agent, ota_aggregator_scanner_agent],
)

# 3. Reasoning model (`gemini-2.5-pro`) compares live fares against persistent 30-day route baselines
price_drop_analyzer_agent = LlmAgent(
    name="price_drop_analyzer_agent",
    model=settings.models.reasoning_coordinator_model,
    description="Evaluates collected fares against 30-day rolling averages to detect 30%, 40%, and 50%+ drops.",
    instruction=PRICE_DROP_ANALYZER_PROMPT,
    tools=[query_route_baseline_average, evaluate_price_drop_thresholds],
    output_key="verified_price_drops",
    generate_content_config=_build_safety_generate_config(),
    before_model_callback=enforce_input_security_guardrails,
    before_tool_callback=validate_and_log_tool_intent,
    after_tool_callback=capture_tool_outcome_metrics,
    on_tool_error_callback=handle_tool_error_with_guidance,
)

# 4. Schema-locked Synthesizer (`gemini-2.5-pro`, `output_schema=FlightScanSummaryReport`, no tools attached)
deal_report_synthesizer_agent = LlmAgent(
    name="deal_report_synthesizer_agent",
    model=settings.models.reasoning_coordinator_model,
    description="Produces the final JSON-schema-validated FlightScanSummaryReport of 30%/40%/50%+ deals.",
    instruction=DEAL_REPORT_SYNTHESIZER_PROMPT,
    output_schema=FlightScanSummaryReport,
    output_key="latest_scan_report",
    generate_content_config=_build_safety_generate_config(),
    before_model_callback=enforce_input_security_guardrails,
)

# End-to-end deterministic SequentialAgent pipeline invoked every 2 hours or on user demand
scheduled_deal_pipeline = SequentialAgent(
    name="scheduled_deal_pipeline",
    description="End-to-end 4-stage airfare scan pipeline: window planning -> parallel OTA scan -> baseline drop analysis -> structured report.",
    sub_agents=[
        travel_window_planner_agent,
        parallel_provider_scanners,
        price_drop_analyzer_agent,
        deal_report_synthesizer_agent,
    ],
)

# Top-level Coordinator (`gemini-2.5-pro`) supporting interactive concierge Q&A, Memory Bank recall, and HITL actions
root_agent = LlmAgent(
    name="flight_concierge_coordinator",
    model=settings.models.reasoning_coordinator_model,
    description="Lead airfare price-drop concierge monitoring SNA & LAX to Japan, London, Vancouver, Toronto, Spain, and Italy every 2 hours.",
    global_instruction=GLOBAL_CONSTITUTION_INSTRUCTION,
    instruction=COORDINATOR_SYSTEM_PROMPT,
    sub_agents=[scheduled_deal_pipeline],
    tools=[
        PreloadMemoryTool(),
        AgentTool(agent=scheduled_deal_pipeline),
        compute_target_travel_window,
        fetch_google_flights_quotes,
        fetch_ota_aggregator_quotes,
        query_route_baseline_average,
        evaluate_price_drop_thresholds,
        FunctionTool(dispatch_price_drop_alert, require_confirmation=True),
        FunctionTool(place_refundable_fare_hold, require_confirmation=True),
    ],
    generate_content_config=_build_safety_generate_config(),
    before_model_callback=enforce_input_security_guardrails,
    before_tool_callback=validate_and_log_tool_intent,
    after_tool_callback=capture_tool_outcome_metrics,
    on_tool_error_callback=handle_tool_error_with_guidance,
    after_agent_callback=schedule_async_memory_consolidation,
)

# Application container wiring compaction, context caching, resumability, and observability/policy plugins
_summarizer = LlmEventSummarizer(
    llm=Gemini(model=settings.models.compaction_summarizer_model)
)

app = App(
    name=settings.app_name,
    root_agent=root_agent,
    events_compaction_config=EventsCompactionConfig(
        summarizer=_summarizer,
        compaction_interval=5,
        overlap_size=1,
        token_threshold=16000,
        event_retention_size=6,
    ),
    context_cache_config=ContextCacheConfig(
        min_tokens=2048,
        ttl_seconds=3600,
        cache_intervals=10,
    ),
    resumability_config=ResumabilityConfig(is_resumable=True),
    plugins=[
        GlobalInstructionPlugin(GLOBAL_CONSTITUTION_INSTRUCTION),
        LoggingPlugin(),
        ReflectAndRetryToolPlugin(max_retries=2),
        ContextFilterPlugin(num_invocations_to_keep=8),
        AutoTracingPlugin(),
        FlightPolicyGuardrailPlugin(max_tool_calls_per_turn=40),
    ],
)
