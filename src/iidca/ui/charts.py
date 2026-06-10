"""Plotly figures for the dashboard.

All figures share a transparent dark style and are presentation-only —
inputs are engine outputs / config values, never recomputed judgments.
"""

from __future__ import annotations

import pandas as pd
import plotly.graph_objects as go

from iidca.ui.theme import COLORS, GRID, TEXT_DIM

_FONT = dict(family="Inter, -apple-system, sans-serif", size=12, color="#c4ccda")


def _base_layout(fig: go.Figure, height: int, **kwargs) -> go.Figure:
    fig.update_layout(
        height=height,
        margin=dict(l=8, r=8, t=28, b=8),
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font=_FONT,
        showlegend=False,
        **kwargs,
    )
    fig.update_xaxes(gridcolor=GRID, zerolinecolor=GRID)
    fig.update_yaxes(gridcolor=GRID, zerolinecolor=GRID)
    return fig


# ---------------------------------------------------------------------------
# M derivation — waterfall from baseline 1.0 to final M
# ---------------------------------------------------------------------------

def m_waterfall(waterfall: list, M: float) -> go.Figure:
    """Waterfall of the fusion steps. *waterfall* is the ordered
    (name, delta) list from Decision.rationale — baseline first."""
    names = [w[0] for w in waterfall] + ["Final M"]
    deltas = [w[1] for w in waterfall]
    measures = ["absolute"] + ["relative"] * (len(deltas) - 1) + ["total"]
    values = deltas + [0.0]

    fig = go.Figure(go.Waterfall(
        x=names,
        y=values,
        measure=measures,
        text=[f"{v:+.2f}" if m == "relative" else f"{(M if m == 'total' else v):.2f}"
              for v, m in zip(values, measures)],
        textposition="outside",
        connector=dict(line=dict(color=GRID, width=1)),
        increasing=dict(marker=dict(color=COLORS["green"])),
        decreasing=dict(marker=dict(color=COLORS["red"])),
        totals=dict(marker=dict(color=COLORS["blue"])),
    ))
    fig.add_hline(y=1.0, line_dash="dot", line_color=TEXT_DIM, line_width=1,
                  annotation_text="baseline 1.0×", annotation_font_size=10,
                  annotation_font_color=TEXT_DIM)
    _base_layout(fig, height=300, title=dict(
        text="How M was built", font=dict(size=13, color=TEXT_DIM), x=0))
    fig.update_yaxes(title=None, rangemode="tozero")
    return fig


# ---------------------------------------------------------------------------
# H composition — weighted contributions of the three pillars
# ---------------------------------------------------------------------------

def h_contribution(subscores: dict, weights: dict, H: float,
                   thresholds: tuple[float, float]) -> go.Figure:
    """Horizontal stacked bar: wᵢ·sᵢ contributions summing to H, with the
    untapped capacity (1 − H) in grey and regime thresholds marked."""
    pillars = [("Labour (Sahm)", "sahm"), ("Yield curve", "curve"), ("Fin. stress", "stress")]
    palette = [COLORS["blue"], "#a78bfa", "#2dd4bf"]

    fig = go.Figure()
    for (label, key), color in zip(pillars, palette):
        contrib = weights[key] * subscores.get(key, 0.0)
        fig.add_trace(go.Bar(
            y=[""], x=[contrib], name=label, orientation="h",
            marker_color=color,
            text=f"{label}<br>{contrib:.2f}", textposition="inside",
            insidetextanchor="middle", textfont=dict(size=11),
            hovertemplate=(f"{label}: score {subscores.get(key, 0.0):.2f} × "
                           f"weight {weights[key]:.2f} = {contrib:.2f}<extra></extra>"),
        ))
    fig.add_trace(go.Bar(
        y=[""], x=[max(1.0 - H, 0.0)], orientation="h",
        marker_color="rgba(148,163,184,0.15)",
        hovertemplate=f"Untapped capacity: {1 - H:.2f}<extra></extra>",
    ))

    caution, expansion = thresholds
    for x, lbl in ((caution, "stress↔caution"), (expansion, "caution↔expansion")):
        fig.add_vline(x=x, line_dash="dot", line_color=TEXT_DIM, line_width=1,
                      annotation_text=lbl, annotation_position="top",
                      annotation_font_size=9, annotation_font_color=TEXT_DIM)

    _base_layout(fig, height=120, barmode="stack", title=dict(
        text=f"H = {H:.2f} — weighted pillar contributions",
        font=dict(size=13, color=TEXT_DIM), x=0))
    fig.update_xaxes(range=[0, 1.02], tickformat=".1f")
    fig.update_yaxes(showticklabels=False)
    return fig


# ---------------------------------------------------------------------------
# Zone band — bullet-style strip showing where a value sits in its zones
# ---------------------------------------------------------------------------

def zone_band(value: float, segments: list[tuple[float, float, str, str]],
              value_label: str | None = None) -> go.Figure:
    """*segments* = [(lo, hi, color_key, zone_label), …] covering the axis.
    Draws translucent zone strips with a marker at *value*."""
    lo_axis = segments[0][0]
    hi_axis = segments[-1][1]
    fig = go.Figure()
    for lo, hi, color, label in segments:
        fig.add_shape(type="rect", x0=lo, x1=hi, y0=0, y1=1,
                      fillcolor=COLORS[color], opacity=0.16, line_width=0)
        fig.add_annotation(x=(lo + hi) / 2, y=1.25, text=label, showarrow=False,
                           font=dict(size=9, color=TEXT_DIM))

    v = min(max(value, lo_axis), hi_axis)
    fig.add_trace(go.Scatter(
        x=[v], y=[0.5], mode="markers+text",
        marker=dict(size=13, color="#ffffff", line=dict(width=2, color="#0e1117"),
                    symbol="diamond"),
        text=[value_label or f"{value:.2f}"], textposition="bottom center",
        textfont=dict(size=10, color="#ffffff"),
        hovertemplate=f"{value:.3f}<extra></extra>",
    ))
    _base_layout(fig, height=86)
    fig.update_xaxes(range=[lo_axis, hi_axis], showgrid=False, tickfont=dict(size=9))
    fig.update_yaxes(visible=False, range=[-0.5, 1.6])
    fig.update_layout(margin=dict(l=4, r=4, t=10, b=2))
    return fig


# ---------------------------------------------------------------------------
# Price + trend channel — the visual explanation of the residual Z
# ---------------------------------------------------------------------------

def trend_channel_chart(history: pd.DataFrame, channel: pd.DataFrame,
                        symbol: str, z: float) -> go.Figure:
    """Price history (log scale) with the current OLS trend channel overlaid.

    history: DataFrame with a 'close' column (longer context window)
    channel: output of tactical.current_trend_channel — fit, ±1σ, ±2σ
    """
    fig = go.Figure()

    # ±2σ band
    fig.add_trace(go.Scatter(x=channel.index, y=channel["hi2"], mode="lines",
                             line=dict(width=0), hoverinfo="skip"))
    fig.add_trace(go.Scatter(x=channel.index, y=channel["lo2"], mode="lines",
                             line=dict(width=0), fill="tonexty",
                             fillcolor="rgba(96,165,250,0.07)", hoverinfo="skip"))
    # ±1σ band
    fig.add_trace(go.Scatter(x=channel.index, y=channel["hi1"], mode="lines",
                             line=dict(width=0), hoverinfo="skip"))
    fig.add_trace(go.Scatter(x=channel.index, y=channel["lo1"], mode="lines",
                             line=dict(width=0), fill="tonexty",
                             fillcolor="rgba(96,165,250,0.13)", hoverinfo="skip"))
    # Trendline
    fig.add_trace(go.Scatter(x=channel.index, y=channel["fit"], mode="lines",
                             line=dict(width=1.4, dash="dash", color=COLORS["blue"]),
                             name="trend fit",
                             hovertemplate="trend: %{y:,.2f}<extra></extra>"))
    # Price
    fig.add_trace(go.Scatter(x=history.index, y=history["close"], mode="lines",
                             line=dict(width=1.6, color="#e6e9f0"), name=symbol,
                             hovertemplate="%{x|%Y-%m-%d}  %{y:,.2f}<extra></extra>"))
    # Last close marker
    last_x, last_y = history.index[-1], float(history["close"].iloc[-1])
    z_color = COLORS["green"] if z <= -1 else COLORS["red"] if z >= 1 else COLORS["neutral"]
    fig.add_trace(go.Scatter(
        x=[last_x], y=[last_y], mode="markers+text",
        marker=dict(size=9, color=z_color),
        text=[f"  Z {z:+.1f}σ"], textposition="middle right",
        textfont=dict(size=11, color=z_color),
        hovertemplate=f"last: %{{y:,.2f}} (Z {z:+.2f})<extra></extra>",
    ))

    _base_layout(fig, height=360, title=dict(
        text=f"{symbol} vs its fitted trend channel (±1σ, ±2σ) — Z measures this gap",
        font=dict(size=13, color=TEXT_DIM), x=0))
    fig.update_yaxes(type="log", tickformat=",.0f")
    return fig


# ---------------------------------------------------------------------------
# Z history with zone shading
# ---------------------------------------------------------------------------

def z_history_chart(z_series: pd.Series) -> go.Figure:
    fig = go.Figure()
    for lo, hi, color in ((-4, -1, "green"), (-1, 1, "neutral"), (1, 4, "red")):
        fig.add_hrect(y0=lo, y1=hi, fillcolor=COLORS[color], opacity=0.06, line_width=0)
    fig.add_hline(y=0, line_color=TEXT_DIM, line_width=1, line_dash="dot")
    fig.add_trace(go.Scatter(
        x=z_series.index, y=z_series.values, mode="lines",
        line=dict(width=1.5, color=COLORS["blue"]),
        hovertemplate="%{x|%Y-%m-%d}  Z %{y:+.2f}<extra></extra>",
    ))
    _base_layout(fig, height=200, title=dict(
        text="Trend-residual Z over time (− = cheap vs trend, + = stretched)",
        font=dict(size=13, color=TEXT_DIM), x=0))
    fig.update_yaxes(range=[-4, 4])
    return fig


# ---------------------------------------------------------------------------
# Decision history — M and H across runs
# ---------------------------------------------------------------------------

def history_chart(df: pd.DataFrame) -> go.Figure:
    """df: index run_ts, columns M and H (ascending)."""
    fig = go.Figure()
    fig.add_hline(y=1.0, line_dash="dot", line_color=TEXT_DIM, line_width=1)
    fig.add_trace(go.Scatter(x=df.index, y=df["M"], mode="lines+markers",
                             name="M (multiplier)", line=dict(color=COLORS["blue"], width=2),
                             marker=dict(size=5),
                             hovertemplate="%{x|%Y-%m-%d}  M %{y:.2f}×<extra></extra>"))
    fig.add_trace(go.Scatter(x=df.index, y=df["H"], mode="lines+markers",
                             name="H (macro health)", line=dict(color=COLORS["amber"], width=1.6),
                             marker=dict(size=4),
                             hovertemplate="%{x|%Y-%m-%d}  H %{y:.2f}<extra></extra>"))
    _base_layout(fig, height=240, title=dict(
        text="Decision history — M and H per run", font=dict(size=13, color=TEXT_DIM), x=0))
    fig.update_layout(showlegend=True, legend=dict(
        orientation="h", y=1.15, x=1, xanchor="right", font=dict(size=10)))
    return fig
