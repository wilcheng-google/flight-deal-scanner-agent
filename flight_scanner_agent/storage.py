"""Persistent SQLite / Cloud SQL storage for rolling 30-day route baselines and fare history."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import sqlite3
from typing import Any

from .config import SEEDED_ROUTE_BASELINES_USD, get_settings
from .observability import trace_span


class FareHistoryRepository:
    """Manages historical airfare observations and rolling 30-day route averages."""

    def __init__(self, db_path: str | None = None) -> None:
        resolved_path = db_path or get_settings().fare_history_db_path
        self._db_path = resolved_path
        if resolved_path != ":memory:":
            Path(os.path.dirname(os.path.abspath(resolved_path))).mkdir(
                parents=True, exist_ok=True
            )
        self._persistent_conn: sqlite3.Connection | None = (
            sqlite3.connect(":memory:", check_same_thread=False)
            if resolved_path == ":memory:"
            else None
        )
        self._initialize_schema()

    def _connect(self) -> sqlite3.Connection:
        if self._persistent_conn is not None:
            return self._persistent_conn
        return sqlite3.connect(self._db_path)

    def close(self) -> None:
        """Closes any persistent in-memory SQLite connection cleanly."""
        if self._persistent_conn is not None:
            self._persistent_conn.close()
            self._persistent_conn = None

    def _initialize_schema(self) -> None:
        conn = self._connect()
        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS fare_observations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    origin_iata TEXT NOT NULL,
                    destination_iata TEXT NOT NULL,
                    destination_region TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    airline TEXT NOT NULL,
                    departure_date TEXT NOT NULL,
                    return_date TEXT NOT NULL,
                    trip_duration_days INTEGER NOT NULL,
                    price_usd REAL NOT NULL,
                    observed_at_utc TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_route_observed
                ON fare_observations (origin_iata, destination_iata, observed_at_utc)
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS triggered_deal_alerts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    origin_iata TEXT NOT NULL,
                    destination_iata TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    current_price_usd REAL NOT NULL,
                    rolling_average_usd REAL NOT NULL,
                    discount_pct REAL NOT NULL,
                    drop_tier TEXT NOT NULL,
                    created_at_utc TEXT NOT NULL
                )
                """
            )
            conn.commit()
        finally:
            if self._persistent_conn is None:
                conn.close()

    def record_observation(
        self,
        *,
        origin_iata: str,
        destination_iata: str,
        destination_region: str,
        provider: str,
        airline: str,
        departure_date: str,
        return_date: str,
        trip_duration_days: int,
        price_usd: float,
    ) -> int:
        """Inserts a new airfare observation into persistent storage."""
        now_iso = datetime.now(timezone.utc).isoformat()
        with trace_span(
            "db.record_fare_observation",
            {"origin": origin_iata, "destination": destination_iata, "provider": provider},
        ):
            conn = self._connect()
            try:
                cursor = conn.execute(
                    """
                    INSERT INTO fare_observations (
                        origin_iata, destination_iata, destination_region,
                        provider, airline, departure_date, return_date,
                        trip_duration_days, price_usd, observed_at_utc
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        origin_iata.upper(),
                        destination_iata.upper(),
                        destination_region.upper(),
                        provider,
                        airline,
                        departure_date,
                        return_date,
                        trip_duration_days,
                        float(price_usd),
                        now_iso,
                    ),
                )
                conn.commit()
                return int(cursor.lastrowid or 0)
            finally:
                if self._persistent_conn is None:
                    conn.close()

    def get_rolling_baseline_usd(
        self, origin_iata: str, destination_iata: str, lookback_days: int = 30
    ) -> dict[str, Any]:
        """Computes the rolling average USD fare for `(origin, destination)` over `lookback_days`.

        Blends live database observations with seeded historical baselines when fewer
        than 5 observations exist, preventing cold-start skew on first deployment.
        """
        origin = origin_iata.upper()
        dest = destination_iata.upper()
        cutoff_iso = (
            datetime.now(timezone.utc) - timedelta(days=lookback_days)
        ).isoformat()
        seeded_default = SEEDED_ROUTE_BASELINES_USD.get((origin, dest), 950.0)

        with trace_span(
            "db.get_rolling_baseline_usd", {"origin": origin, "destination": dest}
        ):
            conn = self._connect()
            try:
                row = conn.execute(
                    """
                    SELECT COUNT(*), AVG(price_usd), MIN(price_usd), MAX(price_usd)
                    FROM fare_observations
                    WHERE origin_iata = ? AND destination_iata = ? AND observed_at_utc >= ?
                    """,
                    (origin, dest, cutoff_iso),
                ).fetchone()
            finally:
                if self._persistent_conn is None:
                    conn.close()

        sample_count = int(row[0] or 0) if row else 0
        observed_avg = float(row[1]) if (row and row[1] is not None) else None

        if sample_count >= 5 and observed_avg is not None:
            rolling_avg = round(observed_avg, 2)
            source = "observed_30d_rolling_avg"
        elif sample_count > 0 and observed_avg is not None:
            # Smooth cold-start average with 5 virtual prior observations at seeded baseline.
            blended = ((seeded_default * 5.0) + (observed_avg * sample_count)) / (
                5.0 + sample_count
            )
            rolling_avg = round(blended, 2)
            source = "bayesian_blended_baseline"
        else:
            rolling_avg = round(seeded_default, 2)
            source = "seeded_historical_baseline"

        return {
            "origin_iata": origin,
            "destination_iata": dest,
            "rolling_average_usd": rolling_avg,
            "observation_count": sample_count,
            "baseline_source": source,
        }

    def record_triggered_alert(
        self,
        *,
        origin_iata: str,
        destination_iata: str,
        provider: str,
        current_price_usd: float,
        rolling_average_usd: float,
        discount_pct: float,
        drop_tier: str,
    ) -> int:
        """Persists a triggered 30%/40%/50%+ deal alert for deduplication and audit."""
        now_iso = datetime.now(timezone.utc).isoformat()
        conn = self._connect()
        try:
            cursor = conn.execute(
                """
                INSERT INTO triggered_deal_alerts (
                    origin_iata, destination_iata, provider,
                    current_price_usd, rolling_average_usd,
                    discount_pct, drop_tier, created_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    origin_iata.upper(),
                    destination_iata.upper(),
                    provider,
                    current_price_usd,
                    rolling_average_usd,
                    discount_pct,
                    drop_tier,
                    now_iso,
                ),
            )
            conn.commit()
            return int(cursor.lastrowid or 0)
        finally:
            if self._persistent_conn is None:
                conn.close()
