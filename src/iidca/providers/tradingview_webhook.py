"""TradingView webhook adapter (§4.2).

A tiny FastAPI endpoint that accepts push alerts from TradingView and appends
bars to a local Parquet store.  The MarketDataProvider reads from that store.

Run with:
  uvicorn iidca.providers.tradingview_webhook:app --port 8765
"""

from __future__ import annotations

import logging
from pathlib import Path

import pandas as pd
from pydantic import BaseModel

logger = logging.getLogger(__name__)

_STORE_DIR = Path.home() / ".cache" / "iidca" / "tradingview"


def _store_path(symbol: str) -> Path:
    return _STORE_DIR / f"{symbol}.parquet"


def _append_bar(symbol: str, row: dict) -> None:
    _STORE_DIR.mkdir(parents=True, exist_ok=True)
    path = _store_path(symbol)
    new_df = pd.DataFrame([row]).set_index("date")
    new_df.index = pd.to_datetime(new_df.index).tz_localize(None)

    if path.exists():
        existing = pd.read_parquet(path)
        combined = pd.concat([existing, new_df])
        combined = combined[~combined.index.duplicated(keep="last")].sort_index()
    else:
        combined = new_df.sort_index()

    combined.to_parquet(path)


# ---------------------------------------------------------------------------
# FastAPI webhook endpoint
# ---------------------------------------------------------------------------

class TVBar(BaseModel):
    symbol: str
    date: str  # ISO 8601 date string
    open: float
    high: float
    low: float
    close: float  # adjusted close
    volume: float


try:
    from fastapi import FastAPI  # noqa: PLC0415

    app = FastAPI(title="iidca TradingView webhook")

    @app.post("/bar")
    async def receive_bar(bar: TVBar) -> dict:
        row = {
            "date": bar.date,
            "Open": bar.open,
            "High": bar.high,
            "Low": bar.low,
            "Close": bar.close,
            "Volume": bar.volume,
        }
        _append_bar(bar.symbol, row)
        logger.info("Received bar for %s @ %s", bar.symbol, bar.date)
        return {"status": "ok", "symbol": bar.symbol, "date": bar.date}

except ImportError:
    app = None  # type: ignore[assignment]
    logger.debug("FastAPI not installed; TradingView webhook endpoint unavailable")


# ---------------------------------------------------------------------------
# MarketDataProvider backed by the local TradingView store
# ---------------------------------------------------------------------------

from iidca.providers.base import (  # noqa: E402
    DataValidationError,
    MarketDataProvider,
    validate_ohlcv,
)


class TradingViewProvider(MarketDataProvider):
    """Read OHLCV data from the local TradingView push store."""

    def ohlcv(self, symbol: str, lookback_days: int = 400) -> pd.DataFrame:
        path = _store_path(symbol)
        if not path.exists():
            raise DataValidationError(
                f"{symbol}: no TradingView local store at {path}. "
                "Start the webhook server and push at least one bar."
            )
        df = pd.read_parquet(path)
        df.index = pd.to_datetime(df.index).tz_localize(None)
        df = df.sort_index()

        cutoff = pd.Timestamp.today() - pd.Timedelta(days=lookback_days + 60)
        df = df[df.index >= cutoff]

        validate_ohlcv(df, symbol)
        return df
