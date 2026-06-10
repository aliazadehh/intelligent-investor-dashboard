"""Fusion engine tests — gates, guards, caps, monotonicity, fail-safety."""

from __future__ import annotations

import pytest

from iidca.config import FusionCfg
from iidca.engines.fusion import decide, macro_gate
from iidca.models import MacroState, TechnicalState


def make_macro(H: float = 0.85, regime: str = "EXPANSION", data_ok: bool = True,
               breakers: list[str] | None = None) -> MacroState:
    return MacroState(
        H=H, regime=regime,
        subscores={"sahm": 0.9, "curve": 0.8, "stress": 0.8},
        raw={"sahm": 0.05, "t10y2y": 1.0, "stlfsi": -0.5},
        breakers_fired=breakers or [],
        data_ok=data_ok,
    )


def make_tech(z: float = 0.0, vol_factor: float = 1.0,
              trend_strong_down: bool = False, data_ok: bool = True) -> TechnicalState:
    return TechnicalState(
        symbol="TEST", price=100.0, sma200=95.0, sma_slope=1.0,
        z=z, trend_drift_annual=0.1, sigma_resid=0.05,
        atr_pct=0.012, atr_pct_baseline=0.012, vol_factor=vol_factor,
        adx=20.0, trend_strong_down=trend_strong_down, data_ok=data_ok,
    )


# ---------------------------------------------------------------------------
# Macro gate ramp
# ---------------------------------------------------------------------------

def test_gate_ramp_boundaries(fusion_cfg):
    assert macro_gate(fusion_cfg.gate_floor_h, fusion_cfg) == 0.0
    assert macro_gate(fusion_cfg.gate_full_h, fusion_cfg) == 1.0
    assert macro_gate(1.0, fusion_cfg) == 1.0
    assert macro_gate(0.0, fusion_cfg) == 0.0
    mid = (fusion_cfg.gate_floor_h + fusion_cfg.gate_full_h) / 2
    assert macro_gate(mid, fusion_cfg) == pytest.approx(0.5)


def test_healthy_macro_passes_aggression_fully(fusion_cfg):
    """H ≥ gate_full_h: a cheap asset gets the full tilt (v1 silently taxed
    it by multiplying by raw H)."""
    d = decide(make_macro(H=0.85), make_tech(z=-2.0), fusion_cfg)
    assert d.rationale["g_macro"] == 1.0
    assert d.rationale["d_eff"] == pytest.approx(d.rationale["d"])
    assert d.M > 1.4


# ---------------------------------------------------------------------------
# Core scenarios (the README worked examples)
# ---------------------------------------------------------------------------

def test_neutral_inputs_give_standard(fusion_cfg):
    d = decide(make_macro(), make_tech(z=0.0), fusion_cfg)
    assert d.M == pytest.approx(1.0)
    assert d.label == "Standard"


def test_falling_knife_never_aggressive(fusion_cfg):
    """Cheap asset + collapsing macro = at most Standard, never Aggressive."""
    macro = make_macro(H=0.15, regime="STRESS")
    tech = make_tech(z=-2.0, vol_factor=0.5, trend_strong_down=True)
    d = decide(macro, tech, fusion_cfg)
    assert d.M <= fusion_cfg.m_cap_stress
    assert d.rationale["g_macro"] == 0.0  # below gate floor → zero aggression


def test_expensive_in_weak_macro_amplified_defence(fusion_cfg):
    strong = decide(make_macro(H=0.45, regime="CAUTION"), make_tech(z=1.8), fusion_cfg)
    healthy = decide(make_macro(H=0.85), make_tech(z=1.8), fusion_cfg)
    assert strong.M < healthy.M  # weak macro amplifies the defensive tilt


def test_guards_apply_only_to_aggression(fusion_cfg):
    """Defensive tilts must never be damped by trend/vol guards."""
    base = decide(make_macro(), make_tech(z=2.0), fusion_cfg)
    guarded = decide(make_macro(), make_tech(z=2.0, vol_factor=0.4,
                                             trend_strong_down=True), fusion_cfg)
    assert guarded.M == pytest.approx(base.M)


def test_trend_guard_damps_aggression(fusion_cfg):
    free = decide(make_macro(), make_tech(z=-2.0), fusion_cfg)
    knived = decide(make_macro(), make_tech(z=-2.0, trend_strong_down=True), fusion_cfg)
    assert knived.M < free.M
    assert knived.rationale["g_trend"] == fusion_cfg.g_trend_down


# ---------------------------------------------------------------------------
# Caps, floor, clamps
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("regime,cap_attr", [
    ("EXPANSION", "m_cap_exp"),
    ("CAUTION", "m_cap_caution"),
    ("STRESS", "m_cap_stress"),
])
def test_regime_caps_bind(fusion_cfg, regime, cap_attr):
    macro = make_macro(H=0.95, regime=regime)
    d = decide(macro, make_tech(z=-5.0), fusion_cfg)
    assert d.M <= getattr(fusion_cfg, cap_attr) + 1e-9


def test_global_floor_binds(fusion_cfg):
    d = decide(make_macro(H=0.30, regime="STRESS"), make_tech(z=5.0), fusion_cfg)
    assert d.M == pytest.approx(fusion_cfg.m_min)
    assert d.label == "Defensive"


def test_m_always_within_global_bounds(fusion_cfg):
    for H in (0.0, 0.3, 0.5, 0.8, 1.0):
        for z in (-5, -2, 0, 2, 5):
            for regime in ("EXPANSION", "CAUTION", "STRESS"):
                d = decide(make_macro(H=H, regime=regime), make_tech(z=z), fusion_cfg)
                assert fusion_cfg.m_min <= d.M <= fusion_cfg.m_max


# ---------------------------------------------------------------------------
# Monotonicity — more health/cheapness never reduces M
# ---------------------------------------------------------------------------

def test_monotone_in_h(fusion_cfg):
    tech = make_tech(z=-2.0)
    ms = [decide(make_macro(H=h), tech, fusion_cfg).M for h in (0.4, 0.5, 0.6, 0.7, 0.8)]
    assert ms == sorted(ms)


def test_monotone_in_z(fusion_cfg):
    macro = make_macro()
    ms = [decide(macro, make_tech(z=z), fusion_cfg).M for z in (3, 2, 1, 0, -1, -2, -3)]
    assert ms == sorted(ms)


# ---------------------------------------------------------------------------
# Fail-safety, labels, waterfall integrity
# ---------------------------------------------------------------------------

def test_bad_data_fail_safe(fusion_cfg):
    d = decide(make_macro(data_ok=False), make_tech(z=-3.0), fusion_cfg)
    assert d.M == 1.0
    assert "fail-safe" in d.label
    d = decide(make_macro(), make_tech(z=-3.0, data_ok=False), fusion_cfg)
    assert d.M == 1.0


def test_instruction_matches_m(fusion_cfg):
    d = decide(make_macro(H=0.30, regime="STRESS"), make_tech(z=5.0), fusion_cfg)
    assert f"{round(d.M * 100)}%" in d.instruction


def test_label_tiers_config_driven():
    cfg = FusionCfg(labels={"defensive_max": 0.5, "cautious_max": 0.8,
                            "standard_max": 1.2, "opportunistic_max": 1.5})
    d = decide(make_macro(), make_tech(z=0.0), cfg)
    assert d.label == "Standard"


def test_waterfall_reconstructs_m(fusion_cfg):
    for z, H, regime in [(-2.0, 0.85, "EXPANSION"), (2.5, 0.45, "CAUTION"),
                         (-1.0, 0.55, "CAUTION"), (3.0, 0.2, "STRESS")]:
        d = decide(make_macro(H=H, regime=regime), make_tech(z=z), fusion_cfg)
        total = sum(delta for _, delta in d.rationale["waterfall"])
        assert total == pytest.approx(d.rationale["M_final"], abs=1e-9)
        assert d.M == pytest.approx(total, abs=1e-4)  # d.M is rounded to 4dp
