"""Macro engine tests — sub-score ramps, blending, breakers, fallbacks."""

from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
import pytest

from iidca.engines.macro import score_macro

HEALTHY = {"sahm": 0.05, "t10y2y": 1.2, "stlfsi": -0.6, "as_of": date(2026, 6, 1)}


def test_healthy_inputs_give_expansion(macro_cfg):
    state = score_macro(dict(HEALTHY), macro_cfg)
    assert state.data_ok
    assert state.regime == "EXPANSION"
    assert state.H > macro_cfg.regime_expansion
    assert not state.breakers_fired


def test_subscore_boundaries(macro_cfg):
    # Sahm at trigger → sub-score 0; at 0 → 1
    s = score_macro({**HEALTHY, "sahm": macro_cfg.sahm_trigger}, macro_cfg)
    assert s.subscores["sahm"] == 0.0
    s = score_macro({**HEALTHY, "sahm": 0.0}, macro_cfg)
    assert s.subscores["sahm"] == 1.0
    # Curve at floor → 0; at healthy ref → 1
    s = score_macro({**HEALTHY, "t10y2y": macro_cfg.curve_inv_floor}, macro_cfg)
    assert s.subscores["curve"] == 0.0
    s = score_macro({**HEALTHY, "t10y2y": macro_cfg.curve_healthy_ref}, macro_cfg)
    assert s.subscores["curve"] == 1.0
    # Stress midpoint
    s = score_macro({**HEALTHY, "stlfsi": 0.0}, macro_cfg)
    assert s.subscores["stress"] == pytest.approx(0.5)


def test_h_is_weighted_blend(macro_cfg):
    state = score_macro(dict(HEALTHY), macro_cfg)
    expected = (
        macro_cfg.w_sahm * state.subscores["sahm"]
        + macro_cfg.w_curve * state.subscores["curve"]
        + macro_cfg.w_stress * state.subscores["stress"]
    )
    assert state.H == pytest.approx(expected)
    assert 0.0 <= state.H <= 1.0


def test_sahm_breaker_forces_stress(macro_cfg):
    raw = {**HEALTHY, "sahm": 0.55}
    state = score_macro(raw, macro_cfg)
    assert "SAHM_RECESSION" in state.breakers_fired
    assert state.regime == "STRESS"


def test_fin_stress_breaker_forces_stress(macro_cfg):
    raw = {**HEALTHY, "stlfsi": 2.0}
    state = score_macro(raw, macro_cfg)
    assert "FIN_STRESS_CRISIS" in state.breakers_fired
    assert state.regime == "STRESS"


def test_missing_pillars_fail_safe(macro_cfg):
    state = score_macro({"t10y2y": 1.0}, macro_cfg)
    assert not state.data_ok
    assert state.regime == "STRESS"  # downstream fusion degrades to M=1.0


def test_nfci_fallback_when_stlfsi_missing(macro_cfg):
    raw = {**HEALTHY}
    del raw["stlfsi"]
    raw["nfci"] = -0.60  # calm on NFCI's scale
    state = score_macro(raw, macro_cfg)
    assert state.data_ok
    assert state.raw["stress_source"] == "NFCI"
    assert state.subscores["stress"] == pytest.approx(1.0)


def _spread_history(values_dates: list[tuple[float, date]]) -> pd.Series:
    return pd.Series(
        [v for v, _ in values_dates],
        index=pd.to_datetime([d for _, d in values_dates]),
    )


def test_curve_disinversion_caps_at_caution(macro_cfg):
    as_of = date(2026, 6, 1)
    history = _spread_history([
        (-0.5, as_of - timedelta(days=200)),   # deeply inverted within lookback
        (-0.2, as_of - timedelta(days=120)),
        (0.4, as_of),                          # now re-steepened
    ])
    raw = {**HEALTHY, "t10y2y": 0.4, "as_of": as_of, "t10y2y_history": history}
    state = score_macro(raw, macro_cfg)
    assert "CURVE_DISINVERSION" in state.soft_breakers_fired
    assert state.regime == "CAUTION"  # capped, not forced to STRESS


def test_disinversion_expires_after_lookback(macro_cfg):
    as_of = date(2026, 6, 1)
    history = _spread_history([
        (-0.5, as_of - timedelta(days=500)),   # inversion outside lookback
        (0.6, as_of - timedelta(days=100)),
        (0.8, as_of),
    ])
    raw = {**HEALTHY, "t10y2y": 0.8, "as_of": as_of, "t10y2y_history": history}
    state = score_macro(raw, macro_cfg)
    assert not state.soft_breakers_fired
    assert state.regime == "EXPANSION"


def test_disinversion_not_fired_while_still_inverted(macro_cfg):
    as_of = date(2026, 6, 1)
    history = _spread_history([
        (-0.5, as_of - timedelta(days=100)),
        (-0.3, as_of),                         # still inverted
    ])
    raw = {**HEALTHY, "t10y2y": -0.3, "as_of": as_of, "t10y2y_history": history}
    state = score_macro(raw, macro_cfg)
    assert "CURVE_DISINVERSION" not in state.soft_breakers_fired


def test_hard_breaker_beats_soft_breaker(macro_cfg):
    as_of = date(2026, 6, 1)
    history = _spread_history([(-0.5, as_of - timedelta(days=100)), (0.4, as_of)])
    raw = {**HEALTHY, "sahm": 0.6, "t10y2y": 0.4, "as_of": as_of,
           "t10y2y_history": history}
    state = score_macro(raw, macro_cfg)
    assert state.regime == "STRESS"
