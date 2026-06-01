"""T12 — Streamlit dashboard: 3-zone glanceable layout (§9.2, §8).

Run with:
  streamlit run src/iidca/dashboard.py

The dashboard is deliberately READ-ONLY with respect to business logic.
It only renders Decision / MacroState / TechnicalState from the latest
persisted snapshot.  A "Run fresh cycle" button triggers run_cycle().
"""

from __future__ import annotations

import json

import streamlit as st

from iidca.config import load_config
from iidca.storage import get_latest_snapshot, get_snapshot_history

st.set_page_config(
    page_title="Intelligent Investor — DCA Dashboard",
    page_icon="📊",
    layout="wide",
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_COLOR = {"green": "#2ecc71", "amber": "#f39c12", "red": "#e74c3c"}
_REGIME_EMOJI = {"EXPANSION": "🟢", "CAUTION": "🟡", "STRESS": "🔴"}


def _color_for(regime: str) -> str:
    m = {"EXPANSION": "green", "CAUTION": "amber", "STRESS": "red"}
    return _COLOR.get(m.get(regime, "amber"), "#f39c12")


# ---------------------------------------------------------------------------
# Sidebar: config + run
# ---------------------------------------------------------------------------

st.sidebar.title("⚙️ Controls")
cfg = load_config()

if st.sidebar.button("🔄 Run fresh cycle", use_container_width=True):
    with st.spinner("Running DCA cycle…"):
        try:
            from iidca.run import run_cycle  # noqa: PLC0415
            run_cycle(cfg)
            st.sidebar.success("Cycle complete.")
            st.rerun()
        except Exception as exc:
            st.sidebar.error(f"Cycle failed: {exc}")

st.sidebar.markdown("---")
st.sidebar.caption(f"Symbol: **{cfg.target_symbol}**")
st.sidebar.caption(f"Provider: `{cfg.market_provider}`")

# ---------------------------------------------------------------------------
# Load latest snapshot
# ---------------------------------------------------------------------------

snap = get_latest_snapshot()

if snap is None:
    st.title("📊 Intelligent Investor — DCA Dashboard")
    st.info("No snapshot found. Click **Run fresh cycle** to generate the first signal.")
    st.stop()

regime = snap.get("regime", "CAUTION")
H = snap.get("H", 0.5)
M = snap.get("M", 1.0)
label = snap.get("label", "Standard")
Z = snap.get("Z", 0.0)
atr_pct = snap.get("atr_pct", 0.0)
adx = snap.get("adx", 0.0)
rsi = snap.get("rsi", 50.0)
data_ok = snap.get("data_ok", True)
as_of = snap.get("as_of", "—")
breakers = json.loads(snap.get("breakers", "[]")) if snap.get("breakers") else []

# Rebuild zone1 status string
_liq_raw = snap.get("stlfsi", None)  # may not be stored; fallback
status_str = f"{regime.replace('EXPANSION','Expansion').replace('CAUTION','Late-Cycle Caution').replace('STRESS','Contraction / Systemic Stress')}"
if breakers:
    status_str += "  ⚠ " + ", ".join(breakers)

# ---------------------------------------------------------------------------
# Zone 1 — Global System Status (top, glanceable)
# ---------------------------------------------------------------------------

color_hex = _color_for(regime)
emoji = _REGIME_EMOJI.get(regime, "🟡")

st.markdown(
    f"<h1 style='color:{color_hex};'>{emoji} {status_str}</h1>",
    unsafe_allow_html=True,
)

col1, col2, col3 = st.columns(3)
col1.metric("Macro Health H", f"{H:.3f}", help="Composite macro health score ∈ [0, 1]")
col2.metric("DCA Multiplier M", f"{M:.3f}×", help="Recommended DCA size vs baseline")
col3.metric("Signal", label)

if not data_ok:
    st.error("⚠️ Data failure detected — M=1.0 fail-safe applied. Check logs.")

st.caption(f"As of {as_of}   ·   run_ts {snap.get('run_ts', '—')}")
st.divider()

# ---------------------------------------------------------------------------
# Zone 2 — Two Pillars (one scroll below)
# ---------------------------------------------------------------------------

left, right = st.columns(2)

with left:
    st.subheader("Column A — Macro Engine")
    sub_raw = snap.get("subscores", {})
    # subscores not stored directly in DuckDB; show H and regime detail
    st.metric("H (health score)", f"{H:.3f}")
    st.metric("Regime", regime)
    if breakers:
        st.warning("Circuit breakers fired: " + ", ".join(breakers))

with right:
    st.subheader("Column B — Tactical Engine")
    st.metric("Z-score (log-price)", f"{Z:.3f}", help="Negative = cheap vs history")
    c1, c2 = st.columns(2)
    c1.metric("ATR%", f"{atr_pct:.4f}", help="Volatility / price")
    c2.metric("ADX(14)", f"{adx:.1f}", help="Trend strength")
    c1.metric("RSI(14)", f"{rsi:.1f}", help="Momentum oscillator")

st.divider()

# ---------------------------------------------------------------------------
# Zone 3 — Actionable Signal Buffer
# ---------------------------------------------------------------------------

st.subheader("Zone 3 — DCA Recommendation")
label_colors = {
    "Defensive": "🔴", "Cautious": "🟠", "Standard": "⚪",
    "Opportunistic": "🟡", "Aggressive": "🟢",
}
st.markdown(
    f"### {label_colors.get(label, '')} {M:.3f}× — {label}",
)

# Show history chart
st.divider()
st.subheader("History")
history = get_snapshot_history(limit=24)
if history:
    import pandas as pd  # noqa: PLC0415
    df_hist = pd.DataFrame(history)[["run_ts", "M", "H", "regime"]].set_index("run_ts")
    st.line_chart(df_hist[["M", "H"]], use_container_width=True)
    with st.expander("Raw snapshot table"):
        st.dataframe(pd.DataFrame(history), use_container_width=True)
