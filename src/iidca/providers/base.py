"""
T4 — Provider abstract base classes (§10.3).

Business logic NEVER imports yfinance, fredapi, stooq, tiingo, or any
data-source library directly.  It always talks to one of these ABCs.
Swapping a broken source is a config change, not a code change.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date, timedelta
from typing import Protocol

import pandas as pd


# ---------------------------------------------------------------------------
# MarketDataProvider ABC
# ---------------------------------------------------------------------------

class MarketDataProvider(ABC):
    """Abstract interface for OHLCV market-data sources.

    All concrete adapters (yfinance, stooq, tiingo, tradingview) must
    implement :meth:`ohlcv` so business logic is provider-agnostic.

    Contract
    --------
    The returned DataFrame must:
      - Be indexed by ``pd.DatetimeIndex`` with timezone-naive UTC dates
        (date component only; no intraday).
      - Be sorted ascending (oldest → newest).
      - Have at minimum these columns (case-sensitive):
        ``Open``, ``High``, ``Low``, ``Close``, ``Volume``.
      - Use **adjusted close** in the ``Close`` column.
      - Contain no all-NaN columns.
      - Have its last bar no older than ``staleness_days`` calendar days
        from today (enforcement is the caller's responsibility via
        :func:`validate_ohlcv`).
    """

    @abstractmethod
    def ohlcv(self, symbol: str, lookback_days: int = 400) -> pd.DataFrame:
        """Fetch OHLCV data for *symbol* covering at least *lookback_days*.

        Parameters
        ----------
        symbol:
            Ticker/asset identifier (e.g. ``"QQQ"``).
        lookback_days:
            Minimum number of calendar days of history required.  Providers
            may return more.  Default 400 gives comfortable warm-up room for
            the 200-day SMA and Z-score window.

        Returns
        -------
        pd.DataFrame
            OHLCV frame as described in the class docstring.
        """


class FredProvider(ABC):
    """Abstract interface for FRED macro-series sources.

    All concrete adapters (fredapi wrapper, requests fallback) must
    implement :meth:`series`.
    """

    @abstractmethod
    def series(self, series_id: str) -> pd.Series:
        """Fetch a FRED series by ID.

        Parameters
        ----------
        series_id:
            FRED series identifier (e.g. ``"STLFSI4"``, ``"SAHMREALTIME"``).

        Returns
        -------
        pd.Series
            Values indexed by ``pd.DatetimeIndex`` (dates only, ascending).
            Series name is set to ``series_id``.
            Callers use ``.iloc[-1]`` to get the latest available observation.
        """


# ---------------------------------------------------------------------------
# Validation helpers (used by all concrete MarketDataProvider implementations)
# ---------------------------------------------------------------------------

REQUIRED_OHLCV_COLUMNS = ("Open", "High", "Low", "Close", "Volume")


class DataValidationError(ValueError):
    """Raised when ingested data fails structural or staleness checks."""


def validate_ohlcv(df: pd.DataFrame, symbol: str, staleness_days: int = 5) -> None:
    """Validate a raw OHLCV DataFrame from any provider.

    Checks
    ------
    1. DataFrame is non-empty.
    2. All required columns are present.
    3. No column is entirely NaN.
    4. Index is monotonically increasing (dates ascending).
    5. Last bar is not older than *staleness_days* calendar days.

    Raises
    ------
    DataValidationError
        On any failed check.  The caller should set ``data_ok = False`` on
        :class:`~iidca.models.TechnicalState` and trigger fail-safe.
    """
    if df.empty:
        raise DataValidationError(f"{symbol}: received empty OHLCV DataFrame")

    missing = [c for c in REQUIRED_OHLCV_COLUMNS if c not in df.columns]
    if missing:
        raise DataValidationError(f"{symbol}: missing columns {missing}")

    all_nan = [c for c in REQUIRED_OHLCV_COLUMNS if df[c].isna().all()]
    if all_nan:
        raise DataValidationError(f"{symbol}: all-NaN columns {all_nan}")

    if not df.index.is_monotonic_increasing:
        raise DataValidationError(f"{symbol}: OHLCV index is not monotonically increasing")

    last_date: date
    try:
        last_date = df.index[-1].date()  # type: ignore[union-attr]
    except AttributeError:
        last_date = pd.Timestamp(df.index[-1]).date()

    cutoff = date.today() - timedelta(days=staleness_days)
    if last_date < cutoff:
        raise DataValidationError(
            f"{symbol}: last bar {last_date} is older than staleness threshold "
            f"({staleness_days} days; cutoff {cutoff})"
        )
