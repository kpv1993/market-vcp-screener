"""market-vcp-screener: deterministic Minervini VCP detection over daily OHLCV data."""

from .engine import ScreenerConfig, screen
from .models import Candle, ScreeningResult, SwingPoint, VCPStage

__version__ = "0.1.0"

__all__ = [
    "Candle",
    "ScreeningResult",
    "ScreenerConfig",
    "SwingPoint",
    "VCPStage",
    "screen",
    "__version__",
]
