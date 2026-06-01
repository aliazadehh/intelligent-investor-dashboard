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
    """Output of the Macro Engine (§5).

    H: continuous health score ∈ [0, 1] (1 = fully healthy)
    regime: discrete regime label — EXPANSION | CAUTION | STRESS
    subscores: per-indicator scores that were blended into H
    raw: raw indicator readings as ingested
    breakers_fired: list of circuit-breaker names that fired (can only worsen regime)
    as_of: date of the latest data observation
    data_ok: False signals a data failure → fusion falls back to M=1.0
    """

    H: float
    regime: str  # "EXPANSION" | "CAUTION" | "STRESS"
    subscores: dict
    raw: dict
    breakers_fired: list[str] = field(default_factory=list)
    as_of: date | None = None
    data_ok: bool = True


@dataclass
class TechnicalState:
    """Output of the Tactical Engine (§6).

    symbol: ticker / asset identifier
    price: latest adjusted close
    sma200: 200-day simple moving average of adjusted close
    sma_slope: SMA200[t] − SMA200[t−20]; sign indicates trend direction
    z: rolling log-price Z-score over the z_window; negative = below mean = cheap
    atr_pct: ATR(14) / Close — volatility as a fraction of price
    vol_factor: clamp(atr_baseline / atr_pct, g_vol_min, 1.0); 1 = normal vol
    adx: ADX(14) trend-strength indicator
    rsi: RSI(14) momentum oscillator
    trend_strong_down: True when Close < SMA200 AND slope < 0 AND ADX > thresh
    as_of: date of latest bar
    data_ok: False signals a data failure → fusion falls back to M=1.0
    """

    symbol: str
    price: float
    sma200: float
    sma_slope: float
    z: float
    atr_pct: float
    vol_factor: float
    adx: float
    rsi: float
    trend_strong_down: bool
    as_of: date | None = None
    data_ok: bool = True


@dataclass
class Decision:
    """Final output of the Fusion Engine (§7, §8).

    M: DCA multiplier (1.0 = standard, <1 = defensive, >1 = aggressive)
    label: human-readable tier (Defensive / Cautious / Standard / Opportunistic / Aggressive)
    instruction: one-sentence plain-language action
    status: Zone 1 regime string (e.g. "Expansion & Stable Liquidity")
    color: display color ("green" | "amber" | "red")
    rationale: every intermediate value for audit/reproducibility
    """

    M: float
    label: str
    instruction: str
    status: str
    color: str
    rationale: dict
