"""Fusion Engine — decision math + global status synthesis.

This is the heart of the system. It is a *pure function*: same inputs
always produce the same output. No I/O, no randomness, no side-effects.
Every intermediate value is captured in `rationale` for full auditability,
including an ordered waterfall from baseline 1.0 to the final M.

Key design principle (asymmetric caution):
    Defensiveness is always permitted.
    Aggression must be *earned* by a healthy macro regime AND a supportive
    trend AND a normal-volatility environment.

    A cheap price (Z ≪ 0) only translates to "buy more" to the extent that
    the regime is healthy enough for cheapness to be *noise around a stable
    trend* rather than *information about a collapsing one*.

Four-step algorithm:
    1. T(Z) = 1 + α·(−tanh(Z/β))           technical tilt, bounded & saturating
    2. Macro gate ramp g(H); aggression × g, defence × (1 + λ·(1 − g))
    3. Tactical guards on aggressive share only (never damp defensive)
    4. Regime hard cap + global clamp

Fail-safe:
    If either macro.data_ok or tech.data_ok is False → return M=1.0
    immediately, flagged. Never silently emit an aggressive or zero
    allocation on bad data.
"""
from __future__ import annotations

import math

from iidca.config import FusionCfg
from iidca.models import Decision, MacroState, TechnicalState


def _clamp(x: float, lo: float, hi: float) -> float:
    """Clamp *x* to the closed interval [lo, hi]."""
    return max(lo, min(hi, x))


def macro_gate(H: float, cfg: FusionCfg) -> float:
    """Macro gate ramp g ∈ [0, 1] (see DECISIONS.md #2).

        g = clamp((H − gate_floor_h) / (gate_full_h − gate_floor_h), 0, 1)

    H ≥ gate_full_h  → 1.0 — a healthy macro passes aggression through fully
    H ≤ gate_floor_h → 0.0 — at/below the STRESS boundary aggression is zeroed

    The v1 design multiplied aggression by raw H, which silently taxed
    aggression ~20–25% even in the healthiest observable macro (H rarely
    exceeds ~0.85). The ramp makes "healthy enough" an explicit, configurable
    judgment instead of an accident of the H scale.
    """
    return _clamp((H - cfg.gate_floor_h) / (cfg.gate_full_h - cfg.gate_floor_h), 0.0, 1.0)


def decide(macro: MacroState, tech: TechnicalState, cfg: FusionCfg) -> Decision:
    """Fuse MacroState + TechnicalState into a single DCA Decision.

    Returns a Decision with M (the multiplier), label, instruction, global
    status/color, and a rationale dict containing every intermediate value
    plus ordered waterfall steps for visualization.
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
            rationale={"reason": "data_ok=False", "waterfall": []},
        )

    H: float = macro.H
    Z: float = tech.z

    # ------------------------------------------------------------------
    # Step 1 — Technical tilt T(Z): smooth, bounded, saturating.
    #
    #   T(Z) = 1 + α · (−tanh(Z / β))
    #
    # Z = 0 → T = 1 (on trend → standard)
    # Z ≪ 0 → T → 1 + α  (below trend → want more)
    # Z ≫ 0 → T → 1 − α  (above trend → want less)
    #
    # tanh saturates: a flash-crash Z=−4 doesn't blow the allocation to
    # absurd size; marginal aggression per extra σ of cheapness shrinks.
    # ------------------------------------------------------------------
    T: float = 1.0 + cfg.alpha * (-math.tanh(Z / cfg.beta))
    d: float = T - 1.0  # deviation from baseline; >0 = aggressive intent

    # ------------------------------------------------------------------
    # Step 2 — Asymmetric macro gate (ramp form).
    #
    #   g = macro_gate(H)                ∈ [0, 1]
    #   d ≥ 0 (cheap):     d_eff = d · g
    #       → aggression is *earned* by macro health; g=0 wipes it out.
    #   d < 0 (expensive): d_eff = d · (1 + λ · (1 − g))
    #       → defensiveness always applies, amplified when macro is weak.
    #
    #   m_core = 1 + d_eff
    # ------------------------------------------------------------------
    g: float = macro_gate(H, cfg)
    if d >= 0.0:
        d_eff: float = d * g
    else:
        d_eff = d * (1.0 + cfg.lam * (1.0 - g))
    m_core: float = 1.0 + d_eff

    # ------------------------------------------------------------------
    # Step 3 — Tactical guards on the aggressive portion only.
    #
    # Being defensive in a strong downtrend or high-vol environment is
    # *correct* behaviour, so guards never damp the defensive component.
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
    #   EXPANSION → 2.00  (full range permitted)
    #   CAUTION   → 1.25  (modest aggression only)
    #   STRESS    → 1.00  (never lever into a collapsing tape)
    #
    # Two-pass clamp: regime cap (ceiling) first, then global [m_min, m_max]
    # so the regime cap can never push M below m_min.
    # ------------------------------------------------------------------
    cap: float = {
        "EXPANSION": cfg.m_cap_exp,
        "CAUTION": cfg.m_cap_caution,
        "STRESS": cfg.m_cap_stress,
    }[macro.regime]

    M: float = _clamp(_clamp(m_pre, cfg.m_min, cap), cfg.m_min, cfg.m_max)

    label = _label(M, cfg)
    instruction = _instruction(M)
    status, color = _zone1(macro)

    # Ordered waterfall: baseline → tilt → macro gate → guards → caps.
    # Each step is (name, delta); running totals reconstruct every stage.
    waterfall = [
        ("Baseline", 1.0),
        ("Valuation tilt", d),
        ("Macro gate", d_eff - d),
        ("Trend guard", agg * (g_trend - 1.0)),
        ("Volatility guard", agg * g_trend * (g_vol - 1.0)),
        ("Regime cap & floor", M - m_pre),
    ]

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
            "soft_breakers": macro.soft_breakers_fired,
            # Step 1
            "T": T,
            "d": d,
            # Step 2
            "g_macro": g,
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
            # Visualization
            "waterfall": waterfall,
        },
    )


# ---------------------------------------------------------------------------
# Label / instruction mapping — tiers are config-driven
# ---------------------------------------------------------------------------


def _label(M: float, cfg: FusionCfg) -> str:
    """Map multiplier *M* to its allocation tier label."""
    t = cfg.labels
    if M < t.defensive_max:
        return "Defensive"
    if M < t.cautious_max:
        return "Cautious"
    if M < t.standard_max:
        return "Standard"
    if M < t.opportunistic_max:
        return "Opportunistic"
    return "Aggressive"


def _instruction(M: float) -> str:
    """One-sentence action derived from M itself, so the text can never
    contradict the number (v1 hard-coded '~50%' next to M=0.25)."""
    pct = round(M * 100)
    if pct == 100:
        return "Invest your normal amount this period."
    if pct < 100:
        return f"Invest about {pct}% of your normal amount this period."
    return f"Invest about {pct}% of your normal amount this period — {pct - 100}% extra."


# ---------------------------------------------------------------------------
# Global status synthesis (Zone 1)
# ---------------------------------------------------------------------------


def _zone1(m: MacroState) -> tuple[str, str]:
    """Synthesise the global status string + display color from MacroState.

    Liquidity wording derives from the stress *sub-score* (and crisis
    breaker), so fusion needs no copy of the macro thresholds — the two
    layers can't drift apart.
    """
    regime_labels: dict[str, str] = {
        "EXPANSION": "Expansion",
        "CAUTION": "Late-Cycle Caution",
        "STRESS": "Contraction / Systemic Stress",
    }
    regime_label: str = regime_labels[m.regime]

    s_stress = float(m.subscores.get("stress", 0.5))
    if "FIN_STRESS_CRISIS" in m.breakers_fired:
        liquidity = "Stressed Liquidity"
    elif s_stress >= 0.5:
        liquidity = "Stable Liquidity"
    else:
        liquidity = "Tightening Liquidity"

    status: str = f"{regime_label} & {liquidity}"
    fired = list(m.breakers_fired) + list(m.soft_breakers_fired)
    if fired:
        status += "  ⚠ " + ", ".join(fired)

    color_map: dict[str, str] = {
        "EXPANSION": "green",
        "CAUTION": "amber",
        "STRESS": "red",
    }
    return status, color_map[m.regime]
