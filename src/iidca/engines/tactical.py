"""
Tactical Engine — Zone 2 / Column B.

Pure function over an OHLCV DataFrame → TechnicalState.
Testable with fixture data; no I/O, no side effects.

Metrics computed (§6.1–6.2):
  • 200-day SMA on adjusted close + slope (sign of SMA[t] − SMA[t−k], k=20)
  • Rolling Z-score on LOG price: Z = (ln P − μ_W) / σ_W, window W=200
      Negative Z = price below rolling mean = cheap
  • ATR% = ATR(14) / Close  — current volatility as a fraction of price
  • vol_factor = clamp(atr_pct_baseline / atr_pct_now, g_vol_min, 1.0)
      atr_pct_baseline = long rolling median of ATR% (default window=252 days)
      High current vol → vol_factor < 1 → damps aggression (§6.3)
  • ADX(14) + DMI  — trend strength, orthogonal to Z
  • RSI(14)        — momentum / oversold timing

Derived guard (§6.3):
  • trend_strong_down = Close < SMA200 AND SMA200_slope < 0 AND ADX > adx_trend_thresh

Note on library: uses pandas_ta_classic (maintained community fork of pandas_ta).
  import pandas_ta_classic as ta   # NOT pandas_ta (inactive/archived ~July 2026)
"""

from __future__ import annotations

import logging
from datetime import date

import numpy as np
import pandas as pd
import pandas_ta_classic as ta  # maintained fork; NOT pandas_ta

from iidca.config import TacticalCfg
from iidca.models import TechnicalState

logger = logging.getLogger(__name__)


def _clamp(x: float, lo: float, hi: float) -> float:
    """Clamp x to [lo, hi]."""
    return max(lo, min(hi, x))


def score_tactical(
    df: pd.DataFrame,
    cfg: TacticalCfg,
    symbol: str,
) -> TechnicalState:
    """Compute all tactical indicators and return a TechnicalState.

    Parameters
    ----------
    df:
        OHLCV DataFrame indexed by date (DatetimeIndex or date index).
        Must have columns: Open, High, Low, Close, Volume.
        'Close' is treated as the adjusted close (providers must adjust
        before passing here — see §4.2 and providers/base.py).
        Must have ≥ max(sma_window, z_window, atr_baseline_window) + sma_slope_lookback
        rows to be fully warmed up (≈ 280+ trading days with defaults).
    cfg:
        TacticalCfg — all thresholds/windows from config, no hard-coded constants.
    symbol:
        Ticker label written through to TechnicalState.symbol.

    Returns
    -------
    TechnicalState with data_ok=True on success, data_ok=False (and M=1.0 fail-safe
    via fusion) if any computation fails or the DataFrame is too short.
    """
    # ------------------------------------------------------------------ #
    #  Guard: minimum rows needed for a fully-warmed-up reading           #
    # ------------------------------------------------------------------ #
    min_rows = max(cfg.sma_window, cfg.z_window, cfg.atr_baseline_window) + cfg.sma_slope_lookback
    if df is None or len(df) < min_rows:
        logger.warning(
            "score_tactical(%s): insufficient data (%d rows, need %d). "
            "Returning fail-safe TechnicalState.",
            symbol, len(df) if df is not None else 0, min_rows,
        )
        return _fail_safe(symbol)

    # ------------------------------------------------------------------ #
    #  Extract series; validate required columns                          #
    # ------------------------------------------------------------------ #
    required = {"High", "Low", "Close"}
    missing = required - set(df.columns)
    if missing:
        logger.error("score_tactical(%s): missing columns %s", symbol, missing)
        return _fail_safe(symbol)

    try:
        return _compute(df, cfg, symbol)
    except Exception:
        logger.exception("score_tactical(%s): unexpected computation error.", symbol)
        return _fail_safe(symbol)


# --------------------------------------------------------------------------- #
#  Internal computation (separated so try/except is clean)                    #
# --------------------------------------------------------------------------- #

def _compute(df: pd.DataFrame, cfg: TacticalCfg, symbol: str) -> TechnicalState:
    c = df["Close"]
    high = df["High"]
    low = df["Low"]

    # ------------------------------------------------------------------ #
    #  1. 200-day SMA + slope                                             #
    # ------------------------------------------------------------------ #
    sma = c.rolling(cfg.sma_window).mean()
    sma_now = float(sma.iloc[-1])

    # Slope = sign of SMA[t] − SMA[t−k]: positive = upward, negative = downward.
    # Store the raw difference so the fusion layer can use the magnitude if needed,
    # but the guard only needs the sign.
    sma_slope = float(sma.iloc[-1] - sma.iloc[-1 - cfg.sma_slope_lookback])

    # ------------------------------------------------------------------ #
    #  2. Rolling Z-score on log price (§6.1)                            #
    #     Z = (ln P − μ_W) / σ_W  — negative = cheap                    #
    # ------------------------------------------------------------------ #
    logp = np.log(c)
    mu = logp.rolling(cfg.z_window).mean()
    sig = logp.rolling(cfg.z_window).std()

    sig_last = float(sig.iloc[-1])
    if sig_last == 0.0 or np.isnan(sig_last):
        # Degenerate: all prices identical in window — treat as at-mean
        z = 0.0
    else:
        z = float((logp.iloc[-1] - mu.iloc[-1]) / sig_last)

    # ------------------------------------------------------------------ #
    #  3. ATR% and vol_factor (§6.2, §6.3)                               #
    # ------------------------------------------------------------------ #
    # ATR% = ATR(14) / Close — volatility as a fraction of price.
    # pandas_ta_classic returns a Series named "ATRr_<length>" or similar;
    # access it generically via .iloc to avoid name-format assumptions.
    atr_series = ta.atr(high, low, c, length=cfg.atr_period)
    atr_pct_series: pd.Series = atr_series / c  # element-wise normalisation

    atr_pct_now = float(atr_pct_series.iloc[-1])

    # atr_pct_baseline = long rolling median (default 252 days ≈ 1 year).
    # Using median (not mean) so a single vol-spike doesn't inflate the baseline.
    atr_pct_baseline = float(
        atr_pct_series.rolling(cfg.atr_baseline_window).median().iloc[-1]
    )

    # vol_factor: 1.0 = current vol matches baseline (normal); < 1 = elevated vol.
    # Clamp to [g_vol_min, 1.0] — never allow vol alone to eliminate the position.
    if atr_pct_now <= 0 or np.isnan(atr_pct_now) or np.isnan(atr_pct_baseline):
        vol_factor = 1.0  # can't compute; fail safe toward allowing normal DCA
    else:
        vol_factor = _clamp(atr_pct_baseline / atr_pct_now, cfg.g_vol_min, 1.0)

    # ------------------------------------------------------------------ #
    #  4. ADX(14) + DMI (§6.2)                                           #
    # ------------------------------------------------------------------ #
    # ta.adx returns a DataFrame with columns ADX_<n>, DMP_<n>, DMN_<n>.
    adx_df = ta.adx(high, low, c, length=cfg.adx_period)
    adx_col = f"ADX_{cfg.adx_period}"
    if adx_col not in adx_df.columns:
        # Fallback: use the first column that starts with ADX
        adx_col = next((col for col in adx_df.columns if col.startswith("ADX")), None)
    adx = float(adx_df[adx_col].iloc[-1]) if adx_col else float("nan")

    # ------------------------------------------------------------------ #
    #  5. RSI(14) (§6.2)                                                  #
    # ------------------------------------------------------------------ #
    rsi = float(ta.rsi(c, length=cfg.rsi_period).iloc[-1])

    # ------------------------------------------------------------------ #
    #  6. Derived tactical guard (§6.3)                                   #
    #     trend_strong_down: Close < SMA200 AND slope < 0 AND ADX > thresh
    # ------------------------------------------------------------------ #
    price_now = float(c.iloc[-1])
    trend_strong_down = bool(
        price_now < sma_now
        and sma_slope < 0
        and not np.isnan(adx)
        and adx > cfg.adx_trend_thresh
    )

    # ------------------------------------------------------------------ #
    #  7. Determine as_of date from DataFrame index                       #
    # ------------------------------------------------------------------ #
    last_idx = df.index[-1]
    if hasattr(last_idx, "date"):
        as_of: date | None = last_idx.date()
    elif isinstance(last_idx, date):
        as_of = last_idx
    else:
        as_of = None

    return TechnicalState(
        symbol=symbol,
        price=price_now,
        sma200=sma_now,
        sma_slope=sma_slope,
        z=z,
        atr_pct=atr_pct_now,
        vol_factor=vol_factor,
        adx=adx,
        rsi=rsi,
        trend_strong_down=trend_strong_down,
        as_of=as_of,
        data_ok=True,
    )


def _fail_safe(symbol: str) -> TechnicalState:
    """Return a safe TechnicalState that signals data failure to the fusion layer.

    data_ok=False causes fusion to degrade to M=1.0 (standard DCA) per §1.3 #3.
    All numeric fields are set to neutral / non-triggering values.
    """
    return TechnicalState(
        symbol=symbol,
        price=float("nan"),
        sma200=float("nan"),
        sma_slope=0.0,
        z=0.0,          # neutral — no signal
        atr_pct=float("nan"),
        vol_factor=1.0,  # neutral — don't damp aggression
        adx=0.0,
        rsi=50.0,        # neutral midpoint
        trend_strong_down=False,
        as_of=None,
        data_ok=False,
    )
