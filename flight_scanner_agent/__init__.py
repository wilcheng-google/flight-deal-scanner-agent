"""Flight Deal Scanner Agent package entrypoint for Google ADK CLI (`adk web`, `adk run`, `adk eval`)."""

from __future__ import annotations

from . import agent
from .agent import app, root_agent

__all__ = ["agent", "app", "root_agent"]
