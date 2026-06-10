"""Config loading, env overlay, v1 backward compatibility, hash stability."""

from __future__ import annotations

import pytest

from iidca.config import AppCfg, load_config


def test_defaults_load_without_file(tmp_path):
    cfg = load_config(tmp_path / "missing.toml")
    assert cfg.watchlist == ["QQQ"]
    assert cfg.provider_chain[0] == "yfinance"
    assert cfg.macro.w_sahm + cfg.macro.w_curve + cfg.macro.w_stress == pytest.approx(1.0)


def test_shipped_default_toml_parses():
    cfg = load_config()  # config/default.toml
    assert cfg.fusion.gate_full_h > cfg.fusion.gate_floor_h
    assert cfg.macro.series["stlfsi"] == "STLFSI4"


def test_env_override(tmp_path, monkeypatch):
    monkeypatch.setenv("IIDCA_MACRO__STRESS_CRISIS", "2.0")
    monkeypatch.setenv("IIDCA_WATCHLIST", '["SPY", "BTC-USD"]')
    cfg = load_config(tmp_path / "missing.toml")
    assert cfg.macro.stress_crisis == 2.0
    assert cfg.watchlist == ["SPY", "BTC-USD"]


def test_v1_config_backward_compat(tmp_path):
    p = tmp_path / "old.toml"
    p.write_text('target_symbol = "SPY"\nmarket_provider = "stooq"\n')
    cfg = load_config(p)
    assert cfg.watchlist == ["SPY"]
    assert cfg.provider_chain[0] == "stooq"


def test_config_hash_changes_with_params():
    a = AppCfg()
    b = AppCfg(fusion={"alpha": 0.5})
    assert a.config_hash() != b.config_hash()
    assert a.config_hash() == AppCfg().config_hash()


def test_label_tiers_must_ascend():
    with pytest.raises(ValueError):
        AppCfg(fusion={"labels": {"defensive_max": 1.2, "cautious_max": 0.9,
                                  "standard_max": 1.1, "opportunistic_max": 1.4}})
