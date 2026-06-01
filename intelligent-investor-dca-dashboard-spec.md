# Intelligent Investor — Quantitative DCA Dashboard

**Project specification & build brief (v1)**
**Status:** Ready for implementation · **Audience:** autonomous agent team (lead + sub-agents)
**Deliverable for this phase:** a runnable v1 ("initial product, so I can see how it looks") — scriptable backend + minimal Streamlit dashboard + monthly alert path.

---

## 0. How to use this document (read first)

This spec is written so a coordinating agent can decompose it into parallel workstreams and produce a working v1 in roughly one focused session. Suggested decomposition:

| Sub-agent | Owns | Sections |
|---|---|---|
| **Data agent** | FRED + market-data ingestion, provider abstraction, caching | §4, §10.2–10.3 |
| **Macro agent** | Macro Engine scoring + circuit breakers | §5, §10.4 |
| **Tactical agent** | Technical indicators + Z-score + guards | §6, §10.5 |
| **Fusion agent** | Zone 3 decision math, Zone 1 synthesis | §7, §8, §10.6 |
| **Platform agent** | config, persistence, scheduling, alerts, Streamlit, tests | §9, §10.1, §10.7, §11 |

**Build order (dependency-respecting):** config models → data providers → macro engine → tactical engine → fusion → CLI/report → Streamlit → alerts → tests. The fusion logic (§7) and macro/tactical engines (§5–§6) are pure functions over typed inputs, so they can be built and unit-tested **before** live data wiring is finished using fixture data.

**Definition of Done for v1** is in §12. **Hard questions deliberately deferred** are in §13 — do not block v1 on them; wire in the stated default and leave a `# TODO(open-question-N)` marker.

> **Note on scope.** This is a *personal decision-support* tool. It encodes a configurable allocation framework, not financial advice, and the author is the sole user/operator. All thresholds are parameters in config, not hard-coded truths. The system should never place trades — it only emits a recommended DCA multiplier and a human reads it.

---

## 1. Product overview & design principles

### 1.1 The problem
Optimize a personal Dollar-Cost-Averaging strategy by *modulating the amount deployed each period* based on (a) the structural macro regime and (b) where the target asset sits relative to its own statistical mean — without information overload. The core behavioral goal: **buy more when assets are genuinely cheap, but refuse to "catch a falling knife" when cheapness reflects a structurally collapsing macro regime.**

### 1.2 The 3-Zone model (restated)
- **Zone 1 — Global System Status:** one synthesized regime string (e.g. `Expansion & Stable Liquidity`, `Late-Cycle Caution`, `Contraction / Systemic Stress`).
- **Zone 2 — Two Pillars:**
  - **Column A — Macro Engine** (structural, slow): FRED data → a continuous **Macro Health Score `H ∈ [0,1]`** + discrete circuit breakers.
  - **Column B — Tactical Engine** (asset, fast): price → 200-day SMA, rolling **Z-score**, plus the additional metrics defined in §6.
- **Zone 3 — Actionable Signal Buffer:** fuse `H` and `Z` (gated by tactical guards) → a single **DCA multiplier `M`** and a plain-language instruction.

### 1.3 Design principles
1. **Deterministic & auditable.** Same inputs → same output. Every recommendation must be reproducible and explainable from the persisted snapshot (§9.4). No black boxes for a money decision.
2. **Config-driven, not hard-coded.** Every threshold, weight, and bound lives in typed config (§10.1). Changing risk appetite = editing one file, not the logic.
3. **Fail-safe defaults.** On any data/compute failure, the system degrades to **Standard DCA (M = 1.0)** and flags the failure — it never silently emits an aggressive or zero allocation.
4. **Asymmetric caution.** Defensiveness is always permitted; aggression must be *earned* by a healthy macro regime AND a supportive trend AND a normal-volatility environment (§7). Circuit breakers can only make the regime worse, never better.
5. **Resist overfitting.** Recessions are rare (few independent samples). Thresholds are chosen from economic meaning and round numbers, not optimized against backtest P&L. See §13-Q1.
6. **Low-stress.** Output is one number + one sentence. The dashboard is glanceable. The agent runs monthly; daily data is cached, not stared at.

### 1.4 Non-goals (v1)
- No order execution / broker integration.
- No intraday signals (the cadence is monthly review; data is daily).
- No multi-asset portfolio optimization (single target asset per run; loopable — see §13-Q4).
- No ML model in v1 (the fusion is transparent closed-form math; ML calibration is a §13 stretch).

---

## 2. System architecture

```
                         ┌────────────────────────────────────────────┐
                         │                CONFIG (pydantic)            │
                         │  series IDs · weights · thresholds · bounds │
                         └───────────────────────┬────────────────────┘
                                                 │
        ┌──────────────────────┐      ┌──────────▼───────────┐
        │   FRED provider      │      │  Market-data provider │   (pluggable:
        │  (macro series)      │      │  yfinance / Stooq /   │    abstract base
        └──────────┬───────────┘      │  Tiingo / TV webhook) │    + adapters)
                   │                  └──────────┬───────────┘
        ┌──────────▼───────────┐      ┌──────────▼───────────┐
        │   Macro Engine §5    │      │  Tactical Engine §6   │
        │  H ∈ [0,1] + breakers│      │  SMA/Z/ATR/ADX/RSI    │
        │  → MacroState        │      │  → TechnicalState     │
        └──────────┬───────────┘      └──────────┬───────────┘
                   └───────────────┬──────────────┘
                          ┌────────▼─────────┐
                          │  Fusion §7        │  → DCA multiplier M + label + rationale
                          │  Synthesis §8     │  → Zone 1 regime string
                          └────────┬─────────┘
                ┌──────────────────┼───────────────────┐
        ┌───────▼──────┐   ┌───────▼──────┐    ┌────────▼───────┐
        │ Persist §9.4 │   │ Streamlit §9 │    │  Alert §9.3    │
        │ DuckDB+parquet│   │  dashboard   │    │ Telegram/email │
        └──────────────┘   └──────────────┘    └────────────────┘
```

**Data flow per run:** `ingest → compute indicators → score macro → score tactical → fuse → synthesize status → persist snapshot → render/alert`. Each arrow is a pure transform over typed dataclasses/pydantic models so each can be tested in isolation.

---

## 3. Recommended tech stack

Chosen for: minimal moving parts, modern tooling, "scriptable agent" friendliness, and **current maintenance status verified May 2026** (corrections below matter — two of the author's original choices are now broken/deprecated).

| Concern | Recommendation | Rationale / **currency note** |
|---|---|---|
| Language | **Python 3.11+** | Match author's stack; 3.11 for `tomllib`, perf, typing. |
| Packaging / env | **`uv`** | Fastest modern resolver/installer; reproducible `uv.lock`; the maintained TA fork ships first-class `uv` support. |
| Config & validation | **`pydantic` v2 + `pydantic-settings`** | Typed config, env-var overlay, validation at load. |
| Macro data | **FRED via `fredapi`** (thin wrapper) with a `requests` fallback adapter | `fredapi` handles series fetch + dates cleanly; keep a raw-`requests` adapter behind the same interface so there's no hard dependency. |
| **Macro series** | **⚠ Use `STLFSI4`, not `STLFSI3`** | `STLFSI3` is **discontinued — frozen at 2022-10-28.** The live series is **`STLFSI4`**. Hard-coding `STLFSI3` would silently feed 3-year-stale data. Recommend adding **`NFCI`** (Chicago Fed) as a robust weekly cross-check. |
| Market data | **Pluggable provider interface**; default `yfinance`, fallbacks `Stooq` (free, no key) and `Tiingo` (cheap, deep history) | `yfinance` scrapes Yahoo and **broke after Yahoo's Feb-2025 redesign**; it still works but rate-limits and returns occasional bad split/dividend values. The abstraction (§10.3) means a broken provider is a config swap, not a rewrite. **TradingView webhook** is a first-class adapter for push-based ingestion. |
| Technical indicators | **`pandas-ta-classic`** (maintained community fork) | ⚠ The original **`pandas-ta` (twopirllc) is "Inactive" and flagged for archival ~July 2026** unless funded. `pandas-ta-classic` (`xgboosted`) is actively maintained, production-grade, `uv`-native, ~200 indicators. **Alternative:** the lightweight `ta` library for fewer deps; **`TA-Lib`** only if you accept a C build for speed. Pin whichever you choose. |
| Numerics | **`numpy`, `pandas`** | pandas 3.0 is current; `pandas-ta-classic` is compatible. |
| Local storage | **DuckDB** (snapshots/queries) + **Parquet** (raw series cache) | Analytical, zero-server, SQL you already think in. SQLite is an acceptable lighter substitute. |
| Scheduling / "agent" | **GitHub Actions scheduled workflow** (primary) + **APScheduler** (local dev) | A monthly cron in CI = zero infra, free, logs retained, runs even when your laptop is off. |
| Alerts | **Telegram bot** (primary) + SMTP email (fallback) | Lowest-friction personal push channels; both free. |
| Dashboard UI | **Streamlit** | Good fit for a personal glanceable dashboard; pairs with a headless report path for the agent (§9.2). |
| Tests | **`pytest`** + **`hypothesis`** | Golden-case unit tests for scoring/fusion; property tests for monotonicity/bounds (§11). |
| Optional orchestration | **Prefect** (only if you want run observability) | Overkill for v1; note as a later option, don't add now. |

> **Three corrections to carry into code:** (1) `STLFSI4` not `STLFSI3`; (2) `pandas-ta-classic` not `pandas-ta`; (3) market data behind a provider interface, never `yfinance` called directly from business logic.

---

## 4. Data layer

### 4.1 Macro data (FRED)

Requires a free FRED API key (`FRED_API_KEY` env var). Series are mixed-frequency; **resample/align to the latest available observation as of run date, and never use a revised value with look-ahead** — store the as-of date with each value (see §13-Q2 on vintages).

| Concept | Series ID | Frequency | Notes |
|---|---|---|---|
| Sahm Rule (recession) | **`SAHMREALTIME`** | Monthly | Real-time Sahm indicator in percentage points. (Can be recomputed from `UNRATE` if you prefer to control the calc.) |
| Yield curve 10Y–2Y | **`T10Y2Y`** | Daily | Spread in pp; negative = inverted. |
| Financial stress | **`STLFSI4`** | Weekly (Fri) | Mean 0; `>0` = above-average stress. **(Not STLFSI3.)** |
| *(cross-check)* Financial conditions | `NFCI` | Weekly | Chicago Fed; `>0` = tighter-than-average. Optional but robust. |

Caching: persist each series to Parquet keyed by series ID + fetch timestamp; only re-pull if the cache is older than the series' update cadence. Keeps monthly runs cheap and offline-resilient.

### 4.2 Market data (Tactical)

Single configurable `target_symbol` (e.g. `QQQ`). Provider interface (§10.3) returns a tidy OHLCV `DataFrame` indexed by date. Requirements:
- ≥ ~300 trading days of history (so the 200-day SMA + Z-window are fully warmed up).
- Adjusted close used for all return/Z computations.
- Validate on ingest: monotonic dates, no all-NaN columns, last bar not older than `staleness_days` (config) — else mark `data_ok = False` and trigger fail-safe (§1.3-#3).
- **TradingView webhook adapter:** a tiny FastAPI (or Flask) endpoint that accepts a JSON push `{symbol, time, ohlcv...}` and appends to the local store; the engine reads from the store either way.

---

## 5. Zone 2 · Column A — The Macro Engine

Produces a `MacroState`: a continuous health score plus discrete overrides. **Two-layer design on purpose:** a single averaged score can mask one critical signal, so we keep (a) a smooth score for the *gradient* and (b) hard circuit breakers for *binary* recession/crisis signals.

### 5.1 Per-indicator sub-scores `sᵢ ∈ [0,1]` (1 = healthy)

Each raw reading is mapped to `[0,1]` by a clamped linear ramp between a `healthy_ref` and a `stress_ref`. All four refs are config.

- **Sahm sub-score:** `s_sahm = clamp((sahm_trigger − sahm) / sahm_trigger, 0, 1)`, with `sahm_trigger = 0.50`. → reading `0.0`→`1.0`; `≥0.50`→`0.0`.
- **Curve sub-score:** `s_curve = clamp((spread − inv_floor) / (healthy_ref − inv_floor), 0, 1)`, with `inv_floor = −0.5`, `healthy_ref = +1.0`. → `+1.0`→`1.0`; `−0.5`→`0.0`.
  *Caveat to encode as a comment:* curve **re-steepening after a deep inversion** historically precedes recession onset; v1 treats only the level — see §13-Q3.
- **Stress sub-score:** `s_stress = clamp((stress_hi − stlfsi4) / (stress_hi − calm_lo), 0, 1)`, with `calm_lo = −1.0`, `stress_hi = +1.0`. → `−1.0`→`1.0`; `+1.0`→`0.0`; `0`→`0.5`.

### 5.2 Macro Health Score
```
H = w_sahm·s_sahm + w_curve·s_curve + w_stress·s_stress      (Σ w = 1)
default weights: w_sahm = 0.40, w_curve = 0.25, w_stress = 0.35
```
Sahm is weighted highest because it is the most reliable *realized*-recession signal; stress next (coincident/forward-looking on liquidity); curve lowest (noisy lead/lag).

### 5.3 Circuit breakers (override the gradient → can only worsen the regime)
- `sahm ≥ 0.50` → force `STRESS` (recession signal fired).
- `stlfsi4 ≥ stress_crisis` (default `+1.5`) → force `STRESS` (acute liquidity event).
- *(optional)* curve un-inversion trigger → `CAUTION` (deferred, §13-Q3).

### 5.4 Regime from `H` (when no breaker fired)
```
H ≥ 0.66           → EXPANSION
0.40 ≤ H < 0.66    → CAUTION
H < 0.40           → STRESS
```
Breakers take precedence and may only push the regime *down* the ladder.

### 5.5 `MacroState` output schema
```
MacroState:
  H: float ∈ [0,1]
  regime: {EXPANSION, CAUTION, STRESS}
  subscores: {sahm, curve, stress}
  raw: {sahm, t10y2y, stlfsi4, nfci?}
  breakers_fired: list[str]
  as_of: date
  data_ok: bool
```

---

## 6. Zone 2 · Column B — The Tactical DCA Engine

### 6.1 Existing metrics (define precisely)
- **200-day SMA** on adjusted close. Also compute its **slope** (sign of `SMA[t] − SMA[t−k]`, `k = 20`) — cheap, and essential for the trend guard.
- **Rolling Z-score** of *log* price vs its rolling mean:
  `Z = (ln P − μ_W) / σ_W`, window `W_z = 200` (config). Use log price so the dispersion measure is scale-stable across a multi-year window. **Negative Z = below mean = cheap.**

### 6.2 Recommended additions (Task 1)
Goal: confirm the asset's *state* with **orthogonal** information — each new metric must answer a question the SMA + Z-score does **not** already answer. Avoid anything that just re-measures "stretch from the mean" (that's the Z-score's job — which is why Bollinger %B is intentionally excluded: it's a 20-period Z-score in disguise).

| Metric (`pandas-ta-classic`) | Dimension it covers | Why it matters here | Redundant with Z? |
|---|---|---|---|
| **ATR%** = `ATR(14) / Close` | **Volatility regime** | Sizes the bet to current turbulence (vol-targeting) and gates aggression — knives fall fastest in high-vol regimes. | No — orthogonal |
| **ADX(14)** (+ `DMP`/`DMN`) | **Trend *strength* (not direction)** | Distinguishes a *strong, established* downtrend (dangerous to buy into) from a *weak, mean-reverting* dip. The single most direct falling-knife discriminator at the asset level. | No — orthogonal |
| **RSI(14)** | **Momentum / oversold timing** | Confirms a bottoming-and-turning condition vs. "oversold and still bleeding." Helps *time* the add within a permitted regime. | Partially — keep as confirmer, not a driver |

**Optional 4th (only if you want inflection, else skip to avoid overload):** **MACD histogram** — sign-flip up = momentum inflection. Recommendation: **leave out of v1**; ADX + RSI already cover "is the down-move losing steam," and the design principle is to resist overload.

So the v1 tactical feature set is: `SMA200`, `SMA200_slope`, `Z`, `ATR%`, `ADX(+DMI)`, `RSI(14)`.

### 6.3 Derived tactical guards (feed §7)
- **`trend_strong_down`** (bool): `Close < SMA200` **AND** `SMA200_slope < 0` **AND** `ADX > adx_trend_thresh` (default `25`).
- **`vol_factor` ∈ (g_vol_min, 1]:** `clamp(atr_pct_baseline / atr_pct_now, g_vol_min, 1.0)`, where `atr_pct_baseline` is a long rolling median of ATR% (config window) and `g_vol_min` default `0.4`. High current vol → `vol_factor < 1` → damps aggression.

### 6.4 `TechnicalState` output schema
```
TechnicalState:
  symbol: str
  price: float
  sma200: float; sma_slope: float
  z: float
  atr_pct: float; vol_factor: float
  adx: float; rsi: float
  trend_strong_down: bool
  as_of: date
  data_ok: bool
```

---

## 7. Zone 3 — Actionable Signal Buffer (the fusion math) · Task 2

**Output:** a DCA multiplier `M` (e.g. `1.0` = standard, `0.5` = defensive, `1.5+` = aggressive) and a plain instruction. The whole design is built around one principle:

> **The technical Z-score proposes the direction and size of the deviation from baseline. The macro regime, the trend, and volatility decide how much of that *aggressive* deviation you are allowed to take. Defensiveness is never gated.**

This asymmetry is what mathematically prevents knife-catching: a cheap price (`Z ≪ 0`) only translates into "buy more" to the extent the regime is healthy enough that the cheapness is *noise around a stable mean* rather than *information about a collapsing one*.

### Step 1 — Technical tilt `T(Z)` (smooth, bounded)
```
T(Z) = 1 + α · ( −tanh(Z / β) )
   α = max tilt amplitude   (default 0.75 → pre-gate range [0.25, 1.75])
   β = saturation scale (std devs)  (default 1.5)
```
- `Z = 0 → T = 1` (mean → standard).
- `Z ≪ 0 → T → 1 + α` (cheap → want more).
- `Z ≫ 0 → T → 1 − α` (expensive → want less).
- **Why `tanh`, not linear:** it *saturates*. A flash-crash `Z = −4` doesn't blow the allocation to absurd size; the marginal aggression per extra std-dev of cheapness shrinks. Symmetric, bounded, smooth → robust to outliers in a money decision.

Let the **deviation from baseline** be `d = T(Z) − 1` (positive = cheap/aggressive, negative = expensive/defensive).

### Step 2 — Asymmetric macro gate
```
if d ≥ 0:   # cheap → aggression must be EARNED by macro health
    d_eff = d · H
else:       # expensive → defensiveness ALWAYS applies, amplified when macro weak
    d_eff = d · (1 + λ · (1 − H))        # λ default 1.0 → factor ∈ [1, 2]
M_core = 1 + d_eff
```
- Cheap + collapsing macro (`H → 0`): `d_eff → 0` → revert toward baseline. **The knife is not caught.**
- Cheap + healthy macro (`H → 1`): full aggressive tilt survives.
- Expensive + weak macro: defensiveness *amplified* (classic dangerous late-cycle setup → trim harder).

### Step 3 — Tactical guards (multiply the *aggressive* portion only)
```
agg  = max(M_core − 1, 0)
def_ = max(1 − M_core, 0)

g_trend = g_trend_down if trend_strong_down else 1.0     # default g_trend_down = 0.30
g_vol   = vol_factor                                      # from §6.3

agg_guarded = agg · g_trend · g_vol
M_pre = 1 + agg_guarded − def_
```
Guards apply to `agg` only — being defensive in a strong downtrend or high vol is *correct*, so we never damp `def_`.

### Step 4 — Regime hard cap + global clamp (the ultimate guard)
```
regime_cap = { EXPANSION: M_cap_exp (2.0),
               CAUTION:   M_cap_caution (1.25),
               STRESS:    M_cap_stress (1.0) }[regime]

M = clamp( M_pre, M_min, regime_cap )          # then clamp to global [M_min, M_max]
   M_min default 0.25   (never fully stop DCA — that defeats the strategy; configurable)
   M_max default 2.00
```
In a `STRESS` regime, `M ≤ 1.0` **by construction**: you keep your baseline DCA (you don't time the bottom by stopping), but you are *never* levered into a structurally collapsing tape no matter how cheap it screens.

### 7.1 Allocation labels (map `M` → instruction text; all bounds config)
| `M` range | Label | Example instruction |
|---|---|---|
| `M < 0.60` | **Defensive** | "Reduce this period's DCA to ~50%. Expensive and/or deteriorating regime." |
| `0.60 ≤ M < 0.90` | **Cautious** | "Slightly below standard (~75%)." |
| `0.90 ≤ M ≤ 1.10` | **Standard** | "Standard DCA (100%). No strong signal." |
| `1.10 < M ≤ 1.40` | **Opportunistic** | "Add modestly (~125%). Cheap, regime supportive." |
| `M > 1.40` | **Aggressive** | "Deploy extra (capped by regime). Deep value in a healthy regime." |

### 7.2 Worked examples (defaults above; verify these as golden tests in §11)
| Scenario | H | Z | trend_down | g_vol | regime | → M | Label |
|---|---|---|---|---|---|---|---|
| A. Healthy expansion, sharp dip | 0.90 | −2.0 | no | 1.0 | EXPANSION | **≈1.59** | Aggressive |
| B. **Falling knife** (cheap, collapsing) | 0.15 | −2.0 | yes(0.3) | 0.5 | STRESS | **1.00** | Standard (knife avoided) |
| C. Expensive, late-cycle | 0.45 | +1.8 | no | 1.0 | CAUTION | **≈0.25** (clamped) | Defensive |
| D. Neutral | 0.70 | 0.0 | no | 1.0 | EXPANSION | **1.00** | Standard |

Example B is the whole point: identical cheapness to A (`Z = −2`) but, because the macro gate (`H = 0.15`), trend guard, vol guard, and the `STRESS` cap all compound, the aggressive tilt is annihilated and the system holds at standard DCA instead of levering into the decline.

---

## 8. Zone 1 — Global System Status (synthesis)

A single string + a status color, derived purely from `MacroState` (+ a liquidity tag from `STLFSI4`):

```
regime_label = { EXPANSION: "Expansion", CAUTION: "Late-Cycle Caution", STRESS: "Contraction / Systemic Stress" }
liquidity    = "Stable Liquidity" if stlfsi4 < 0 else ("Tightening Liquidity" if stlfsi4 < stress_crisis else "Stressed Liquidity")
status = f"{regime_label} & {liquidity}"   e.g. "Expansion & Stable Liquidity"
if breakers_fired: status += f"  ⚠ {', '.join(breakers_fired)}"
color = {EXPANSION: green, CAUTION: amber, STRESS: red}
```
Zone 1 also surfaces `H` (0–1 gauge) and the current `M` + label as the headline. Everything else (raw indicators, technical panel) sits one scroll below — glanceable-first.

---

## 9. Backend architecture & automation

### 9.1 Project structure
```
intelligent-investor/
├── pyproject.toml            # uv-managed; pin pandas-ta-classic, fredapi, etc.
├── config/
│   └── default.toml          # all thresholds/weights/bounds (see §10.1)
├── src/iidca/
│   ├── config.py             # pydantic-settings models
│   ├── providers/
│   │   ├── base.py           # MarketDataProvider ABC, FredProvider ABC
│   │   ├── yfinance_provider.py
│   │   ├── stooq_provider.py
│   │   ├── tiingo_provider.py
│   │   ├── tradingview_webhook.py   # FastAPI receiver → local store
│   │   └── fred.py
│   ├── engines/
│   │   ├── macro.py          # §5  → MacroState
│   │   ├── tactical.py       # §6  → TechnicalState  (pandas-ta-classic)
│   │   └── fusion.py         # §7,§8 → Decision
│   ├── storage.py            # DuckDB + Parquet; append snapshot
│   ├── alerts.py             # Telegram + SMTP
│   ├── report.py             # headless render (for the agent path)
│   ├── run.py                # CLI entrypoint: one full cycle
│   └── dashboard.py          # Streamlit app
├── tests/                    # pytest + hypothesis (§11)
└── .github/workflows/monthly.yml   # scheduled cron
```

### 9.2 Two run modes from one core
- **`run.py` (headless):** executes a full cycle, persists a snapshot, returns a `Decision`, optionally fires an alert. This is what the scheduler/agent calls.
- **`dashboard.py` (Streamlit):** reads the latest persisted snapshot (and can trigger a fresh `run`) for the visual 3-zone layout. The dashboard never contains business logic — it only renders `Decision`/`MacroState`/`TechnicalState`.

### 9.3 Alerting
Monthly: send Zone 1 status + headline `M`/label + the one-line instruction + the three macro sub-scores. **Idempotency:** include the snapshot `as_of` in the message and skip re-sending if the latest snapshot was already alerted (store an `alerted_at`).

### 9.4 Persistence / audit
Every run appends one immutable snapshot row to DuckDB: `{run_ts, as_of, symbol, H, regime, Z, atr_pct, adx, rsi, M, label, breakers, config_hash, data_ok}` + the raw series to Parquet. `config_hash` makes every past recommendation fully reproducible (which config produced it).

### 9.5 Scheduling / "agent"
- **Primary:** `.github/workflows/monthly.yml` with `on: schedule: cron`. Secrets (`FRED_API_KEY`, Telegram token) in repo secrets. Free, off-box, logged.
- **Local:** APScheduler for iteration. (Prefect only if observability is later wanted — not v1.)

---

## 10. Skeleton code (core decision logic)

Reference implementation of the typed contracts and the **fusion function** (the heart). Engines that need live libraries are stubbed at the I/O boundary so the math is testable immediately with fixtures.

### 10.1 Config (`config.py`)
```python
from pydantic import BaseModel, Field

class MacroCfg(BaseModel):
    series: dict[str, str] = {
        "sahm": "SAHMREALTIME",
        "t10y2y": "T10Y2Y",
        "stlfsi": "STLFSI4",      # NOT STLFSI3 (discontinued)
        "nfci": "NFCI",
    }
    sahm_trigger: float = 0.50
    curve_inv_floor: float = -0.5
    curve_healthy_ref: float = 1.0
    stress_calm_lo: float = -1.0
    stress_hi: float = 1.0
    stress_crisis: float = 1.5
    w_sahm: float = 0.40
    w_curve: float = 0.25
    w_stress: float = 0.35
    regime_expansion: float = 0.66
    regime_caution: float = 0.40

class TacticalCfg(BaseModel):
    z_window: int = 200
    sma_window: int = 200
    sma_slope_lookback: int = 20
    adx_period: int = 14
    rsi_period: int = 14
    atr_period: int = 14
    atr_baseline_window: int = 252
    adx_trend_thresh: float = 25.0
    g_vol_min: float = 0.40
    staleness_days: int = 5

class FusionCfg(BaseModel):
    alpha: float = 0.75
    beta: float = 1.5
    lam: float = 1.0
    g_trend_down: float = 0.30
    m_cap_exp: float = 2.00
    m_cap_caution: float = 1.25
    m_cap_stress: float = 1.00
    m_min: float = 0.25
    m_max: float = 2.00

class AppCfg(BaseModel):
    target_symbol: str = "QQQ"
    market_provider: str = "yfinance"   # yfinance|stooq|tiingo|tradingview
    macro: MacroCfg = Field(default_factory=MacroCfg)
    tactical: TacticalCfg = Field(default_factory=TacticalCfg)
    fusion: FusionCfg = Field(default_factory=FusionCfg)
```

### 10.2 Typed states
```python
from dataclasses import dataclass, field
from datetime import date

@dataclass
class MacroState:
    H: float
    regime: str                 # EXPANSION | CAUTION | STRESS
    subscores: dict
    raw: dict
    breakers_fired: list[str] = field(default_factory=list)
    as_of: date | None = None
    data_ok: bool = True

@dataclass
class TechnicalState:
    symbol: str
    price: float
    sma200: float
    sma_slope: float
    z: float
    atr_pct: float
    vol_factor: float
    adx: float
    rsi: float
    trend_strong_down: bool
    as_of: date | None = None
    data_ok: bool = True

@dataclass
class Decision:
    M: float
    label: str
    instruction: str
    status: str                 # Zone 1 string
    color: str
    rationale: dict             # every intermediate value, for audit
```

### 10.3 Provider interface (`providers/base.py`)
```python
from abc import ABC, abstractmethod
import pandas as pd

class MarketDataProvider(ABC):
    @abstractmethod
    def ohlcv(self, symbol: str, lookback_days: int = 400) -> pd.DataFrame:
        """Return tidy OHLCV indexed by date (adjusted close in 'Close')."""

class FredProvider(ABC):
    @abstractmethod
    def series(self, series_id: str) -> pd.Series: ...

# yfinance_provider.py / stooq_provider.py implement ohlcv() so business
# logic NEVER imports yfinance directly — swapping a broken source is config.
```

### 10.4 Macro engine (`engines/macro.py`)
```python
def _clamp(x, lo, hi): return max(lo, min(hi, x))

def score_macro(raw: dict, cfg) -> MacroState:
    sahm, spread, stlfsi = raw["sahm"], raw["t10y2y"], raw["stlfsi"]
    s_sahm   = _clamp((cfg.sahm_trigger - sahm) / cfg.sahm_trigger, 0, 1)
    s_curve  = _clamp((spread - cfg.curve_inv_floor) /
                      (cfg.curve_healthy_ref - cfg.curve_inv_floor), 0, 1)
    s_stress = _clamp((cfg.stress_hi - stlfsi) /
                      (cfg.stress_hi - cfg.stress_calm_lo), 0, 1)
    H = cfg.w_sahm*s_sahm + cfg.w_curve*s_curve + cfg.w_stress*s_stress

    breakers = []
    if sahm >= cfg.sahm_trigger:    breakers.append("SAHM_RECESSION")
    if stlfsi >= cfg.stress_crisis: breakers.append("FIN_STRESS_CRISIS")

    if breakers:                         regime = "STRESS"
    elif H >= cfg.regime_expansion:      regime = "EXPANSION"
    elif H >= cfg.regime_caution:        regime = "CAUTION"
    else:                                regime = "STRESS"

    return MacroState(H=H, regime=regime,
                      subscores={"sahm": s_sahm, "curve": s_curve, "stress": s_stress},
                      raw=raw, breakers_fired=breakers)
```

### 10.5 Tactical engine (`engines/tactical.py`)
```python
import numpy as np, pandas as pd
import pandas_ta_classic as ta   # maintained fork; NOT pandas_ta

def score_tactical(df: pd.DataFrame, cfg, symbol: str) -> TechnicalState:
    c = df["Close"]
    sma = c.rolling(cfg.sma_window).mean()
    sma_slope = sma.iloc[-1] - sma.iloc[-1 - cfg.sma_slope_lookback]

    logp = np.log(c)
    mu  = logp.rolling(cfg.z_window).mean()
    sig = logp.rolling(cfg.z_window).std()
    z = float((logp.iloc[-1] - mu.iloc[-1]) / sig.iloc[-1])

    atr = ta.atr(df["High"], df["Low"], c, length=cfg.atr_period)
    atr_pct = float(atr.iloc[-1] / c.iloc[-1])
    atr_base = float((atr / c).rolling(cfg.atr_baseline_window).median().iloc[-1])
    vol_factor = _clamp(atr_base / atr_pct, cfg.g_vol_min, 1.0)

    adx_df = ta.adx(df["High"], df["Low"], c, length=cfg.adx_period)
    adx = float(adx_df[f"ADX_{cfg.adx_period}"].iloc[-1])
    rsi = float(ta.rsi(c, length=cfg.rsi_period).iloc[-1])

    trend_strong_down = (c.iloc[-1] < sma.iloc[-1]) and (sma_slope < 0) and (adx > cfg.adx_trend_thresh)

    return TechnicalState(symbol=symbol, price=float(c.iloc[-1]),
                          sma200=float(sma.iloc[-1]), sma_slope=float(sma_slope),
                          z=z, atr_pct=atr_pct, vol_factor=vol_factor,
                          adx=adx, rsi=rsi, trend_strong_down=trend_strong_down)
```

### 10.6 Fusion — the core decision logic (`engines/fusion.py`)
```python
import math

def _clamp(x, lo, hi): return max(lo, min(hi, x))

def decide(macro: MacroState, tech: TechnicalState, cfg) -> Decision:
    # Fail-safe: any bad data → Standard DCA, flagged.
    if not (macro.data_ok and tech.data_ok):
        return Decision(M=1.0, label="Standard (fail-safe)",
                        instruction="Data incomplete — defaulting to standard DCA.",
                        status="UNKNOWN — data error", color="amber",
                        rationale={"reason": "data_ok=False"})

    H, Z = macro.H, tech.z

    # Step 1 — technical tilt (bounded, saturating)
    T = 1 + cfg.alpha * (-math.tanh(Z / cfg.beta))
    d = T - 1

    # Step 2 — asymmetric macro gate
    if d >= 0:
        d_eff = d * H
    else:
        d_eff = d * (1 + cfg.lam * (1 - H))
    m_core = 1 + d_eff

    # Step 3 — tactical guards on the aggressive portion only
    agg  = max(m_core - 1, 0.0)
    defn = max(1 - m_core, 0.0)
    g_trend = cfg.g_trend_down if tech.trend_strong_down else 1.0
    g_vol   = tech.vol_factor
    agg_guarded = agg * g_trend * g_vol
    m_pre = 1 + agg_guarded - defn

    # Step 4 — regime cap + global clamp
    cap = {"EXPANSION": cfg.m_cap_exp, "CAUTION": cfg.m_cap_caution,
           "STRESS": cfg.m_cap_stress}[macro.regime]
    M = _clamp(_clamp(m_pre, cfg.m_min, cap), cfg.m_min, cfg.m_max)

    label, instruction = _label(M)
    status, color = _zone1(macro)
    return Decision(M=round(M, 3), label=label, instruction=instruction,
                    status=status, color=color,
                    rationale={"T": T, "d": d, "d_eff": d_eff, "m_core": m_core,
                               "g_trend": g_trend, "g_vol": g_vol, "cap": cap,
                               "H": H, "Z": Z, "regime": macro.regime,
                               "breakers": macro.breakers_fired})

def _label(M):
    if M < 0.60: return "Defensive", "Reduce this period's DCA (~50%). Expensive and/or deteriorating regime."
    if M < 0.90: return "Cautious", "Slightly below standard (~75%)."
    if M <= 1.10: return "Standard", "Standard DCA (100%). No strong signal."
    if M <= 1.40: return "Opportunistic", "Add modestly (~125%). Cheap and regime-supportive."
    return "Aggressive", "Deploy extra (regime-capped). Deep value in a healthy regime."

def _zone1(m: MacroState):
    rl = {"EXPANSION": "Expansion", "CAUTION": "Late-Cycle Caution",
          "STRESS": "Contraction / Systemic Stress"}[m.regime]
    stl = m.raw.get("stlfsi", 0.0)
    liq = "Stable Liquidity" if stl < 0 else ("Tightening Liquidity" if stl < 1.5 else "Stressed Liquidity")
    s = f"{rl} & {liq}"
    if m.breakers_fired: s += "  ⚠ " + ", ".join(m.breakers_fired)
    return s, {"EXPANSION": "green", "CAUTION": "amber", "STRESS": "red"}[m.regime]
```

### 10.7 Entrypoint (`run.py`)
```python
def run_cycle(cfg: AppCfg) -> Decision:
    fred = make_fred_provider()
    mkt  = make_market_provider(cfg.market_provider)
    raw  = {k: fred.series(sid).iloc[-1] for k, sid in cfg.macro.series.items() if sid}
    macro = score_macro(raw, cfg.macro)
    df    = mkt.ohlcv(cfg.target_symbol)
    tech  = score_tactical(df, cfg.tactical, cfg.target_symbol)
    decision = decide(macro, tech, cfg.fusion)
    persist_snapshot(macro, tech, decision, cfg)     # DuckDB + Parquet
    return decision
# CLI: `python -m iidca.run` → prints Decision; `--alert` also pushes Telegram.
```

---

## 11. Testing & validation

- **Golden cases:** the four scenarios in §7.2 become exact-value `pytest` assertions on `decide(...)`. These lock the intended behavior (especially Example B — knife avoidance).
- **Property tests (`hypothesis`):**
  - *Bounds:* `m_min ≤ M ≤ m_max` for all `H ∈ [0,1]`, `Z ∈ [−6,6]`.
  - *Monotonicity in cheapness:* with everything else fixed and `H` fixed healthy, `M` is non-increasing in `Z` (cheaper ⇒ at least as aggressive).
  - *Macro gating:* for any cheap `Z<0`, `M` is non-decreasing in `H` (healthier macro ⇒ at least as much permitted aggression).
  - *Stress cap:* `regime == STRESS ⇒ M ≤ 1.0`, for all `Z`.
- **Engine unit tests** with fixture series (no network). Provider tests behind a `@pytest.mark.network` flag.
- **Calibration (separate notebook, not v1 gating):** a backtest harness to *sanity-check* threshold behavior across history — used to confirm the logic does sensible things in 2008 / 2020 / 2022, **not** to optimize P&L (see §13-Q1).

---

## 12. Definition of Done (v1)

1. `python -m iidca.run` fetches live FRED (`STLFSI4`) + market data, prints a `Decision` with `M`, label, instruction, and Zone 1 status.
2. All four §7.2 golden tests + the four property tests pass.
3. Streamlit dashboard renders the 3-zone layout from the latest snapshot.
4. A monthly GitHub Action runs a cycle and pushes a Telegram/email alert.
5. Every run persists an auditable snapshot incl. `config_hash`.
6. Swapping `market_provider` in config switches data source with no code change.
7. Fail-safe verified: corrupt/stale data → `M = 1.0` + flag, never an aggressive number.

---

## 13. Open design questions (address during implementation)

Genuinely hard and intentionally **not** blocking v1 — each has a stated default to ship now.

- **Q1 — Threshold calibration without overfitting.** Recessions/crises are few and non-independent; optimizing thresholds against historical P&L will overfit catastrophically. *v1 default:* economically-motivated round-number thresholds (§10.1), validated for *plausibility* not *returns*. *Later:* sensitivity analysis / walk-forward; possibly a logistic regression for `H` trained on recession labels — but beware label scarcity.
- **Q2 — FRED revisions & look-ahead (vintages).** Sahm and unemployment are *revised*; using the latest revised value to "decide" at a past date is look-ahead bias and flatters any backtest. *v1 default:* live runs use latest available (fine for forward use). *Backtests must* use ALFRED vintage (as-first-released) data. Decide whether to wire vintage support.
- **Q3 — Yield-curve *un-inversion* signal.** Historically the re-steepening *after* a deep inversion has preceded recession onset better than the inversion itself. *v1 default:* level-only score. *Later:* a state-machine trigger (was-inverted → now-steepening ⇒ CAUTION breaker).
- **Q4 — Single asset vs portfolio.** v1 scores one `target_symbol`. For multiple assets, is `M` per-asset, or do you need a portfolio-level budget so multipliers don't collectively over-deploy your cash buffer? *Default:* loop per asset, independent `M`. *Open:* a shared cash-budget normalizer.
- **Q5 — What does `M` multiply, and where does "extra" cash come from?** Aggressive `M > 1` implies a reserve to draw from. Is there a defined DCA cash buffer, or does `M > 1` pull future contributions forward (which changes the risk profile)? Needs an explicit funding model.
- **Q6 — Frequency mismatch & signal staleness.** Sahm is monthly, `STLFSI4` weekly, price daily; a monthly decision cadence is coarse for the tactical Z-score in fast drawdowns. Decide: keep monthly (low-stress, as designed) or allow an event-triggered re-run when `Z` crosses a threshold mid-month.
- **Q7 — Tax / transaction friction.** Modulating contribution size is friction-light, but if `M < 1` ever implies *selling*, that triggers taxable events. *v1 default:* `M` scales *buys* only, never sells (`M_min ≥ 0`, no negative allocation).
- **Q8 — Parameter governance.** Since thresholds drive a money decision, changes should be deliberate. Version `config/default.toml` and log `config_hash` (already in §9.4) so any past recommendation is attributable to an exact parameter set.

---

## 14. Disclaimer

This is a personal, configurable decision-support tool, not financial advice, and the figures (multipliers, thresholds, regime labels) are illustrative parameters rather than recommendations to buy or sell any security. Macro and technical indicators describe conditions; they do not predict returns, and rare-event signals (recessions, stress spikes) have very small historical sample sizes. Validate the logic's behavior yourself before relying on its output, and treat `M` as one input to a human decision.
