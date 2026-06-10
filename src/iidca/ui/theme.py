"""Design tokens + global CSS for the dashboard (dark, Koyfin/Linear-ish)."""

from __future__ import annotations

# Semantic palette — keyed by the color names engines emit.
COLORS: dict[str, str] = {
    "green": "#34d399",
    "amber": "#fbbf24",
    "red": "#f87171",
    "blue": "#60a5fa",
    "neutral": "#94a3b8",
}

# Soft (translucent) backgrounds for chips/cards per semantic color.
SOFT_BG: dict[str, str] = {
    "green": "rgba(52, 211, 153, 0.13)",
    "amber": "rgba(251, 191, 36, 0.13)",
    "red": "rgba(248, 113, 113, 0.13)",
    "blue": "rgba(96, 165, 250, 0.13)",
    "neutral": "rgba(148, 163, 184, 0.13)",
}

BG_CARD = "#161b26"
BORDER = "#2a3242"
TEXT_DIM = "#8b95a7"
TEXT_MAIN = "#e6e9f0"
GRID = "rgba(148, 163, 184, 0.12)"

CSS = f"""
<style>
  .block-container {{ padding-top: 2.2rem; max-width: 1250px; }}

  .iidca-card {{
    background: {BG_CARD};
    border: 1px solid {BORDER};
    border-radius: 12px;
    padding: 14px 16px 12px 16px;
    margin-bottom: 10px;
    height: 100%;
  }}
  .iidca-card .title {{
    font-size: 0.72rem; letter-spacing: 0.08em; text-transform: uppercase;
    color: {TEXT_DIM}; margin-bottom: 4px; display: flex;
    justify-content: space-between; align-items: center; gap: 8px;
  }}
  .iidca-card .value {{
    font-size: 1.55rem; font-weight: 650; color: {TEXT_MAIN};
    font-variant-numeric: tabular-nums; line-height: 1.15;
  }}
  .iidca-card .value small {{ font-size: 0.85rem; color: {TEXT_DIM}; font-weight: 500; }}
  .iidca-card .reading {{
    font-size: 0.84rem; color: {TEXT_DIM}; margin-top: 6px; line-height: 1.45;
  }}

  .iidca-chip {{
    display: inline-block; padding: 2px 10px; border-radius: 999px;
    font-size: 0.72rem; font-weight: 600; letter-spacing: 0.02em;
    white-space: nowrap;
  }}

  .iidca-hero {{
    background: {BG_CARD};
    border: 1px solid {BORDER};
    border-radius: 14px;
    padding: 20px 22px;
  }}
  .iidca-hero .big {{
    font-size: 3.0rem; font-weight: 700; line-height: 1.05;
    font-variant-numeric: tabular-nums;
  }}
  .iidca-hero .sub {{ font-size: 0.95rem; color: {TEXT_DIM}; margin-top: 8px; line-height: 1.5; }}

  .iidca-banner {{
    border-radius: 10px; padding: 10px 14px; font-size: 0.88rem;
    margin: 6px 0 10px 0; border: 1px solid;
  }}

  .iidca-section {{
    font-size: 0.78rem; letter-spacing: 0.1em; text-transform: uppercase;
    color: {TEXT_DIM}; margin: 18px 0 8px 0; font-weight: 600;
  }}

  div[data-testid="stMetric"] {{
    background: {BG_CARD}; border: 1px solid {BORDER};
    border-radius: 12px; padding: 12px 16px;
  }}
</style>
"""
