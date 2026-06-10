"""Plain-language interpretation layer.

Every metric on the dashboard is rendered as value + zone + a one/two
sentence reading of what it means *right now for this asset*. All zone
boundaries come from config or engine outputs — nothing judgmental is
hard-coded here beyond wording.
"""

from __future__ import annotations

from dataclasses import dataclass

from iidca.config import FusionCfg, MacroCfg, TacticalCfg


@dataclass
class Reading:
    zone: str    # short zone/regime label shown as a chip
    color: str   # green | amber | red | blue | neutral
    text: str    # plain-language sentence(s)


# ---------------------------------------------------------------------------
# Macro indicators
# ---------------------------------------------------------------------------

def read_sahm(value: float, cfg: MacroCfg) -> Reading:
    half = cfg.sahm_trigger / 2
    if value >= cfg.sahm_trigger:
        return Reading("Recession signal", "red",
                       f"Unemployment has risen {value:.2f}pp from its recent low — at or past "
                       f"the {cfg.sahm_trigger:.2f}pp Sahm threshold. Historically this means a "
                       "recession has already begun. The recession circuit breaker is active.")
    if value >= half:
        return Reading("Deteriorating", "amber",
                       f"Unemployment is {value:.2f}pp above its recent low — labour conditions are "
                       f"softening but below the {cfg.sahm_trigger:.2f}pp recession threshold.")
    return Reading("Healthy", "green",
                   f"Unemployment is only {value:.2f}pp above its 12-month low — no meaningful "
                   "labour-market deterioration, the most reliable all-clear this system has.")


def read_curve(spread: float, disinv_active: bool, cfg: MacroCfg) -> Reading:
    if disinv_active:
        return Reading("Dis-inversion watch", "amber",
                       f"The 10Y−2Y spread ({spread:+.2f}pp) has re-steepened after a deep inversion "
                       "within the past year. Historically this re-steepening — not the inversion "
                       "itself — is the proximate pre-recession signal, so the regime is capped at "
                       "Caution while the watch is active.")
    if spread <= cfg.curve_inv_floor:
        return Reading("Deeply inverted", "red",
                       f"The 10Y−2Y spread is {spread:+.2f}pp — a deep inversion. The bond market is "
                       "pricing significant future rate cuts, a classic (early) recession warning.")
    if spread < 0:
        return Reading("Inverted", "amber",
                       f"The 10Y−2Y spread is {spread:+.2f}pp — inverted. A warning sign, but one that "
                       "typically leads recessions by a year or more.")
    if spread < cfg.curve_healthy_ref:
        return Reading("Flat-to-normal", "amber",
                       f"The 10Y−2Y spread is {spread:+.2f}pp — positive but below the "
                       f"{cfg.curve_healthy_ref:+.1f}pp healthy reference. Neutral-ish.")
    return Reading("Healthy slope", "green",
                   f"The 10Y−2Y spread is {spread:+.2f}pp — a normally sloped curve, consistent with "
                   "an economy expected to keep growing.")


def read_stress(value: float, source: str, cfg: MacroCfg) -> Reading:
    src_note = "" if source == "STLFSI4" else f" (via fallback source {source})"
    if value >= cfg.stress_crisis:
        return Reading("Crisis", "red",
                       f"Financial stress reads {value:+.2f}{src_note} — at crisis level "
                       f"(≥ {cfg.stress_crisis:+.1f}). The liquidity circuit breaker is active.")
    if value >= 0:
        return Reading("Tightening", "amber",
                       f"Financial stress reads {value:+.2f}{src_note} — above its long-run average. "
                       "Funding conditions are tighter than normal.")
    return Reading("Calm", "green",
                   f"Financial stress reads {value:+.2f}{src_note} — below the long-run average of 0. "
                   "Markets are liquid and funding is easy.")


def read_h(H: float, regime: str, cfg: MacroCfg) -> Reading:
    color = {"EXPANSION": "green", "CAUTION": "amber", "STRESS": "red"}[regime]
    if regime == "EXPANSION":
        text = (f"Composite macro health is {H:.2f} — above the {cfg.regime_expansion:.2f} expansion "
                "threshold. Buying extra into dips is permitted at full strength.")
    elif regime == "CAUTION":
        text = (f"Composite macro health is {H:.2f} — between the {cfg.regime_caution:.2f} and "
                f"{cfg.regime_expansion:.2f} thresholds. Aggressive buying is capped and defensive "
                "tilts are amplified.")
    else:
        text = (f"Composite macro health is {H:.2f} — below the {cfg.regime_caution:.2f} stress "
                "threshold. No buying above baseline is allowed, no matter how cheap anything looks.")
    return Reading(regime.title(), color, text)


# ---------------------------------------------------------------------------
# Per-asset metrics
# ---------------------------------------------------------------------------

def read_z(z: float, symbol: str, drift_annual: float) -> Reading:
    trend_word = "rising" if drift_annual >= 0 else "falling"
    trend_note = f"its own {trend_word} ~{abs(drift_annual):.0%}/yr trend path"
    if z <= -2:
        return Reading("Very cheap vs trend", "green",
                       f"{symbol} trades {abs(z):.1f}σ below {trend_note} — a deep statistical "
                       "discount to its recent trajectory. This is the setup the system sizes up "
                       "into, if the macro allows it.")
    if z <= -1:
        return Reading("Cheap vs trend", "green",
                       f"{symbol} trades {abs(z):.1f}σ below {trend_note} — moderately cheap "
                       "relative to its own path.")
    if z < 1:
        return Reading("On trend", "neutral",
                       f"{symbol} is within ±1σ of {trend_note} — no meaningful valuation signal "
                       "either way.")
    if z < 2:
        return Reading("Stretched vs trend", "amber",
                       f"{symbol} trades {z:.1f}σ above {trend_note} — moderately extended even "
                       "after accounting for the trend itself.")
    return Reading("Very stretched", "red",
                   f"{symbol} trades {z:.1f}σ above {trend_note} — a large overshoot of its own "
                   "trend channel. The system trims this period's buy.")


def read_vol(atr_pct: float, baseline: float, vol_factor: float, cfg: TacticalCfg) -> Reading:
    if baseline != baseline or atr_pct != atr_pct:  # NaN
        return Reading("Unavailable", "neutral", "Volatility could not be computed; no dampening applied.")
    ratio = atr_pct / baseline if baseline > 0 else 1.0
    if vol_factor >= 0.95:
        return Reading("Normal", "green",
                       f"Daily range is {atr_pct:.1%} of price vs a {baseline:.1%} one-year norm — "
                       "volatility is normal for this asset, so no dampening is applied.")
    if vol_factor > cfg.g_vol_min:
        return Reading("Elevated", "amber",
                       f"Daily range is {atr_pct:.1%} of price — {ratio:.1f}× this asset's one-year "
                       f"norm. Any extra buying is scaled to {vol_factor:.0%} (knives fall fastest "
                       "in high-vol regimes).")
    return Reading("Spiking", "red",
                   f"Daily range is {atr_pct:.1%} of price — {ratio:.1f}× this asset's one-year norm. "
                   f"Extra buying is damped to the {cfg.g_vol_min:.0%} floor.")


def read_trend(price: float, sma: float, slope: float, adx: float,
               strong_down: bool, cfg: TacticalCfg, g_trend_down: float) -> Reading:
    above = price >= sma
    rising = slope >= 0
    strong = adx > cfg.adx_trend_thresh
    strength = f"ADX {adx:.0f} ({'strong' if strong else 'weak/ranging'})"
    if strong_down:
        return Reading("Falling knife", "red",
                       f"Price is below its long-term average, that average is falling, and the "
                       f"downtrend is strong ({strength}). The falling-knife guard cuts any extra "
                       f"buying to {g_trend_down:.0%} — wait for the trend to stabilise.")
    if above and rising:
        return Reading("Uptrend", "green",
                       f"Price is above its rising long-term average — established uptrend, {strength}. "
                       "No trend guard applies.")
    if not above and not rising:
        return Reading("Downtrend", "amber",
                       f"Price is below its falling long-term average ({strength}), but the downtrend "
                       "isn't strong enough to trigger the falling-knife guard.")
    return Reading("Transitioning", "neutral",
                   f"Price and its long-term average disagree on direction ({strength}) — "
                   "the trend is turning; no guard applies.")


def read_m(M: float, label: str, regime: str, cap: float, cfg: FusionCfg) -> Reading:
    color = {"Defensive": "red", "Cautious": "amber", "Standard": "neutral",
             "Opportunistic": "blue", "Aggressive": "green"}.get(label.split(" ")[0], "neutral")
    extra = ""
    if M >= cap - 1e-9 and cap < cfg.m_max:
        extra = f" The {regime.title()} regime cap ({cap:.2f}×) is binding."
    if abs(M - cfg.m_min) < 1e-9:
        extra = f" The global floor ({cfg.m_min:.2f}×) is binding — DCA never fully stops."
    return Reading(label, color,
                   f"Multiply this period's planned contribution by {M:.2f}.{extra}")
