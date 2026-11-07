"""Typer-based terminal entry point.

Commands:

* ``scan``      screen one or more CSV/JSON files (or directories of them)
* ``inspect``   show the contraction legs and metrics for a single file
* ``mock``      write deterministic synthetic fixtures to a directory
* ``profiles``  list the available mock profiles
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Optional

import typer
from rich import box
from rich.console import Console
from rich.table import Table

from . import __version__
from .data_loader import (
    MOCK_PROFILES,
    DataError,
    generate_mock_candles,
    load_candles,
    load_many,
    mock_series,
    symbol_from_path,
    write_csv,
)
from .engine import ScreenerConfig, find_swing_points, screen, screen_many
from .models import ScreeningResult, SetupStatus

app = typer.Typer(
    name="vcp-screener",
    help="Deterministic Minervini VCP + volume dry-up screener for daily OHLCV data.",
    no_args_is_help=True,
    add_completion=False,
)
# A real terminal reports its own width; piped/captured output gets a wide
# canvas so Rich does not fold every column into a vertical ribbon.
console = Console(width=None if sys.stdout.isatty() else 160)
err_console = Console(stderr=True)


def _version_callback(value: bool) -> None:
    if value:
        console.print(f"vcp-screener {__version__}")
        raise typer.Exit()


@app.callback()
def _root(
    version: bool = typer.Option(
        False, "--version", "-V", callback=_version_callback, is_eager=True, help="Show version."
    ),
) -> None:
    """Deterministic Minervini VCP + volume dry-up screener."""


# --------------------------------------------------------------------------- #
# Shared option plumbing
# --------------------------------------------------------------------------- #
def _config_from_options(
    lookback: int,
    reversal: float,
    min_contractions: int,
    max_contractions: int,
    contraction_ratio: float,
    max_base_depth: float,
    max_final_depth: float,
    dry_up_ratio: float,
    dry_up_bars: int,
    max_pivot_distance: float,
    require_trend: bool,
) -> ScreenerConfig:
    cfg = ScreenerConfig(
        lookback=lookback,
        reversal_pct=reversal,
        min_contractions=min_contractions,
        max_contractions=max_contractions,
        contraction_ratio=contraction_ratio,
        max_base_depth_pct=max_base_depth,
        max_final_depth_pct=max_final_depth,
        dry_up_ratio=dry_up_ratio,
        dry_up_bars=dry_up_bars,
        max_pivot_distance_pct=max_pivot_distance,
        require_trend=require_trend,
    )
    try:
        cfg.validate()
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    return cfg


LOOKBACK = typer.Option(120, "--lookback", min=10, help="Bars searched for the base.")
REVERSAL = typer.Option(3.0, "--reversal", min=0.1, help="Zig-zag reversal threshold (%).")
MIN_CONTR = typer.Option(2, "--min-contractions", min=1, help="Minimum contraction legs.")
MAX_CONTR = typer.Option(6, "--max-contractions", min=1, help="Maximum contraction legs.")
RATIO = typer.Option(0.8, "--contraction-ratio", min=0.05, max=1.0, help="Each leg depth <= previous * ratio.")
MAX_BASE = typer.Option(35.0, "--max-base-depth", help="Max depth of the first leg (%).")
MAX_FINAL = typer.Option(10.0, "--max-final-depth", help="Max depth of the last leg (%).")
DRY_RATIO = typer.Option(0.7, "--dry-up-ratio", help="Recent volume / 50d avg must be <= this.")
DRY_BARS = typer.Option(5, "--dry-up-bars", min=1, help="Bars averaged for the dry-up check.")
MAX_DIST = typer.Option(10.0, "--max-pivot-distance", help="Max % the close may sit below the pivot.")
TREND = typer.Option(True, "--trend/--no-trend", help="Require the Minervini trend template.")


def _fmt(x: Optional[float], nd: int = 2, suffix: str = "") -> str:
    return "-" if x is None else f"{x:.{nd}f}{suffix}"


def _tri(x: Optional[bool]) -> str:
    return "?" if x is None else ("yes" if x else "no")


STATUS_STYLE = {
    SetupStatus.FORMING: "green",
    SetupStatus.BREAKOUT: "bold green",
    SetupStatus.EXTENDED: "yellow",
    SetupStatus.TOO_FAR: "red",
    SetupStatus.NONE: "dim",
}


def _results_table(results: list[ScreeningResult]) -> Table:
    table = Table(title="VCP scan", box=box.SIMPLE_HEAD, pad_edge=False)
    table.add_column("Symbol", style="bold", no_wrap=True)
    table.add_column("Pass", no_wrap=True)
    table.add_column("Score", justify="right", no_wrap=True)
    table.add_column("Status", no_wrap=True)
    table.add_column("Legs", justify="right")
    table.add_column("Depths %", no_wrap=True)
    table.add_column("Pivot", justify="right")
    table.add_column("Close", justify="right")
    table.add_column("Dist %", justify="right")
    table.add_column("Vol dry", justify="right")
    table.add_column("Trend")
    for r in results:
        table.add_row(
            r.symbol,
            "[green]PASS[/green]" if r.passed else "[red]fail[/red]",
            f"{r.score:.1f}",
            f"[{STATUS_STYLE[r.status]}]{r.status.value}[/{STATUS_STYLE[r.status]}]",
            str(r.contraction_count),
            " > ".join(f"{d:.1f}" for d in r.depths) or "-",
            _fmt(r.pivot_price),
            f"{r.last_close:.2f}",
            _fmt(r.distance_to_pivot_pct, 1),
            _fmt(r.volume_dry_up_ratio),
            _tri(r.trend_ok),
        )
    return table


def _print_reasons(results: list[ScreeningResult]) -> None:
    failed = [r for r in results if r.reasons]
    if not failed:
        return
    console.print("[yellow]Reasons[/yellow]")
    for r in failed:
        console.print(f"  [bold]{r.symbol}[/bold]")
        for reason in r.reasons:
            console.print(f"    - {reason}")


# --------------------------------------------------------------------------- #
# scan
# --------------------------------------------------------------------------- #
@app.command()
def scan(
    paths: list[Path] = typer.Argument(
        None, help="CSV/JSON files or directories. Omit to scan built-in mock data.", show_default=False
    ),
    lookback: int = LOOKBACK,
    reversal: float = REVERSAL,
    min_contractions: int = MIN_CONTR,
    max_contractions: int = MAX_CONTR,
    contraction_ratio: float = RATIO,
    max_base_depth: float = MAX_BASE,
    max_final_depth: float = MAX_FINAL,
    dry_up_ratio: float = DRY_RATIO,
    dry_up_bars: int = DRY_BARS,
    max_pivot_distance: float = MAX_DIST,
    require_trend: bool = TREND,
    show_all: bool = typer.Option(False, "--all", "-a", help="Show failing symbols too."),
    min_score: float = typer.Option(0.0, "--min-score", help="Hide results scoring below this."),
    as_json: bool = typer.Option(False, "--json", help="Emit JSON instead of a table."),
    mock: bool = typer.Option(False, "--mock", help="Use built-in mock data (ignores PATHS)."),
) -> None:
    """Screen files for Volatility Contraction Patterns."""
    cfg = _config_from_options(
        lookback, reversal, min_contractions, max_contractions, contraction_ratio,
        max_base_depth, max_final_depth, dry_up_ratio, dry_up_bars, max_pivot_distance, require_trend,
    )
    try:
        if mock or not paths:
            if not paths and not mock:
                err_console.print("[dim]No paths given; scanning built-in mock data.[/dim]")
            series = mock_series()
        else:
            series = load_many(paths)
    except DataError as exc:
        err_console.print(f"[red]error:[/red] {exc}")
        raise typer.Exit(code=2)

    results = screen_many(series, cfg)
    shown = [r for r in results if (show_all or r.passed) and r.score >= min_score]

    if as_json:
        payload = {
            "config": cfg.__dict__,
            "scanned": len(results),
            "passed": sum(1 for r in results if r.passed),
            "results": [r.to_dict() for r in shown],
        }
        sys.stdout.write(json.dumps(payload, indent=2) + "\n")
        return

    console.print(_results_table(shown))
    if show_all:
        _print_reasons(shown)
    console.print(
        f"scanned {len(results)} symbol(s), {sum(1 for r in results if r.passed)} passed"
        + ("" if show_all else "  (use --all to see failures)")
    )


# --------------------------------------------------------------------------- #
# inspect
# --------------------------------------------------------------------------- #
@app.command()
def inspect(
    path: Path = typer.Argument(..., exists=True, dir_okay=False, help="One CSV/JSON file."),
    lookback: int = LOOKBACK,
    reversal: float = REVERSAL,
    min_contractions: int = MIN_CONTR,
    max_contractions: int = MAX_CONTR,
    contraction_ratio: float = RATIO,
    max_base_depth: float = MAX_BASE,
    max_final_depth: float = MAX_FINAL,
    dry_up_ratio: float = DRY_RATIO,
    dry_up_bars: int = DRY_BARS,
    max_pivot_distance: float = MAX_DIST,
    require_trend: bool = TREND,
    swings: bool = typer.Option(False, "--swings", help="Also list raw zig-zag swing points."),
    as_json: bool = typer.Option(False, "--json", help="Emit JSON instead of tables."),
) -> None:
    """Show contraction legs, volume and trend details for one symbol."""
    cfg = _config_from_options(
        lookback, reversal, min_contractions, max_contractions, contraction_ratio,
        max_base_depth, max_final_depth, dry_up_ratio, dry_up_bars, max_pivot_distance, require_trend,
    )
    try:
        candles = load_candles(path)
    except DataError as exc:
        err_console.print(f"[red]error:[/red] {exc}")
        raise typer.Exit(code=2)
    symbol = symbol_from_path(path)
    result = screen(symbol, candles, cfg)

    if as_json:
        payload = result.to_dict()
        if swings:
            payload["swings"] = [
                {"index": s.index, "date": candles[s.index].date.isoformat(), "price": s.price,
                 "kind": s.kind.value, "confirmed": s.confirmed}
                for s in find_swing_points(candles, cfg.reversal_pct)
            ]
        sys.stdout.write(json.dumps(payload, indent=2) + "\n")
        return

    verdict = "[green]PASS[/green]" if result.passed else "[red]FAIL[/red]"
    console.print(
        f"[bold]{symbol}[/bold]  {verdict}  score={result.score:.1f}  status={result.status.value}  "
        f"bars={result.bars}  last={result.last_date}  close={result.last_close:.2f}"
    )
    console.print(
        f"pivot={_fmt(result.pivot_price)}  distance={_fmt(result.distance_to_pivot_pct, 2, '%')}  "
        f"volume dry-up={_fmt(result.volume_dry_up_ratio)}  stage volume declining={_tri(result.stage_volume_declining)}  "
        f"trend={_tri(result.trend_ok)}"
    )

    legs = Table(title="Contraction legs")
    for col in ("#", "High date", "High", "Low date", "Low", "Depth %", "Bars", "Avg volume", "Confirmed"):
        legs.add_column(col, justify="right" if col not in ("High date", "Low date") else "left")
    for s in result.stages:
        legs.add_row(
            str(s.number),
            candles[s.high_index].date.isoformat(),
            f"{s.high:.2f}",
            candles[s.low_index].date.isoformat(),
            f"{s.low:.2f}",
            f"{s.depth_pct:.2f}",
            str(s.bars),
            f"{s.avg_volume:,.0f}",
            "yes" if s.confirmed else "no",
        )
    console.print(legs)

    if swings:
        sw = Table(title=f"Swing points (reversal {cfg.reversal_pct:.1f}%)")
        for col in ("Index", "Date", "Kind", "Price", "Confirmed"):
            sw.add_column(col)
        for s in find_swing_points(candles, cfg.reversal_pct):
            sw.add_row(str(s.index), candles[s.index].date.isoformat(), s.kind.value, f"{s.price:.2f}",
                       "yes" if s.confirmed else "no")
        console.print(sw)

    if result.reasons:
        console.print("[yellow]Reasons:[/yellow]")
        for reason in result.reasons:
            console.print(f"  - {reason}")


# --------------------------------------------------------------------------- #
# mock / profiles
# --------------------------------------------------------------------------- #
@app.command()
def mock(
    out_dir: Path = typer.Argument(..., help="Directory to write CSV files into."),
    profiles: Optional[list[str]] = typer.Option(
        None, "--profile", "-p", help="Profile(s) to generate (default: all)."
    ),
    bars: int = typer.Option(260, "--bars", min=2, help="Number of daily bars."),
    seed: int = typer.Option(42, "--seed", help="Deterministic seed."),
) -> None:
    """Write deterministic synthetic OHLCV CSV files."""
    names = profiles or list(MOCK_PROFILES)
    unknown = [p for p in names if p not in MOCK_PROFILES]
    if unknown:
        raise typer.BadParameter(f"unknown profile(s) {unknown}; choose from {sorted(MOCK_PROFILES)}")
    for name in names:
        candles = generate_mock_candles(name, bars=bars, seed=seed)
        target = write_csv(out_dir / f"MOCK_{name.upper()}.csv", candles)
        console.print(f"wrote {target} ({len(candles)} bars)")


@app.command()
def profiles() -> None:
    """List the built-in mock data profiles."""
    table = Table(title="Mock profiles")
    table.add_column("Name", style="bold")
    table.add_column("Description")
    for name, desc in MOCK_PROFILES.items():
        table.add_row(name, desc)
    console.print(table)


def main() -> None:  # pragma: no cover - thin wrapper
    app()


if __name__ == "__main__":  # pragma: no cover
    main()
