"""
Typed configuration models for the Intelligent Investor DCA Dashboard.

All thresholds, weights, and bounds live here — changing risk appetite
means editing config, not business logic. See §10.1 of the spec.

Loading order:
  1. config/default.toml  (shipped defaults)
  2. IIDCA_* environment variables (nested with __, e.g. IIDCA_MACRO__STRESS_CRISIS=2.0)
"""

from __future__ import annotations

import hashlib
import json
import os
import tomllib
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field


class MacroCfg(BaseModel):
    series: dict[str, str] = Field(
        default={
            "sahm": "SAHMREALTIME",
            "t10y2y": "T10Y2Y",
            "stlfsi": "STLFSI4",  # NOT STLFSI3 — discontinued 2022-10-28
            "nfci": "NFCI",
        }
    )
    sahm_trigger: float = 0.50
    curve_inv_floor: float = -0.5
    curve_healthy_ref: float = 1.0
    stress_calm_lo: float = -1.0
    stress_hi: float = 1.0
    stress_crisis: float = 1.5
    w_sahm: float = 0.40
    w_curve: float = 0.25
    w_stress: float = 0.35
    regime_expansion: float = 0.66
    regime_caution: float = 0.40


class TacticalCfg(BaseModel):
    z_window: int = 200
    sma_window: int = 200
    sma_slope_lookback: int = 20
    adx_period: int = 14
    rsi_period: int = 14
    atr_period: int = 14
    atr_baseline_window: int = 252
    adx_trend_thresh: float = 25.0
    g_vol_min: float = 0.40
    staleness_days: int = 5


class FusionCfg(BaseModel):
    alpha: float = 0.75
    beta: float = 1.5
    lam: float = 1.0
    g_trend_down: float = 0.30
    m_cap_exp: float = 2.00
    m_cap_caution: float = 1.25
    m_cap_stress: float = 1.00
    m_min: float = 0.25
    m_max: float = 2.00


class AppCfg(BaseModel):
    target_symbol: str = "QQQ"
    market_provider: str = "yfinance"  # yfinance | stooq | tiingo | tradingview
    macro: MacroCfg = Field(default_factory=MacroCfg)
    tactical: TacticalCfg = Field(default_factory=TacticalCfg)
    fusion: FusionCfg = Field(default_factory=FusionCfg)

    def config_hash(self) -> str:
        """SHA-256 (first 16 hex chars) of the serialised config.

        Stored with every DuckDB snapshot (§9.4) so any past recommendation
        can be fully attributed to an exact parameter set.
        """
        raw = json.dumps(self.model_dump(), sort_keys=True, default=str)
        return hashlib.sha256(raw.encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------

_DEFAULT_TOML = Path(__file__).parent.parent.parent / "config" / "default.toml"


def load_config(path: Path | None = None) -> AppCfg:
    """Load AppCfg from a TOML file with environment-variable overlay.

    Parameters
    ----------
    path:
        Path to a TOML file.  Defaults to ``config/default.toml`` relative to
        the project root.  Missing file is silently ignored (pydantic defaults
        take over).

    Environment variables
    ---------------------
    Any ``IIDCA_*`` env var overrides the corresponding config key.
    Use double-underscores for nesting:
      ``IIDCA_TARGET_SYMBOL=SPY``
      ``IIDCA_MACRO__STRESS_CRISIS=2.0``
    """
    toml_path = path or _DEFAULT_TOML
    data: dict[str, Any] = {}

    if toml_path.exists():
        with open(toml_path, "rb") as fh:
            data = tomllib.load(fh)

    # Apply IIDCA_* environment-variable overrides
    prefix = "IIDCA_"
    for key, val in os.environ.items():
        if not key.startswith(prefix):
            continue
        parts = key[len(prefix):].lower().split("__")
        target: dict[str, Any] = data
        for part in parts[:-1]:
            target = target.setdefault(part, {})
        # Attempt numeric coercion so "2.0" becomes 2.0
        try:
            target[parts[-1]] = json.loads(val)
        except (ValueError, json.JSONDecodeError):
            target[parts[-1]] = val

    return AppCfg.model_validate(data)
