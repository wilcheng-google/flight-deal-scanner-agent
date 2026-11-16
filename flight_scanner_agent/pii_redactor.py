"""Active PII redaction using Google Cloud Sensitive Data Protection (DLP) + regex scrubbing."""

from __future__ import annotations

import os
import re
from typing import Any

try:
    from google.cloud import dlp_v2
except ImportError:  # pragma: no cover - optional when running offline tests
    dlp_v2 = None  # type: ignore[assignment]

_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_PHONE_RE = re.compile(
    r"(?<!\d)(?:\+?1[-.\s]?)?(?:\(\d{3}\)|\d{3})[-.\s]\d{3}[-.\s]\d{4}(?!\d)"
)
_CARD_RE = re.compile(r"\b(?:\d[ -]*?){13,19}\b")
_PASSPORT_RE = re.compile(r"(?i)\b(?:passport[\s#:_-]*)([A-Z0-9]{6,9})\b")


def mask_pii_regex(text: str) -> str:
    """Applies deterministic local regex masking for emails, phones, cards, and passports."""
    if not text:
        return text
    scrubbed = _EMAIL_RE.sub("[REDACTED_EMAIL]", text)
    scrubbed = _PHONE_RE.sub("[REDACTED_PHONE]", scrubbed)
    scrubbed = _PASSPORT_RE.sub("passport [REDACTED_PASSPORT]", scrubbed)
    scrubbed = _CARD_RE.sub("[REDACTED_PAYMENT_CARD]", scrubbed)
    return scrubbed


def redact_sensitive_pii(text: str, project_id: str | None = None) -> str:
    """Scrubs sensitive PII via Cloud DLP `deidentify_content` with local regex fallback.

    Always runs deterministic local masking first, and when `ENABLE_CLOUD_DLP=true`
    with a valid GCP project, invokes Google Cloud Sensitive Data Protection to
    de-identify names, passport numbers, phone numbers, and email addresses.

    Args:
        text: Input string potentially containing traveler PII.
        project_id: Optional GCP project ID for Cloud DLP API calls.

    Returns:
        Sanitized text safe for structured logs, LLM prompts, and Memory Bank storage.
    """
    if not text:
        return text

    locally_scrubbed = mask_pii_regex(text)
    use_cloud_dlp = os.getenv("ENABLE_CLOUD_DLP", "false").lower() == "true"
    resolved_project = project_id or os.getenv("GOOGLE_CLOUD_PROJECT", "")
    if not use_cloud_dlp or not resolved_project or dlp_v2 is None:
        return locally_scrubbed

    try:
        dlp_client = dlp_v2.DlpServiceClient()
        parent = f"projects/{resolved_project}/locations/global"
        inspect_config = {
            "info_types": [
                {"name": "EMAIL_ADDRESS"},
                {"name": "PHONE_NUMBER"},
                {"name": "PERSON_NAME"},
                {"name": "PASSPORT"},
                {"name": "CREDIT_CARD_NUMBER"},
            ]
        }
        deidentify_config = {
            "info_type_transformations": {
                "transformations": [
                    {
                        "primitive_transformation": {
                            "replace_with_info_type_config": {}
                        }
                    }
                ]
            }
        }
        response = dlp_client.deidentify_content(
            request={
                "parent": parent,
                "inspect_config": inspect_config,
                "deidentify_config": deidentify_config,
                "item": {"value": locally_scrubbed},
            }
        )
        return response.item.value
    except Exception:
        return locally_scrubbed


def scrub_payload(payload: Any) -> Any:
    """Recursively scrubs strings in dicts/lists prior to logging or persistence."""
    if isinstance(payload, str):
        return redact_sensitive_pii(payload)
    if isinstance(payload, dict):
        return {str(k): scrub_payload(v) for k, v in payload.items()}
    if isinstance(payload, (list, tuple)):
        return [scrub_payload(item) for item in payload]
    return payload
