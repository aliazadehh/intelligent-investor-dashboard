"""Typed state dataclasses shared across all engines.

These are the canonical contracts between the data layer, macro engine,
tactical engine, and fusion engine. Every intermediate result is a typed
dataclass so each stage can be tested in isolation with fixture data.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date


@dataclass
class MacroState:
    """Output of the Macro Engine.

    H: continuous health score ∈ [0, 1] (1 = fully healthy)
    regime: discrete regime label — EXPANSION | CAUTION | STRESS
    subscores: per-indicator scores that were blended into H
    raw: raw indicator readings as ingested (plus annotations such as
         "stress_source": which series fed the stress pillar)
    breakers_fired: hard circuit-breaker names that fired (force STRESS)
    soft_breakers_fired: soft breaker names (cap regime at CAUTION,
         e.g. CURVE_DISINVERSION)
    as_of: date of the latest data observation
    data_ok: False signals a data failure → fusion falls back to M=1.0
    """

    H: float
    regime: str  # "EXPANSION" | "CAUTION" | "STRESS"
    subscores: dict
    raw: dict
    breakers_fired: list[str] = field(default_factory=list)
    soft_breakers_fired: list[str] = field(default_factory=list)
    as_of: date | None = None
    data_ok: bool = True


@dataclass
class TechnicalState:
    """Output of the Tactical Engine.

    symbol: ticker / asset identifier
    price: latest adjusted close
    sma200: long SMA of adjusted close (window = cfg.sma_window)
    sma_slope: SMA[t] − SMA[t−lookback]; sign indicates trend direction
    z: trend-residual Z-score — standardized deviation of log price from
       its own rolling OLS trendline over z_window days.
       Negative = below trend = cheap relative to the asset's recent path.
       (Mean-zero by construction even on strongly trending assets.)
    trend_drift_annual: annualized log-price drift implied by the fitted
       trendline (e.g. 0.25 ≈ trend rising ~25%/yr) — for display.
    sigma_resid: std-dev of the trendline residuals (log units) — the
       channel half-width; z = residual / sigma_resid.
    atr_pct: ATR / Close — volatility as a fraction of price
    atr_pct_baseline: long rolling median of ATR% (the asset's "normal" vol)
    vol_factor: clamp(atr_baseline / atr_pct, g_vol_min, 1.0); 1 = normal vol
    adx: ADX trend-strength indicator
    trend_strong_down: True when Close < SMA AND slope < 0 AND ADX > thresh
    as_of: date of latest bar
    data_ok: False signals a data failure → fusion falls back to M=1.0
    """

    symbol: str
    price: float
    sma200: float
    sma_slope: float
    z: float
    trend_drift_annual: float
    sigma_resid: float
    atr_pct: float
    atr_pct_baseline: float
    vol_factor: float
    adx: float
    trend_strong_down: bool
    as_of: date | None = None
    data_ok: bool = True


@dataclass
class Decision:
    """Final output of the Fusion Engine.

    M: DCA multiplier (1.0 = standard, <1 = defensive, >1 = aggressive)
    label: human-readable tier (Defensive / Cautious / Standard / Opportunistic / Aggressive)
    instruction: one-sentence plain-language action, derived from M itself
    status: Zone 1 regime string (e.g. "Expansion & Stable Liquidity")
    color: display color ("green" | "amber" | "red")
    rationale: every intermediate value for audit/reproducibility; includes
       the ordered waterfall steps from baseline 1.0 to final M.
    """

    M: float
    label: str
    instruction: str
    status: str
    color: str
    rationale: dict
