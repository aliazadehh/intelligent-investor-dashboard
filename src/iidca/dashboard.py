"""Streamlit dashboard — multi-asset watchlist + self-explaining detail view.

Run with:
  streamlit run src/iidca/dashboard.py

The dashboard is READ-ONLY with respect to business logic: it renders
persisted snapshots (DuckDB) and cached price history (Parquet). Charts
that need indicator series recompute them with the same pure tactical
functions the engine uses — one source of truth for the math.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

# Make 'iidca' importable when running directly (e.g. on Streamlit Cloud)
_src = Path(__file__).parent.parent
if str(_src) not in sys.path:
    sys.path.insert(0, str(_src))

import pandas as pd
import streamlit as st

from iidca.config import load_config
from iidca.engines.tactical import current_trend_channel, tactical_series
from iidca.providers.market import read_cached_ohlcv
from iidca.storage import (
    get_asset_history,
    get_latest_assets,
    get_latest_macro,
    get_macro_history,
    get_watchlist,
    watchlist_add,
    watchlist_remove,
)
from iidca.ui import charts, readings
from iidca.ui.components import banner, chip, hero_m, inject_css, metric_card, section
from iidca.ui.theme import COLORS

st.set_page_config(
    page_title="Intelligent Investor — DCA Dashboard",
    page_icon="📊",
    layout="wide",
)
inject_css()

# ---------------------------------------------------------------------------
# Secrets / config
# ---------------------------------------------------------------------------

_fred_key_ok = bool(os.environ.get("FRED_API_KEY"))
_db_url_ok = bool(os.environ.get("DATABASE_URL"))
try:
    if "FRED_API_KEY" in st.secrets:
        os.environ["FRED_API_KEY"] = str(st.secrets["FRED_API_KEY"])
        _fred_key_ok = True
    if "DATABASE_URL" in st.secrets:
        os.environ["DATABASE_URL"] = str(st.secrets["DATABASE_URL"])
        _db_url_ok = True
except Exception:
    pass

cfg = load_config()


def _run_cycle(symbols: list[str] | None = None):
    from iidca.run import run_cycle  # noqa: PLC0415
    return run_cycle(cfg, symbols=symbols)


def _is_stale(macro_row: dict | None) -> bool:
    if macro_row is None:
        return True
    if not macro_row.get("data_ok", True):
        return True  # previous run had errors — always retry
    run_ts = macro_row.get("run_ts")
    if run_ts is None:
        return True
    if hasattr(run_ts, "to_pydatetime"):
        run_ts = run_ts.to_pydatetime()
    if isinstance(run_ts, str):
        run_ts = datetime.fromisoformat(run_ts)
    if run_ts.tzinfo is None:
        run_ts = run_ts.replace(tzinfo=UTC)
    return datetime.now(tz=UTC) - run_ts > timedelta(hours=cfg.refresh_hours)


# ---------------------------------------------------------------------------
# Sidebar — watchlist management + refresh + data status
# ---------------------------------------------------------------------------

watchlist = get_watchlist(seed=cfg.watchlist)

with st.sidebar:
    st.title("⚙️ Controls")

    if not _db_url_ok:
        st.error(
            "**DATABASE_URL not configured.** Add your Supabase connection string "
            "to `.streamlit/secrets.toml` (local) or Space secrets (HF Spaces). "
            "See `.streamlit/secrets.toml.example` for the format."
        )
    if not _fred_key_ok:
        st.error(
            "**FRED_API_KEY not configured.** Add it to your environment or "
            "Streamlit secrets — get a free key at "
            "https://fredaccount.stlouisfed.org/apikeys"
        )

    new_symbol = st.text_input(
        "Add asset by ticker",
        placeholder="e.g. SPY, VWCE.DE, BTC-USD",
        help="Anything your data providers know: equities, ETFs, crypto pairs.",
    )
    if st.button("➕ Add to watchlist", width="stretch") and new_symbol.strip():
        sym = new_symbol.strip().upper()
        if sym in watchlist:
            st.info(f"{sym} is already tracked.")
        else:
            with st.spinner(f"Fetching {sym}…"):
                watchlist_add(sym)
                try:
                    result = _run_cycle(symbols=[sym])
                    res = result.assets.get(sym)
                    if res is None or res.source == "none":
                        watchlist_remove(sym)
                        st.error(f"Could not fetch data for {sym} from any provider.")
                    else:
                        st.session_state.selected_symbol = sym
                        st.rerun()
                except Exception as exc:
                    watchlist_remove(sym)
                    st.error(f"Could not add {sym}: {exc}")

    if watchlist:
        rm = st.selectbox("Remove asset", ["—"] + watchlist)
        if st.button("🗑 Remove", width="stretch") and rm != "—":
            watchlist_remove(rm)
            if st.session_state.get("selected_symbol") == rm:
                st.session_state.pop("selected_symbol", None)
            st.rerun()

    st.markdown("---")
    if st.button("🔄 Run fresh cycle", width="stretch"):
        with st.spinner("Running full cycle…"):
            try:
                _run_cycle()
                st.session_state.auto_refreshed = True
                st.success("Cycle complete.")
                st.rerun()
            except Exception as exc:
                st.error(f"Cycle failed: {exc}")

    st.caption(f"Providers: `{' → '.join(cfg.provider_chain)}`")
    st.caption(f"Auto-refresh every **{cfg.refresh_hours}h**")

# ---------------------------------------------------------------------------
# Auto-refresh when data is stale or new symbols lack snapshots
# ---------------------------------------------------------------------------

if "auto_refreshed" not in st.session_state:
    st.session_state.auto_refreshed = False

if not st.session_state.auto_refreshed:
    _macro_check = get_latest_macro()
    _assets_check = get_latest_assets()
    missing = [s for s in watchlist if s not in _assets_check]
    if _is_stale(_macro_check) or missing:
        st.session_state.auto_refreshed = True
        with st.spinner("Refreshing data…"):
            try:
                _run_cycle()
            except Exception as exc:
                st.session_state.auto_refreshed = False  # allow retry after fix
                st.warning(f"Auto-refresh failed: {exc}")
        st.rerun()

# ---------------------------------------------------------------------------
# Load latest state
# ---------------------------------------------------------------------------

macro = get_latest_macro()
assets = get_latest_assets()

if macro is None or not watchlist:
    st.title("📊 Intelligent Investor — DCA Dashboard")
    st.info("No data yet. Add an asset in the sidebar or click **Run fresh cycle**.")
    st.stop()

regime = macro.get("regime", "CAUTION")
H = float(macro.get("H", 0.5))
macro_ok = bool(macro.get("data_ok", True))
breakers = json.loads(macro.get("breakers") or "[]")
soft_breakers = json.loads(macro.get("soft_breakers") or "[]")
regime_color = {"EXPANSION": "green", "CAUTION": "amber", "STRESS": "red"}.get(regime, "amber")
regime_word = {"EXPANSION": "Expansion", "CAUTION": "Late-Cycle Caution",
               "STRESS": "Contraction / Systemic Stress"}.get(regime, regime)

# ---------------------------------------------------------------------------
# Header — global macro strip (shared across all assets)
# ---------------------------------------------------------------------------

h_reading = readings.read_h(H, regime, cfg.macro)
st.markdown(
    f"<h2 style='margin-bottom:0.2rem;'>📊 Intelligent Investor "
    f"<span style='color:{COLORS[regime_color]};font-size:1.1rem;'>"
    f"&nbsp;● {regime_word}</span>&nbsp; "
    f"<span style='font-size:1.0rem;color:#8b95a7;'>H {H:.2f}</span></h2>",
    unsafe_allow_html=True,
)
st.caption(
    f"Macro as of {macro.get('as_of', '—')} · last run "
    f"{pd.Timestamp(macro.get('run_ts')).strftime('%Y-%m-%d %H:%M UTC') if macro.get('run_ts') is not None else '—'}"
)

if not macro_ok:
    banner("Macro data failure — decisions degraded to the M = 1.0 fail-safe. "
           "Check FRED connectivity / API key.", "red")
for b in breakers:
    banner(f"Circuit breaker active: {b} — regime forced to STRESS; "
           f"no buying above baseline is permitted.", "red")
for b in soft_breakers:
    banner(f"Soft breaker active: {b} — regime capped at CAUTION. The yield curve "
           f"re-steepened after a deep inversion, historically the proximate "
           f"pre-recession window.", "amber")

# ---------------------------------------------------------------------------
# Watchlist table
# ---------------------------------------------------------------------------

section("Watchlist")

rows = []
spark_map: dict[str, list[float]] = {}
for sym in watchlist:
    snap = assets.get(sym)
    cached = read_cached_ohlcv(sym)
    closes = cached["Close"].iloc[-30:].tolist() if cached is not None else []
    spark_map[sym] = closes
    chg = None
    if cached is not None and len(cached) >= 2:
        chg = float(cached["Close"].iloc[-1] / cached["Close"].iloc[-2] - 1)
    if snap is None:
        rows.append({"Symbol": sym, "Price": None, "1d %": chg, "Trend (30d)": closes,
                     "Z vs trend": None, "M": None, "Signal": "no data", "Data": "—"})
        continue
    fresh = bool(snap.get("data_fresh", True))
    ok = bool(snap.get("data_ok", True))
    data_note = str(snap.get("data_source") or "—") + ("" if fresh else " (stale)")
    rows.append({
        "Symbol": sym,
        "Price": float(snap["price"]) if snap.get("price") == snap.get("price") else None,
        "1d %": chg,
        "Trend (30d)": closes,
        "Z vs trend": float(snap["z"]) if snap.get("z") is not None else None,
        "M": float(snap["M"]) if snap.get("M") is not None else None,
        "Signal": str(snap.get("label", "—")) + ("" if ok else " ⚠"),
        "Data": data_note,
    })

wl_df = pd.DataFrame(rows)
event = st.dataframe(
    wl_df,
    width="stretch",
    hide_index=True,
    on_select="rerun",
    selection_mode="single-row",
    column_config={
        "Price": st.column_config.NumberColumn(format="%.2f"),
        "1d %": st.column_config.NumberColumn(format="percent"),
        "Trend (30d)": st.column_config.LineChartColumn(width="small"),
        "Z vs trend": st.column_config.NumberColumn(
            format="%+.2f",
            help="Trend-residual Z: σ above/below the asset's own fitted trend. Negative = cheap.",
        ),
        "M": st.column_config.NumberColumn(format="%.2f×"),
    },
)

selected = st.session_state.get("selected_symbol", watchlist[0])
if event.selection and event.selection.rows:
    selected = wl_df.iloc[event.selection.rows[0]]["Symbol"]
    st.session_state.selected_symbol = selected
if selected not in watchlist:
    selected = watchlist[0]

# ---------------------------------------------------------------------------
# Detail view — selected asset
# ---------------------------------------------------------------------------

snap = assets.get(selected)
st.markdown("---")

if snap is None:
    st.info(f"No snapshot for {selected} yet — run a fresh cycle.")
    st.stop()

price = float(snap.get("price") or float("nan"))
z = float(snap.get("z") or 0.0)
M = float(snap.get("M") or 1.0)
label = str(snap.get("label", "Standard"))
drift = float(snap.get("trend_drift_annual") or 0.0)
vol_factor = float(snap.get("vol_factor") or 1.0)
atr_pct = float(snap.get("atr_pct") or float("nan"))
atr_base = float(snap.get("atr_pct_baseline") or float("nan"))
adx = float(snap.get("adx") or 0.0)
sma = float(snap.get("sma200") or float("nan"))
sma_slope = float(snap.get("sma_slope") or 0.0)
strong_down = bool(snap.get("trend_strong_down", False))
asset_ok = bool(snap.get("data_ok", True))
as_of = str(snap.get("as_of", "—"))
try:
    rationale = json.loads(snap.get("rationale") or "{}")
except (TypeError, json.JSONDecodeError):
    rationale = {}

st.markdown(
    f"<h3 style='margin-bottom:0;'>{selected} "
    f"<span style='color:#8b95a7;font-size:1.05rem;'>"
    f"{price:,.2f}</span> {chip(str(snap.get('data_source') or '—'), 'neutral')}</h3>",
    unsafe_allow_html=True,
)

if not asset_ok:
    banner(f"Data for {selected} is incomplete or stale — the decision below is the "
           f"M = 1.0 fail-safe, not a live signal.", "red")

# --- Decision hero + derivation waterfall -----------------------------------
col_hero, col_wf = st.columns([2, 3])
cap = float(rationale.get("cap", cfg.fusion.m_cap_exp))
m_reading = readings.read_m(M, label, regime, cap, cfg.fusion)
instruction = (f"Invest about {round(M * 100)}% of your normal amount this period."
               if round(M * 100) != 100 else "Invest your normal amount this period.")
with col_hero:
    hero_m(M, m_reading, instruction, as_of)
with col_wf:
    waterfall = rationale.get("waterfall") or []
    if waterfall:
        st.plotly_chart(charts.m_waterfall(waterfall, M), width="stretch",
                        config={"displayModeBar": False})
        st.caption(
            "Baseline 1.0× · **Valuation tilt** from how far price sits from its own "
            "trend (Z) · **Macro gate** scales buying by macro health (and amplifies "
            "defence when weak) · **Guards** damp aggression in strong downtrends / "
            "vol spikes · **Cap & floor** apply the regime ceiling and the never-stop floor."
        )

# --- Macro engine (global) ---------------------------------------------------
section("Macro engine — global, shared by all assets")

mc1, mc2 = st.columns([3, 2])
with mc1:
    st.plotly_chart(
        charts.h_contribution(
            {"sahm": float(macro.get("s_sahm") or 0), "curve": float(macro.get("s_curve") or 0),
             "stress": float(macro.get("s_stress") or 0)},
            {"sahm": cfg.macro.w_sahm, "curve": cfg.macro.w_curve, "stress": cfg.macro.w_stress},
            H, (cfg.macro.regime_caution, cfg.macro.regime_expansion),
        ),
        width="stretch", config={"displayModeBar": False},
    )
with mc2:
    metric_card("Macro health H", f"{H:.2f}", h_reading, sub="of 1.00")

mcol1, mcol2, mcol3 = st.columns(3)
sahm_v = float(macro.get("sahm") or 0.0)
curve_v = float(macro.get("t10y2y") or 0.0)
stress_source = str(macro.get("stress_source") or "STLFSI4")
stress_v = float(macro.get("stlfsi") if stress_source == "STLFSI4" and macro.get("stlfsi") == macro.get("stlfsi")
                 else macro.get("nfci") or 0.0)

with mcol1:
    metric_card("Sahm rule (unemployment rise)", f"{sahm_v:.2f}",
                readings.read_sahm(sahm_v, cfg.macro), sub="pp from low")
    st.plotly_chart(charts.zone_band(
        sahm_v,
        [(0.0, cfg.macro.sahm_trigger / 2, "green", "healthy"),
         (cfg.macro.sahm_trigger / 2, cfg.macro.sahm_trigger, "amber", "softening"),
         (cfg.macro.sahm_trigger, max(1.0, sahm_v + 0.2), "red", "recession")],
    ), width="stretch", config={"displayModeBar": False})
with mcol2:
    metric_card("Yield curve 10Y−2Y", f"{curve_v:+.2f}",
                readings.read_curve(curve_v, bool(soft_breakers), cfg.macro), sub="pp")
    st.plotly_chart(charts.zone_band(
        curve_v,
        [(-1.5, cfg.macro.curve_inv_floor, "red", "deep inv."),
         (cfg.macro.curve_inv_floor, 0.0, "amber", "inverted"),
         (0.0, cfg.macro.curve_healthy_ref, "neutral", "flat"),
         (cfg.macro.curve_healthy_ref, 2.5, "green", "healthy")],
    ), width="stretch", config={"displayModeBar": False})
with mcol3:
    metric_card(f"Financial stress ({stress_source})", f"{stress_v:+.2f}",
                readings.read_stress(stress_v, stress_source, cfg.macro), sub="index")
    st.plotly_chart(charts.zone_band(
        stress_v,
        [(-2.0, 0.0, "green", "calm"),
         (0.0, cfg.macro.stress_crisis, "amber", "tightening"),
         (cfg.macro.stress_crisis, 3.0, "red", "crisis")],
    ), width="stretch", config={"displayModeBar": False})

# --- Valuation — trend-residual Z --------------------------------------------
section(f"Valuation — where {selected} sits vs its own trend")

cached = read_cached_ohlcv(selected)
vcol1, vcol2 = st.columns([3, 2])
with vcol1:
    if cached is not None and len(cached) >= cfg.tactical.z_window:
        channel = current_trend_channel(cached, cfg.tactical)
        hist_view = cached.iloc[-(cfg.tactical.z_window * 2):]
        st.plotly_chart(
            charts.trend_channel_chart(
                pd.DataFrame({"close": hist_view["Close"]}, index=hist_view.index),
                channel, selected, z),
            width="stretch", config={"displayModeBar": False},
        )
    else:
        st.info("Not enough cached price history to draw the trend channel yet.")
with vcol2:
    metric_card("Trend-residual Z", f"{z:+.2f}σ", readings.read_z(z, selected, drift))
    st.plotly_chart(charts.zone_band(
        z,
        [(-4.0, -1.0, "green", "cheap vs trend"),
         (-1.0, 1.0, "neutral", "on trend"),
         (1.0, 4.0, "red", "stretched")],
        value_label=f"{z:+.1f}σ",
    ), width="stretch", config={"displayModeBar": False})
    if cached is not None and len(cached) >= cfg.tactical.z_window + 30:
        tser = tactical_series(cached, cfg.tactical)
        st.plotly_chart(charts.z_history_chart(tser["z"].dropna().iloc[-252:]),
                        width="stretch", config={"displayModeBar": False})

# --- Risk guards --------------------------------------------------------------
section("Risk guards — applied only to buying above baseline")

gcol1, gcol2 = st.columns(2)
with gcol1:
    metric_card(
        "Volatility (ATR% vs 1-yr norm)",
        f"{atr_pct:.2%}" if atr_pct == atr_pct else "—",
        readings.read_vol(atr_pct, atr_base, vol_factor, cfg.tactical),
        sub=f"guard ×{vol_factor:.2f}",
    )
with gcol2:
    metric_card(
        f"Trend (price vs SMA{cfg.tactical.sma_window}, ADX)",
        f"{((price / sma) - 1):+.1%}" if sma == sma and sma > 0 else "—",
        readings.read_trend(price, sma, sma_slope, adx, strong_down,
                            cfg.tactical, cfg.fusion.g_trend_down),
        sub="vs SMA",
    )

# --- History -------------------------------------------------------------------
section("History")

asset_hist = get_asset_history(selected, limit=120)
macro_hist = get_macro_history(limit=240)
if asset_hist:
    a = pd.DataFrame(asset_hist)[["run_ts", "M"]]
    m = pd.DataFrame(macro_hist)[["run_ts", "H"]]
    merged = pd.merge_asof(
        a.sort_values("run_ts"), m.sort_values("run_ts"),
        on="run_ts", direction="nearest",
    ).set_index("run_ts")
    st.plotly_chart(charts.history_chart(merged), width="stretch",
                    config={"displayModeBar": False})
    with st.expander("Raw snapshots"):
        st.dataframe(pd.DataFrame(asset_hist), width="stretch")
