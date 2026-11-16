"""Automated ADK Golden Evaluation runner gated in Pre-Merge CI and Staging CD."""

from __future__ import annotations

import asyncio
from pathlib import Path
import sys

from flight_scanner_agent.observability import configure_observability

logger = configure_observability("flight-scanner-eval-runner")


async def run_golden_evaluation() -> int:
    """Executes `AgentEvaluator.evaluate` against `evals/flight_scanner.evalset.json`."""
    from google.adk.evaluation.agent_evaluator import AgentEvaluator

    repo_root = Path(__file__).resolve().parent.parent
    evalset_path = repo_root / "evals" / "flight_scanner.evalset.json"
    config_path = repo_root / "evals" / "test_config.json"

    logger.info(
        "Starting automated ADK golden evaluation suite",
        extra={
            "structured_data": {
                "evalset_path": str(evalset_path),
                "config_path": str(config_path),
            }
        },
    )
    await AgentEvaluator.evaluate(
        agent_module="flight_scanner_agent",
        eval_dataset_file_path_or_dir=str(evalset_path),
        num_runs=2,
        config_file_path=str(config_path),
    )
    logger.info("Completed ADK golden evaluation suite successfully")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(run_golden_evaluation()))
