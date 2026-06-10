"""CLI entrypoint: one full multi-asset DCA cycle.

Usage
-----
  python -m iidca.run                   # all watchlist symbols
  python -m iidca.run --symbol SPY      # single symbol (added to watchlist)
  python -m iidca.run --alert           # also push Telegram/email alert
  python -m iidca.run --config cfg.toml # custom config file
  iidca                                 # same as above (installed script)

The macro engine is global and runs once per cycle; the tactical/valuation
engine runs per asset.

Fail-safe:
  Any data / compute failure degrades that asset to M=1.0 Standard and
  prints a clear error. The system never silently emits an aggressive
  allocation on bad data.
"""

from __future__ import annotations

import logging
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import click
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from iidca.config import AppCfg, load_config
from iidca.models import Decision, MacroState, TechnicalState

console = Console()
logger = logging.getLogger(__name__)


@dataclass
class AssetResult:
    tech: TechnicalState
    decision: Decision
    source: str = ""
    fresh: bool = True
    errors: list[str] = field(default_factory=list)


@dataclass
class CycleResult:
    run_ts: datetime
    macro: MacroState
    assets: dict[str, AssetResult]


# ---------------------------------------------------------------------------
# Pipeline stages
# ---------------------------------------------------------------------------

def fetch_macro_state(cfg: AppCfg) -> MacroState:
    """Fetch FRED series and score the global macro regime."""
    from iidca.engines.macro import score_macro
    from iidca.providers.fred import make_fred_provider

    raw: dict = {}
    try:
        fred = make_fred_provider()
        latest_dates = []
        for key, sid in cfg.macro.series.items():
            if not sid:
                continue
            try:
                series = fred.series(sid)
                raw[key] = float(series.iloc[-1])
                latest_dates.append(series.index[-1].date())
                if key == "t10y2y":
                    # Full history enables the dis-inversion soft breaker.
                    raw["t10y2y_history"] = series
            except Exception as exc:
                logger.warning("Could not fetch FRED series %s: %s", sid, exc)
        if latest_dates:
            raw["as_of"] = max(latest_dates)
    except Exception as exc:
        logger.error("Macro data failure: %s", exc)

    return score_macro(raw, cfg.macro)


def run_asset(symbol: str, macro: MacroState, cfg: AppCfg) -> AssetResult:
    """Fetch market data, score tactically, and fuse — for one asset."""
    from iidca.engines.fusion import decide
    from iidca.engines.tactical import _fail_safe, score_tactical
    from iidca.providers.market import fetch_ohlcv

    try:
        fetched = fetch_ohlcv(symbol, cfg)
        tech = score_tactical(fetched.df, cfg.tactical, symbol)
        if not fetched.fresh:
            # Served from stale cache after provider failures — keep the
            # numbers visible but force the fail-safe decision path.
            tech.data_ok = False
        source, fresh, errors = fetched.source, fetched.fresh, fetched.errors
    except Exception as exc:
        logger.error("All market data sources failed for %s: %s", symbol, exc)
        tech = _fail_safe(symbol)
        source, fresh, errors = "none", False, [str(exc)]

    decision = decide(macro, tech, cfg.fusion)
    return AssetResult(tech=tech, decision=decision, source=source,
                       fresh=fresh, errors=errors)


def run_cycle(cfg: AppCfg, symbols: list[str] | None = None) -> CycleResult:
    """Execute one full pipeline cycle for all (or given) symbols.

    Steps: fetch FRED → score macro (once, global) → per asset:
           fetch market data → score tactical → fuse → persist snapshots.
    """
    from iidca.storage import (
        get_watchlist,
        persist_asset_snapshot,
        persist_macro_snapshot,
    )

    run_ts = datetime.now(tz=UTC)
    macro = fetch_macro_state(cfg)

    if symbols is None:
        symbols = get_watchlist(seed=cfg.watchlist)

    assets: dict[str, AssetResult] = {}
    for symbol in symbols:
        assets[symbol] = run_asset(symbol, macro, cfg)

    try:
        persist_macro_snapshot(macro, cfg, run_ts)
        for res in assets.values():
            persist_asset_snapshot(
                res.tech, res.decision, cfg, run_ts,
                data_source=res.source, data_fresh=res.fresh,
            )
    except Exception as exc:
        logger.warning("Could not persist snapshots: %s", exc)

    return CycleResult(run_ts=run_ts, macro=macro, assets=assets)


# ---------------------------------------------------------------------------
# Terminal output
# ---------------------------------------------------------------------------

def _print_cycle(result: CycleResult) -> None:
    macro = result.macro
    color_map = {"green": "green", "amber": "yellow", "red": "red"}

    any_decision = next(iter(result.assets.values())).decision if result.assets else None
    status = any_decision.status if any_decision else macro.regime
    c = color_map.get(any_decision.color if any_decision else "amber", "white")

    console.print()
    console.print(Panel(
        f"[bold {c}]{status}[/bold {c}]\n"
        f"Macro Health H = {macro.H:.3f}   regime: {macro.regime}",
        title="Global Macro",
        border_style=c,
    ))

    table = Table(box=None, padding=(0, 2))
    table.add_column("Symbol", style="bold")
    table.add_column("Price", justify="right")
    table.add_column("Z (trend)", justify="right")
    table.add_column("Vol", justify="right")
    table.add_column("M", justify="right", style="bold")
    table.add_column("Signal")
    table.add_column("Data")

    for symbol, res in result.assets.items():
        t, d = res.tech, res.decision
        data_note = res.source if res.fresh else f"[red]stale ({res.source})[/red]"
        if not t.data_ok and res.fresh:
            data_note = "[red]fail-safe[/red]"
        table.add_row(
            symbol,
            f"{t.price:,.2f}" if t.price == t.price else "—",
            f"{t.z:+.2f}",
            f"{t.vol_factor:.2f}",
            f"{d.M:.2f}×",
            d.label,
            data_note,
        )
    console.print(table)

    if macro.breakers_fired or macro.soft_breakers_fired:
        fired = ", ".join(macro.breakers_fired + macro.soft_breakers_fired)
        console.print(f"[red]⚠ Breakers:[/red] {fired}")
    if not macro.data_ok:
        console.print("[red]⚠ Macro data failure — fail-safe M=1.0 applied.[/red]")
    console.print()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

@click.command()
@click.option("--config", "config_path", type=click.Path(exists=True, path_type=Path),
              default=None, help="Path to TOML config file.")
@click.option("--symbol", default=None, help="Run a single symbol (e.g. SPY).")
@click.option("--alert", is_flag=True, default=False, help="Send Telegram/email alert.")
@click.option("--verbose", is_flag=True, default=False, help="Enable debug logging.")
def cli(config_path: Path | None, symbol: str | None, alert: bool, verbose: bool) -> None:
    """Run one DCA cycle and print the recommendations."""
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )

    cfg = load_config(config_path)
    symbols = [symbol.upper()] if symbol else None

    console.print("[dim]Running DCA cycle…[/dim]")
    try:
        result = run_cycle(cfg, symbols=symbols)
    except Exception as exc:
        console.print(f"[red]Fatal error:[/red] {exc}")
        logger.exception("Unhandled error in run_cycle")
        sys.exit(1)

    _print_cycle(result)

    if alert:
        from iidca.alerts import send_alert  # noqa: PLC0415
        from iidca.storage import mark_alerted  # noqa: PLC0415
        ok = send_alert(result)
        if ok:
            mark_alerted(result.run_ts)
        console.print("[green]Alert sent.[/green]" if ok else "[red]Alert failed.[/red]")


if __name__ == "__main__":
    cli()
