"""Tactical engine tests — the trend-bias regression test lives here."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from iidca.engines.tactical import (
    current_trend_channel,
    score_tactical,
    tactical_series,
    trend_residual_stats,
)
from tests.conftest import make_ohlcv

# ---------------------------------------------------------------------------
# The headline fix: residual Z is unbiased on trending assets
# ---------------------------------------------------------------------------

def test_pure_trend_gives_zero_residual_z():
    """A noiseless constant-growth asset sits exactly ON its trend: Z ≈ 0.

    The v1 rolling-mean Z reads ≈ +1.73 on this same series for ANY growth
    rate, permanently flagging trending assets as 'expensive'.
    """
    n, w = 400, 200
    logp = 0.001 * np.arange(n) + np.log(100)

    # Old (v1) statistic, for documentation of the bias:
    s = pd.Series(logp)
    old_z = float(
        (s.iloc[-1] - s.rolling(w).mean().iloc[-1]) / s.rolling(w).std().iloc[-1]
    )
    assert old_z > 1.5  # structurally biased upward on a pure trend

    # New statistic:
    stats = trend_residual_stats(logp, w)
    assert abs(float(stats["z"].iloc[-1])) < 1e-6


def test_trending_asset_with_noise_is_roughly_unbiased(trending_ohlcv, tactical_cfg):
    """On a noisy uptrend, the time-series of residual Z should be centred
    near zero — not structurally positive."""
    ser = tactical_series(trending_ohlcv, tactical_cfg)
    z = ser["z"].dropna()
    assert abs(float(z.mean())) < 0.75  # the v1 statistic centres near +1.7


def test_drop_below_trend_gives_negative_z(trending_ohlcv, tactical_cfg):
    df = trending_ohlcv.copy()
    df.iloc[-10:, df.columns.get_loc("Close")] *= 0.85  # 15% air pocket
    df["Low"] = np.minimum(df["Low"], df["Close"])
    state = score_tactical(df, tactical_cfg, "TEST")
    assert state.data_ok
    assert state.z < -1.0


def test_spike_above_trend_gives_positive_z(trending_ohlcv, tactical_cfg):
    df = trending_ohlcv.copy()
    df.iloc[-5:, df.columns.get_loc("Close")] *= 1.15
    df["High"] = np.maximum(df["High"], df["Close"])
    state = score_tactical(df, tactical_cfg, "TEST")
    assert state.z > 1.0


def test_trend_drift_annual_matches_input(tactical_cfg):
    # Near-noiseless trend: the fitted local drift must recover the input.
    # (On a noisy random walk the local 200-day slope is dominated by the
    # realized path, so only the low-noise case pins down the estimator.)
    df = make_ohlcv(n=600, daily_drift=0.001, daily_vol=0.0005, seed=5)
    state = score_tactical(df, tactical_cfg, "TEST")
    assert state.trend_drift_annual == pytest.approx(0.252, abs=0.05)


# ---------------------------------------------------------------------------
# Volatility guard
# ---------------------------------------------------------------------------

def test_vol_factor_normal_regime_is_one(flat_ohlcv, tactical_cfg):
    state = score_tactical(flat_ohlcv, tactical_cfg, "TEST")
    assert state.vol_factor == pytest.approx(1.0, abs=0.15)


def test_vol_spike_damps_and_respects_floor(tactical_cfg):
    df = make_ohlcv(n=600, daily_vol=0.008, seed=3)
    # Manufacture a violent recent regime: huge daily ranges
    tail = df.index[-20:]
    df.loc[tail, "High"] = df.loc[tail, "Close"] * 1.10
    df.loc[tail, "Low"] = df.loc[tail, "Close"] * 0.90
    state = score_tactical(df, tactical_cfg, "TEST")
    assert state.vol_factor < 1.0
    assert state.vol_factor >= tactical_cfg.g_vol_min


# ---------------------------------------------------------------------------
# Falling-knife guard & fail-safety
# ---------------------------------------------------------------------------

def test_strong_downtrend_detected(tactical_cfg):
    df = make_ohlcv(n=600, daily_drift=-0.004, daily_vol=0.01, seed=11)
    state = score_tactical(df, tactical_cfg, "TEST")
    assert state.price < state.sma200
    assert state.sma_slope < 0
    assert state.trend_strong_down == (state.adx > tactical_cfg.adx_trend_thresh)


def test_insufficient_data_fail_safe(tactical_cfg):
    df = make_ohlcv(n=100)
    state = score_tactical(df, tactical_cfg, "TEST")
    assert not state.data_ok
    assert state.z == 0.0
    assert state.vol_factor == 1.0
    assert not state.trend_strong_down


def test_missing_columns_fail_safe(tactical_cfg, flat_ohlcv):
    df = flat_ohlcv.drop(columns=["High"])
    state = score_tactical(df, tactical_cfg, "TEST")
    assert not state.data_ok


# ---------------------------------------------------------------------------
# Chart helpers
# ---------------------------------------------------------------------------

def test_current_trend_channel_brackets_price(trending_ohlcv, tactical_cfg):
    ch = current_trend_channel(trending_ohlcv, tactical_cfg)
    assert len(ch) == tactical_cfg.z_window
    assert (ch["lo2"] <= ch["lo1"]).all()
    assert (ch["lo1"] <= ch["fit"]).all()
    assert (ch["fit"] <= ch["hi1"]).all()
    assert (ch["hi1"] <= ch["hi2"]).all()
    # ~95% of closes should sit inside the 2σ channel
    inside = ((ch["close"] >= ch["lo2"]) & (ch["close"] <= ch["hi2"])).mean()
    assert inside > 0.85
