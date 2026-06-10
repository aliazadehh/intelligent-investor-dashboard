"""
Typed configuration models for the Intelligent Investor DCA Dashboard.

All thresholds, weights, and bounds live here — changing risk appetite
means editing config, not business logic.

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

from pydantic import BaseModel, Field, model_validator


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

    # NFCI ramp — used only when STLFSI4 is unavailable (redundant source
    # for the financial-stress pillar; see DECISIONS.md #4).
    nfci_calm_lo: float = -0.60
    nfci_hi: float = 0.30

    w_sahm: float = 0.40
    w_curve: float = 0.25
    w_stress: float = 0.35
    regime_expansion: float = 0.66
    regime_caution: float = 0.40

    # Curve dis-inversion watch (see DECISIONS.md #3).
    # Historically the *re-steepening after* a deep inversion is the proximate
    # recession signal, not the inversion itself. If the curve was inverted
    # below `disinv_inverted_thresh` within the last `disinv_lookback_days`
    # and has now recovered above `disinv_recovered_thresh`, a *soft* breaker
    # fires that caps the regime at CAUTION (never forces STRESS).
    disinv_enabled: bool = True
    disinv_inverted_thresh: float = -0.10
    disinv_recovered_thresh: float = 0.0
    disinv_lookback_days: int = 365


class TacticalCfg(BaseModel):
    # Valuation: Z is the standardized residual from a rolling OLS trendline
    # fitted to log price over z_window days (see DECISIONS.md #1 — the old
    # rolling-mean Z is structurally biased ≈ +1.7σ on any trending asset).
    z_window: int = 200
    sma_window: int = 200
    sma_slope_lookback: int = 20
    adx_period: int = 14
    atr_period: int = 14
    atr_baseline_window: int = 252
    adx_trend_thresh: float = 25.0
    g_vol_min: float = 0.40
    staleness_days: int = 5


class LabelTiersCfg(BaseModel):
    """Upper bounds (exclusive) for each allocation label, ascending."""
    defensive_max: float = 0.60
    cautious_max: float = 0.90
    standard_max: float = 1.10
    opportunistic_max: float = 1.40

    @model_validator(mode="after")
    def _ascending(self) -> LabelTiersCfg:
        seq = [self.defensive_max, self.cautious_max, self.standard_max, self.opportunistic_max]
        if seq != sorted(seq):
            raise ValueError(f"label tiers must be ascending, got {seq}")
        return self


class FusionCfg(BaseModel):
    alpha: float = 0.75
    beta: float = 1.5
    lam: float = 1.0

    # Macro gate ramp (see DECISIONS.md #2): aggression is multiplied by
    #   g = clamp((H − gate_floor_h) / (gate_full_h − gate_floor_h), 0, 1)
    # so a healthy macro (H ≥ gate_full_h) passes aggression through fully,
    # and at/below the STRESS boundary (gate_floor_h) it is zeroed.
    # Defense is amplified by 1 + lam·(1 − g) with the same ramp.
    gate_floor_h: float = 0.40
    gate_full_h: float = 0.75

    g_trend_down: float = 0.30
    m_cap_exp: float = 2.00
    m_cap_caution: float = 1.25
    m_cap_stress: float = 1.00
    m_min: float = 0.25
    m_max: float = 2.00
    labels: LabelTiersCfg = Field(default_factory=LabelTiersCfg)

    @model_validator(mode="after")
    def _gate_valid(self) -> FusionCfg:
        if self.gate_full_h <= self.gate_floor_h:
            raise ValueError("gate_full_h must be > gate_floor_h")
        return self


class AppCfg(BaseModel):
    # Initial watchlist — seeds the persistent watchlist on first run.
    # Assets added in the dashboard are stored in DuckDB, not here.
    watchlist: list[str] = Field(default=["QQQ"])

    # Ordered fallback chain — first provider that returns valid data wins.
    provider_chain: list[str] = Field(default=["yfinance", "stooq"])

    lookback_days: int = 1200  # calendar days of price history to fetch/cache
    refresh_hours: int = 24    # auto-refresh interval for the dashboard

    macro: MacroCfg = Field(default_factory=MacroCfg)
    tactical: TacticalCfg = Field(default_factory=TacticalCfg)
    fusion: FusionCfg = Field(default_factory=FusionCfg)

    def config_hash(self) -> str:
        """SHA-256 (first 16 hex chars) of the serialised config.

        Stored with every DuckDB snapshot so any past recommendation
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
      ``IIDCA_LOOKBACK_DAYS=800``
      ``IIDCA_MACRO__STRESS_CRISIS=2.0``
    """
    toml_path = path or _DEFAULT_TOML
    data: dict[str, Any] = {}

    if toml_path.exists():
        with open(toml_path, "rb") as fh:
            data = tomllib.load(fh)

    # Backward compatibility with the v1 config shape.
    if "target_symbol" in data and "watchlist" not in data:
        data["watchlist"] = [data.pop("target_symbol")]
    if "market_provider" in data and "provider_chain" not in data:
        primary = data.pop("market_provider")
        chain = [primary] + [p for p in ("yfinance", "stooq") if p != primary]
        data["provider_chain"] = chain

    # Apply IIDCA_* environment-variable overrides
    prefix = "IIDCA_"
    for key, val in os.environ.items():
        if not key.startswith(prefix):
            continue
        parts = key[len(prefix):].lower().split("__")
        target: dict[str, Any] = data
        for part in parts[:-1]:
            target = target.setdefault(part, {})
        # Attempt JSON coercion so "2.0" becomes 2.0 and '["QQQ","SPY"]' a list
        try:
            target[parts[-1]] = json.loads(val)
        except (ValueError, json.JSONDecodeError):
            target[parts[-1]] = val

    return AppCfg.model_validate(data)
