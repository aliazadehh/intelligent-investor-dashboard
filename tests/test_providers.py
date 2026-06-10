"""Provider-layer data hygiene tests (no network)."""

from __future__ import annotations

import numpy as np
import pytest

from iidca.providers.base import DataValidationError, clean_ohlcv, validate_ohlcv
from tests.conftest import make_ohlcv


def test_clean_ohlcv_drops_half_formed_live_bar():
    """yfinance sometimes appends today's in-progress bar with NaN OHLC and
    volume only — it must be dropped, not fed into the indicators."""
    df = make_ohlcv(n=300)
    df.iloc[-1, df.columns.get_loc("Open")] = np.nan
    df.iloc[-1, df.columns.get_loc("High")] = np.nan
    df.iloc[-1, df.columns.get_loc("Low")] = np.nan
    df.iloc[-1, df.columns.get_loc("Close")] = np.nan
    cleaned = clean_ohlcv(df)
    assert len(cleaned) == len(df) - 1
    assert cleaned["Close"].notna().all()


def test_validate_rejects_nan_last_close():
    df = make_ohlcv(n=300)
    df.iloc[-1, df.columns.get_loc("Close")] = np.nan
    with pytest.raises(DataValidationError):
        validate_ohlcv(df, "TEST")


def test_validate_rejects_stale_data():
    df = make_ohlcv(n=300)
    df.index = df.index - np.timedelta64(30, "D")
    with pytest.raises(DataValidationError):
        validate_ohlcv(df, "TEST", staleness_days=5)


def test_validate_accepts_clean_fresh_frame():
    validate_ohlcv(make_ohlcv(n=300), "TEST")
