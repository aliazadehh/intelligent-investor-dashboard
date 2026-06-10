"""Small HTML components rendered through st.markdown."""

from __future__ import annotations

import html

import streamlit as st

from iidca.ui.readings import Reading
from iidca.ui.theme import COLORS, CSS, SOFT_BG


def inject_css() -> None:
    st.markdown(CSS, unsafe_allow_html=True)


def chip(text: str, color: str) -> str:
    """Return HTML for a small colored zone chip."""
    return (
        f'<span class="iidca-chip" style="color:{COLORS[color]};'
        f'background:{SOFT_BG[color]};">{html.escape(text)}</span>'
    )


def metric_card(title: str, value: str, reading: Reading, sub: str = "") -> None:
    """A self-explaining metric card: value + zone chip + plain-language reading."""
    sub_html = f" <small>{html.escape(sub)}</small>" if sub else ""
    st.markdown(
        f"""
        <div class="iidca-card">
          <div class="title"><span>{html.escape(title)}</span>{chip(reading.zone, reading.color)}</div>
          <div class="value">{html.escape(value)}{sub_html}</div>
          <div class="reading">{html.escape(reading.text)}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def banner(text: str, color: str) -> None:
    st.markdown(
        f"""
        <div class="iidca-banner" style="color:{COLORS[color]};
             background:{SOFT_BG[color]}; border-color:{COLORS[color]}33;">
          {html.escape(text)}
        </div>
        """,
        unsafe_allow_html=True,
    )


def section(title: str) -> None:
    st.markdown(f'<div class="iidca-section">{html.escape(title)}</div>',
                unsafe_allow_html=True)


def hero_m(M: float, reading: Reading, instruction: str, as_of: str) -> None:
    """The headline DCA multiplier card."""
    st.markdown(
        f"""
        <div class="iidca-hero">
          <div class="title" style="font-size:0.72rem;letter-spacing:0.08em;
               text-transform:uppercase;color:#8b95a7;margin-bottom:6px;">
               DCA multiplier · this period {chip(reading.zone, reading.color)}</div>
          <div class="big" style="color:{COLORS[reading.color]};">{M:.2f}×</div>
          <div class="sub"><b>{html.escape(instruction)}</b><br>{html.escape(reading.text)}</div>
          <div class="sub" style="font-size:0.78rem;">Data as of {html.escape(as_of)}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )
