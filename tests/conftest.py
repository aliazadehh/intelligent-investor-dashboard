"""Shared fixtures: synthetic OHLCV frames and config objects."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from iidca.config import AppCfg, FusionCfg, MacroCfg, TacticalCfg


@pytest.fixture
def macro_cfg() -> MacroCfg:
    return MacroCfg()


@pytest.fixture
def tactical_cfg() -> TacticalCfg:
    return TacticalCfg()


@pytest.fixture
def fusion_cfg() -> FusionCfg:
    return FusionCfg()


@pytest.fixture
def app_cfg() -> AppCfg:
    return AppCfg()


def make_ohlcv(
    n: int = 600,
    daily_drift: float = 0.0,
    daily_vol: float = 0.01,
    start_price: float = 100.0,
    seed: int = 7,
) -> pd.DataFrame:
    """Synthetic OHLCV frame: geometric random walk with drift."""
    rng = np.random.default_rng(seed)
    rets = daily_drift + daily_vol * rng.standard_normal(n)
    close = start_price * np.exp(np.cumsum(rets))
    high = close * (1 + np.abs(rng.normal(0, daily_vol / 2, n)))
    low = close * (1 - np.abs(rng.normal(0, daily_vol / 2, n)))
    open_ = np.concatenate([[start_price], close[:-1]])
    idx = pd.bdate_range(end=pd.Timestamp.today().normalize(), periods=n)
    return pd.DataFrame(
        {
            "Open": open_,
            "High": np.maximum.reduce([high, close, open_]),
            "Low": np.minimum.reduce([low, close, open_]),
            "Close": close,
            "Volume": rng.integers(1e6, 5e6, n).astype(float),
        },
        index=idx,
    )


@pytest.fixture
def trending_ohlcv() -> pd.DataFrame:
    """Steady uptrend ≈ +25%/yr with modest noise — the case that broke
    the v1 rolling-mean Z-score."""
    return make_ohlcv(n=600, daily_drift=0.001, daily_vol=0.008)


@pytest.fixture
def flat_ohlcv() -> pd.DataFrame:
    return make_ohlcv(n=600, daily_drift=0.0, daily_vol=0.01)
