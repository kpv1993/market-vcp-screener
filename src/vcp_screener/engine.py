"""Pure, deterministic math for VCP detection.

Nothing in this module touches I/O, randomness or wall-clock time. Given the
same candles and the same ``ScreenerConfig`` it always returns the same result.

Pipeline (see :func:`screen`):

1. Moving averages (SMA/EMA) and the Minervini trend template.
2. Zig-zag swing detection with a percentage reversal threshold.
3. Contraction stages: successive swing-high -> swing-low legs whose depth
   shrinks by at least ``contraction_ratio`` each time.
4. Volume dry-up: recent average volume versus the 50-day average.
5. Pivot proximity and a composite 0-100 score.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional, Sequence

from .models import (
    Candle,
    ScreeningResult,
    SetupStatus,
    SwingKind,
    SwingPoint,
    VCPStage,
)


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ScreenerConfig:
    """All tunable thresholds. Defaults follow common VCP rules of thumb."""

    lookback: int = 120                 # bars searched for the base
    reversal_pct: float = 3.0           # zig-zag reversal threshold
    min_contractions: int = 2
    max_contractions: int = 6
    contraction_ratio: float = 0.8      # each depth <= previous depth * ratio
    max_base_depth_pct: float = 35.0    # first (deepest) contraction
    max_final_depth_pct: float = 10.0   # last (tightest) contraction
    high_tolerance_pct: float = 3.0     # later highs may exceed earlier by this
    volume_sma_period: int = 50
    dry_up_bars: int = 5
    dry_up_ratio: float = 0.7           # recent vol / 50d vol must be <= this
    max_pivot_distance_pct: float = 10.0
    breakout_tolerance_pct: float = 5.0
    require_trend: bool = True
    min_bars: int = 60

    def validate(self) -> None:
        if self.lookback < 10:
            raise ValueError("lookback must be >= 10")
        if self.reversal_pct <= 0:
            raise ValueError("reversal_pct must be > 0")
        if self.min_contractions < 1:
            raise ValueError("min_contractions must be >= 1")
        if self.max_contractions < self.min_contractions:
            raise ValueError("max_contractions must be >= min_contractions")
        if not (0 < self.contraction_ratio <= 1):
            raise ValueError("contraction_ratio must be in (0, 1]")
        if self.dry_up_bars < 1 or self.volume_sma_period < 1:
            raise ValueError("volume windows must be >= 1")
        if self.min_bars < 2:
            raise ValueError("min_bars must be >= 2")


# --------------------------------------------------------------------------- #
# Moving averages
# --------------------------------------------------------------------------- #
def sma(values: Sequence[float], period: int) -> list[Optional[float]]:
    """Simple moving average. ``None`` until ``period`` values are available."""
    if period < 1:
        raise ValueError("period must be >= 1")
    out: list[Optional[float]] = [None] * len(values)
    for i in range(period - 1, len(values)):
        out[i] = math.fsum(values[i - period + 1 : i + 1]) / period
    return out


def ema(values: Sequence[float], period: int) -> list[Optional[float]]:
    """Exponential moving average seeded with the SMA of the first ``period`` values."""
    if period < 1:
        raise ValueError("period must be >= 1")
    out: list[Optional[float]] = [None] * len(values)
    if len(values) < period:
        return out
    alpha = 2.0 / (period + 1)
    prev = math.fsum(values[:period]) / period
    out[period - 1] = prev
    for i in range(period, len(values)):
        prev = alpha * values[i] + (1.0 - alpha) * prev
        out[i] = prev
    return out


def average_true_range(candles: Sequence[Candle], period: int = 14) -> list[Optional[float]]:
    """Wilder ATR. Provided for callers that want a volatility read-out."""
    if period < 1:
        raise ValueError("period must be >= 1")
    out: list[Optional[float]] = [None] * len(candles)
    if len(candles) < period + 1:
        return out
    trs: list[float] = []
    for i in range(1, len(candles)):
        c, p = candles[i], candles[i - 1]
        trs.append(max(c.high - c.low, abs(c.high - p.close), abs(c.low - p.close)))
    prev = math.fsum(trs[:period]) / period
    out[period] = prev
    for i in range(period, len(trs)):
        prev = (prev * (period - 1) + trs[i]) / period
        out[i + 1] = prev
    return out


# --------------------------------------------------------------------------- #
# Trend template
# --------------------------------------------------------------------------- #
def trend_template(candles: Sequence[Candle]) -> Optional[bool]:
    """Simplified Minervini trend template.

    Returns ``None`` when fewer than 200 bars are available, otherwise True iff:
    close > SMA50 > SMA150 > SMA200, SMA200 is not falling over the last 20
    bars, close >= 1.25 x 52-week low and close >= 0.75 x 52-week high.
    """
    n = len(candles)
    if n < 200:
        return None
    closes = [c.close for c in candles]
    s50 = sma(closes, 50)
    s150 = sma(closes, 150)
    s200 = sma(closes, 200)
    close = closes[-1]
    last50, last150, last200 = s50[-1], s150[-1], s200[-1]
    assert last50 is not None and last150 is not None and last200 is not None
    if not (close > last50 > last150 > last200):
        return False
    if n >= 220:
        earlier = s200[-21]
        assert earlier is not None
        if last200 < earlier:
            return False
    window = candles[-252:]
    lo = min(c.low for c in window)
    hi = max(c.high for c in window)
    return close >= 1.25 * lo and close >= 0.75 * hi


# --------------------------------------------------------------------------- #
# Swing detection
# --------------------------------------------------------------------------- #
def find_swing_points(candles: Sequence[Candle], reversal_pct: float) -> list[SwingPoint]:
    """Percentage zig-zag over highs/lows.

    A swing high is confirmed once a later low trades ``reversal_pct`` below it;
    a swing low once a later high trades ``reversal_pct`` above it. The last,
    still-forming extreme is appended with ``confirmed=False`` so callers can
    treat the current pullback/rally as a provisional leg.
    """
    n = len(candles)
    if n == 0:
        return []
    thr = reversal_pct / 100.0
    pivots: list[SwingPoint] = []

    direction: Optional[SwingKind] = None  # kind of extreme currently tracked
    ext_idx = 0
    ext_hi = candles[0].high
    ext_lo = candles[0].low
    hi_idx = 0
    lo_idx = 0

    for i in range(1, n):
        c = candles[i]
        if direction is None:
            if c.high > ext_hi:
                ext_hi, hi_idx = c.high, i
            if c.low < ext_lo:
                ext_lo, lo_idx = c.low, i
            if c.low <= ext_hi * (1 - thr):
                pivots.append(SwingPoint(hi_idx, ext_hi, SwingKind.HIGH))
                direction = SwingKind.LOW
                ext_lo, ext_idx = c.low, i
            elif c.high >= ext_lo * (1 + thr):
                pivots.append(SwingPoint(lo_idx, ext_lo, SwingKind.LOW))
                direction = SwingKind.HIGH
                ext_hi, ext_idx = c.high, i
            continue

        if direction == SwingKind.HIGH:
            if c.high > ext_hi:
                ext_hi, ext_idx = c.high, i
            elif c.low <= ext_hi * (1 - thr):
                pivots.append(SwingPoint(ext_idx, ext_hi, SwingKind.HIGH))
                direction = SwingKind.LOW
                ext_lo, ext_idx = c.low, i
        else:
            if c.low < ext_lo:
                ext_lo, ext_idx = c.low, i
            elif c.high >= ext_lo * (1 + thr):
                pivots.append(SwingPoint(ext_idx, ext_lo, SwingKind.LOW))
                direction = SwingKind.HIGH
                ext_hi, ext_idx = c.high, i

    if direction == SwingKind.HIGH:
        pivots.append(SwingPoint(ext_idx, ext_hi, SwingKind.HIGH, confirmed=False))
    elif direction == SwingKind.LOW:
        pivots.append(SwingPoint(ext_idx, ext_lo, SwingKind.LOW, confirmed=False))
    return pivots


# --------------------------------------------------------------------------- #
# Contraction stages
# --------------------------------------------------------------------------- #
def _avg_volume(candles: Sequence[Candle], start: int, end: int) -> float:
    seg = candles[start : end + 1]
    return math.fsum(c.volume for c in seg) / len(seg) if seg else 0.0


def _make_stage(candles: Sequence[Candle], number: int, hi: SwingPoint, lo: SwingPoint) -> VCPStage:
    depth = (hi.price - lo.price) / hi.price * 100.0
    return VCPStage(
        number=number,
        high_index=hi.index,
        low_index=lo.index,
        high=hi.price,
        low=lo.price,
        depth_pct=depth,
        bars=lo.index - hi.index,
        avg_volume=_avg_volume(candles, hi.index, lo.index),
        confirmed=lo.confirmed,
    )


def _renumber(stages: Sequence[VCPStage]) -> list[VCPStage]:
    return [
        VCPStage(
            number=i + 1,
            high_index=s.high_index,
            low_index=s.low_index,
            high=s.high,
            low=s.low,
            depth_pct=s.depth_pct,
            bars=s.bars,
            avg_volume=s.avg_volume,
            confirmed=s.confirmed,
        )
        for i, s in enumerate(stages)
    ]


def detect_contractions(
    candles: Sequence[Candle],
    swings: Sequence[SwingPoint],
    config: ScreenerConfig,
) -> list[VCPStage]:
    """Build the chain of contracting legs ending at the most recent structure.

    Starting from the highest swing high inside the lookback window, legs are
    accumulated as (high, low) pairs. Whenever a later high breaks above the
    previous high by more than ``high_tolerance_pct`` or a leg fails to
    contract by ``contraction_ratio``, the chain restarts at that leg; so the
    returned stages always describe the *latest* valid sequence.
    """
    n = len(candles)
    if n == 0 or not swings:
        return []
    start_bar = max(0, n - config.lookback)
    window = [p for p in swings if p.index >= start_bar]
    highs = [p for p in window if p.kind == SwingKind.HIGH]
    if not highs:
        return []
    base = max(highs, key=lambda p: (p.price, -p.index))
    seq = [p for p in window if p.index >= base.index]

    stages: list[VCPStage] = []
    cur_high: Optional[SwingPoint] = None
    ratio = config.contraction_ratio
    tol = 1.0 + config.high_tolerance_pct / 100.0

    for p in seq:
        if p.kind == SwingKind.HIGH:
            if stages and p.price > stages[-1].high * tol:
                stages = []  # structure broken: restart from this high
            cur_high = p
            continue
        if cur_high is None:  # a low before any high: skip
            continue
        candidate = _make_stage(candles, len(stages) + 1, cur_high, p)
        if stages and candidate.depth_pct > stages[-1].depth_pct * ratio:
            stages = [candidate]  # failed to contract: new base leg
        else:
            stages.append(candidate)
        cur_high = None

    return _renumber(stages)


# --------------------------------------------------------------------------- #
# Volume
# --------------------------------------------------------------------------- #
def volume_dry_up_ratio(candles: Sequence[Candle], config: ScreenerConfig) -> Optional[float]:
    """Mean volume of the last ``dry_up_bars`` bars divided by the volume SMA."""
    vols = [float(c.volume) for c in candles]
    if len(vols) < max(config.volume_sma_period, config.dry_up_bars):
        return None
    base = sma(vols, config.volume_sma_period)[-1]
    if base is None or base <= 0:
        return None
    recent = math.fsum(vols[-config.dry_up_bars :]) / config.dry_up_bars
    return recent / base


def stage_volume_declining(stages: Sequence[VCPStage]) -> Optional[bool]:
    """True when every stage's average volume is <= the previous stage's."""
    if len(stages) < 2:
        return None
    return all(stages[i].avg_volume <= stages[i - 1].avg_volume for i in range(1, len(stages)))


# --------------------------------------------------------------------------- #
# Scoring and the top-level screen
# --------------------------------------------------------------------------- #
def _clamp01(x: float) -> float:
    return 0.0 if x < 0 else 1.0 if x > 1 else x


def compute_score(
    stages: Sequence[VCPStage],
    dry_up: Optional[float],
    distance_pct: Optional[float],
    trend_ok: Optional[bool],
    vol_declining: Optional[bool],
    config: ScreenerConfig,
) -> float:
    """Composite 0-100 score. Higher is a tighter, drier, closer-to-pivot setup.

    Weights: contraction count 25, final tightness 20, volume dry-up 20,
    stage-volume decline 10, pivot proximity 15, trend template 10.
    """
    if not stages:
        return 0.0
    count_part = 25.0 * _clamp01(len(stages) / 4.0)
    tight_part = 20.0 * _clamp01(1.0 - stages[-1].depth_pct / config.max_final_depth_pct)
    dry_part = 0.0 if dry_up is None else 20.0 * _clamp01(1.0 - dry_up / config.dry_up_ratio)
    vol_part = 10.0 if vol_declining else 0.0
    if distance_pct is None:
        prox_part = 0.0
    else:
        prox_part = 15.0 * _clamp01(1.0 - abs(distance_pct) / config.max_pivot_distance_pct)
    trend_part = 10.0 if trend_ok else 0.0
    return round(count_part + tight_part + dry_part + vol_part + prox_part + trend_part, 2)


def screen(
    symbol: str, candles: Sequence[Candle], config: ScreenerConfig | None = None
) -> ScreeningResult:
    """Run the full VCP screen on one symbol."""
    cfg = config or ScreenerConfig()
    cfg.validate()
    candles = list(candles)
    n = len(candles)
    last = candles[-1] if candles else None
    result = ScreeningResult(
        symbol=symbol,
        bars=n,
        last_date=last.date if last else None,
        last_close=last.close if last else 0.0,
        passed=False,
        score=0.0,
        status=SetupStatus.NONE,
    )
    if n < cfg.min_bars:
        result.reasons.append(f"insufficient history: {n} bars < {cfg.min_bars}")
        return result
    assert last is not None

    swings = find_swing_points(candles, cfg.reversal_pct)
    stages = detect_contractions(candles, swings, cfg)
    result.stages = stages
    reasons = result.reasons

    # --- contraction structure ------------------------------------------------
    if len(stages) < cfg.min_contractions:
        reasons.append(f"only {len(stages)} contraction(s), need >= {cfg.min_contractions}")
    if len(stages) > cfg.max_contractions:
        reasons.append(f"{len(stages)} contractions exceed max {cfg.max_contractions}")
    if stages:
        if stages[0].depth_pct > cfg.max_base_depth_pct:
            reasons.append(
                f"base depth {stages[0].depth_pct:.1f}% > {cfg.max_base_depth_pct:.1f}%"
            )
        if stages[-1].depth_pct > cfg.max_final_depth_pct:
            reasons.append(
                f"final contraction {stages[-1].depth_pct:.1f}% > {cfg.max_final_depth_pct:.1f}%"
            )

    # --- pivot / proximity ----------------------------------------------------
    distance: Optional[float] = None
    if stages:
        pivot = stages[-1].high
        result.pivot_price = pivot
        distance = (pivot - last.close) / pivot * 100.0
        result.distance_to_pivot_pct = distance
        if last.close < stages[-1].low:
            result.status = SetupStatus.TOO_FAR
            reasons.append("close undercut the final contraction low")
        elif distance < 0 and -distance > cfg.breakout_tolerance_pct:
            result.status = SetupStatus.EXTENDED
            reasons.append(
                f"extended {-distance:.1f}% above pivot (> {cfg.breakout_tolerance_pct:.1f}%)"
            )
        elif distance < 0:
            result.status = SetupStatus.BREAKOUT
        elif distance > cfg.max_pivot_distance_pct:
            result.status = SetupStatus.TOO_FAR
            reasons.append(
                f"close {distance:.1f}% below pivot (> {cfg.max_pivot_distance_pct:.1f}%)"
            )
        else:
            result.status = SetupStatus.FORMING

    # --- volume ---------------------------------------------------------------
    dry = volume_dry_up_ratio(candles, cfg)
    result.volume_dry_up_ratio = dry
    if dry is None:
        reasons.append("volume history too short for dry-up check")
    elif dry > cfg.dry_up_ratio:
        reasons.append(f"no volume dry-up: recent/avg = {dry:.2f} > {cfg.dry_up_ratio:.2f}")
    vol_decl = stage_volume_declining(stages)
    result.stage_volume_declining = vol_decl

    # --- trend ----------------------------------------------------------------
    trend = trend_template(candles)
    result.trend_ok = trend
    if cfg.require_trend:
        if trend is None:
            reasons.append(f"trend template needs >= 200 bars (have {n})")
        elif not trend:
            reasons.append("fails trend template (close > SMA50 > SMA150 > SMA200)")

    result.score = compute_score(stages, dry, distance, trend, vol_decl, cfg)
    result.passed = not reasons
    return result


def screen_many(
    series: dict[str, Sequence[Candle]], config: ScreenerConfig | None = None
) -> list[ScreeningResult]:
    """Screen several symbols; sorted by score desc, then symbol for stability."""
    results = [screen(sym, cs, config) for sym, cs in series.items()]
    results.sort(key=lambda r: (-r.score, r.symbol))
    return results
