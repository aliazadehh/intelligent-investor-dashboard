"""Tiingo market-data provider — cheap, deep history (§4.2).

Requires TIINGO_API_KEY environment variable.
"""

from __future__ import annotations

import logging
import os
from datetime import date, timedelta

import pandas as pd

from iidca.providers.base import DataValidationError, MarketDataProvider, validate_ohlcv

logger = logging.getLogger(__name__)


class TiingoProvider(MarketDataProvider):
    """Market-data provider backed by Tiingo REST API."""

    _BASE = "https://api.tiingo.com/tiingo/daily"

    def __init__(self, api_key: str | None = None) -> None:
        self._api_key = api_key or os.environ.get("TIINGO_API_KEY", "")
        if not self._api_key:
            raise ValueError("TIINGO_API_KEY env var not set")

    def ohlcv(self, symbol: str, lookback_days: int = 400) -> pd.DataFrame:
        import requests  # noqa: PLC0415

        end = date.today()
        start = end - timedelta(days=lookback_days + 60)
        url = f"{self._BASE}/{symbol}/prices"
        params = {
            "startDate": start.isoformat(),
            "endDate": end.isoformat(),
            "resampleFreq": "daily",
            "token": self._api_key,
        }
        resp = requests.get(url, params=params, timeout=30)
        resp.raise_for_status()
        records = resp.json()

        if not records:
            raise DataValidationError(f"{symbol}: Tiingo returned empty list")

        df = pd.DataFrame(records)
        df["date"] = pd.to_datetime(df["date"]).dt.tz_localize(None)
        df = df.set_index("date").sort_index()

        # Tiingo uses adjClose for adjusted close
        df = df.rename(columns={
            "adjOpen": "Open",
            "adjHigh": "High",
            "adjLow": "Low",
            "adjClose": "Close",
            "adjVolume": "Volume",
        })
        df = df[["Open", "High", "Low", "Close", "Volume"]].copy()

        validate_ohlcv(df, symbol)
        logger.debug("tiingo: fetched %d bars for %s", len(df), symbol)
        return df
