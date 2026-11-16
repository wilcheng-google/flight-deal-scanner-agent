"""Deterministic guardrails, intent/outcome telemetry callbacks, and async Memory Bank consolidation."""

from __future__ import annotations

import asyncio
import re
import time
from typing import Any
import uuid

from .config import ALLOWED_ORIGIN_AIRPORTS
from .observability import get_structured_logger
from .pii_redactor import redact_sensitive_pii, scrub_payload

try:
    from google.adk.plugins.base_plugin import BasePlugin
except ImportError:  # pragma: no cover

    class BasePlugin:  # type: ignore[no-redef]
        def __init__(self, name: str = "base_plugin") -> None:
            self.name = name


try:
    from google.adk.models.llm_response import LlmResponse
    from google.genai import types as genai_types
except ImportError:  # pragma: no cover
    LlmResponse = None  # type: ignore[assignment,misc]
    genai_types = None  # type: ignore[assignment]

logger = get_structured_logger()

_PROMPT_INJECTION_BLOCKLIST = re.compile(
    r"(?i)(?:ignore\s+(?:all\s+)?previous\s+instructions|"
    r"system\s+override|reveal\s+(?:your\s+)?system\s+prompt|"
    r"bypass\s+guardrails|exfiltrate\s+secret)"
)

# Tracks in-flight tool call start timestamps by correlation key for latency measurement.
_INFLIGHT_TOOL_TIMERS: dict[str, float] = {}


def enforce_input_security_guardrails(
    callback_context: Any, llm_request: Any
) -> Any | None:
    """`before_model_callback`: Blocks prompt injection and scrubs PII prior to LLM invocation."""
    contents = getattr(llm_request, "contents", None) or []
    for content in contents:
        parts = getattr(content, "parts", None) or []
        for part in parts:
            raw_text = getattr(part, "text", None)
            if not raw_text:
                continue
            if _PROMPT_INJECTION_BLOCKLIST.search(raw_text):
                logger.warning(
                    "Blocked prompt injection attempt in before_model_callback guardrail",
                    extra={
                        "structured_data": {
                            "event_phase": "before_model_guardrail_blocked",
                            "reason": "prompt_injection_pattern_detected",
                        }
                    },
                )
                if LlmResponse is not None and genai_types is not None:
                    return LlmResponse(
                        content=genai_types.Content(
                            role="model",
                            parts=[
                                genai_types.Part.from_text(
                                    text=(
                                        "Request blocked by security policy: untrusted prompt-override "
                                        "instructions are not permitted."
                                    )
                                )
                            ],
                        )
                    )
                return {"status": "blocked", "reason": "prompt_injection_detected"}

            # Scrub any accidental traveler PII in-place before sending to the model.
            sanitized = redact_sensitive_pii(raw_text)
            if sanitized != raw_text:
                try:
                    part.text = sanitized
                except Exception:
                    pass
    return None


def validate_and_log_tool_intent(
    tool: Any, args: dict[str, Any], tool_context: Any
) -> dict[str, Any] | None:
    """`before_tool_callback`: Logs planned tool intent and enforces origin/duration allowlists."""
    tool_name = getattr(tool, "name", str(tool))
    correlation_id = str(uuid.uuid4())
    if isinstance(args, dict):
        state = getattr(tool_context, "state", None)
        if isinstance(state, dict):
            state["temp:last_tool_correlation_id"] = correlation_id
    _INFLIGHT_TOOL_TIMERS[correlation_id] = time.perf_counter()

    intent_meta = {
        "event_phase": "tool_intent_before",
        "correlation_id": correlation_id,
        "tool_name": tool_name,
        "planned_args": scrub_payload(args),
    }
    logger.info("Executing planned tool_call intent before execution", extra={"structured_data": intent_meta})

    # Deterministic policy enforcement before tool execution
    origin = args.get("origin_iata") if isinstance(args, dict) else None
    if isinstance(origin, str) and origin.strip().upper() not in ALLOWED_ORIGIN_AIRPORTS:
        logger.warning(
            "Blocked disallowed departure airport in before_tool_callback",
            extra={
                "structured_data": {
                    "event_phase": "tool_outcome_blocked",
                    "correlation_id": correlation_id,
                    "tool_name": tool_name,
                    "rejected_origin": origin,
                }
            },
        )
        return {
            "status": "error",
            "error_code": "GUARDRAIL_ORIGIN_ALLOWLIST",
            "error_message": (
                f"Guardrail blocked origin '{origin}'. Valid values are SNA or LAX. "
                "Please ask the user to select SNA or LAX and try again."
            ),
        }

    stay_days = args.get("trip_duration_days") if isinstance(args, dict) else None
    if isinstance(stay_days, int) and stay_days < 7:
        return {
            "status": "error",
            "error_code": "GUARDRAIL_MIN_STAY_DAYS",
            "error_message": (
                f"Guardrail blocked trip_duration_days={stay_days}. Minimum duration is 7 days (1 week). "
                "Please retry with trip_duration_days >= 7."
            ),
        }

    return None


def capture_tool_outcome_metrics(
    tool: Any,
    args: dict[str, Any],
    tool_context: Any,
    tool_response: dict[str, Any],
) -> dict[str, Any] | None:
    """`after_tool_callback`: Records actual tool outcome and latency correlated with intent."""
    tool_name = getattr(tool, "name", str(tool))
    state = getattr(tool_context, "state", None)
    correlation_id = (
        state.get("temp:last_tool_correlation_id", "unknown")
        if isinstance(state, dict)
        else "unknown"
    )
    started = _INFLIGHT_TOOL_TIMERS.pop(correlation_id, None)
    latency_ms = round((time.perf_counter() - started) * 1000.0, 2) if started else 0.0
    outcome_status = (
        tool_response.get("status", "ok")
        if isinstance(tool_response, dict)
        else "ok"
    )

    outcome_meta = {
        "event_phase": "tool_outcome_after",
        "correlation_id": correlation_id,
        "tool_name": tool_name,
        "outcome_status": outcome_status,
        "latency_ms": latency_ms,
    }
    logger.info("Completed tool_response outcome after execution", extra={"structured_data": outcome_meta})
    return None


def handle_tool_error_with_guidance(
    tool: Any,
    args: dict[str, Any],
    tool_context: Any,
    error: Exception,
) -> dict[str, Any] | None:
    """`on_tool_error_callback`: Converts unexpected runtime exceptions into guided LLM recovery."""
    tool_name = getattr(tool, "name", str(tool))
    logger.error(
        "Tool execution failed; returning structured recovery guidance",
        extra={
            "structured_data": {
                "event_phase": "tool_outcome_failed",
                "tool_name": tool_name,
                "error": redact_sensitive_pii(str(error)),
            }
        },
    )
    return {
        "status": "error",
        "error_code": "UNEXPECTED_TOOL_EXCEPTION",
        "error_message": (
            f"Tool '{tool_name}' encountered a transient failure ({redact_sensitive_pii(str(error))}). "
            "Please verify parameters and try again."
        ),
    }


async def _consolidate_session_memory_async(callback_context: Any) -> None:
    """Background coroutine persisting sanitized session facts to Vertex AI Memory Bank."""
    try:
        inv_ctx = getattr(callback_context, "_invocation_context", None)
        memory_service = getattr(inv_ctx, "memory_service", None)
        session = getattr(inv_ctx, "session", None)
        if memory_service is not None and session is not None:
            add_fn = getattr(memory_service, "add_session_to_memory", None)
            if callable(add_fn):
                result = add_fn(session)
                if asyncio.iscoroutine(result):
                    await result
                logger.info(
                    "Completed async background memory consolidation to Memory Bank",
                    extra={
                        "structured_data": {
                            "event_phase": "async_memory_consolidated",
                            "session_id": getattr(session, "id", "unknown"),
                        }
                    },
                )
    except Exception as exc:
        logger.warning(
            "Async memory consolidation skipped or failed non-fatally",
            extra={"structured_data": {"error": redact_sensitive_pii(str(exc))}},
        )


def schedule_async_memory_consolidation(callback_context: Any) -> Any | None:
    """`after_agent_callback`: Offloads expensive memory extraction/consolidation off the UI path."""
    try:
        asyncio.get_running_loop()
        asyncio.create_task(_consolidate_session_memory_async(callback_context))
    except RuntimeError:
        # No active event loop in synchronous unit test harnesses
        pass
    return None


class FlightPolicyGuardrailPlugin(BasePlugin):
    """App-level ADK plugin enforcing tool call integrity and PII hygiene across all sub-agents."""

    def __init__(self, max_tool_calls_per_turn: int = 40) -> None:
        super().__init__(name="flight_policy_guardrail_plugin")
        self._max_tool_calls_per_turn = max_tool_calls_per_turn
        self._turn_tool_counts: dict[str, int] = {}

    async def before_run_callback(self, *, invocation_context: Any) -> Any | None:
        inv_id = str(getattr(invocation_context, "invocation_id", "default"))
        self._turn_tool_counts[inv_id] = 0
        return None

    async def before_tool_callback(
        self, *, tool: Any, tool_args: dict[str, Any], tool_context: Any
    ) -> dict[str, Any] | None:
        inv_id = str(getattr(tool_context, "invocation_id", "default"))
        count = self._turn_tool_counts.get(inv_id, 0) + 1
        self._turn_tool_counts[inv_id] = count
        if count > self._max_tool_calls_per_turn:
            return {
                "status": "error",
                "error_code": "CIRCUIT_BREAKER_MAX_TOOL_CALLS",
                "error_message": (
                    "Circuit breaker triggered: exceeded maximum tool calls for a single scan turn. "
                    "Please summarize collected quotes now."
                ),
            }
        return validate_and_log_tool_intent(tool, tool_args, tool_context)

    async def after_tool_callback(
        self,
        *,
        tool: Any,
        tool_args: dict[str, Any],
        tool_context: Any,
        result: dict[str, Any],
    ) -> dict[str, Any] | None:
        return capture_tool_outcome_metrics(tool, tool_args, tool_context, result)

    async def after_run_callback(self, *, invocation_context: Any) -> None:
        inv_id = str(getattr(invocation_context, "invocation_id", "default"))
        self._turn_tool_counts.pop(inv_id, None)
