"""
Tactical Engine — per-asset statistics.

Pure functions over an OHLCV DataFrame → TechnicalState (+ chart series).
Testable with fixture data; no I/O, no side effects.

Valuation measure (see DECISIONS.md #1):
  Z is the *trend-residual Z-score*: fit an OLS trendline to log price over
  the trailing z_window days, then standardize today's deviation from that
  line by the residual std-dev:

      ln P_k ≈ a + b·k          (k = 0 … W−1, OLS fit)
      Z = (ln P_today − fit_today) / σ_resid

  Why not the old rolling-mean Z?  For an asset growing at a constant rate,
  (ln P − rolling mean) / rolling std converges to a CONSTANT ≈ +1.73
  regardless of the growth rate — a steadily trending asset reads as
  "permanently expensive" and the system structurally under-invests in it.
  The residual Z is mean-zero on a pure trend by construction: it measures
  displacement from the asset's own recent path, which is the quantity a
  DCA tilt should respond to. All outputs are dimensionless / log-scale, so
  the same parameters generalize across equities, ETFs and crypto.

Other metrics:
  • SMA(sma_window) + slope (sign over sma_slope_lookback days)
  • ATR% = ATR(atr_period) / Close — volatility as a fraction of price
  • vol_factor = clamp(atr_pct_baseline / atr_pct_now, g_vol_min, 1.0)
      atr_pct_baseline = rolling median of ATR% (atr_baseline_window days)
  • ADX(adx_period) — trend strength, direction-agnostic
  • trend_strong_down = Close < SMA AND slope < 0 AND ADX > adx_trend_thresh

RSI was removed from the pipeline (DECISIONS.md #5): it never entered the
decision math, and a 14-day oscillator is noise at a monthly decision cadence.

Note on library: uses pandas_ta_classic (maintained community fork of pandas_ta).
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

TRADING_DAYS_PER_YEAR = 252


def _clamp(x: float, lo: float, hi: float) -> float:
    """Clamp x to [lo, hi]."""
    return max(lo, min(hi, x))


# --------------------------------------------------------------------------- #
#  Trend-residual Z — vectorized rolling OLS on log price                      #
# --------------------------------------------------------------------------- #

def trend_residual_stats(logp: np.ndarray, window: int) -> pd.DataFrame:
    """Rolling OLS of log price on time over *window* bars.

    Returns a DataFrame with one row per input bar (first window−1 rows NaN):
      z            — (logp − fitted endpoint) / residual std
      slope_daily  — fitted log-price slope per bar
      sigma_resid  — residual std-dev (log units), ddof=2
      fit_end      — fitted log price at the window's last bar
    """
    n = len(logp)
    out = pd.DataFrame(
        np.nan,
        index=range(n),
        columns=["z", "slope_daily", "sigma_resid", "fit_end"],
    )
    if n < window or window < 3:
        return out

    w = window
    k = np.arange(w, dtype=float)
    kbar = (w - 1) / 2.0
    sxx = w * (w * w - 1) / 12.0  # Σ(k − kbar)²

    windows = np.lib.stride_tricks.sliding_window_view(logp, w)  # (n−w+1, w)
    ybar = windows.mean(axis=1)
    slope = windows @ (k - kbar) / sxx
    fit_end = ybar + slope * (w - 1 - kbar)

    # residual SS = total SS − explained SS; guard tiny negatives from fp error
    ss_tot = ((windows - ybar[:, None]) ** 2).sum(axis=1)
    ss_res = np.maximum(ss_tot - slope**2 * sxx, 0.0)
    sigma = np.sqrt(ss_res / (w - 2))

    last = windows[:, -1]
    with np.errstate(divide="ignore", invalid="ignore"):
        z = np.where(sigma > 0, (last - fit_end) / sigma, 0.0)

    idx = np.arange(w - 1, n)
    out.loc[idx, "z"] = z
    out.loc[idx, "slope_daily"] = slope
    out.loc[idx, "sigma_resid"] = sigma
    out.loc[idx, "fit_end"] = fit_end
    return out


def current_trend_channel(df: pd.DataFrame, cfg: TacticalCfg) -> pd.DataFrame:
    """Trendline + σ-bands over the *current* z_window, for charting.

    Returns a DataFrame indexed like the last z_window bars of *df* with
    columns: close, fit, lo1, hi1, lo2, hi2 (price units — bands are the
    fitted log-price line ± 1σ/2σ of residuals, exponentiated).
    """
    w = cfg.z_window
    tail = df.iloc[-w:]
    logp = np.log(tail["Close"].to_numpy(dtype=float))
    k = np.arange(w, dtype=float)
    kbar = (w - 1) / 2.0
    sxx = w * (w * w - 1) / 12.0
    ybar = logp.mean()
    slope = float((logp * (k - kbar)).sum() / sxx)
    fit = ybar + slope * (k - kbar)
    ss_res = max(((logp - fit) ** 2).sum(), 0.0)
    sigma = float(np.sqrt(ss_res / (w - 2)))

    return pd.DataFrame(
        {
            "close": tail["Close"].to_numpy(dtype=float),
            "fit": np.exp(fit),
            "lo1": np.exp(fit - sigma),
            "hi1": np.exp(fit + sigma),
            "lo2": np.exp(fit - 2 * sigma),
            "hi2": np.exp(fit + 2 * sigma),
        },
        index=tail.index,
    )


def tactical_series(df: pd.DataFrame, cfg: TacticalCfg) -> pd.DataFrame:
    """Per-bar history of the headline tactical metrics, for charting.

    Columns: close, sma, z, drift_annual, atr_pct. Index matches *df*.
    """
    c = df["Close"].astype(float)
    stats = trend_residual_stats(np.log(c.to_numpy()), cfg.z_window)
    stats.index = df.index

    atr = ta.atr(df["High"], df["Low"], c, length=cfg.atr_period)
    return pd.DataFrame(
        {
            "close": c,
            "sma": c.rolling(cfg.sma_window).mean(),
            "z": stats["z"],
            "drift_annual": stats["slope_daily"] * TRADING_DAYS_PER_YEAR,
            "atr_pct": atr / c,
        },
        index=df.index,
    )


# --------------------------------------------------------------------------- #
#  Public API                                                                  #
# --------------------------------------------------------------------------- #

def score_tactical(
    df: pd.DataFrame,
    cfg: TacticalCfg,
    symbol: str,
) -> TechnicalState:
    """Compute all tactical indicators and return a TechnicalState.

    Parameters
    ----------
    df:
        OHLCV DataFrame indexed by date, ascending. Must have columns
        Open, High, Low, Close, Volume with 'Close' already adjusted.
        Needs ≥ max(sma_window, z_window, atr_baseline_window) +
        sma_slope_lookback rows to be fully warmed up.
    cfg:
        TacticalCfg — all thresholds/windows from config.
    symbol:
        Ticker label written through to TechnicalState.symbol.

    Returns
    -------
    TechnicalState with data_ok=True on success, data_ok=False (→ fusion
    degrades to M=1.0 fail-safe) if the frame is too short or any
    computation fails.
    """
    min_rows = (
        max(cfg.sma_window, cfg.z_window, cfg.atr_baseline_window)
        + cfg.sma_slope_lookback
    )
    if df is None or len(df) < min_rows:
        logger.warning(
            "score_tactical(%s): insufficient data (%d rows, need %d). "
            "Returning fail-safe TechnicalState.",
            symbol, len(df) if df is not None else 0, min_rows,
        )
        return _fail_safe(symbol)

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


def _compute(df: pd.DataFrame, cfg: TacticalCfg, symbol: str) -> TechnicalState:
    c = df["Close"].astype(float)
    high = df["High"]
    low = df["Low"]

    # ------------------------------------------------------------------ #
    #  1. SMA + slope                                                     #
    # ------------------------------------------------------------------ #
    sma = c.rolling(cfg.sma_window).mean()
    sma_now = float(sma.iloc[-1])
    sma_slope = float(sma.iloc[-1] - sma.iloc[-1 - cfg.sma_slope_lookback])

    # ------------------------------------------------------------------ #
    #  2. Trend-residual Z on log price                                   #
    # ------------------------------------------------------------------ #
    logp = np.log(c.to_numpy())
    stats = trend_residual_stats(logp, cfg.z_window)
    z_now = float(stats["z"].iloc[-1])
    sigma_resid = float(stats["sigma_resid"].iloc[-1])
    drift_annual = float(stats["slope_daily"].iloc[-1]) * TRADING_DAYS_PER_YEAR
    if np.isnan(z_now):
        z_now = 0.0  # degenerate window (e.g. constant prices) — at trend

    # ------------------------------------------------------------------ #
    #  3. ATR% and vol_factor                                             #
    # ------------------------------------------------------------------ #
    atr_series = ta.atr(high, low, c, length=cfg.atr_period)
    atr_pct_series: pd.Series = atr_series / c

    atr_pct_now = float(atr_pct_series.iloc[-1])
    # Median (not mean) so a single vol spike doesn't inflate the baseline.
    atr_pct_baseline = float(
        atr_pct_series.rolling(cfg.atr_baseline_window).median().iloc[-1]
    )

    if atr_pct_now <= 0 or np.isnan(atr_pct_now) or np.isnan(atr_pct_baseline):
        vol_factor = 1.0  # can't compute; fail safe toward allowing normal DCA
    else:
        vol_factor = _clamp(atr_pct_baseline / atr_pct_now, cfg.g_vol_min, 1.0)

    # ------------------------------------------------------------------ #
    #  4. ADX                                                             #
    # ------------------------------------------------------------------ #
    adx_df = ta.adx(high, low, c, length=cfg.adx_period)
    adx_col = f"ADX_{cfg.adx_period}"
    if adx_col not in adx_df.columns:
        adx_col = next((col for col in adx_df.columns if col.startswith("ADX")), None)
    adx = float(adx_df[adx_col].iloc[-1]) if adx_col else float("nan")

    # ------------------------------------------------------------------ #
    #  5. Falling-knife guard                                             #
    # ------------------------------------------------------------------ #
    price_now = float(c.iloc[-1])
    trend_strong_down = bool(
        price_now < sma_now
        and sma_slope < 0
        and not np.isnan(adx)
        and adx > cfg.adx_trend_thresh
    )

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
        z=z_now,
        trend_drift_annual=drift_annual,
        sigma_resid=sigma_resid,
        atr_pct=atr_pct_now,
        atr_pct_baseline=atr_pct_baseline,
        vol_factor=vol_factor,
        adx=adx,
        trend_strong_down=trend_strong_down,
        as_of=as_of,
        data_ok=True,
    )


def _fail_safe(symbol: str) -> TechnicalState:
    """Return a safe TechnicalState that signals data failure to the fusion layer.

    data_ok=False causes fusion to degrade to M=1.0 (standard DCA).
    All numeric fields are set to neutral / non-triggering values.
    """
    return TechnicalState(
        symbol=symbol,
        price=float("nan"),
        sma200=float("nan"),
        sma_slope=0.0,
        z=0.0,                # neutral — no signal
        trend_drift_annual=0.0,
        sigma_resid=float("nan"),
        atr_pct=float("nan"),
        atr_pct_baseline=float("nan"),
        vol_factor=1.0,       # neutral — don't damp aggression
        adx=0.0,
        trend_strong_down=False,
        as_of=None,
        data_ok=False,
    )
