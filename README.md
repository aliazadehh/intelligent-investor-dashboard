# Intelligent Investor — Quantitative DCA Dashboard

A personal decision-support tool that tells you **how much to invest this month** by combining a macro health score with a statistical measure of how cheap or expensive your target asset is right now.

One number in, one number out: **M** (the DCA multiplier). M = 1.0 means invest your normal amount. M = 0.5 means invest half. M = 1.75 means invest 75% extra. You read it, you decide, the system never trades for you.

---

## Table of Contents

1. [The Big Idea](#1-the-big-idea)
2. [Data Sources](#2-data-sources)
3. [Zone 2A — Macro Engine](#3-zone-2a--macro-engine)
4. [Zone 2B — Tactical Engine](#4-zone-2b--tactical-engine)
5. [Zone 3 — Fusion: How M is Calculated](#5-zone-3--fusion-how-m-is-calculated)
6. [Zone 1 — Global System Status](#6-zone-1--global-system-status)
7. [Reading the Dashboard](#7-reading-the-dashboard)
8. [Worked Examples](#8-worked-examples)
9. [Configuration Reference](#9-configuration-reference)
10. [Running the Project](#10-running-the-project)

---

## 1. The Big Idea

Dollar-cost averaging (DCA) means investing a fixed amount on a fixed schedule regardless of price. It is simple and effective. But it ignores two things that matter:

1. **Is the macro environment healthy?** Buying aggressively into a recession is "catching a falling knife" — the asset is cheap because the economy is collapsing, not because it is temporarily oversold.
2. **Is the asset actually cheap right now?** Sometimes the macro is fine but the asset has just had a huge run-up and is statistically expensive. Deploying extra in that moment is poor timing.

This system addresses both. It modulates *how much* you deploy each period — not whether you DCA at all. You never fully stop (that defeats the strategy), but you deploy more when conditions are genuinely favourable and less when they are not.

The core principle, stated plainly:

> **A cheap price only justifies buying more if the macro regime is healthy enough that the cheapness is noise around a stable mean — not a signal that the mean itself is collapsing.**

---

## 2. Data Sources

### Macro data — FRED (Federal Reserve Economic Data)

Fetched via the `fredapi` library (free API key required from fred.stlouisfed.org). Cached locally as Parquet files so monthly runs are fast and offline-resilient.

| Series ID | What it measures | Frequency | Why it matters |
|---|---|---|---|
| `SAHMREALTIME` | **Sahm Rule indicator** — rise in unemployment from its recent low, in percentage points | Monthly | The most reliable real-time recession signal. When it hits 0.50, a recession has historically already started. |
| `T10Y2Y` | **Yield curve** — 10-year minus 2-year Treasury yield spread, in percentage points | Daily | Negative (inverted) = bond market expects future rate cuts = recession concern. Positive = normal. |
| `STLFSI4` | **St. Louis Fed Financial Stress Index** — composite of interest rates, yield spreads, and other financial indicators | Weekly | Mean of 0. Above 0 = above-average stress. Above 1.5 = acute liquidity event (crisis-level). |
| `NFCI` | **Chicago Fed National Financial Conditions Index** | Weekly | Secondary cross-check on financial conditions. Above 0 = tighter than average. |

> ⚠️ **Important:** The series is `STLFSI4`, not `STLFSI3`. STLFSI3 was discontinued in October 2022 and frozen — using it would silently feed 3-year-stale data.

### Market data — price history

Fetched via `yfinance` by default (configurable). Returns daily OHLCV data with **adjusted close** — meaning splits and dividends are already factored in. The system needs at least ~300 trading days of history to warm up the 200-day indicators.

**Provider fallback order** (set in config):
- `yfinance` — default, free, scrapes Yahoo Finance
- `stooq` — free, no API key, uses pandas-datareader
- `tiingo` — paid but cheap, deep history, requires `TIINGO_API_KEY`
- `tradingview` — push-based via a local webhook server

---

## 3. Zone 2A — Macro Engine

**File:** `src/iidca/engines/macro.py`
**Output:** `MacroState` — a health score plus a regime label

### Step 1: Convert each raw reading to a sub-score

Each FRED reading is mapped to a score between 0 and 1, where **1 = healthy** and **0 = maximum stress**. The mapping is a clamped linear ramp between two reference points.

---

**Sahm sub-score** (`s_sahm`)

```
s_sahm = clamp( (0.50 − sahm_reading) / 0.50,  min=0, max=1 )
```

| Sahm reading | s_sahm | Interpretation |
|---|---|---|
| 0.00 | 1.00 | No unemployment rise — fully healthy |
| 0.25 | 0.50 | Moderate deterioration |
| 0.50 | 0.00 | Recession signal fired — fully stressed |
| > 0.50 | 0.00 (clamped) | Deep recession |

---

**Yield curve sub-score** (`s_curve`)

```
s_curve = clamp( (spread − (−0.5)) / (1.0 − (−0.5)),  min=0, max=1 )
         = clamp( (spread + 0.5) / 1.5,  min=0, max=1 )
```

| Spread (10Y−2Y) | s_curve | Interpretation |
|---|---|---|
| +1.0 pp or above | 1.00 | Healthy, normal yield curve |
| 0.0 pp | 0.33 | Flat — warning sign |
| −0.5 pp | 0.00 | Maximum inversion — fully stressed |
| < −0.5 pp | 0.00 (clamped) | Deep inversion |

---

**Financial stress sub-score** (`s_stress`)

```
s_stress = clamp( (1.0 − stlfsi4) / (1.0 − (−1.0)),  min=0, max=1 )
          = clamp( (1.0 − stlfsi4) / 2.0,  min=0, max=1 )
```

| STLFSI4 | s_stress | Interpretation |
|---|---|---|
| −1.0 or below | 1.00 | Unusually calm financial conditions |
| 0.0 | 0.50 | Average conditions |
| +1.0 | 0.00 | High stress — fully stressed sub-score |
| > +1.0 | 0.00 (clamped) | Crisis territory |

---

### Step 2: Blend into the Macro Health Score H

```
H = 0.40 × s_sahm  +  0.25 × s_curve  +  0.35 × s_stress
```

H is always in [0, 1]. Weights reflect confidence in each signal:
- Sahm (40%) — highest weight because it is the most reliable *realised* recession signal
- STLFSI4 (35%) — coincident and forward-looking on liquidity conditions
- Yield curve (25%) — useful but noisy, with variable lead/lag

---

### Step 3: Circuit breakers

Two hard overrides that **can only make the regime worse, never better**:

| Condition | Breaker name | Effect |
|---|---|---|
| `sahm_reading ≥ 0.50` | `SAHM_RECESSION` | Forces regime to STRESS regardless of H |
| `stlfsi4 ≥ 1.5` | `FIN_STRESS_CRISIS` | Forces regime to STRESS regardless of H |

---

### Step 4: Assign regime from H

If no circuit breaker fired:

| H range | Regime | Meaning |
|---|---|---|
| H ≥ 0.66 | **EXPANSION** | Healthy macro — aggression permitted |
| 0.40 ≤ H < 0.66 | **CAUTION** | Deteriorating — reduce aggression cap |
| H < 0.40 | **STRESS** | Collapsing — no aggressive buying, ever |

---

### MacroState output

```
H          = 0.766          ← composite health score
regime     = "EXPANSION"    ← discrete label
subscores  = {sahm: 0.94, curve: 0.72, stress: 0.61}
breakers   = []             ← none fired
as_of      = 2026-05-29
data_ok    = True
```

---

## 4. Zone 2B — Tactical Engine

**File:** `src/iidca/engines/tactical.py`
**Output:** `TechnicalState` — where the asset sits relative to its own history

All calculations use the **adjusted close** price.

---

### 200-day Simple Moving Average (SMA200)

```
SMA200[t] = average of the last 200 daily closing prices
```

**SMA slope** = `SMA200[today] − SMA200[20 days ago]`
- Positive slope = the trend is rising
- Negative slope = the trend is falling

The SMA200 is the canonical "is this asset in a long-term uptrend?" indicator.

---

### Z-score

```
Z = (ln(price_today) − mean(ln(price), last 200 days))
    ─────────────────────────────────────────────────────
         std_dev(ln(price), last 200 days)
```

Log price is used (not raw price) so the dispersion measure is scale-stable — a $500 stock and a $50 stock are comparable.

| Z | Interpretation |
|---|---|
| +3 or above | Extremely expensive — 3 standard deviations above the 200-day mean |
| +1 to +2 | Above average — moderately expensive |
| 0 | Exactly at the 200-day mean — fair value by this measure |
| −1 to −2 | Below average — moderately cheap |
| −3 or below | Extremely cheap — deep statistical discount |

**Current reading: Z = +3.114** — QQQ is ~3 standard deviations above its 200-day mean. Historically stretched.

---

### ATR% — Volatility measure

```
ATR(14) = average of the daily true range (High − Low adjusted for gaps) over 14 days
ATR%    = ATR(14) / Close
```

ATR% expresses volatility as a fraction of price, making it comparable over time.

**vol_factor** scales down aggression when volatility is abnormally high:

```
atr_baseline = rolling 252-day median of ATR%   (≈ 1 year of "normal" vol)
vol_factor   = clamp( atr_baseline / atr_pct_today,  min=0.40, max=1.0 )
```

- Normal vol today → `vol_factor ≈ 1.0` (no dampening)
- Spike vol today → `vol_factor < 1.0` (aggression damped)
- Minimum is 0.40 — even in a crash, you still deploy 40% of the intended extra

Rationale: knives fall fastest in high-volatility regimes. Sizing to current volatility is a classic risk-management technique.

---

### ADX — Trend strength

```
ADX(14) = directional movement index, averaged over 14 days
```

ADX measures **how strong** a trend is, not which direction it points.

| ADX | Interpretation |
|---|---|
| < 20 | Weak or no trend — ranging market |
| 20–25 | Emerging trend |
| 25–40 | **Strong trend** (threshold used: 25) |
| > 40 | Very strong trend |

**Current reading: ADX = 37.8** — a strong, established trend.

---

### RSI — Momentum

```
RSI(14) = 100 − (100 / (1 + avg_gains_14 / avg_losses_14))
```

| RSI | Interpretation |
|---|---|
| > 70 | Overbought — momentum extended to the upside |
| 50–70 | Bullish momentum |
| 30–50 | Bearish momentum |
| < 30 | Oversold — momentum extended to the downside |

**Current reading: RSI = 77.2** — overbought. Confirms the Z-score reading.

---

### trend_strong_down (boolean guard)

```
trend_strong_down = True   if:
    price < SMA200          (asset is below its long-term average)
    AND SMA200_slope < 0    (the average itself is falling)
    AND ADX > 25            (the downtrend is strong, not noise)
```

This is the most direct "falling knife" detector. If all three conditions are true, the system applies a hard multiplier of 0.30 to any aggressive buying intent.

---

### TechnicalState output

```
symbol           = "QQQ"
price            = ~530 (example)
sma200           = ~480 (example)
sma_slope        = positive
z                = 3.114     ← 3+ std devs above mean = expensive
atr_pct          = 0.0140    ← 1.4% daily range = normal vol
vol_factor       = 1.0       ← vol is normal, no dampening
adx              = 37.8      ← strong trend
rsi              = 77.2      ← overbought
trend_strong_down = False    ← price > SMA200, trend is UP not down
```

---

## 5. Zone 3 — Fusion: How M is Calculated

**File:** `src/iidca/engines/fusion.py`
**Output:** `Decision` — the DCA multiplier M and all intermediate values

This is where macro and tactical come together. Four sequential steps.

---

### Step 1: Technical tilt T(Z)

```
T(Z) = 1 + 0.75 × (−tanh(Z / 1.5))
```

`tanh` (hyperbolic tangent) is an S-curve that saturates at ±1. This means an extreme reading like Z = −5 doesn't produce an absurdly large multiplier — the response saturates.

| Z | T(Z) | Raw interpretation |
|---|---|---|
| +3.1 (current) | ≈ 0.38 | Very expensive → want to invest much less |
| +1.5 | ≈ 0.73 | Somewhat expensive → want slightly less |
| 0.0 | 1.00 | Fair value → standard DCA |
| −1.5 | ≈ 1.27 | Somewhat cheap → want slightly more |
| −3.0 | ≈ 1.62 | Very cheap → want much more |

**d = T − 1** is the deviation from baseline:
- Positive d = want more than baseline (cheap asset)
- Negative d = want less than baseline (expensive asset)

For the current reading: `T ≈ 0.38`, so `d ≈ −0.62` — a strong defensive tilt.

---

### Step 2: Asymmetric macro gate

This step is the heart of the falling-knife prevention.

**If d ≥ 0 (cheap asset — considering buying more):**
```
d_eff = d × H
```
The aggressive intent is *multiplied by* the macro health score. A cheap asset in a collapsing macro (H → 0) produces zero extra buying. A cheap asset in a healthy macro (H → 1) passes through fully.

**If d < 0 (expensive asset — wanting to reduce):**
```
d_eff = d × (1 + 1.0 × (1 − H))
```
Defensive intent is *amplified* when the macro is weak. An expensive asset in a deteriorating macro triggers more caution, not less.

Then: `M_core = 1 + d_eff`

**For the current case:** d = −0.62 (defensive), H = 0.766
```
d_eff = −0.62 × (1 + 1.0 × (1 − 0.766))
       = −0.62 × 1.234
       = −0.765
M_core = 1 − 0.765 = 0.235
```

---

### Step 3: Tactical guards (on aggressive portion only)

Guards only apply to any *extra buying* — they never amplify the defensive signal.

```
agg  = max(M_core − 1, 0)    ← the aggressive portion above baseline
def_ = max(1 − M_core, 0)    ← the defensive portion below baseline

g_trend = 0.30  if trend_strong_down else 1.0
g_vol   = vol_factor   (0.40 to 1.0)

agg_guarded = agg × g_trend × g_vol
M_pre = 1 + agg_guarded − def_
```

Here M_core = 0.235, so `agg = 0` (no aggressive portion), `def_ = 0.765`.
Guards have nothing to apply to. `M_pre = 1 + 0 − 0.765 = 0.235`.

---

### Step 4: Regime cap + global clamp

```
regime_cap = EXPANSION → 2.00
             CAUTION   → 1.25
             STRESS    → 1.00   ← in STRESS you can never lever above baseline

M = clamp( clamp(M_pre, 0.25, regime_cap), 0.25, 2.00 )
```

The regime caps are a hard safety rail. In STRESS regime, M ≤ 1.0 by construction — no matter how cheap the asset looks, you cannot lever into a structurally collapsing market.

**Current:** M_pre = 0.235, clamped up to global floor of **0.25**.

---

### Allocation labels

| M range | Label | Instruction |
|---|---|---|
| M < 0.60 | **Defensive** | Reduce this period's DCA (~50%). Expensive and/or deteriorating regime. |
| 0.60 ≤ M < 0.90 | **Cautious** | Slightly below standard (~75%). |
| 0.90 ≤ M ≤ 1.10 | **Standard** | Standard DCA (100%). No strong signal. |
| 1.10 < M ≤ 1.40 | **Opportunistic** | Add modestly (~125%). Cheap and regime-supportive. |
| M > 1.40 | **Aggressive** | Deploy extra (regime-capped). Deep value in a healthy regime. |

---

### Full current calculation summary

| Step | Value | Note |
|---|---|---|
| Z | +3.114 | QQQ is 3.1 std devs above 200d mean |
| T(Z) | 0.38 | Technical tilt — strong defensive |
| d | −0.62 | Deviation from baseline |
| H | 0.766 | Macro is healthy |
| d_eff | −0.765 | Defensive signal amplified slightly (macro < 1) |
| M_core | 0.235 | Pre-guard multiplier |
| Guards | n/a | No aggressive portion to guard |
| M_pre | 0.235 | Pre-clamp |
| Regime cap | 2.00 | EXPANSION — not binding here |
| **M** | **0.25** | Clamped up to global floor |
| **Label** | **Defensive** | Invest ~50% of your normal DCA amount |

---

## 6. Zone 1 — Global System Status

A single synthesised string derived from the macro regime + STLFSI4 liquidity reading:

```
regime_label = Expansion | Late-Cycle Caution | Contraction / Systemic Stress

liquidity    = "Stable Liquidity"    if STLFSI4 < 0
               "Tightening Liquidity" if 0 ≤ STLFSI4 < 1.5
               "Stressed Liquidity"  if STLFSI4 ≥ 1.5

status = "{regime_label} & {liquidity}"
```

If any circuit breaker fired, its name is appended with a ⚠️.

**Current:** `Expansion & Stable Liquidity` — healthy macro, loose financial conditions.

---

## 7. Reading the Dashboard

```
streamlit run src/iidca/dashboard.py
```

| Element | What to look at |
|---|---|
| **Zone 1 colour** | Green = go, Yellow = caution, Red = stress |
| **H gauge** | How healthy is the macro? Below 0.40 is danger zone |
| **M multiplier** | Your action: multiply your planned investment by this number |
| **Z-score** | How cheap/expensive is the asset? Below −1.5 = interesting, above +2 = stretched |
| **ADX** | Is there a strong trend? Above 25 = yes; combined with price < SMA200 = falling knife risk |
| **RSI** | Is momentum extended? Above 70 = overbought, below 30 = oversold |
| **History chart** | Are M and H trending up or down over recent months? |

---

## 8. Worked Examples

### Example A — Healthy dip (best case for buying more)
```
H = 0.90  (strong expansion)
Z = −2.0  (asset is 2 std devs below mean — genuinely cheap)
trend_strong_down = False
vol_factor = 1.0

T(Z) = 1 + 0.75 × (−tanh(−2.0/1.5)) ≈ 1.59
d = +0.59   (aggressive)
d_eff = 0.59 × 0.90 = 0.53   (macro gate passes most of it)
M_core = 1.53
Guards: agg = 0.53 × 1.0 × 1.0 = 0.53
M_pre = 1.53
Regime cap: 2.00  →  M = 1.53 → Aggressive ✅
```

### Example B — The falling knife (the whole point of the system)
```
H = 0.15  (STRESS regime — economy collapsing)
Z = −2.0  (asset looks cheap, same as above)
trend_strong_down = True  →  g_trend = 0.30
vol_factor = 0.50         →  g_vol = 0.50

T(Z) ≈ 1.59 (same)
d = +0.59   (aggressive)
d_eff = 0.59 × 0.15 = 0.089   ← macro gate kills most of the aggression
M_core = 1.089
agg = 0.089 × 0.30 × 0.50 = 0.013   ← guards kill what's left
M_pre = 1.013
Regime cap: STRESS → 1.00  →  M = 1.00 → Standard ✅
```

Same Z-score, completely different outcome. Cheap in a collapsing macro = Standard DCA, not Aggressive.

### Example C — Expensive in late cycle
```
H = 0.45  (CAUTION)
Z = +1.8  (expensive)
d = −0.55  (defensive)
d_eff = −0.55 × (1 + 1.0 × 0.55) = −0.85
M_core = 0.15  →  clamped to M_min = 0.25 → Defensive
```

### Example D — No signal
```
H = 0.70, Z = 0.0  →  T = 1.0, d = 0, M = 1.00 → Standard
```

---

## 9. Configuration Reference

**File:** `config/default.toml`

All thresholds are parameters, not hard-coded truths. Edit this file to adjust your personal risk appetite. Every snapshot stores a `config_hash` so you can always trace which parameter set produced a given recommendation.

```toml
target_symbol = "QQQ"        # what asset to analyse
market_provider = "yfinance" # yfinance | stooq | tiingo | tradingview

[macro]
sahm_trigger      = 0.50   # Sahm ≥ this → SAHM_RECESSION breaker
stress_crisis     = 1.50   # STLFSI4 ≥ this → FIN_STRESS_CRISIS breaker
w_sahm            = 0.40   # weight in H blend
w_curve           = 0.25
w_stress          = 0.35
regime_expansion  = 0.66   # H ≥ this → EXPANSION
regime_caution    = 0.40   # H ≥ this → CAUTION (else STRESS)

[tactical]
z_window          = 200    # days for Z-score rolling window
sma_window        = 200    # days for SMA
adx_trend_thresh  = 25.0   # ADX above this = strong trend
g_vol_min         = 0.40   # floor on vol dampening factor
staleness_days    = 5      # max age of last price bar before data_ok=False

[fusion]
alpha        = 0.75   # max tilt amplitude (M range: [0.25, 1.75] pre-gate)
beta         = 1.5    # tanh saturation (higher = more linear response to Z)
lam          = 1.0    # defensive amplifier strength when macro weak
g_trend_down = 0.30   # multiplier on aggression when falling-knife detected
m_cap_exp    = 2.00   # max M in EXPANSION
m_cap_caution= 1.25   # max M in CAUTION
m_cap_stress = 1.00   # max M in STRESS (never lever into collapsing tape)
m_min        = 0.25   # global floor — never fully stop DCA
m_max        = 2.00   # global ceiling
```

---

## 10. Running the Project

### Prerequisites

```bash
# Free FRED API key — get at fred.stlouisfed.org/docs/api/api_key.html
export FRED_API_KEY=your_32_char_key

# Optional — for Telegram alerts
export TELEGRAM_BOT_TOKEN=your_bot_token
export TELEGRAM_CHAT_ID=your_chat_id
```

### Install

```bash
uv sync
```

### Run a cycle (headless)

```bash
uv run python -m iidca.run             # print signal to terminal
uv run python -m iidca.run --alert     # also push Telegram/email
uv run python -m iidca.run --symbol SPY --verbose
```

### Dashboard

```bash
uv run streamlit run src/iidca/dashboard.py
```

### Tests

```bash
uv run pytest -m "not network"   # fast, no internet required
uv run pytest                    # all tests including live data checks
```

### Automation (GitHub Actions)

Push to GitHub, add `FRED_API_KEY` / `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` as repository secrets. The workflow in `.github/workflows/monthly.yml` runs automatically on the 1st of each month at 08:00 UTC and uploads the DuckDB snapshot as an artifact.

---

## Architecture at a glance

```
FRED API                    yfinance / stooq / tiingo
    │                               │
    ▼                               ▼
fred.py (cached Parquet)    yfinance_provider.py etc.
    │                               │
    ▼                               ▼
macro.py ──── MacroState    tactical.py ── TechnicalState
         H, regime,                  Z, ATR%, ADX,
         sub-scores,                 RSI, vol_factor,
         circuit breakers            trend_strong_down
              │                           │
              └──────────┬────────────────┘
                         ▼
                    fusion.py
               4-step calculation → M
                         │
              ┌──────────┼──────────┐
              ▼          ▼          ▼
         storage.py  dashboard.py  alerts.py
         DuckDB      Streamlit     Telegram/SMTP
         snapshot    3-zone UI     monthly push
```

Every arrow is a pure function over typed dataclasses — each stage can be tested in isolation with fixture data, independent of live network calls.
