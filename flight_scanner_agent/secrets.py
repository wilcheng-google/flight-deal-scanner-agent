"""Secure credential retrieval backed by Google Cloud Secret Manager with env fallback."""

from __future__ import annotations

from functools import lru_cache
import os

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # pragma: no cover - optional in bare stdlib environments
    pass

try:
    from google.cloud import secretmanager
except ImportError:  # pragma: no cover - graceful fallback when GCP SDK absent locally
    secretmanager = None  # type: ignore[assignment]

_PLACEHOLDER_PREFIXES = ("your_", "<", "changeme", "placeholder", "dummy", "none")


def _is_placeholder(value: str | None) -> bool:
    if not value:
        return True
    normalized = value.strip().lower()
    return any(normalized.startswith(prefix) for prefix in _PLACEHOLDER_PREFIXES)


@lru_cache(maxsize=32)
def get_secret(secret_id: str, project_id: str | None = None) -> str | None:
    """Retrieves a secret from Environment or Google Cloud Secret Manager.

    Never hardcodes credentials in source code. Checks environment variables
    injected via Cloud Run `--set-secrets` first, then queries Secret Manager
    directly when `GOOGLE_CLOUD_PROJECT` is configured.

    Args:
        secret_id: Name of the secret or environment variable (e.g. `SERPAPI_API_KEY`).
        project_id: Optional GCP project ID override.

    Returns:
        Resolved secret string, or `None` if unavailable or set to a template placeholder.
    """
    env_val = os.getenv(secret_id)
    if env_val and not _is_placeholder(env_val):
        return env_val.strip()

    resolved_project = project_id or os.getenv("GOOGLE_CLOUD_PROJECT", "")
    if not resolved_project or _is_placeholder(resolved_project) or secretmanager is None:
        return None

    try:
        client = secretmanager.SecretManagerServiceClient()
        resource_name = f"projects/{resolved_project}/secrets/{secret_id}/versions/latest"
        response = client.access_secret_version(request={"name": resource_name})
        payload = response.payload.data.decode("utf-8").strip()
        return None if _is_placeholder(payload) else payload
    except Exception:
        return None
