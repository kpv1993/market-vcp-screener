"""Load daily OHLCV candles from local CSV/JSON files, or synthesise mock data.

Accepted CSV header names (case-insensitive): ``date``/``timestamp``/``time``,
``open``, ``high``, ``low``, ``close``, ``volume``/``vol``. Extra columns such
as ``adj close`` are ignored. JSON may be a list of candle objects or an
object ``{"symbol": "...", "candles": [...]}``.

The mock generator is seeded, so the same ``(profile, bars, seed)`` always
yields the same candles.
"""

from __future__ import annotations

import csv
import json
import random
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable, Iterator, Sequence

from .models import Candle

SUPPORTED_SUFFIXES = {".csv", ".json"}

_DATE_KEYS = ("date", "timestamp", "time", "datetime")
_VOLUME_KEYS = ("volume", "vol")


class DataError(ValueError):
    """Raised for malformed or inconsistent input files."""


# --------------------------------------------------------------------------- #
# Parsing helpers
# --------------------------------------------------------------------------- #
def _parse_date(raw: str) -> date:
    s = str(raw).strip()
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%d-%m-%Y", "%d/%m/%Y", "%Y%m%d", "%m/%d/%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(s).date()
    except ValueError as exc:
        raise DataError(f"unparseable date {raw!r}") from exc


def _pick(row: dict[str, str], keys: Iterable[str]) -> str:
    for k in keys:
        if k in row and row[k] not in (None, ""):
            return row[k]
    raise DataError(f"missing column: one of {list(keys)}")


def _candle_from_mapping(row: dict, line_no: int) -> Candle:
    norm = {str(k).strip().lower(): v for k, v in row.items() if k is not None}
    try:
        candle = Candle(
            date=_parse_date(_pick(norm, _DATE_KEYS)),
            open=float(_pick(norm, ("open",))),
            high=float(_pick(norm, ("high",))),
            low=float(_pick(norm, ("low",))),
            close=float(_pick(norm, ("close",))),
            volume=int(float(_pick(norm, _VOLUME_KEYS))),
        )
        candle.validate()
    except (DataError, ValueError) as exc:
        raise DataError(f"row {line_no}: {exc}") from exc
    return candle


def _finalise(candles: list[Candle], source: str) -> list[Candle]:
    if not candles:
        raise DataError(f"{source}: no candles found")
    by_date: dict[date, Candle] = {}
    for c in candles:  # last duplicate wins, deterministic
        by_date[c.date] = c
    return [by_date[d] for d in sorted(by_date)]


# --------------------------------------------------------------------------- #
# Public loaders
# --------------------------------------------------------------------------- #
def load_csv(path: str | Path) -> list[Candle]:
    path = Path(path)
    with path.open(newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        if not reader.fieldnames:
            raise DataError(f"{path}: empty file")
        candles = [_candle_from_mapping(row, i + 2) for i, row in enumerate(reader)]
    return _finalise(candles, str(path))


def load_json(path: str | Path) -> list[Candle]:
    path = Path(path)
    with path.open(encoding="utf-8") as fh:
        payload = json.load(fh)
    if isinstance(payload, dict):
        rows = payload.get("candles") or payload.get("data")
        if rows is None:
            raise DataError(f"{path}: JSON object needs a 'candles' list")
    elif isinstance(payload, list):
        rows = payload
    else:
        raise DataError(f"{path}: unsupported JSON top-level type")
    candles = [_candle_from_mapping(row, i + 1) for i, row in enumerate(rows)]
    return _finalise(candles, str(path))


def load_candles(path: str | Path) -> list[Candle]:
    """Dispatch on file extension."""
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix == ".csv":
        return load_csv(path)
    if suffix == ".json":
        return load_json(path)
    raise DataError(f"{path}: unsupported extension {suffix!r} (use .csv or .json)")


def symbol_from_path(path: str | Path) -> str:
    return Path(path).stem.upper()


def iter_data_files(paths: Sequence[str | Path]) -> Iterator[Path]:
    """Expand files and directories into a sorted, de-duplicated file list."""
    seen: set[Path] = set()
    out: list[Path] = []
    for raw in paths:
        p = Path(raw)
        if p.is_dir():
            found = sorted(q for q in p.iterdir() if q.suffix.lower() in SUPPORTED_SUFFIXES)
        elif p.is_file():
            found = [p]
        else:
            raise DataError(f"{p}: no such file or directory")
        for q in found:
            r = q.resolve()
            if r not in seen:
                seen.add(r)
                out.append(q)
    return iter(out)


def load_many(paths: Sequence[str | Path]) -> dict[str, list[Candle]]:
    """Load every supported file under ``paths`` keyed by symbol (file stem)."""
    series: dict[str, list[Candle]] = {}
    for file in iter_data_files(paths):
        series[symbol_from_path(file)] = load_candles(file)
    return series


# --------------------------------------------------------------------------- #
# Writers
# --------------------------------------------------------------------------- #
def write_csv(path: str | Path, candles: Sequence[Candle]) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["date", "open", "high", "low", "close", "volume"])
        for c in candles:
            writer.writerow(
                [c.date.isoformat(), f"{c.open:.4f}", f"{c.high:.4f}", f"{c.low:.4f}", f"{c.close:.4f}", c.volume]
            )
    return path


# --------------------------------------------------------------------------- #
# Mock data
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class _Segment:
    target: float      # price at end of segment
    bars: int
    volume: float      # volume multiplier for this segment


MOCK_PROFILES: dict[str, str] = {
    "vcp": "uptrend into a textbook 3-stage contraction (20% / 12% / 6%) with drying volume",
    "flat": "sideways chop with no trend and steady volume",
    "volatile": "expanding swings (10% / 18% / 28%) on heavy, rising volume",
    "downtrend": "persistent decline with lower highs and lower lows",
    "breakout": "same base as 'vcp' but the last bars close above the pivot",
}


def _profile_segments(profile: str) -> tuple[float, list[_Segment], float]:
    """Return (start price, segments, noise level)."""
    if profile == "vcp":
        segs = [
            _Segment(100.0, 200, 1.0),   # long uptrend 40 -> 100
            _Segment(80.0, 15, 1.3),     # stage 1: -20%
            _Segment(98.0, 15, 1.0),
            _Segment(86.2, 10, 0.8),     # stage 2: -12%
            _Segment(97.0, 10, 0.7),
            _Segment(91.2, 8, 0.6),      # stage 3: -6%
            _Segment(95.5, 6, 0.5),
            _Segment(95.6, 6, 0.4),      # tight, dry close near pivot
        ]
        return 40.0, segs, 0.003
    if profile == "breakout":
        segs = [
            _Segment(100.0, 200, 1.0),
            _Segment(80.0, 15, 1.3),
            _Segment(98.0, 15, 1.0),
            _Segment(86.2, 10, 0.8),
            _Segment(97.0, 10, 0.7),
            _Segment(91.2, 8, 0.6),
            _Segment(95.5, 6, 0.5),
            _Segment(95.6, 4, 0.4),
            _Segment(99.0, 2, 0.6),      # pushes through the 97 pivot
        ]
        return 40.0, segs, 0.003
    if profile == "flat":
        segs = [_Segment(100.0 + (1.5 if i % 2 else -1.5), 13, 1.0) for i in range(20)]
        return 100.0, segs, 0.006
    if profile == "volatile":
        segs = [
            _Segment(100.0, 140, 1.0),
            _Segment(90.0, 10, 1.2),     # -10%
            _Segment(100.0, 10, 1.3),
            _Segment(82.0, 12, 1.5),     # -18%
            _Segment(99.0, 12, 1.7),
            _Segment(71.3, 14, 2.0),     # -28%
            _Segment(90.0, 14, 2.2),
            _Segment(80.0, 10, 2.4),
            _Segment(88.0, 8, 2.6),
            _Segment(84.0, 5, 2.5),
            _Segment(86.0, 5, 2.5),
        ]
        return 60.0, segs, 0.012
    if profile == "downtrend":
        segs = [
            _Segment(90.0, 40, 1.0),
            _Segment(75.0, 40, 1.2),
            _Segment(82.0, 20, 0.9),
            _Segment(60.0, 60, 1.3),
            _Segment(66.0, 20, 0.9),
            _Segment(48.0, 60, 1.4),
            _Segment(50.0, 20, 1.0),
        ]
        return 100.0, segs, 0.006
    raise ValueError(f"unknown mock profile {profile!r}; choose from {sorted(MOCK_PROFILES)}")


def _trading_days(start: date, count: int) -> list[date]:
    days: list[date] = []
    d = start
    while len(days) < count:
        if d.weekday() < 5:
            days.append(d)
        d += timedelta(days=1)
    return days


def generate_mock_candles(
    profile: str = "vcp",
    bars: int = 260,
    seed: int = 42,
    start: date = date(2025, 1, 2),
    base_volume: int = 1_000_000,
) -> list[Candle]:
    """Deterministic synthetic daily candles for a named profile.

    The profile's shape is defined over its natural length; ``bars`` longer
    than that are prepended as a gentle lead-in, shorter values truncate the
    *front* so the pattern at the end survives.
    """
    if bars < 2:
        raise ValueError("bars must be >= 2")
    start_price, segments, noise = _profile_segments(profile)
    rng = random.Random(f"{profile}:{seed}")

    closes: list[float] = []
    vol_mult: list[float] = []
    price = start_price
    for seg in segments:
        step = (seg.target - price) / seg.bars
        for _ in range(seg.bars):
            price += step
            closes.append(price * (1.0 + rng.uniform(-noise, noise)))
            vol_mult.append(seg.volume * (1.0 + rng.uniform(-0.15, 0.15)))

    natural = len(closes)
    if bars > natural:  # prepend a flat-ish lead-in at the starting price
        pad = bars - natural
        lead = [start_price * (1.0 + rng.uniform(-noise, noise)) for _ in range(pad)]
        lead_v = [1.0 * (1.0 + rng.uniform(-0.15, 0.15)) for _ in range(pad)]
        closes = lead + closes
        vol_mult = lead_v + vol_mult
    elif bars < natural:
        closes = closes[-bars:]
        vol_mult = vol_mult[-bars:]

    days = _trading_days(start, len(closes))
    candles: list[Candle] = []
    prev_close = closes[0]
    for i, (d, close) in enumerate(zip(days, closes)):
        open_ = prev_close if i else close
        wick = abs(close) * noise * 1.5
        high = max(open_, close) * (1.0 + rng.uniform(0, noise)) + wick
        low = min(open_, close) * (1.0 - rng.uniform(0, noise)) - wick
        volume = int(base_volume * vol_mult[i])
        candles.append(
            Candle(
                date=d,
                open=round(open_, 4),
                high=round(high, 4),
                low=round(max(low, 0.01), 4),
                close=round(close, 4),
                volume=volume,
            )
        )
        prev_close = close
    return candles


def mock_series(
    profiles: Sequence[str] | None = None, bars: int = 260, seed: int = 42
) -> dict[str, list[Candle]]:
    """Map of ``MOCK_<PROFILE>`` -> candles for the requested profiles."""
    names = list(profiles) if profiles else list(MOCK_PROFILES)
    return {f"MOCK_{p.upper()}": generate_mock_candles(p, bars=bars, seed=seed) for p in names}
