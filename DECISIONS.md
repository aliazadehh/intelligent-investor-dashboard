# Model & Architecture Decisions

The v1 → v2 audit log. Each entry: what was found, why it mattered, what was
done. Parameters named here live in `config/default.toml` — none are
hard-coded.

---

## 1. The valuation Z-score was structurally biased on trending assets

**Finding.** v1 defined cheapness as
`Z = (ln P − mean₂₀₀(ln P)) / std₂₀₀(ln P)`. For an asset growing at a
constant rate, this statistic converges to a **constant ≈ +1.73 regardless of
the growth rate** (last point of a linear ramp sits (W−1)/2·m above its mean,
and the ramp's std is m·W/√12; the m cancels). A steadily trending asset is
therefore flagged "permanently expensive": v1 read QQQ at Z = +3.1 and pinned
M at the 0.25 floor *during a healthy expansion*. The defect compounds for
strong-trend assets like BTC — exactly the assets the watchlist now admits.

**Resolution.** Z is now the **trend-residual Z-score**: fit an OLS line to
log price over the trailing `z_window` (200d) and standardize today's
deviation from it by the residual std-dev. On a pure trend Z ≡ 0 by
construction; it measures displacement from the asset's *own recent path*,
which is what a DCA tilt should respond to. The tanh tilt, asymmetric gate,
and guards are unchanged — only the input statistic was repaired.
Regression test: `tests/test_tactical.py::test_pure_trend_gives_zero_residual_z`
(documents the old bias and asserts the new behavior).

*Known limitation, accepted:* in a sustained crash the fitted trend itself
turns down, so residual Z normalizes toward 0 after ~a window. That is what
the macro gate, falling-knife guard, and regime caps are for — the valuation
signal is deliberately *local*, the safety logic is *structural*.

## 2. The macro gate silently taxed aggression even in the best regimes

**Finding.** v1 multiplied aggressive intent by raw H (`d_eff = d·H`).
Observable H rarely exceeds ~0.85, so even a textbook-perfect macro cut
buying intent by 15–25% — an arbitrary tax determined by the H scale, not by
a judgment anyone made. Meanwhile the defensive amplifier `1+λ(1−H)` mildly
amplified defense even in expansions.

**Resolution.** Both sides now use an explicit ramp
`g = clamp((H − gate_floor_h)/(gate_full_h − gate_floor_h), 0, 1)`
(defaults 0.40 → 0.75): aggression × g, defense × (1 + λ(1 − g)).
H ≥ 0.75 passes aggression fully and applies no defensive amplification;
at/below the STRESS boundary aggression is zero. "Healthy enough" is now a
named, configurable judgment.

## 3. Leading and lagging signals were blended as interchangeable

**Finding.** The yield-curve *level* leads recessions by 12–24 months; Sahm
is coincident/realized. Worse, history says the **re-steepening after** a
deep inversion is the proximate danger window — precisely when the level
score recovers and looks healthy again. v1 acknowledged this in a TODO and
shipped the contradiction.

**Resolution.** Two-part treatment: the level keeps its (lowest, 25%) weight
in H as a slow leading input, and a new **soft breaker** `CURVE_DISINVERSION`
fires when the spread was below −0.10pp within the past 365 days and has now
recovered above 0. Soft breakers cap the regime at CAUTION (M ≤ 1.25, reduced
gate) — they never force STRESS, because dis-inversion is a warning, not a
realized event. The signal is stateless (recomputed from the series) and
self-expires when the inversion falls out of the lookback.

## 4. NFCI was fetched and never used; the stress pillar had a single source

**Finding.** v1 listed NFCI as a "secondary cross-check" but no code path
read it — a dead input. Simultaneously the stress pillar depended solely on
STLFSI4 (a series that has already been discontinued/renamed once:
STLFSI3 → STLFSI4).

**Resolution.** NFCI is now the **fallback source for the stress pillar**,
with its own ramp (`nfci_calm_lo`/`nfci_hi` — it runs on a different scale).
If STLFSI4 goes missing or stale, the pillar degrades to NFCI instead of
taking the whole macro engine down; the snapshot records `stress_source`.

## 5. RSI was decorative

**Finding.** RSI(14) appeared on the dashboard ("confirms the Z reading")
but never entered any calculation. A 14-day momentum oscillator carries
essentially no information at a monthly decision cadence, and its
"confirmation" of Z was redundant by construction (both are functions of
recent price). Showing a number that does nothing invites misreading.

**Resolution.** Removed from the pipeline and the UI. ADX stays — it is a
live input to the falling-knife guard and is displayed as part of the Trend
card with that role stated.

## 6. The instruction text contradicted the number it described

**Finding.** v1 hard-coded "Reduce this period's DCA (~50%)" onto the
Defensive tier — shown beside M = 0.25. Two parts of the system gave
different instructions for the same decision. Label tier boundaries were
also hard-coded in `fusion.py` despite the "everything is config" principle.

**Resolution.** The instruction is now generated from M itself ("Invest
about 25% of your normal amount this period") and can't drift. Tier
boundaries moved to `[fusion.labels]` in config with an ascending-order
validator.

## 7. Market data had a single point of failure (and a poisoned-bar bug)

**Finding.** The provider fallback was one hard-coded stooq attempt inside
`run.py`. Separately, yfinance sometimes appends today's half-formed bar
(NaN OHLC, volume only); v1 validation passed it through, and every
last-row indicator became NaN — observed live during this rework.

**Resolution.** New `providers/market.py` orchestrator: an ordered,
config-driven `provider_chain`, per-symbol last-good Parquet cache as the
final fallback (served with `fresh=False`, which forces the M = 1.0
fail-safe path while keeping numbers visible), and `clean_ohlcv()` dropping
rows with missing prices at every entry point. `validate_ohlcv` now rejects
a NaN last close outright. FRED already had cache + raw-requests fallback;
the macro side additionally gained #4.

## 8. Duplicated thresholds between engines

**Finding.** Zone-1 liquidity wording re-implemented the STLFSI crisis
threshold inside `fusion.py` via a smuggled `raw["_stress_crisis"]` default
of 1.5 — a second copy of a config value that could silently diverge.

**Resolution.** The status string now derives from the stress *sub-score*
and the crisis breaker the macro engine already computed. Fusion holds no
macro thresholds.

## 9. Inconsistent fail-safe states

**Finding.** On macro data failure, `macro.py` returned regime STRESS while
`run.py`'s exception path fabricated regime CAUTION; `as_of` was never set
at all (always None in snapshots).

**Resolution.** One fail-safe path (the engine's), exercised by `run.py`
simply passing through whatever it could fetch. `as_of` is now the max
observation date across fetched FRED series; per-asset `as_of` comes from
the last bar.

## 10. Single-asset architecture

**Finding.** One `target_symbol`, one conflated snapshot row mixing macro
and tactical state.

**Resolution.** Watchlist (seeded from config, persisted in DuckDB, managed
in the UI). The macro engine runs once per cycle and is shared; tactical +
fusion run per asset. Storage split into `macro_snapshots` and
`asset_snapshots` (with the full rationale JSON for the derivation view).
No per-asset parameter overrides were added *deliberately*: every tactical
metric is dimensionless and self-normalizing (log-scale residual Z, ATR%
vs the asset's own baseline, ADX), so one parameter set generalizes across
equities, ETFs, and crypto. US macro gating crypto is a documented judgment:
BTC drawdowns have coincided with USD liquidity/stress regimes, and the
gate only ever *reduces* aggression.

## 11. The test suite was empty

**Finding.** `tests/test_fusion.py`, `tests/test_engines.py`, and
`tests/conftest.py` were blank files; the README implied coverage existed.

**Resolution.** 50 tests, no network: macro ramps/breakers/dis-inversion/
fallbacks, the trend-bias regression test, vol/knife guards, fusion caps/
floor/monotonicity/waterfall integrity, config overlay/back-compat/hash,
provider hygiene.
