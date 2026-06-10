"""T5 — FRED data provider with Parquet series caching (§4.1).

Uses fredapi as the primary client with a raw-requests fallback adapter.
Every fetched series is cached to Parquet keyed by series_id; the cache is
only re-pulled when it is older than the series' update cadence.

Update cadences (conservative floor):
  SAHMREALTIME  — monthly  (31 days)
  T10Y2Y        — daily    (1 day)
  STLFSI4       — weekly   (7 days)
  NFCI          — weekly   (7 days)
"""

from __future__ import annotations

import logging
import os
from datetime import date
from pathlib import Path

import pandas as pd

from iidca.providers.base import FredProvider

logger = logging.getLogger(__name__)

# Cadence in days per series (re-pull if cache older than this)
_CADENCE: dict[str, int] = {
    "SAHMREALTIME": 31,
    "T10Y2Y": 1,
    "STLFSI4": 7,
    "NFCI": 7,
}
_DEFAULT_CADENCE = 7  # fallback for unknown series

_CACHE_DIR = Path.home() / ".cache" / "iidca" / "fred"


def _cache_path(series_id: str) -> Path:
    return _CACHE_DIR / f"{series_id}.parquet"


def _cache_is_fresh(series_id: str) -> bool:
    p = _cache_path(series_id)
    if not p.exists():
        return False
    age_days = (date.today() - date.fromtimestamp(p.stat().st_mtime)).days
    cadence = _CADENCE.get(series_id, _DEFAULT_CADENCE)
    return age_days < cadence


def _read_cache(series_id: str) -> pd.Series:
    df = pd.read_parquet(_cache_path(series_id))
    s = df.iloc[:, 0]
    s.index = pd.to_datetime(s.index)
    s.name = series_id
    return s.sort_index()


def _write_cache(series_id: str, s: pd.Series) -> None:
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    s.to_frame().to_parquet(_cache_path(series_id))


class FredApiProvider(FredProvider):
    """Primary FRED provider using the ``fredapi`` library.

    Requires ``FRED_API_KEY`` environment variable.
    Falls back to :class:`FredRequestsProvider` if fredapi is unavailable.
    """

    def __init__(self, api_key: str | None = None) -> None:
        self._api_key = api_key or os.environ.get("FRED_API_KEY", "")

    def series(self, series_id: str) -> pd.Series:
        if _cache_is_fresh(series_id):
            logger.debug("FRED cache hit: %s", series_id)
            return _read_cache(series_id)

        logger.info("Fetching FRED series %s", series_id)
        try:
            from fredapi import Fred  # type: ignore[import-untyped]
            fred = Fred(api_key=self._api_key)
            raw: pd.Series = fred.get_series(series_id)
        except Exception as exc:
            logger.warning("fredapi failed (%s), trying requests fallback", exc)
            raw = FredRequestsProvider(self._api_key).series(series_id)

        s = raw.dropna().sort_index()
        s.name = series_id
        _write_cache(series_id, s)
        return s


class FredRequestsProvider(FredProvider):
    """Fallback FRED provider using raw ``requests`` (no fredapi dependency)."""

    _BASE = "https://api.stlouisfed.org/fred/series/observations"

    def __init__(self, api_key: str | None = None) -> None:
        self._api_key = api_key or os.environ.get("FRED_API_KEY", "")

    def series(self, series_id: str) -> pd.Series:
        if _cache_is_fresh(series_id):
            return _read_cache(series_id)

        import requests  # noqa: PLC0415

        params = {
            "series_id": series_id,
            "api_key": self._api_key,
            "file_type": "json",
            "observation_start": "1900-01-01",
        }
        resp = requests.get(self._BASE, params=params, timeout=30)
        resp.raise_for_status()
        obs = resp.json()["observations"]

        index = pd.to_datetime([o["date"] for o in obs])
        values = pd.to_numeric([o["value"] for o in obs], errors="coerce")
        s = pd.Series(values, index=index, name=series_id).dropna().sort_index()
        _write_cache(series_id, s)
        return s


def make_fred_provider(api_key: str | None = None) -> FredProvider:
    """Return the best available FredProvider."""
    try:
        import fredapi  # noqa: F401
        return FredApiProvider(api_key)
    except ImportError:
        logger.warning("fredapi not installed, using requests fallback")
        return FredRequestsProvider(api_key)
