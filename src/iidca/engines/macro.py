"""
Macro Engine — Zone 2, Column A (§5 of the spec).

Pure function: score_macro(raw, cfg) -> MacroState
No I/O, no side effects — fully testable with fixture data.

Design (two-layer):
  (a) Continuous health score H ∈ [0,1] — smooth gradient for sizing
  (b) Hard circuit breakers — binary override for recession/crisis events
      Breakers can only *worsen* the regime, never improve it.
"""

from __future__ import annotations

from datetime import date
from typing import Optional

from iidca.config import MacroCfg
from iidca.models import MacroState


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _clamp(x: float, lo: float, hi: float) -> float:
    """Clamp x to [lo, hi]."""
    return max(lo, min(hi, x))


def _sub_score_sahm(sahm: float, cfg: MacroCfg) -> float:
    """Map Sahm Rule reading to [0, 1].

    Formula (§5.1):
        s_sahm = clamp((sahm_trigger - sahm) / sahm_trigger, 0, 1)

    Boundaries:
        sahm = 0.0  → s_sahm = 1.0  (no recession signal, healthy)
        sahm ≥ 0.50 → s_sahm = 0.0  (trigger breached, recession signal)
    """
    if cfg.sahm_trigger == 0:
        return 0.0
    return _clamp((cfg.sahm_trigger - sahm) / cfg.sahm_trigger, 0.0, 1.0)


def _sub_score_curve(spread: float, cfg: MacroCfg) -> float:
    """Map 10Y-2Y yield spread to [0, 1].

    Formula (§5.1):
        s_curve = clamp((spread - inv_floor) / (healthy_ref - inv_floor), 0, 1)

    Boundaries:
        spread ≥ +1.0 → s_curve = 1.0  (healthy upward slope)
        spread ≤ -0.5 → s_curve = 0.0  (deeply inverted)

    # TODO(open-question-3): v1 scores the *level* only. Historically the
    # re-steepening after a deep inversion precedes recession onset better
    # than the inversion itself. A later version could add a state-machine
    # trigger: was-inverted → now-steepening ⇒ CAUTION breaker.
    """
    denom = cfg.curve_healthy_ref - cfg.curve_inv_floor
    if denom == 0:
        return 0.0
    return _clamp((spread - cfg.curve_inv_floor) / denom, 0.0, 1.0)


def _sub_score_stress(stlfsi: float, cfg: MacroCfg) -> float:
    """Map STLFSI4 financial-stress index to [0, 1].

    Formula (§5.1):
        s_stress = clamp((stress_hi - stlfsi) / (stress_hi - calm_lo), 0, 1)

    Boundaries:
        stlfsi ≤ -1.0 → s_stress = 1.0  (below-average stress, calm)
        stlfsi =  0.0 → s_stress = 0.5  (average)
        stlfsi ≥ +1.0 → s_stress = 0.0  (above-average stress)

    Uses STLFSI4, NOT the discontinued STLFSI3 (frozen at 2022-10-28).
    """
    denom = cfg.stress_hi - cfg.stress_calm_lo
    if denom == 0:
        return 0.0
    return _clamp((cfg.stress_hi - stlfsi) / denom, 0.0, 1.0)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def score_macro(raw: dict, cfg: MacroCfg) -> MacroState:
    """Score the macro regime from raw FRED readings.

    Parameters
    ----------
    raw:
        Dict with keys:
          "sahm"   — Sahm Rule real-time indicator (SAHMREALTIME), percentage points
          "t10y2y" — 10Y-2Y Treasury spread (T10Y2Y), percentage points
          "stlfsi" — St. Louis Fed Financial Stress Index v4 (STLFSI4), z-score
          "nfci"   — Chicago Fed National Financial Conditions Index (optional)
          "as_of"  — datetime.date of the latest observation (optional)
        Missing required keys → data_ok=False, fail-safe MacroState returned.

    cfg:
        MacroCfg instance with all thresholds and weights.

    Returns
    -------
    MacroState with H, regime, subscores, raw, breakers_fired, as_of, data_ok.

    Design guarantee (§1.3 #4):
        Circuit breakers can only *worsen* the regime (push it down the ladder),
        never improve it. A cheap Z-score can never rescue a STRESS regime.
    """
    # ------------------------------------------------------------------
    # 0. Extract required readings; fail-safe on missing/bad data
    # ------------------------------------------------------------------
    required = ("sahm", "t10y2y", "stlfsi")
    missing = [k for k in required if k not in raw or raw[k] is None]
    if missing:
        return MacroState(
            H=0.5,
            regime="STRESS",
            subscores={"sahm": 0.0, "curve": 0.0, "stress": 0.0},
            raw=raw,
            breakers_fired=[f"DATA_MISSING:{','.join(missing)}"],
            as_of=raw.get("as_of"),
            data_ok=False,
        )

    try:
        sahm   = float(raw["sahm"])
        spread = float(raw["t10y2y"])
        stlfsi = float(raw["stlfsi"])
    except (TypeError, ValueError) as exc:
        return MacroState(
            H=0.5,
            regime="STRESS",
            subscores={"sahm": 0.0, "curve": 0.0, "stress": 0.0},
            raw=raw,
            breakers_fired=[f"DATA_INVALID:{exc}"],
            as_of=raw.get("as_of"),
            data_ok=False,
        )

    as_of: Optional[date] = raw.get("as_of")

    # ------------------------------------------------------------------
    # 1. Per-indicator sub-scores  s ∈ [0, 1]  (1 = healthy)  §5.1
    # ------------------------------------------------------------------
    s_sahm   = _sub_score_sahm(sahm, cfg)
    s_curve  = _sub_score_curve(spread, cfg)
    s_stress = _sub_score_stress(stlfsi, cfg)

    # ------------------------------------------------------------------
    # 2. Macro Health Score  H = Σ wᵢ·sᵢ   §5.2
    #    Weights default: w_sahm=0.40, w_curve=0.25, w_stress=0.35 (Σ=1)
    # ------------------------------------------------------------------
    H = (
        cfg.w_sahm   * s_sahm
        + cfg.w_curve  * s_curve
        + cfg.w_stress * s_stress
    )
    # H is guaranteed ∈ [0,1] because each sᵢ ∈ [0,1] and weights sum to 1.

    # ------------------------------------------------------------------
    # 3. Circuit breakers  §5.3
    #    Fire on hard recession / acute liquidity-crisis signals.
    #    Each breaker is append-only; they can only worsen the regime.
    # ------------------------------------------------------------------
    breakers: list[str] = []

    if sahm >= cfg.sahm_trigger:
        # Sahm Rule threshold crossed — realized recession signal
        breakers.append("SAHM_RECESSION")

    if stlfsi >= cfg.stress_crisis:
        # STLFSI4 ≥ 1.5 — acute financial stress / liquidity crisis
        breakers.append("FIN_STRESS_CRISIS")

    # TODO(open-question-3): curve un-inversion breaker (was-inverted →
    # now-steepening) — deferred to post-v1. Would append "CURVE_REINVERSION"
    # and force at minimum CAUTION.

    # ------------------------------------------------------------------
    # 4. Regime assignment  §5.4
    #    Breakers take precedence and may only push regime *down* the ladder.
    # ------------------------------------------------------------------
    if breakers:
        # Any breaker fires → force STRESS regardless of H
        regime = "STRESS"
    elif H >= cfg.regime_expansion:
        regime = "EXPANSION"
    elif H >= cfg.regime_caution:
        regime = "CAUTION"
    else:
        regime = "STRESS"

    # ------------------------------------------------------------------
    # 5. Return fully-populated MacroState
    # ------------------------------------------------------------------
    return MacroState(
        H=H,
        regime=regime,
        subscores={
            "sahm":   s_sahm,
            "curve":  s_curve,
            "stress": s_stress,
        },
        raw=raw,
        breakers_fired=breakers,
        as_of=as_of,
        data_ok=True,
    )
