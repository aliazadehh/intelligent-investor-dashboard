"""Storage layer — PostgreSQL (Supabase) snapshots + persistent watchlist.

Three tables:
  macro_snapshots   — one row per run; the macro engine is global/shared
  asset_snapshots   — one row per (run, symbol); tactical state + decision
  watchlist         — the set of tracked symbols (seeded from config once,
                      then managed from the dashboard)

Every run appends immutable rows. Raw FRED / market series are cached
separately as Parquet files (handled by the provider layer).

Connection is via DATABASE_URL env var (PostgreSQL / Supabase connection string).
"""

from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime

import pandas as pd
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from iidca.config import AppCfg
from iidca.models import Decision, MacroState, TechnicalState

logger = logging.getLogger(__name__)

_engine: Engine | None = None

_CREATE_MACRO = """
CREATE TABLE IF NOT EXISTS macro_snapshots (
    run_ts        TIMESTAMPTZ NOT NULL,
    as_of         DATE,
    "H"           FLOAT8,
    regime        TEXT,
    s_sahm        FLOAT8,
    s_curve       FLOAT8,
    s_stress      FLOAT8,
    sahm          FLOAT8,
    t10y2y        FLOAT8,
    stlfsi        FLOAT8,
    nfci          FLOAT8,
    stress_source TEXT,
    breakers      TEXT,
    soft_breakers TEXT,
    config_hash   TEXT,
    data_ok       BOOLEAN
)
"""

_CREATE_ASSETS = """
CREATE TABLE IF NOT EXISTS asset_snapshots (
    run_ts             TIMESTAMPTZ NOT NULL,
    as_of              DATE,
    symbol             TEXT NOT NULL,
    price              FLOAT8,
    sma200             FLOAT8,
    sma_slope          FLOAT8,
    z                  FLOAT8,
    trend_drift_annual FLOAT8,
    sigma_resid        FLOAT8,
    atr_pct            FLOAT8,
    atr_pct_baseline   FLOAT8,
    vol_factor         FLOAT8,
    adx                FLOAT8,
    trend_strong_down  BOOLEAN,
    "M"                FLOAT8,
    label              TEXT,
    rationale          TEXT,
    data_source        TEXT,
    data_fresh         BOOLEAN,
    data_ok            BOOLEAN,
    config_hash        TEXT,
    alerted_at         TIMESTAMPTZ
)
"""

_CREATE_WATCHLIST = """
CREATE TABLE IF NOT EXISTS watchlist (
    symbol   TEXT NOT NULL,
    added_ts TIMESTAMPTZ NOT NULL
)
"""


def _get_engine() -> Engine:
    global _engine
    if _engine is None:
        url = os.environ.get("DATABASE_URL")
        if not url:
            raise RuntimeError(
                "DATABASE_URL is not set. Add it to .streamlit/secrets.toml "
                "or set it as an environment variable."
            )
        _engine = create_engine(url, pool_pre_ping=True, pool_size=5, max_overflow=5)
        with _engine.begin() as conn:
            conn.execute(text(_CREATE_MACRO))
            conn.execute(text(_CREATE_ASSETS))
            conn.execute(text(_CREATE_WATCHLIST))
    return _engine


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
) -> None:
    """Append one immutable macro snapshot row."""
    engine = _get_engine()
    raw = macro.raw or {}
    with engine.begin() as conn:
        conn.execute(
            text("""
            INSERT INTO macro_snapshots
              (run_ts, as_of, "H", regime, s_sahm, s_curve, s_stress,
               sahm, t10y2y, stlfsi, nfci, stress_source,
               breakers, soft_breakers, config_hash, data_ok)
            VALUES (:run_ts, :as_of, :H, :regime, :s_sahm, :s_curve, :s_stress,
                    :sahm, :t10y2y, :stlfsi, :nfci, :stress_source,
                    :breakers, :soft_breakers, :config_hash, :data_ok)
            """),
            {
                "run_ts": run_ts,
                "as_of": macro.as_of,
                "H": macro.H,
                "regime": macro.regime,
                "s_sahm": macro.subscores.get("sahm"),
                "s_curve": macro.subscores.get("curve"),
                "s_stress": macro.subscores.get("stress"),
                "sahm": _none_if_nan(raw.get("sahm")),
                "t10y2y": _none_if_nan(raw.get("t10y2y")),
                "stlfsi": _none_if_nan(raw.get("stlfsi")),
                "nfci": _none_if_nan(raw.get("nfci")),
                "stress_source": raw.get("stress_source"),
                "breakers": json.dumps(macro.breakers_fired),
                "soft_breakers": json.dumps(macro.soft_breakers_fired),
                "config_hash": cfg.config_hash(),
                "data_ok": macro.data_ok,
            },
        )


def persist_asset_snapshot(
    tech: TechnicalState,
    decision: Decision,
    cfg: AppCfg,
    run_ts: datetime,
    data_source: str = "",
    data_fresh: bool = True,
) -> None:
    """Append one immutable per-asset snapshot row."""
    engine = _get_engine()
    with engine.begin() as conn:
        conn.execute(
            text("""
            INSERT INTO asset_snapshots
              (run_ts, as_of, symbol, price, sma200, sma_slope, z,
               trend_drift_annual, sigma_resid, atr_pct, atr_pct_baseline,
               vol_factor, adx, trend_strong_down, "M", label, rationale,
               data_source, data_fresh, data_ok, config_hash, alerted_at)
            VALUES (:run_ts, :as_of, :symbol, :price, :sma200, :sma_slope, :z,
                    :trend_drift_annual, :sigma_resid, :atr_pct, :atr_pct_baseline,
                    :vol_factor, :adx, :trend_strong_down, :M, :label, :rationale,
                    :data_source, :data_fresh, :data_ok, :config_hash, NULL)
            """),
            {
                "run_ts": run_ts,
                "as_of": tech.as_of,
                "symbol": tech.symbol,
                "price": _none_if_nan(tech.price),
                "sma200": _none_if_nan(tech.sma200),
                "sma_slope": _none_if_nan(tech.sma_slope),
                "z": _none_if_nan(tech.z),
                "trend_drift_annual": _none_if_nan(tech.trend_drift_annual),
                "sigma_resid": _none_if_nan(tech.sigma_resid),
                "atr_pct": _none_if_nan(tech.atr_pct),
                "atr_pct_baseline": _none_if_nan(tech.atr_pct_baseline),
                "vol_factor": _none_if_nan(tech.vol_factor),
                "adx": _none_if_nan(tech.adx),
                "trend_strong_down": tech.trend_strong_down,
                "M": decision.M,
                "label": decision.label,
                "rationale": json.dumps(decision.rationale, default=str),
                "data_source": data_source,
                "data_fresh": data_fresh,
                "data_ok": tech.data_ok,
                "config_hash": cfg.config_hash(),
            },
        )
    logger.info("Snapshot persisted: %s M=%.3f %s", tech.symbol, decision.M, decision.label)


def get_latest_macro() -> dict | None:
    """Most recent macro snapshot as a dict, or None."""
    try:
        engine = _get_engine()
    except RuntimeError:
        return None
    with engine.connect() as conn:
        result = conn.execute(
            text("SELECT * FROM macro_snapshots ORDER BY run_ts DESC LIMIT 1")
        )
        rows = result.mappings().all()
    if not rows:
        return None
    return dict(rows[0])


def get_latest_assets() -> dict[str, dict]:
    """Latest snapshot per symbol, keyed by symbol."""
    try:
        engine = _get_engine()
    except RuntimeError:
        return {}
    with engine.connect() as conn:
        result = conn.execute(
            text("""
            SELECT DISTINCT ON (symbol) *
            FROM asset_snapshots
            ORDER BY symbol, run_ts DESC
            """)
        )
        rows = result.mappings().all()
    return {r["symbol"]: dict(r) for r in rows}


def get_asset_history(symbol: str, limit: int = 60) -> list[dict]:
    """The *limit* most recent snapshots for one symbol, newest first."""
    try:
        engine = _get_engine()
    except RuntimeError:
        return []
    with engine.connect() as conn:
        result = conn.execute(
            text("""
            SELECT * FROM asset_snapshots
            WHERE symbol = :sym
            ORDER BY run_ts DESC
            LIMIT :lim
            """),
            {"sym": symbol, "lim": limit},
        )
        rows = result.mappings().all()
    return [dict(r) for r in rows]


def get_macro_history(limit: int = 60) -> list[dict]:
    """The *limit* most recent macro snapshots, newest first."""
    try:
        engine = _get_engine()
    except RuntimeError:
        return []
    with engine.connect() as conn:
        result = conn.execute(
            text("SELECT * FROM macro_snapshots ORDER BY run_ts DESC LIMIT :lim"),
            {"lim": limit},
        )
        rows = result.mappings().all()
    return [dict(r) for r in rows]


def mark_alerted(run_ts: datetime) -> None:
    """Set alerted_at for all asset snapshots of a run (idempotency guard)."""
    engine = _get_engine()
    with engine.begin() as conn:
        conn.execute(
            text("""
            UPDATE asset_snapshots SET alerted_at = :now
            WHERE run_ts = :run_ts AND alerted_at IS NULL
            """),
            {"now": datetime.now(tz=UTC), "run_ts": run_ts},
        )


# ---------------------------------------------------------------------------
# Watchlist
# ---------------------------------------------------------------------------

def get_watchlist(seed: list[str] | None = None) -> list[str]:
    """Tracked symbols in insertion order. Seeds from *seed* if empty."""
    try:
        engine = _get_engine()
    except RuntimeError:
        return list(seed or [])
    with engine.begin() as conn:
        rows = conn.execute(
            text("SELECT symbol FROM watchlist ORDER BY added_ts")
        ).mappings().all()
        if not rows and seed:
            now = datetime.now(tz=UTC)
            for sym in seed:
                conn.execute(
                    text("INSERT INTO watchlist (symbol, added_ts) VALUES (:sym, :now)"),
                    {"sym": sym.upper(), "now": now},
                )
            rows = conn.execute(
                text("SELECT symbol FROM watchlist ORDER BY added_ts")
            ).mappings().all()
    return [r["symbol"] for r in rows]


def watchlist_add(symbol: str) -> bool:
    """Add *symbol* to the watchlist. Returns False if already present."""
    sym = symbol.strip().upper()
    if not sym:
        return False
    engine = _get_engine()
    with engine.begin() as conn:
        existing = conn.execute(
            text("SELECT 1 FROM watchlist WHERE symbol = :sym"),
            {"sym": sym},
        ).fetchone()
        if existing:
            return False
        conn.execute(
            text("INSERT INTO watchlist (symbol, added_ts) VALUES (:sym, :now)"),
            {"sym": sym, "now": datetime.now(tz=UTC)},
        )
    return True


def watchlist_remove(symbol: str) -> None:
    """Remove *symbol* from the watchlist (snapshots are kept)."""
    engine = _get_engine()
    with engine.begin() as conn:
        conn.execute(
            text("DELETE FROM watchlist WHERE symbol = :sym"),
            {"sym": symbol.strip().upper()},
        )
