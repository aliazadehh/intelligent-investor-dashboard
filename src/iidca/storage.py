"""T10 — Storage layer: DuckDB snapshots + Parquet raw-series cache (§9.4).

Every run appends one immutable row to DuckDB.  Raw FRED/market series are
cached separately as Parquet files (handled by the provider layer).

Schema (one row per run):
  run_ts        TIMESTAMP   — wall-clock time of the run
  as_of         DATE        — date of the latest data observation used
  symbol        VARCHAR     — target asset ticker
  H             DOUBLE      — macro health score ∈ [0, 1]
  regime        VARCHAR     — EXPANSION | CAUTION | STRESS
  Z             DOUBLE      — log-price Z-score
  atr_pct       DOUBLE      — ATR% at run time
  adx           DOUBLE      — ADX(14)
  rsi           DOUBLE      — RSI(14)
  M             DOUBLE      — DCA multiplier
  label         VARCHAR     — allocation label
  breakers      VARCHAR     — JSON list of fired circuit-breaker names
  config_hash   VARCHAR     — first 16 hex chars of SHA-256 of config
  data_ok       BOOLEAN     — False if any data failure occurred
  alerted_at    TIMESTAMP   — when the alert was sent (NULL = not yet sent)
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

import duckdb

from iidca.config import AppCfg
from iidca.models import Decision, MacroState, TechnicalState

logger = logging.getLogger(__name__)

_DEFAULT_DB = Path.home() / ".cache" / "iidca" / "snapshots.duckdb"

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS snapshots (
    run_ts      TIMESTAMP NOT NULL,
    as_of       DATE,
    symbol      VARCHAR,
    H           DOUBLE,
    regime      VARCHAR,
    Z           DOUBLE,
    atr_pct     DOUBLE,
    adx         DOUBLE,
    rsi         DOUBLE,
    M           DOUBLE,
    label       VARCHAR,
    breakers    VARCHAR,
    config_hash VARCHAR,
    data_ok     BOOLEAN,
    alerted_at  TIMESTAMP
);
"""


def _connect(db_path: Path = _DEFAULT_DB) -> duckdb.DuckDBPyConnection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = duckdb.connect(str(db_path))
    conn.execute(_CREATE_TABLE)
    return conn


def persist_snapshot(
    macro: MacroState,
    tech: TechnicalState,
    decision: Decision,
    cfg: AppCfg,
    db_path: Path = _DEFAULT_DB,
) -> None:
    """Append one immutable snapshot row to DuckDB."""
    conn = _connect(db_path)
    run_ts = datetime.now(tz=timezone.utc)
    as_of = macro.as_of or tech.as_of

    conn.execute(
        """
        INSERT INTO snapshots
          (run_ts, as_of, symbol, H, regime, Z, atr_pct, adx, rsi,
           M, label, breakers, config_hash, data_ok, alerted_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
        """,
        [
            run_ts,
            as_of,
            tech.symbol,
            macro.H,
            macro.regime,
            tech.z,
            tech.atr_pct,
            tech.adx,
            tech.rsi,
            decision.M,
            decision.label,
            json.dumps(macro.breakers_fired),
            cfg.config_hash(),
            macro.data_ok and tech.data_ok,
        ],
    )
    conn.close()
    logger.info("Snapshot persisted: M=%.3f  %s  %s", decision.M, decision.label, run_ts.date())


def get_latest_snapshot(db_path: Path = _DEFAULT_DB) -> dict | None:
    """Return the most recent snapshot row as a dict, or None."""
    if not db_path.exists():
        return None
    conn = _connect(db_path)
    result = conn.execute(
        "SELECT * FROM snapshots ORDER BY run_ts DESC LIMIT 1"
    ).fetchdf()
    conn.close()
    if result.empty:
        return None
    return result.iloc[0].to_dict()


def get_snapshot_history(limit: int = 24, db_path: Path = _DEFAULT_DB) -> list[dict]:
    """Return the *limit* most recent snapshot rows, newest first."""
    if not db_path.exists():
        return []
    conn = _connect(db_path)
    df = conn.execute(
        f"SELECT * FROM snapshots ORDER BY run_ts DESC LIMIT {limit}"
    ).fetchdf()
    conn.close()
    return df.to_dict("records")


def mark_alerted(run_ts: datetime, db_path: Path = _DEFAULT_DB) -> None:
    """Set alerted_at for the snapshot with the given run_ts (idempotency guard)."""
    conn = _connect(db_path)
    now = datetime.now(tz=timezone.utc)
    conn.execute(
        "UPDATE snapshots SET alerted_at = ? WHERE run_ts = ? AND alerted_at IS NULL",
        [now, run_ts],
    )
    conn.close()
