"""
Macro Engine — global regime scoring (shared across all assets).

Pure function: score_macro(raw, cfg) -> MacroState
No I/O, no side effects — fully testable with fixture data.

Design (three layers):
  (a) Continuous health score H ∈ [0,1] — smooth gradient for sizing
  (b) Hard circuit breakers — binary override that forces STRESS
      (realized recession / acute liquidity crisis)
  (c) Soft breakers — cap the regime at CAUTION without forcing STRESS
      (leading warnings, e.g. curve dis-inversion after a deep inversion)
  Breakers can only *worsen* the regime, never improve it.

Signal roles (leading vs coincident — see DECISIONS.md #3):
  Sahm (SAHMREALTIME)  — coincident/realized: recession has effectively begun
  STLFSI4 (or NFCI)    — coincident: financial-system stress right now
  T10Y2Y level         — leading, noisy: priced-in expectations
  T10Y2Y dis-inversion — leading, sharper: re-steepening after deep inversion
                         historically precedes recession onset; handled as a
                         soft breaker, not blended into H.
"""

from __future__ import annotations

from datetime import date, timedelta

import pandas as pd

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

        s_sahm = clamp((sahm_trigger - sahm) / sahm_trigger, 0, 1)

    Boundaries:
        sahm = 0.0  → 1.0  (no unemployment rise, healthy)
        sahm ≥ 0.50 → 0.0  (trigger breached, recession signal)
    """
    if cfg.sahm_trigger == 0:
        return 0.0
    return _clamp((cfg.sahm_trigger - sahm) / cfg.sahm_trigger, 0.0, 1.0)


def _sub_score_curve(spread: float, cfg: MacroCfg) -> float:
    """Map 10Y-2Y yield spread to [0, 1].

        s_curve = clamp((spread - inv_floor) / (healthy_ref - inv_floor), 0, 1)

    Boundaries:
        spread ≥ +1.0 → 1.0  (healthy upward slope)
        spread ≤ -0.5 → 0.0  (deeply inverted)

    The *level* is a noisy leading signal; the sharper dis-inversion timing
    signal is handled separately as a soft breaker (see _disinversion_active).
    """
    denom = cfg.curve_healthy_ref - cfg.curve_inv_floor
    if denom == 0:
        return 0.0
    return _clamp((spread - cfg.curve_inv_floor) / denom, 0.0, 1.0)


def _sub_score_stress(stlfsi: float, cfg: MacroCfg) -> float:
    """Map STLFSI4 financial-stress index to [0, 1].

        s_stress = clamp((stress_hi - stlfsi) / (stress_hi - calm_lo), 0, 1)

    Boundaries:
        stlfsi ≤ -1.0 → 1.0  (unusually calm)
        stlfsi =  0.0 → 0.5  (average)
        stlfsi ≥ +1.0 → 0.0  (high stress)

    Uses STLFSI4, NOT the discontinued STLFSI3 (frozen at 2022-10-28).
    """
    denom = cfg.stress_hi - cfg.stress_calm_lo
    if denom == 0:
        return 0.0
    return _clamp((cfg.stress_hi - stlfsi) / denom, 0.0, 1.0)


def _sub_score_nfci(nfci: float, cfg: MacroCfg) -> float:
    """Map NFCI to [0, 1] — fallback for the stress pillar when STLFSI4
    is unavailable. NFCI runs on a different scale (calm ≈ −0.6, tight > 0),
    hence its own ramp boundaries.
    """
    denom = cfg.nfci_hi - cfg.nfci_calm_lo
    if denom == 0:
        return 0.0
    return _clamp((cfg.nfci_hi - nfci) / denom, 0.0, 1.0)


def _disinversion_active(
    history: pd.Series, as_of: date, cfg: MacroCfg
) -> bool:
    """True when the curve was deeply inverted within the lookback window
    and has now re-steepened above the recovery threshold.

    The signal self-expires: once the last inverted observation falls out of
    the lookback window, the breaker stops firing. Stateless — derived
    entirely from the series each run.
    """
    if history is None or len(history) == 0:
        return False

    current = float(history.iloc[-1])
    if current <= cfg.disinv_recovered_thresh:
        return False  # still inverted/flat — level score already handles it

    cutoff = pd.Timestamp(as_of - timedelta(days=cfg.disinv_lookback_days))
    window = history[history.index >= cutoff]
    if window.empty:
        return False
    return bool(window.min() <= cfg.disinv_inverted_thresh)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def score_macro(raw: dict, cfg: MacroCfg) -> MacroState:
    """Score the macro regime from raw FRED readings.

    Parameters
    ----------
    raw:
        Dict with keys:
          "sahm"           — Sahm Rule real-time indicator, percentage points
          "t10y2y"         — 10Y-2Y Treasury spread, percentage points
          "stlfsi"         — STLFSI4 reading (optional if "nfci" present)
          "nfci"           — Chicago Fed NFCI (optional; stress fallback)
          "t10y2y_history" — pd.Series of the spread (optional; enables the
                             dis-inversion soft breaker)
          "as_of"          — datetime.date of the latest observation (optional)
        Missing required pillars → data_ok=False, fail-safe MacroState.

    Returns
    -------
    MacroState with H, regime, subscores, raw, breakers, as_of, data_ok.

    Design guarantee:
        Breakers can only *worsen* the regime, never improve it. A cheap
        Z-score can never rescue a STRESS regime (enforced downstream by
        the fusion regime cap).
    """
    as_of: date | None = raw.get("as_of")

    # ------------------------------------------------------------------
    # 0. Extract pillar readings; the stress pillar accepts STLFSI4 or,
    #    failing that, NFCI (redundant sourcing — no single point of
    #    failure for the pillar; see DECISIONS.md #4).
    # ------------------------------------------------------------------
    def _num(key: str) -> float | None:
        v = raw.get(key)
        if v is None:
            return None
        try:
            f = float(v)
        except (TypeError, ValueError):
            return None
        return None if f != f else f  # NaN check

    sahm = _num("sahm")
    spread = _num("t10y2y")
    stlfsi = _num("stlfsi")
    nfci = _num("nfci")

    missing = [k for k, v in (("sahm", sahm), ("t10y2y", spread)) if v is None]
    if stlfsi is None and nfci is None:
        missing.append("stlfsi/nfci")
    if missing:
        return MacroState(
            H=0.0,
            regime="STRESS",
            subscores={"sahm": 0.0, "curve": 0.0, "stress": 0.0},
            raw=raw,
            breakers_fired=[f"DATA_MISSING:{','.join(missing)}"],
            as_of=as_of,
            data_ok=False,
        )

    # ------------------------------------------------------------------
    # 1. Per-pillar sub-scores  s ∈ [0, 1]  (1 = healthy)
    # ------------------------------------------------------------------
    s_sahm = _sub_score_sahm(sahm, cfg)
    s_curve = _sub_score_curve(spread, cfg)
    if stlfsi is not None:
        s_stress = _sub_score_stress(stlfsi, cfg)
        stress_source = "STLFSI4"
    else:
        s_stress = _sub_score_nfci(nfci, cfg)
        stress_source = "NFCI"

    # ------------------------------------------------------------------
    # 2. Macro Health Score  H = Σ wᵢ·sᵢ
    #    Weights default: w_sahm=0.40, w_curve=0.25, w_stress=0.35 (Σ=1)
    # ------------------------------------------------------------------
    H = cfg.w_sahm * s_sahm + cfg.w_curve * s_curve + cfg.w_stress * s_stress

    # ------------------------------------------------------------------
    # 3a. Hard circuit breakers — force STRESS.
    # ------------------------------------------------------------------
    breakers: list[str] = []
    if sahm >= cfg.sahm_trigger:
        breakers.append("SAHM_RECESSION")
    if stlfsi is not None and stlfsi >= cfg.stress_crisis:
        breakers.append("FIN_STRESS_CRISIS")

    # ------------------------------------------------------------------
    # 3b. Soft breakers — cap regime at CAUTION (leading warnings).
    #     Curve dis-inversion: deep inversion within the lookback window,
    #     now re-steepened. Historically the re-steepening is the proximate
    #     pre-recession signal, exactly when the *level* score looks healthy
    #     again — this closes that hole.
    # ------------------------------------------------------------------
    soft_breakers: list[str] = []
    history = raw.get("t10y2y_history")
    if cfg.disinv_enabled and isinstance(history, pd.Series) and as_of is not None:
        if _disinversion_active(history, as_of, cfg):
            soft_breakers.append("CURVE_DISINVERSION")

    # ------------------------------------------------------------------
    # 4. Regime assignment. Breakers only push the regime *down* the ladder.
    # ------------------------------------------------------------------
    if H >= cfg.regime_expansion:
        regime = "EXPANSION"
    elif H >= cfg.regime_caution:
        regime = "CAUTION"
    else:
        regime = "STRESS"

    if breakers:
        regime = "STRESS"
    elif soft_breakers and regime == "EXPANSION":
        regime = "CAUTION"

    raw_out = dict(raw)
    raw_out["stress_source"] = stress_source

    return MacroState(
        H=H,
        regime=regime,
        subscores={"sahm": s_sahm, "curve": s_curve, "stress": s_stress},
        raw=raw_out,
        breakers_fired=breakers,
        soft_breakers_fired=soft_breakers,
        as_of=as_of,
        data_ok=True,
    )
