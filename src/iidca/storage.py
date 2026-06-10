"""Storage layer — DuckDB snapshots + persistent watchlist.

Three tables (schema v2 — multi-asset):

  macro_snapshots   — one row per run; the macro engine is global/shared
  asset_snapshots   — one row per (run, symbol); tactical state + decision
  watchlist         — the set of tracked symbols (seeded from config once,
                      then managed from the dashboard)

Every run appends immutable rows. Raw FRED / market series are cached
separately as Parquet files (handled by the provider layer).
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path

import duckdb

from iidca.config import AppCfg
from iidca.models import Decision, MacroState, TechnicalState

logger = logging.getLogger(__name__)

_DEFAULT_DB = Path.home() / ".cache" / "iidca" / "snapshots.duckdb"

_CREATE_TABLES = """
CREATE TABLE IF NOT EXISTS macro_snapshots (
    run_ts        TIMESTAMP NOT NULL,
    as_of         DATE,
    H             DOUBLE,
    regime        VARCHAR,
    s_sahm        DOUBLE,
    s_curve       DOUBLE,
    s_stress      DOUBLE,
    sahm          DOUBLE,
    t10y2y        DOUBLE,
    stlfsi        DOUBLE,
    nfci          DOUBLE,
    stress_source VARCHAR,
    breakers      VARCHAR,
    soft_breakers VARCHAR,
    config_hash   VARCHAR,
    data_ok       BOOLEAN
);

CREATE TABLE IF NOT EXISTS asset_snapshots (
    run_ts             TIMESTAMP NOT NULL,
    as_of              DATE,
    symbol             VARCHAR NOT NULL,
    price              DOUBLE,
    sma200             DOUBLE,
    sma_slope          DOUBLE,
    z                  DOUBLE,
    trend_drift_annual DOUBLE,
    sigma_resid        DOUBLE,
    atr_pct            DOUBLE,
    atr_pct_baseline   DOUBLE,
    vol_factor         DOUBLE,
    adx                DOUBLE,
    trend_strong_down  BOOLEAN,
    M                  DOUBLE,
    label              VARCHAR,
    rationale          VARCHAR,
    data_source        VARCHAR,
    data_fresh         BOOLEAN,
    data_ok            BOOLEAN,
    config_hash        VARCHAR,
    alerted_at         TIMESTAMP
);

CREATE TABLE IF NOT EXISTS watchlist (
    symbol   VARCHAR NOT NULL,
    added_ts TIMESTAMP NOT NULL
);
"""


def _connect(db_path: Path = _DEFAULT_DB) -> duckdb.DuckDBPyConnection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = duckdb.connect(str(db_path))
    conn.execute(_CREATE_TABLES)
    return conn


def _none_if_nan(x):
    try:
        return None if x != x else float(x)
    except TypeError:
        return None


# ---------------------------------------------------------------------------
# Snapshots
# ---------------------------------------------------------------------------

def persist_macro_snapshot(
    macro: MacroState,
    cfg: AppCfg,
    run_ts: datetime,
    db_path: Path = _DEFAULT_DB,
) -> None:
    """Append one immutable macro snapshot row."""
    conn = _connect(db_path)
    raw = macro.raw or {}
    conn.execute(
        """
        INSERT INTO macro_snapshots
          (run_ts, as_of, H, regime, s_sahm, s_curve, s_stress,
           sahm, t10y2y, stlfsi, nfci, stress_source,
           breakers, soft_breakers, config_hash, data_ok)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            run_ts,
            macro.as_of,
            macro.H,
            macro.regime,
            macro.subscores.get("sahm"),
            macro.subscores.get("curve"),
            macro.subscores.get("stress"),
            _none_if_nan(raw.get("sahm")),
            _none_if_nan(raw.get("t10y2y")),
            _none_if_nan(raw.get("stlfsi")),
            _none_if_nan(raw.get("nfci")),
            raw.get("stress_source"),
            json.dumps(macro.breakers_fired),
            json.dumps(macro.soft_breakers_fired),
            cfg.config_hash(),
            macro.data_ok,
        ],
    )
    conn.close()


def persist_asset_snapshot(
    tech: TechnicalState,
    decision: Decision,
    cfg: AppCfg,
    run_ts: datetime,
    data_source: str = "",
    data_fresh: bool = True,
    db_path: Path = _DEFAULT_DB,
) -> None:
    """Append one immutable per-asset snapshot row."""
    conn = _connect(db_path)
    conn.execute(
        """
        INSERT INTO asset_snapshots
          (run_ts, as_of, symbol, price, sma200, sma_slope, z,
           trend_drift_annual, sigma_resid, atr_pct, atr_pct_baseline,
           vol_factor, adx, trend_strong_down, M, label, rationale,
           data_source, data_fresh, data_ok, config_hash, alerted_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
        """,
        [
            run_ts,
            tech.as_of,
            tech.symbol,
            _none_if_nan(tech.price),
            _none_if_nan(tech.sma200),
            _none_if_nan(tech.sma_slope),
            _none_if_nan(tech.z),
            _none_if_nan(tech.trend_drift_annual),
            _none_if_nan(tech.sigma_resid),
            _none_if_nan(tech.atr_pct),
            _none_if_nan(tech.atr_pct_baseline),
            _none_if_nan(tech.vol_factor),
            _none_if_nan(tech.adx),
            tech.trend_strong_down,
            decision.M,
            decision.label,
            json.dumps(decision.rationale, default=str),
            data_source,
            data_fresh,
            tech.data_ok,
            cfg.config_hash(),
        ],
    )
    conn.close()
    logger.info(
        "Snapshot persisted: %s M=%.3f %s", tech.symbol, decision.M, decision.label
    )


def get_latest_macro(db_path: Path = _DEFAULT_DB) -> dict | None:
    """Most recent macro snapshot as a dict, or None."""
    if not db_path.exists():
        return None
    conn = _connect(db_path)
    df = conn.execute(
        "SELECT * FROM macro_snapshots ORDER BY run_ts DESC LIMIT 1"
    ).fetchdf()
    conn.close()
    return None if df.empty else df.iloc[0].to_dict()


def get_latest_assets(db_path: Path = _DEFAULT_DB) -> dict[str, dict]:
    """Latest snapshot per symbol, keyed by symbol."""
    if not db_path.exists():
        return {}
    conn = _connect(db_path)
    df = conn.execute(
        """
        SELECT * FROM asset_snapshots
        QUALIFY ROW_NUMBER() OVER (PARTITION BY symbol ORDER BY run_ts DESC) = 1
        """
    ).fetchdf()
    conn.close()
    return {row["symbol"]: row.to_dict() for _, row in df.iterrows()}


def get_asset_history(
    symbol: str, limit: int = 60, db_path: Path = _DEFAULT_DB
) -> list[dict]:
    """The *limit* most recent snapshots for one symbol, newest first."""
    if not db_path.exists():
        return []
    conn = _connect(db_path)
    df = conn.execute(
        "SELECT * FROM asset_snapshots WHERE symbol = ? "
        "ORDER BY run_ts DESC LIMIT ?",
        [symbol, limit],
    ).fetchdf()
    conn.close()
    return df.to_dict("records")


def get_macro_history(limit: int = 60, db_path: Path = _DEFAULT_DB) -> list[dict]:
    """The *limit* most recent macro snapshots, newest first."""
    if not db_path.exists():
        return []
    conn = _connect(db_path)
    df = conn.execute(
        "SELECT * FROM macro_snapshots ORDER BY run_ts DESC LIMIT ?", [limit]
    ).fetchdf()
    conn.close()
    return df.to_dict("records")


def mark_alerted(run_ts: datetime, db_path: Path = _DEFAULT_DB) -> None:
    """Set alerted_at for all asset snapshots of a run (idempotency guard)."""
    conn = _connect(db_path)
    now = datetime.now(tz=UTC)
    conn.execute(
        "UPDATE asset_snapshots SET alerted_at = ? WHERE run_ts = ? AND alerted_at IS NULL",
        [now, run_ts],
    )
    conn.close()


# ---------------------------------------------------------------------------
# Watchlist
# ---------------------------------------------------------------------------

def get_watchlist(seed: list[str] | None = None, db_path: Path = _DEFAULT_DB) -> list[str]:
    """Tracked symbols in insertion order. Seeds from *seed* if empty."""
    conn = _connect(db_path)
    rows = conn.execute("SELECT symbol FROM watchlist ORDER BY added_ts").fetchall()
    if not rows and seed:
        now = datetime.now(tz=UTC)
        for sym in seed:
            conn.execute(
                "INSERT INTO watchlist (symbol, added_ts) VALUES (?, ?)",
                [sym.upper(), now],
            )
        rows = conn.execute("SELECT symbol FROM watchlist ORDER BY added_ts").fetchall()
    conn.close()
    return [r[0] for r in rows]


def watchlist_add(symbol: str, db_path: Path = _DEFAULT_DB) -> bool:
    """Add *symbol* to the watchlist. Returns False if already present."""
    sym = symbol.strip().upper()
    if not sym:
        return False
    conn = _connect(db_path)
    exists = conn.execute(
        "SELECT 1 FROM watchlist WHERE symbol = ?", [sym]
    ).fetchone()
    if exists:
        conn.close()
        return False
    conn.execute(
        "INSERT INTO watchlist (symbol, added_ts) VALUES (?, ?)",
        [sym, datetime.now(tz=UTC)],
    )
    conn.close()
    return True


def watchlist_remove(symbol: str, db_path: Path = _DEFAULT_DB) -> None:
    """Remove *symbol* from the watchlist (snapshots are kept)."""
    conn = _connect(db_path)
    conn.execute("DELETE FROM watchlist WHERE symbol = ?", [symbol.strip().upper()])
    conn.close()
