"""T11 — CLI entrypoint: one full DCA cycle (§9.2, §10.7).

Usage
-----
  python -m iidca.run                   # print Decision to stdout
  python -m iidca.run --alert           # also push Telegram/email alert
  python -m iidca.run --config cfg.toml # custom config file
  python -m iidca.run --symbol SPY      # override target symbol
  iidca                                 # same as above (installed script)

Definition of Done §12-item-1:
  Must fetch live FRED (STLFSI4) + market data, print a Decision with
  M, label, instruction, and Zone 1 status.

Fail-safe (§1.3-#3):
  Any data / compute failure degrades to M=1.0 Standard and prints a
  clear error.  The system never silently emits an aggressive allocation.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import click
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from iidca.config import AppCfg, load_config
from iidca.models import Decision, MacroState, TechnicalState

console = Console()
logger = logging.getLogger(__name__)


def run_cycle(cfg: AppCfg) -> Decision:
    """Execute one full pipeline cycle and return the Decision.

    Steps: ingest FRED → score macro → ingest market data → score tactical
           → fuse → persist snapshot.
    """
    from iidca.engines.fusion import decide
    from iidca.engines.macro import score_macro
    from iidca.engines.tactical import score_tactical
    from iidca.providers.fred import make_fred_provider
    from iidca.storage import persist_snapshot

    # ── 1. FRED data ─────────────────────────────────────────────────────────
    macro_state: MacroState
    try:
        fred = make_fred_provider()
        raw: dict = {}
        for key, sid in cfg.macro.series.items():
            if sid:
                try:
                    raw[key] = float(fred.series(sid).iloc[-1])
                except Exception as exc:
                    logger.warning("Could not fetch FRED series %s: %s", sid, exc)
        macro_state = score_macro(raw, cfg.macro)
    except Exception as exc:
        logger.error("Macro data failure: %s", exc)
        from iidca.models import MacroState as MS  # noqa: PLC0415
        macro_state = MS(
            H=0.5, regime="CAUTION", subscores={}, raw={}, data_ok=False
        )

    # ── 2. Market data ────────────────────────────────────────────────────────
    tech_state: TechnicalState
    try:
        provider = _make_market_provider(cfg.market_provider)
        df = provider.ohlcv(cfg.target_symbol)
        tech_state = score_tactical(df, cfg.tactical, cfg.target_symbol)
    except Exception as exc:
        logger.error("Market data failure: %s", exc)
        from iidca.models import TechnicalState as TS  # noqa: PLC0415
        tech_state = TS(
            symbol=cfg.target_symbol,
            price=0.0, sma200=0.0, sma_slope=0.0,
            z=0.0, atr_pct=0.0, vol_factor=1.0,
            adx=0.0, rsi=50.0, trend_strong_down=False,
            data_ok=False,
        )

    # ── 3. Fuse ───────────────────────────────────────────────────────────────
    decision = decide(macro_state, tech_state, cfg.fusion)

    # ── 4. Persist ────────────────────────────────────────────────────────────
    try:
        persist_snapshot(macro_state, tech_state, decision, cfg)
    except Exception as exc:
        logger.warning("Could not persist snapshot: %s", exc)

    return decision


def _make_market_provider(name: str):
    if name == "yfinance":
        from iidca.providers.yfinance_provider import YFinanceProvider  # noqa: PLC0415
        return YFinanceProvider()
    if name == "stooq":
        from iidca.providers.stooq_provider import StooqProvider  # noqa: PLC0415
        return StooqProvider()
    if name == "tiingo":
        from iidca.providers.tiingo_provider import TiingoProvider  # noqa: PLC0415
        return TiingoProvider()
    if name == "tradingview":
        from iidca.providers.tradingview_webhook import TradingViewProvider  # noqa: PLC0415
        return TradingViewProvider()
    raise ValueError(f"Unknown market_provider: {name!r}")


def _print_decision(decision: Decision, macro, tech) -> None:
    color_map = {"green": "green", "amber": "yellow", "red": "red"}
    c = color_map.get(decision.color, "white")

    console.print()
    console.print(Panel(
        f"[bold {c}]{decision.status}[/bold {c}]",
        title="Zone 1 — Global System Status",
        border_style=c,
    ))

    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_row("DCA Multiplier", f"[bold]{decision.M:.3f}×[/bold]  {decision.label}")
    table.add_row("Instruction", f"[italic]{decision.instruction}[/italic]")
    table.add_row("Macro Health H", f"{macro.H:.3f}  (regime: {macro.regime})")
    table.add_row("Z-score", f"{tech.z:.3f}")
    table.add_row("ATR%", f"{tech.atr_pct:.4f}")
    table.add_row("ADX", f"{tech.adx:.1f}")
    table.add_row("RSI", f"{tech.rsi:.1f}")
    if macro.breakers_fired:
        table.add_row("[red]⚠ Breakers[/red]", ", ".join(macro.breakers_fired))
    if not (macro.data_ok and tech.data_ok):
        table.add_row("[red]⚠ Data[/red]", "FAIL-SAFE — one or more data sources failed")
    console.print(table)
    console.print()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

@click.command()
@click.option("--config", "config_path", type=click.Path(exists=True, path_type=Path),
              default=None, help="Path to TOML config file.")
@click.option("--symbol", default=None, help="Override target symbol (e.g. SPY).")
@click.option("--alert", is_flag=True, default=False, help="Send Telegram/email alert.")
@click.option("--verbose", is_flag=True, default=False, help="Enable debug logging.")
def cli(config_path: Path | None, symbol: str | None, alert: bool, verbose: bool) -> None:
    """Run one DCA cycle and print the recommendation."""
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )

    cfg = load_config(config_path)
    if symbol:
        cfg = cfg.model_copy(update={"target_symbol": symbol})

    console.print("[dim]Running DCA cycle…[/dim]")
    try:
        decision = run_cycle(cfg)
    except Exception as exc:
        console.print(f"[red]Fatal error:[/red] {exc}")
        logger.exception("Unhandled error in run_cycle")
        sys.exit(1)

    # Re-fetch for display (already persisted above)
    from iidca.storage import get_latest_snapshot  # noqa: PLC0415
    snap = get_latest_snapshot()

    # Reconstruct lightweight display objects from snapshot
    from iidca.models import MacroState, TechnicalState  # noqa: PLC0415
    _m = MacroState(
        H=snap["H"] if snap else 0.5,
        regime=snap["regime"] if snap else "CAUTION",
        subscores={},
        raw={},
        breakers_fired=[],
        data_ok=snap["data_ok"] if snap else True,
    )
    _t = TechnicalState(
        symbol=cfg.target_symbol,
        price=0.0, sma200=0.0, sma_slope=0.0,
        z=snap["Z"] if snap else 0.0,
        atr_pct=snap["atr_pct"] if snap else 0.0,
        vol_factor=1.0,
        adx=snap["adx"] if snap else 0.0,
        rsi=snap["rsi"] if snap else 50.0,
        trend_strong_down=False,
        data_ok=snap["data_ok"] if snap else True,
    )
    _print_decision(decision, _m, _t)

    if alert:
        from iidca.alerts import send_alert  # noqa: PLC0415
        from iidca.storage import mark_alerted  # noqa: PLC0415
        if snap and snap.get("alerted_at") is not None:
            console.print("[dim]Alert already sent for this snapshot — skipping.[/dim]")
        else:
            # Reconstruct full MacroState for alert
            from iidca.engines.macro import score_macro  # noqa: PLC0415
            from iidca.providers.fred import make_fred_provider  # noqa: PLC0415
            try:
                fred = make_fred_provider()
                raw = {k: float(fred.series(sid).iloc[-1])
                       for k, sid in cfg.macro.series.items() if sid}
                macro_full = score_macro(raw, cfg.macro)
            except Exception:
                macro_full = _m
            ok = send_alert(macro_full, decision)
            if ok and snap:
                from datetime import datetime  # noqa: PLC0415
                mark_alerted(snap["run_ts"])
            console.print("[green]Alert sent.[/green]" if ok else "[red]Alert failed.[/red]")


if __name__ == "__main__":
    cli()
