"""yfinance market-data provider (§4.2, §10.3)."""

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


class YFinanceProvider(MarketDataProvider):
    """Market-data provider backed by yfinance.

    yfinance scrapes Yahoo Finance and has been known to break after Yahoo
    redesigns (e.g. Feb 2025).  Always use through the MarketDataProvider ABC
    so swapping to stooq/tiingo is a config change, not a rewrite.
    """

    def ohlcv(self, symbol: str, lookback_days: int = 400) -> pd.DataFrame:
        import yfinance as yf  # noqa: PLC0415

        period = f"{lookback_days + 30}d"  # add buffer for weekends/holidays
        ticker = yf.Ticker(symbol)
        df: pd.DataFrame = ticker.history(period=period, auto_adjust=True)

        if df.empty:
            raise DataValidationError(f"{symbol}: yfinance returned empty DataFrame")

        # Normalise columns
        df = df[["Open", "High", "Low", "Close", "Volume"]].copy()
        df.index = pd.to_datetime(df.index).tz_localize(None)
        df = clean_ohlcv(df.sort_index())

        validate_ohlcv(df, symbol)
        logger.debug("yfinance: fetched %d bars for %s", len(df), symbol)
        return df
