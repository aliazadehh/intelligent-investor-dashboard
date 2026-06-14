---
title: Intelligent Investor DCA Dashboard
emoji: 📊
colorFrom: blue
colorTo: green
sdk: streamlit
sdk_version: 1.46.0
app_file: src/iidca/dashboard.py
pinned: false
---

# Intelligent Investor — Quantitative DCA Dashboard

A personal decision-support tool that tells you **how much to invest this
period, per asset**, by combining one global macro health score with a
per-asset statistical measure of how far price sits from its own trend.

One number out per asset: **M** (the DCA multiplier). M = 1.0 means invest
your normal amount. M = 0.5 means invest half. M = 1.75 means invest 75%
extra. You read it, you decide — the system never trades for you.

Track any ticker your data providers know (equities, ETFs, crypto pairs):
the **macro engine is global and shared**, the **valuation/tactical engine
runs per asset**.

> Model changes and their rationale are logged in [DECISIONS.md](DECISIONS.md).
> Every threshold in this document is a parameter in `config/default.toml`.

---

## Table of Contents

1. [The Big Idea](#1-the-big-idea)
2. [Data Sources](#2-data-sources)
3. [Macro Engine (global)](#3-macro-engine-global)
4. [Tactical Engine (per asset)](#4-tactical-engine-per-asset)
5. [Fusion: How M is Calculated](#5-fusion-how-m-is-calculated)
6. [Reading the Dashboard](#6-reading-the-dashboard)
7. [Worked Examples](#7-worked-examples)
8. [Configuration Reference](#8-configuration-reference)
9. [Running the Project](#9-running-the-project)

---

## 1. The Big Idea

Dollar-cost averaging (DCA) means investing a fixed amount on a fixed
schedule regardless of price. It is simple and effective. But it ignores two
things that matter:

1. **Is the macro environment healthy?** Buying aggressively into a recession
   is "catching a falling knife" — the asset is cheap because the economy is
   collapsing, not because it is temporarily oversold.
2. **Is the asset actually cheap right now?** Sometimes the macro is fine but
   the asset has overshot its own trend. Deploying extra in that moment is
   poor timing.

This system modulates *how much* you deploy each period — never whether you
DCA at all. You never fully stop (that defeats the strategy), but you deploy
more when conditions are genuinely favourable and less when they are not.

The core principle:

> **A cheap price only justifies buying more if the macro regime is healthy
> enough that the cheapness is noise around a stable trend — not a signal
> that the trend itself is collapsing.**

And its structural corollary (asymmetric caution): defensiveness is always
permitted; aggression must be *earned* — by macro health, a non-collapsing
trend, and a normal-volatility environment, all at once.

---

## 2. Data Sources

### Macro data — FRED (Federal Reserve Economic Data)

Fetched via `fredapi` (free API key) with a raw-HTTP fallback, cached locally
as Parquet so runs are fast and offline-resilient.

| Series ID | What it measures | Role |
|---|---|---|
| `SAHMREALTIME` | **Sahm Rule** — rise in unemployment from its recent low (pp) | Coincident/realized. The most reliable real-time "recession has begun" signal (fires at 0.50). |
| `T10Y2Y` | **Yield curve** — 10Y minus 2Y Treasury spread (pp) | Leading, noisy. The *level* feeds H; the *dis-inversion pattern* feeds a soft breaker (see §3). |
| `STLFSI4` | **St. Louis Fed Financial Stress Index** | Coincident liquidity/stress. Mean 0; ≥ 1.5 = crisis. |
| `NFCI` | **Chicago Fed Financial Conditions Index** | Fallback source for the stress pillar if STLFSI4 is unavailable — no single point of failure. |

> ⚠️ The series is `STLFSI4` — STLFSI3 was discontinued in October 2022 and
> would silently feed stale data.

### Market data — price history

Daily OHLCV with **adjusted close** (splits/dividends factored in), fetched
through an ordered, config-driven **provider chain** — first source that
returns valid data wins:

- `yfinance` — default; equities, ETFs, crypto pairs (e.g. `BTC-USD`)
- `stooq` — free, no key (US equities/ETFs)
- `tiingo` — paid, requires `TIINGO_API_KEY`
- last-good **Parquet cache** — final fallback if every provider fails; the
  decision then degrades to the M = 1.0 fail-safe but numbers stay visible

Half-formed live bars (NaN prices) are dropped at ingestion; stale data
(older than `staleness_days`) is rejected.

---

## 3. Macro Engine (global)

**File:** `src/iidca/engines/macro.py` · **Output:** `MacroState` — health
score H, regime label, breakers. Computed **once per cycle**, shared by all
assets.

### Step 1: Convert each raw reading to a sub-score

Each reading maps to [0, 1] (1 = healthy) via a clamped linear ramp:

```
s_sahm   = clamp( (0.50 − sahm) / 0.50 ,            0, 1 )   # 0.50 = recession trigger
s_curve  = clamp( (spread + 0.5) / 1.5 ,            0, 1 )   # −0.5 = max inversion, +1.0 = healthy
s_stress = clamp( (1.0 − stlfsi) / 2.0 ,            0, 1 )   # −1 calm … +1 stressed
```

If STLFSI4 is unavailable, the stress pillar falls back to NFCI with its own
ramp (different scale); the snapshot records which source was used.

### Step 2: Blend into the Macro Health Score H

```
H = 0.40·s_sahm + 0.25·s_curve + 0.35·s_stress        ∈ [0, 1]
```

Weights reflect signal quality: Sahm (realized, reliable) > stress
(coincident) > curve level (leading but noisy, with variable lead time).

### Step 3: Breakers — hard and soft

Breakers can only *worsen* the regime, never improve it.

| Condition | Breaker | Effect |
|---|---|---|
| `sahm ≥ 0.50` | `SAHM_RECESSION` (hard) | Forces STRESS |
| `stlfsi ≥ 1.5` | `FIN_STRESS_CRISIS` (hard) | Forces STRESS |
| Curve was ≤ −0.10pp within 365d and is now > 0 | `CURVE_DISINVERSION` (soft) | Caps regime at CAUTION |

The dis-inversion breaker exists because historically the **re-steepening
after** a deep inversion — not the inversion itself — is the proximate
pre-recession window, exactly when the level score recovers and looks healthy
again. It is recomputed from the series each run and self-expires.

### Step 4: Assign regime from H

| H range | Regime | Meaning |
|---|---|---|
| H ≥ 0.66 | **EXPANSION** | Healthy macro — aggression permitted |
| 0.40 ≤ H < 0.66 | **CAUTION** | Deteriorating — aggression capped at 1.25× |
| H < 0.40 | **STRESS** | Collapsing — never above baseline |

---

## 4. Tactical Engine (per asset)

**File:** `src/iidca/engines/tactical.py` · **Output:** `TechnicalState`.
All metrics are **dimensionless and self-normalizing**, so one parameter set
generalizes across equities, ETFs, and crypto.

### Trend-residual Z-score — the valuation signal

```
fit:  ln P_k ≈ a + b·k        (OLS over the last 200 trading days)
Z  =  (ln P_today − fit_today) / σ_residuals
```

Z measures how far price sits **above or below the asset's own fitted trend
channel**, in units of that channel's typical width. Negative = below trend
= cheap relative to the asset's recent path.

Why not distance from the 200-day *mean*? Because on any steadily trending
asset that statistic converges to a constant **+1.73σ regardless of the
growth rate** — it flags trends, not value, and would permanently starve
strong-trend assets (see DECISIONS.md #1). The residual Z is mean-zero on a
pure trend by construction. The dashboard draws the fitted channel (±1σ/±2σ)
on the price chart so the number is never abstract.

| Z | Reading |
|---|---|
| ≤ −2 | Very cheap vs trend — the setup the system sizes up into |
| −2 … −1 | Cheap vs trend |
| −1 … +1 | On trend — no signal |
| +1 … +2 | Stretched above trend |
| ≥ +2 | Very stretched — trims the period's buy |

### Volatility guard — ATR% vs the asset's own norm

```
ATR%        = ATR(14) / Close
baseline    = rolling 252-day median of ATR%
vol_factor  = clamp( baseline / ATR%_today , 0.40, 1.0 )
```

Vol spikes scale down *extra* buying (knives fall fastest in high-vol
regimes); the 0.40 floor means dampening never fully cancels a planned
opportunity. Self-normalizing: a 4%-daily-range crypto and a 1% ETF are each
measured against their own baseline.

### Falling-knife guard

```
trend_strong_down = price < SMA200  AND  SMA200 falling  AND  ADX(14) > 25
```

All three at once = a strong, established downtrend. Any extra buying is cut
to 30% (`g_trend_down`). ADX is displayed on the dashboard *in this role* —
it is a guard input, not a standalone signal.

---

## 5. Fusion: How M is Calculated

**File:** `src/iidca/engines/fusion.py` · **Output:** `Decision` with the
full derivation (rendered as a waterfall chart in the dashboard).

### Step 1 — Technical tilt T(Z)

```
T(Z) = 1 + 0.75 · (−tanh(Z / 1.5))          d = T − 1
```

Saturating S-curve: Z = 0 → T = 1; deep discounts approach 1.75, extreme
overshoots approach 0.25, and a flash-crash Z = −5 cannot produce an absurd
multiplier.

### Step 2 — Asymmetric macro gate

```
g = clamp( (H − 0.40) / (0.75 − 0.40), 0, 1 )      # the gate ramp

d ≥ 0 (cheap):      d_eff = d · g                  # aggression earned by health
d < 0 (expensive):  d_eff = d · (1 + 1.0·(1 − g))  # defence amplified when weak

m_core = 1 + d_eff
```

H ≥ 0.75 passes aggression through **fully** (v1 multiplied by raw H, which
silently taxed even perfect regimes); at or below the STRESS boundary (0.40)
aggression is zero. Defensive tilts are never gated — only amplified.

### Step 3 — Tactical guards (aggressive portion only)

```
agg = max(m_core − 1, 0)        def = max(1 − m_core, 0)
agg_guarded = agg × g_trend × g_vol          # guards never touch `def`
m_pre = 1 + agg_guarded − def
```

### Step 4 — Regime cap + global clamp

```
cap: EXPANSION → 2.00 | CAUTION → 1.25 | STRESS → 1.00
M = clamp( min(m_pre, cap), 0.25, 2.00 )
```

In STRESS, M ≤ 1.0 *by construction* — no cheapness can lever you into a
collapsing market. The 0.25 floor means DCA never fully stops.

### Allocation labels (boundaries in `[fusion.labels]`)

| M range | Label |
|---|---|
| < 0.60 | **Defensive** |
| 0.60 – 0.90 | **Cautious** |
| 0.90 – 1.10 | **Standard** |
| 1.10 – 1.40 | **Opportunistic** |
| > 1.40 | **Aggressive** |

The instruction text is generated from M itself ("Invest about 42% of your
normal amount this period"), so words and numbers can't contradict.

---

## 6. Reading the Dashboard

```
uv run streamlit run src/iidca/dashboard.py
```

- **Header strip** — global regime + H. Banner alerts when any breaker is
  active or data has failed (fail-safe M = 1.0 is always flagged, never
  silent).
- **Watchlist** — every tracked asset: price, 30-day sparkline,
  trend-residual Z, M, signal label, data source. Click a row to open it.
  Add/remove tickers in the sidebar; adding fetches, validates, and scores
  immediately.
- **Decision hero + waterfall** — M with its plain-language instruction next
  to a waterfall chart: baseline 1.0 → valuation tilt → macro gate → trend
  guard → vol guard → cap & floor. Nothing about M is a black box.
- **Macro section** — H as a stacked contribution bar (weight × sub-score per
  pillar, regime thresholds marked) and one card per indicator: value, zone
  band with marker, and a sentence on what it means right now.
- **Valuation section** — the price chart with the fitted trend channel
  (±1σ/±2σ) and the current Z marked on it; Z history below.
- **Risk guards** — volatility vs the asset's own one-year norm, and the
  trend/falling-knife state, each with the guard value it contributes.
- **History** — M and H across runs.

Every metric card shows **value + current zone + plain-language reading for
this asset** — no bare numbers with tooltips.

---

## 7. Worked Examples

### A — Healthy dip (best case for buying more)
```
H = 0.90 (EXPANSION, g = 1.0)   Z = −2.0   no guards triggered
T(−2.0) ≈ 1.65 → d = +0.65 → d_eff = 0.65·1.0 = 0.65
M = 1.65 → Aggressive ✅  (v1 would have taxed this to ≈1.59 via raw H)
```

### B — The falling knife (the whole point of the system)
```
H = 0.15 (STRESS, g = 0)   Z = −2.0 (same cheapness!)
trend_strong_down = True, vol_factor = 0.50
d_eff = 0.65 · 0 = 0 → m_pre = 1.0 → STRESS cap 1.00
M = 1.00 → Standard ✅   Same Z, opposite macro, no extra buying.
```

### C — Expensive in late cycle
```
H = 0.50 (CAUTION, g = 0.29)   Z = +1.8
T ≈ 0.37 → d = −0.63 → d_eff = −0.63·(1 + 0.71) = −1.08
m_pre = −0.08 → floor → M = 0.25 → Defensive
```

### D — Strong uptrend, on trend (the case v1 got wrong)
```
H = 0.74 (EXPANSION)   asset rising ~25%/yr, price ON its trend → Z ≈ 0
T = 1.0 → d = 0 → M = 1.00 → Standard ✅
v1's mean-based Z read ≈ +1.7 here and cut the buy to ~0.45 permanently.
```

---

## 8. Configuration Reference

**File:** `config/default.toml` — every threshold is a parameter; snapshots
store a `config_hash` so any past recommendation is attributable to an exact
parameter set. Env vars override anything: `IIDCA_MACRO__STRESS_CRISIS=2.0`,
`IIDCA_WATCHLIST='["SPY","BTC-USD"]'`.

Key parameters (see the file for the full annotated list):

```toml
watchlist      = ["QQQ"]                 # seed; manage in the dashboard after
provider_chain = ["yfinance", "stooq"]   # ordered fallback
lookback_days  = 1200

[macro]      # ramps, weights, regime thresholds, breaker triggers,
             # NFCI fallback ramp, dis-inversion watch parameters
[tactical]   # z_window, SMA/ADX/ATR windows, vol-guard floor, staleness
[fusion]     # alpha/beta tilt, gate_floor_h/gate_full_h ramp, lam,
             # g_trend_down, regime caps, m_min/m_max
[fusion.labels]  # allocation tier boundaries
```

---

## 9. Running the Project

### Prerequisites

```bash
export FRED_API_KEY=your_32_char_key     # free: fred.stlouisfed.org
export TELEGRAM_BOT_TOKEN=...            # optional, for alerts
export TELEGRAM_CHAT_ID=...
```

### Install & run

```bash
uv sync
uv run python -m iidca.run               # one cycle, all watchlist assets
uv run python -m iidca.run --symbol SPY  # single symbol
uv run python -m iidca.run --alert       # also push Telegram/email
uv run streamlit run src/iidca/dashboard.py
```

### Tests

```bash
uv run pytest -m "not network"           # fast, no internet (50 tests)
uv run pytest                            # all tests
```

### Automation (GitHub Actions)

Add `FRED_API_KEY` / `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` as repository
secrets; `.github/workflows/monthly.yml` runs on the 1st of each month and
uploads the DuckDB snapshot as an artifact.

---

## Architecture at a glance

```
FRED API (cached, dual-client)        provider chain (yfinance → stooq → cache)
        │                                          │  per asset
        ▼                                          ▼
macro.py ── MacroState (global)       tactical.py ── TechnicalState
   H, regime, sub-scores,                trend-residual Z, ATR%/vol_factor,
   hard + soft breakers                  ADX, falling-knife guard
        │                                          │
        └──────────────┬───────────────────────────┘
                       ▼
                   fusion.py  — tilt → gate → guards → caps → M (+ waterfall)
                       │
        ┌──────────────┼──────────────┐
        ▼              ▼              ▼
   storage.py     dashboard.py    alerts.py
   DuckDB:        Streamlit +     Telegram/SMTP
   macro/asset    Plotly, self-   per-cycle push
   snapshots,     explaining UI
   watchlist
```

Every arrow is a pure function over typed dataclasses — each stage is tested
in isolation with fixture data, independent of live network calls.
