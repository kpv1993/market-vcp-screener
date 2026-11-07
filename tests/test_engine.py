"""Unit tests for the deterministic math in vcp_screener.engine."""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pytest

from vcp_screener.data_loader import generate_mock_candles, load_csv
from vcp_screener.engine import (
    ScreenerConfig,
    average_true_range,
    compute_score,
    detect_contractions,
    ema,
    find_swing_points,
    screen,
    screen_many,
    sma,
    stage_volume_declining,
    trend_template,
    volume_dry_up_ratio,
)
from vcp_screener.models import Candle, SetupStatus, SwingKind

FIXTURES = Path(__file__).parent / "fixtures"


def _candles_from_closes(closes, volume=1_000_000, spread=0.0):
    """Flat candles (open=high=low=close unless spread>0) for pure-math tests."""
    out = []
    d = date(2024, 1, 1)
    for i, c in enumerate(closes):
        v = volume[i] if isinstance(volume, (list, tuple)) else volume
        out.append(Candle(d + timedelta(days=i), c, c * (1 + spread), c * (1 - spread), c, v))
    return out


# --------------------------------------------------------------------------- #
# Moving averages
# --------------------------------------------------------------------------- #
class TestMovingAverages:
    def test_sma_basic(self):
        assert sma([1, 2, 3, 4, 5], 3) == [None, None, 2.0, 3.0, 4.0]

    def test_sma_period_longer_than_series(self):
        assert sma([1, 2], 5) == [None, None]

    def test_sma_rejects_bad_period(self):
        with pytest.raises(ValueError):
            sma([1, 2, 3], 0)

    def test_ema_seeded_with_sma_then_recurses(self):
        out = ema([1, 2, 3, 4, 5], 3)
        assert out[:2] == [None, None]
        assert out[2] == pytest.approx(2.0)
        alpha = 0.5
        assert out[3] == pytest.approx(alpha * 4 + (1 - alpha) * 2.0)
        assert out[4] == pytest.approx(alpha * 5 + (1 - alpha) * out[3])

    def test_ema_constant_series_is_constant(self):
        out = ema([7.0] * 20, 5)
        assert all(v == pytest.approx(7.0) for v in out[4:])

    def test_atr_constant_range(self):
        candles = _candles_from_closes([100.0] * 30, spread=0.01)
        out = average_true_range(candles, 14)
        assert out[13] is None
        assert out[14] == pytest.approx(2.0)  # high-low = 101 - 99
        assert out[-1] == pytest.approx(2.0)


# --------------------------------------------------------------------------- #
# Swing detection
# --------------------------------------------------------------------------- #
class TestSwings:
    def test_zigzag_alternates_and_marks_provisional(self):
        closes = [100, 103, 106, 110, 105, 100, 103, 108, 104, 101, 103]
        swings = find_swing_points(_candles_from_closes(closes), reversal_pct=3.0)
        kinds = [s.kind for s in swings]
        assert kinds[0] == SwingKind.LOW  # first move up confirms the start as a low
        for a, b in zip(kinds, kinds[1:]):
            assert a != b
        assert all(s.confirmed for s in swings[:-1])
        assert swings[-1].confirmed is False
        highs = [s for s in swings if s.kind == SwingKind.HIGH and s.confirmed]
        assert [h.price for h in highs] == [110, 108]

    def test_small_moves_below_threshold_are_ignored(self):
        closes = [100, 101, 100, 101, 100, 101]
        swings = find_swing_points(_candles_from_closes(closes), reversal_pct=3.0)
        assert swings == []

    def test_empty_input(self):
        assert find_swing_points([], 3.0) == []


# --------------------------------------------------------------------------- #
# Contraction stages
# --------------------------------------------------------------------------- #
class TestContractions:
    def _vcp_closes(self):
        # 100 -> 80 (-20%), -> 98 -> 86.24 (-12%), -> 97 -> 91.18 (-6%), -> 95.5
        path = [100, 95, 90, 85, 80, 85, 90, 95, 98, 94, 90, 86.24, 90, 94, 97, 94, 91.18, 93, 95.5]
        lead = [60 + i * 2 for i in range(21)]  # 60..100 uptrend
        return lead + path[1:]

    def test_three_contracting_legs(self):
        candles = _candles_from_closes(self._vcp_closes())
        cfg = ScreenerConfig(lookback=len(candles), min_bars=10)
        stages = detect_contractions(candles, find_swing_points(candles, 3.0), cfg)
        assert [s.number for s in stages] == [1, 2, 3]
        assert [round(s.depth_pct, 2) for s in stages] == [20.0, 12.0, 6.0]
        assert stages[0].high == 100 and stages[0].low == 80
        assert stages[-1].confirmed is True  # 95.5 is >3% above 91.18, so the low is confirmed
        last_swing = find_swing_points(candles, 3.0)[-1]
        assert last_swing.kind == SwingKind.HIGH and last_swing.confirmed is False
        assert all(s.bars > 0 for s in stages)

    def test_expanding_leg_restarts_chain(self):
        # second leg (-25%) deeper than first (-10%): chain restarts at leg 2
        closes = [100, 95, 90, 95, 100, 90, 80, 75, 80, 85, 88]
        candles = _candles_from_closes(closes)
        cfg = ScreenerConfig(lookback=len(candles), min_bars=5)
        stages = detect_contractions(candles, find_swing_points(candles, 3.0), cfg)
        assert len(stages) == 1
        assert stages[0].high == 100 and stages[0].low == 75

    def test_higher_high_beyond_tolerance_restarts_chain(self):
        closes = [100, 90, 95, 110, 104, 107]  # 110 > 100 * 1.03
        candles = _candles_from_closes(closes)
        cfg = ScreenerConfig(lookback=len(candles), min_bars=5)
        stages = detect_contractions(candles, find_swing_points(candles, 3.0), cfg)
        assert len(stages) == 1
        assert stages[0].high == 110

    def test_no_swings_no_stages(self):
        candles = _candles_from_closes([100.0] * 30)
        assert detect_contractions(candles, [], ScreenerConfig()) == []

    def test_stage_volume_declining(self):
        candles = load_csv(FIXTURES / "clean_contraction.csv")
        cfg = ScreenerConfig()
        stages = detect_contractions(candles, find_swing_points(candles, cfg.reversal_pct), cfg)
        assert stage_volume_declining(stages) is True
        assert stage_volume_declining(stages[:1]) is None


# --------------------------------------------------------------------------- #
# Volume + trend
# --------------------------------------------------------------------------- #
class TestVolumeAndTrend:
    def test_dry_up_ratio(self):
        vols = [1_000_000] * 55 + [400_000] * 5
        candles = _candles_from_closes([100.0] * 60, volume=vols)
        ratio = volume_dry_up_ratio(candles, ScreenerConfig())
        # 50d avg = (45*1M + 5*0.4M)/50 = 940k ; recent = 400k
        assert ratio == pytest.approx(400_000 / 940_000)

    def test_dry_up_needs_history(self):
        candles = _candles_from_closes([100.0] * 20)
        assert volume_dry_up_ratio(candles, ScreenerConfig()) is None

    def test_trend_template_none_when_short(self):
        assert trend_template(_candles_from_closes([100.0] * 150)) is None

    def test_trend_template_uptrend_true_downtrend_false(self):
        up = _candles_from_closes([50 + i * 0.25 for i in range(260)])
        down = _candles_from_closes([150 - i * 0.25 for i in range(260)])
        assert trend_template(up) is True
        assert trend_template(down) is False


# --------------------------------------------------------------------------- #
# Full screen on fixtures
# --------------------------------------------------------------------------- #
class TestScreenFixtures:
    def test_clean_contraction_passes(self):
        r = screen("CLEAN", load_csv(FIXTURES / "clean_contraction.csv"))
        assert r.passed, r.reasons
        assert r.status == SetupStatus.FORMING
        assert r.contraction_count == 3
        d = r.depths
        assert d[0] > d[1] > d[2]
        assert d[-1] < 10
        assert r.volume_dry_up_ratio < 0.7
        assert r.trend_ok is True
        assert 0 < r.distance_to_pivot_pct < 10
        assert r.score > 50

    def test_breakout_passes_with_breakout_status(self):
        r = screen("BO", load_csv(FIXTURES / "breakout.csv"))
        assert r.passed, r.reasons
        assert r.status == SetupStatus.BREAKOUT
        assert r.distance_to_pivot_pct < 0

    def test_flat_fails(self):
        r = screen("FLAT", load_csv(FIXTURES / "flat.csv"))
        assert not r.passed
        assert any("trend template" in x for x in r.reasons)
        assert any("volume dry-up" in x for x in r.reasons)

    def test_high_volatility_fails(self):
        r = screen("VOL", load_csv(FIXTURES / "high_volatility.csv"))
        assert not r.passed
        assert r.volume_dry_up_ratio > 0.7
        assert r.trend_ok is False

    def test_downtrend_fails_trend(self):
        r = screen("DN", load_csv(FIXTURES / "downtrend.csv"))
        assert not r.passed
        assert r.trend_ok is False

    def test_insufficient_history(self):
        r = screen("TINY", _candles_from_closes([100.0] * 10))
        assert not r.passed
        assert r.status == SetupStatus.NONE
        assert r.reasons == ["insufficient history: 10 bars < 60"]

    def test_no_trend_flag_relaxes_requirement(self):
        candles = load_csv(FIXTURES / "clean_contraction.csv")[-150:]
        strict = screen("X", candles, ScreenerConfig(require_trend=True))
        relaxed = screen("X", candles, ScreenerConfig(require_trend=False))
        assert any("needs >= 200 bars" in x for x in strict.reasons)
        assert not any("200 bars" in x for x in relaxed.reasons)

    def test_tighter_final_depth_rejects(self):
        candles = load_csv(FIXTURES / "clean_contraction.csv")
        r = screen("X", candles, ScreenerConfig(max_final_depth_pct=5.0))
        assert not r.passed
        assert any("final contraction" in x for x in r.reasons)

    def test_config_validation(self):
        with pytest.raises(ValueError):
            screen("X", _candles_from_closes([1.0] * 100), ScreenerConfig(lookback=1))

    def test_determinism(self):
        candles = load_csv(FIXTURES / "clean_contraction.csv")
        a = screen("X", candles).to_dict()
        b = screen("X", candles).to_dict()
        assert a == b
        gen1 = generate_mock_candles("vcp", bars=260, seed=42)
        gen2 = generate_mock_candles("vcp", bars=260, seed=42)
        assert gen1 == gen2
        assert gen1 != generate_mock_candles("vcp", bars=260, seed=43)

    def test_fixture_matches_generator(self):
        # The committed fixture is exactly what the generator produces.
        fixture = load_csv(FIXTURES / "clean_contraction.csv")
        generated = generate_mock_candles("vcp", bars=260, seed=42)
        assert [c.date for c in fixture] == [c.date for c in generated]
        assert [c.close for c in fixture] == pytest.approx([c.close for c in generated], abs=1e-4)

    def test_screen_many_sorted_by_score(self):
        series = {
            name: load_csv(FIXTURES / f"{name}.csv")
            for name in ("flat", "clean_contraction", "high_volatility")
        }
        results = screen_many(series)
        scores = [r.score for r in results]
        assert scores == sorted(scores, reverse=True)
        assert results[0].symbol == "clean_contraction"

    def test_score_bounds(self):
        cfg = ScreenerConfig()
        assert compute_score([], None, None, None, None, cfg) == 0.0
        candles = load_csv(FIXTURES / "clean_contraction.csv")
        r = screen("X", candles, cfg)
        assert 0 <= r.score <= 100

    def test_to_dict_is_json_friendly(self):
        import json

        r = screen("X", load_csv(FIXTURES / "clean_contraction.csv"))
        text = json.dumps(r.to_dict())
        assert '"passed": true' in text
