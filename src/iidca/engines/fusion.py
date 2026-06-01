"""Fusion Engine — Zone 3 decision math + Zone 1 synthesis (§7, §8, §10.6).

This is the heart of the system. It is a *pure function*: same inputs
always produce the same output. No I/O, no randomness, no side-effects.
Every intermediate value is captured in `rationale` for full auditability.

Key design principle (§1.3 #4 — Asymmetric caution):
    Defensiveness is always permitted.
    Aggression must be *earned* by a healthy macro regime AND a supportive
    trend AND a normal-volatility environment.

    A cheap price (Z ≪ 0) only translates to "buy more" to the extent that
    the regime is healthy enough for cheapness to be *noise around a stable
    mean* rather than *information about a collapsing one*.

Four-step algorithm (§7):
    1. T(Z) = 1 + α·(−tanh(Z/β))         technical tilt, bounded & saturating
    2. d_eff via asymmetric macro gate     aggression gated by H, defence amplified
    3. Tactical guards on aggressive share only (never damp defensive)
    4. Regime hard cap + global clamp      ultimate guard

Fail-safe (§1.3 #3):
    If either macro.data_ok or tech.data_ok is False → return M=1.0 immediately,
    flagged. Never silently emit an aggressive or zero allocation on bad data.
"""
from __future__ import annotations

import math

from iidca.config import FusionCfg
from iidca.models import Decision, MacroState, TechnicalState


def _clamp(x: float, lo: float, hi: float) -> float:
    """Clamp *x* to the closed interval [lo, hi]."""
    return max(lo, min(hi, x))


# ---------------------------------------------------------------------------
# Zone 3 — Fusion
# ---------------------------------------------------------------------------


def decide(macro: MacroState, tech: TechnicalState, cfg: FusionCfg) -> Decision:
    """Fuse MacroState + TechnicalState into a single DCA Decision.

    Parameters
    ----------
    macro:
        Output of the Macro Engine — includes H, regime, breakers, raw indicators.
    tech:
        Output of the Tactical Engine — includes Z, vol_factor, trend_strong_down.
    cfg:
        FusionCfg instance with all tunable parameters (all have safe defaults).

    Returns
    -------
    Decision
        M (the multiplier), label, instruction, Zone 1 status/color, and a
        rationale dict containing every intermediate value for audit.

    Fail-safe
    ---------
    If ``macro.data_ok is False`` or ``tech.data_ok is False`` the function
    returns ``M = 1.0`` (Standard) immediately, flagged in the rationale.
    It never silently emits an aggressive or zero allocation on bad data.
    """
    # ------------------------------------------------------------------
    # Fail-safe: bad data → Standard DCA, flagged.
    # ------------------------------------------------------------------
    if not (macro.data_ok and tech.data_ok):
        return Decision(
            M=1.0,
            label="Standard (fail-safe)",
            instruction="Data incomplete — defaulting to standard DCA.",
            status="UNKNOWN — data error",
            color="amber",
            rationale={"reason": "data_ok=False"},
        )

    H: float = macro.H
    Z: float = tech.z

    # ------------------------------------------------------------------
    # Step 1 — Technical tilt T(Z): smooth, bounded, saturating.
    #
    #   T(Z) = 1 + α · (−tanh(Z / β))
    #
    # Z = 0 → T = 1 (at mean → standard)
    # Z ≪ 0 → T → 1 + α  (cheap → want more)
    # Z ≫ 0 → T → 1 − α  (expensive → want less)
    #
    # tanh saturates: a flash-crash Z=−4 doesn't blow the allocation to
    # absurd size; marginal aggression per extra σ of cheapness shrinks.
    # ------------------------------------------------------------------
    T: float = 1.0 + cfg.alpha * (-math.tanh(Z / cfg.beta))
    d: float = T - 1.0  # deviation from baseline; >0 = aggressive intent

    # ------------------------------------------------------------------
    # Step 2 — Asymmetric macro gate.
    #
    #   d ≥ 0 (cheap):     d_eff = d · H
    #       → aggression is *earned* by macro health; H=0 wipes it out.
    #   d < 0 (expensive): d_eff = d · (1 + λ · (1 − H))
    #       → defensiveness always applies, amplified when macro is weak.
    #         λ=1 (default) → factor ∈ [1, 2]
    #
    #   m_core = 1 + d_eff
    # ------------------------------------------------------------------
    if d >= 0.0:
        d_eff: float = d * H
    else:
        d_eff = d * (1.0 + cfg.lam * (1.0 - H))
    m_core: float = 1.0 + d_eff

    # ------------------------------------------------------------------
    # Step 3 — Tactical guards on the aggressive portion only.
    #
    # Split m_core into its aggressive surplus and its defensive discount.
    # Guards (trend, vol) multiply only the aggressive surplus — being
    # defensive in a strong downtrend or high-vol environment is *correct*
    # behaviour, so we never damp the defensive component.
    #
    #   agg  = max(m_core − 1, 0)
    #   def_ = max(1 − m_core, 0)
    #
    #   g_trend: 0.30 when trend_strong_down=True, else 1.0
    #   g_vol  : vol_factor ∈ [g_vol_min, 1.0] from TechnicalState
    #
    #   agg_guarded = agg · g_trend · g_vol
    #   m_pre       = 1 + agg_guarded − def_
    # ------------------------------------------------------------------
    agg: float = max(m_core - 1.0, 0.0)
    def_: float = max(1.0 - m_core, 0.0)

    g_trend: float = cfg.g_trend_down if tech.trend_strong_down else 1.0
    g_vol: float = tech.vol_factor

    agg_guarded: float = agg * g_trend * g_vol
    m_pre: float = 1.0 + agg_guarded - def_

    # ------------------------------------------------------------------
    # Step 4 — Regime hard cap + global clamp.
    #
    # regime_cap maps the discrete regime to a ceiling on M:
    #   EXPANSION → 2.00  (full range permitted)
    #   CAUTION   → 1.25  (modest aggression only)
    #   STRESS    → 1.00  (never lever into a collapsing tape)
    #
    # Two-pass clamp: first apply regime cap (ceil), then apply global
    # [m_min, m_max] so the regime cap can never push M below m_min.
    # ------------------------------------------------------------------
    cap: float = {
        "EXPANSION": cfg.m_cap_exp,
        "CAUTION": cfg.m_cap_caution,
        "STRESS": cfg.m_cap_stress,
    }[macro.regime]

    # Apply regime cap (upper bound), then enforce global floor and ceiling.
    M: float = _clamp(_clamp(m_pre, cfg.m_min, cap), cfg.m_min, cfg.m_max)

    label, instruction = _label(M)
    status, color = _zone1(macro)

    return Decision(
        M=round(M, 4),
        label=label,
        instruction=instruction,
        status=status,
        color=color,
        rationale={
            # Inputs
            "H": H,
            "Z": Z,
            "regime": macro.regime,
            "breakers": macro.breakers_fired,
            # Step 1
            "T": T,
            "d": d,
            # Step 2
            "d_eff": d_eff,
            "m_core": m_core,
            # Step 3
            "agg": agg,
            "def_": def_,
            "g_trend": g_trend,
            "g_vol": g_vol,
            "agg_guarded": agg_guarded,
            "m_pre": m_pre,
            # Step 4
            "cap": cap,
            "M_final": M,
        },
    )


# ---------------------------------------------------------------------------
# Label / instruction mapping (§7.1)
# ---------------------------------------------------------------------------

# Tier boundaries — all are inclusive on the left, exclusive on the right
# (except the outer bounds which are clamped by m_min/m_max).
_LABEL_TIERS: list[tuple[float, str, str]] = [
    # (upper_bound_exclusive, label, instruction)
    (0.60, "Defensive",    "Reduce this period's DCA (~50%). Expensive and/or deteriorating regime."),
    (0.90, "Cautious",     "Slightly below standard (~75%)."),
    (1.10, "Standard",     "Standard DCA (100%). No strong signal."),
    (1.40, "Opportunistic","Add modestly (~125%). Cheap and regime-supportive."),
]
_LABEL_AGGRESSIVE = ("Aggressive", "Deploy extra (regime-capped). Deep value in a healthy regime.")


def _label(M: float) -> tuple[str, str]:
    """Map multiplier *M* to a (label, instruction) pair (§7.1)."""
    for upper, lbl, instr in _LABEL_TIERS:
        if M < upper:
            return lbl, instr
    return _LABEL_AGGRESSIVE


# ---------------------------------------------------------------------------
# Zone 1 synthesis (§8)
# ---------------------------------------------------------------------------


def _zone1(m: MacroState) -> tuple[str, str]:
    """Synthesise the Zone 1 status string + display color from MacroState (§8).

    Format:
        "{regime_label} & {liquidity_label}"
        + optional "  ⚠ {breaker1}, {breaker2}" suffix

    Color: green (EXPANSION) | amber (CAUTION) | red (STRESS)
    """
    regime_labels: dict[str, str] = {
        "EXPANSION": "Expansion",
        "CAUTION": "Late-Cycle Caution",
        "STRESS": "Contraction / Systemic Stress",
    }
    regime_label: str = regime_labels[m.regime]

    stlfsi: float = float(m.raw.get("stlfsi", 0.0))
    stress_crisis: float = float(m.raw.get("_stress_crisis", 1.5))  # injected by macro engine if present
    if stlfsi < 0.0:
        liquidity = "Stable Liquidity"
    elif stlfsi < stress_crisis:
        liquidity = "Tightening Liquidity"
    else:
        liquidity = "Stressed Liquidity"

    status: str = f"{regime_label} & {liquidity}"
    if m.breakers_fired:
        status += "  ⚠ " + ", ".join(m.breakers_fired)

    color_map: dict[str, str] = {
        "EXPANSION": "green",
        "CAUTION": "amber",
        "STRESS": "red",
    }
    return status, color_map[m.regime]
