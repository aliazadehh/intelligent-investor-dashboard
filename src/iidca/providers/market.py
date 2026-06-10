"""Market-data orchestrator — provider chain + last-good Parquet cache.

Eliminates the single point of failure in price sourcing:
  1. Try each provider in cfg.provider_chain, in order; first one whose
     data passes validate_ohlcv wins and refreshes the local cache.
  2. If every provider fails, fall back to the last-good cached frame
     (marked stale so the caller can flag data_ok accordingly).

The dashboard also reads the cache directly for charting, so charts work
offline and never trigger a network call on render.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from iidca.config import AppCfg
from iidca.providers.base import MarketDataProvider, clean_ohlcv, validate_ohlcv

logger = logging.getLogger(__name__)

_CACHE_DIR = Path.home() / ".cache" / "iidca" / "market"


@dataclass
class MarketFetchResult:
    df: pd.DataFrame
    source: str        # provider name, or "cache" when all providers failed
    fresh: bool        # False = served from last-good cache after failures
    errors: list[str]  # one entry per failed provider, for the data-status UI


def _cache_path(symbol: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9._=-]", "_", symbol.upper())
    return _CACHE_DIR / f"{safe}.parquet"


def _write_cache(symbol: str, df: pd.DataFrame) -> None:
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    df.to_parquet(_cache_path(symbol))


def read_cached_ohlcv(symbol: str) -> pd.DataFrame | None:
    """Last-good OHLCV frame for *symbol*, or None. No network."""
    p = _cache_path(symbol)
    if not p.exists():
        return None
    try:
        df = pd.read_parquet(p)
        df.index = pd.to_datetime(df.index)
        return clean_ohlcv(df.sort_index())
    except Exception:
        logger.exception("Failed to read market cache for %s", symbol)
        return None


def _make_provider(name: str) -> MarketDataProvider:
    if name == "yfinance":
        from iidca.providers.yfinance_provider import YFinanceProvider  # noqa: PLC0415
        return YFinanceProvider()
    if name == "stooq":
        from iidca.providers.stooq_provider import StooqProvider  # noqa: PLC0415
        return StooqProvider()
    if name == "tiingo":
        from iidca.providers.tiingo_provider import TiingoProvider  # noqa: PLC0415
        return TiingoProvider()
    if name == "tradingview":
        from iidca.providers.tradingview_webhook import TradingViewProvider  # noqa: PLC0415
        return TradingViewProvider()
    raise ValueError(f"Unknown market provider: {name!r}")


def fetch_ohlcv(symbol: str, cfg: AppCfg) -> MarketFetchResult:
    """Fetch OHLCV for *symbol* through the configured provider chain.

    Returns a MarketFetchResult; raises only if every provider fails AND
    no cached frame exists.
    """
    errors: list[str] = []

    for name in cfg.provider_chain:
        try:
            provider = _make_provider(name)
            df = provider.ohlcv(symbol, lookback_days=cfg.lookback_days)
            validate_ohlcv(df, symbol, staleness_days=cfg.tactical.staleness_days)
            _write_cache(symbol, df)
            logger.info("%s: %d bars via %s", symbol, len(df), name)
            return MarketFetchResult(df=df, source=name, fresh=True, errors=errors)
        except Exception as exc:
            msg = f"{name}: {exc}"
            errors.append(msg)
            logger.warning("Provider failed for %s — %s", symbol, msg)

    cached = read_cached_ohlcv(symbol)
    if cached is not None:
        logger.warning(
            "%s: all providers failed (%s) — serving last-good cache (%s bars, last %s)",
            symbol, "; ".join(errors), len(cached), cached.index[-1].date(),
        )
        return MarketFetchResult(df=cached, source="cache", fresh=False, errors=errors)

    raise RuntimeError(
        f"{symbol}: all market providers failed and no cache exists: {'; '.join(errors)}"
    )
