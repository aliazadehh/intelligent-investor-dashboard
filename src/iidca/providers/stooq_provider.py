"""Stooq market-data provider — free, no API key required (§4.2)."""

from __future__ import annotations

import logging

import pandas as pd

from iidca.providers.base import (
    DataValidationError,
    MarketDataProvider,
    clean_ohlcv,
    validate_ohlcv,
)

logger = logging.getLogger(__name__)


class StooqProvider(MarketDataProvider):
    """Market-data provider backed by Stooq via pandas_datareader."""

    def ohlcv(self, symbol: str, lookback_days: int = 400) -> pd.DataFrame:
        try:
            from pandas_datareader import data as pdr  # noqa: PLC0415
        except ImportError as exc:
            raise ImportError(
                "pandas-datareader is required for StooqProvider. "
                "Install with: uv add pandas-datareader"
            ) from exc

        from datetime import date, timedelta  # noqa: PLC0415

        end = date.today()
        start = end - timedelta(days=lookback_days + 60)

        # Stooq uses US-style tickers with .US suffix for US equities
        stooq_symbol = symbol if "." in symbol else f"{symbol}.US"
        df: pd.DataFrame = pdr.DataReader(stooq_symbol, "stooq", start=start, end=end)

        if df.empty:
            raise DataValidationError(f"{symbol}: Stooq returned empty DataFrame")

        df = df[["Open", "High", "Low", "Close", "Volume"]].copy()
        df.index = pd.to_datetime(df.index).tz_localize(None)
        df = clean_ohlcv(df.sort_index())

        validate_ohlcv(df, symbol)
        logger.debug("stooq: fetched %d bars for %s", len(df), symbol)
        return df
