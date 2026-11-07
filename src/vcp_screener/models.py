"""Plain dataclasses shared by the engine, loader and CLI."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date
from enum import Enum
from typing import Any, Optional


@dataclass(frozen=True)
class Candle:
    """One daily OHLCV bar."""

    date: date
    open: float
    high: float
    low: float
    close: float
    volume: int

    def validate(self) -> None:
        if self.high < self.low:
            raise ValueError(f"{self.date}: high {self.high} < low {self.low}")
        if not (self.low <= self.open <= self.high):
            raise ValueError(f"{self.date}: open {self.open} outside [{self.low}, {self.high}]")
        if not (self.low <= self.close <= self.high):
            raise ValueError(f"{self.date}: close {self.close} outside [{self.low}, {self.high}]")
        if self.low <= 0:
            raise ValueError(f"{self.date}: non-positive price {self.low}")
        if self.volume < 0:
            raise ValueError(f"{self.date}: negative volume {self.volume}")


class SwingKind(str, Enum):
    HIGH = "high"
    LOW = "low"


@dataclass(frozen=True)
class SwingPoint:
    """A zig-zag pivot. ``confirmed`` is False for the still-forming last extreme."""

    index: int
    price: float
    kind: SwingKind
    confirmed: bool = True


@dataclass(frozen=True)
class VCPStage:
    """One contraction leg: a swing high down to the following swing low."""

    number: int
    high_index: int
    low_index: int
    high: float
    low: float
    depth_pct: float
    bars: int
    avg_volume: float
    confirmed: bool = True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class SetupStatus(str, Enum):
    FORMING = "forming"      # close below pivot, within allowed distance
    BREAKOUT = "breakout"    # close above pivot, within breakout tolerance
    EXTENDED = "extended"    # close too far above the pivot
    TOO_FAR = "too_far"      # close too far below the pivot
    NONE = "none"            # no usable pattern


@dataclass
class ScreeningResult:
    symbol: str
    bars: int
    last_date: Optional[date]
    last_close: float
    passed: bool
    score: float
    status: SetupStatus
    stages: list[VCPStage] = field(default_factory=list)
    pivot_price: Optional[float] = None
    distance_to_pivot_pct: Optional[float] = None
    volume_dry_up_ratio: Optional[float] = None
    stage_volume_declining: Optional[bool] = None
    trend_ok: Optional[bool] = None
    reasons: list[str] = field(default_factory=list)

    @property
    def contraction_count(self) -> int:
        return len(self.stages)

    @property
    def depths(self) -> list[float]:
        return [s.depth_pct for s in self.stages]

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "bars": self.bars,
            "last_date": self.last_date.isoformat() if self.last_date else None,
            "last_close": self.last_close,
            "passed": self.passed,
            "score": self.score,
            "status": self.status.value,
            "contractions": self.contraction_count,
            "depths_pct": self.depths,
            "stages": [s.to_dict() for s in self.stages],
            "pivot_price": self.pivot_price,
            "distance_to_pivot_pct": self.distance_to_pivot_pct,
            "volume_dry_up_ratio": self.volume_dry_up_ratio,
            "stage_volume_declining": self.stage_volume_declining,
            "trend_ok": self.trend_ok,
            "reasons": list(self.reasons),
        }
