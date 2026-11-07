# market-vcp-screener

A deterministic, dependency-light Python CLI that scans historical daily OHLCV
candles for Mark Minervini **Volatility Contraction Patterns (VCP)** and
**volume dry-ups**. Same input, same config, same answer, every time: no
randomness, no network calls, no wall-clock dependence.

```
$ vcp-screener scan data/ --all
                                   VCP scan
 Symbol             Pass   Score   Status     Legs   Depths %             Pivot    Close   Dist %   Vol dry   Trend
 ──────────────────────────────────────────────────────────────────────────────────────────────────────────────────
 CLEAN_CONTRACTION  PASS    60.3   forming       3   21.3 > 13.1 > 7.3    97.91    95.68      2.3      0.54   yes
 BREAKOUT           PASS    58.9   breakout      3   20.8 > 12.7 > 7.1    97.64    98.93     -1.3      0.65   yes
 DOWNTREND          fail    49.6   breakout      2   29.3 > 3.3           49.48    49.89     -0.8      0.81   no
 FLAT               fail    33.0   forming       1   3.3                 102.34   101.14      1.2      0.98   no
 HIGH_VOLATILITY    fail    29.4   forming       2   9.2 > 6.2            88.41    84.98      3.9      1.13   no
Reasons
  DOWNTREND
    - no volume dry-up: recent/avg = 0.81 > 0.70
    - fails trend template (close > SMA50 > SMA150 > SMA200)
  ...
scanned 5 symbol(s), 2 passed
```

## Quickstart

```bash
git clone https://github.com/kpv1993/market-vcp-screener
cd market-vcp-screener
python -m venv .venv && . .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -e ".[dev]"

vcp-screener scan                 # no data yet? scans built-in mock profiles
vcp-screener mock ./data          # write synthetic CSVs to play with
vcp-screener scan ./data --all    # show failures and the reason for each
pytest                            # run the test suite
```

Requires Python 3.9+. Runtime dependencies are only `typer` and `rich`.

## Input data

Point the CLI at one or more `.csv` / `.json` files, or at directories of them.
The file stem becomes the symbol (`AAPL.csv` -> `AAPL`).

CSV header names are case-insensitive. Required columns:

| column | accepted names                          |
|--------|-----------------------------------------|
| date   | `date`, `timestamp`, `time`, `datetime` |
| open   | `open`                                  |
| high   | `high`                                  |
| low    | `low`                                   |
| close  | `close`                                 |
| volume | `volume`, `vol`                         |

Extra columns (e.g. `adj close`) are ignored. Rows are sorted by date and
duplicate dates keep the last row. Dates accept `YYYY-MM-DD`, `YYYY/MM/DD`,
`DD-MM-YYYY`, `DD/MM/YYYY`, `YYYYMMDD`, `MM/DD/YYYY` and ISO datetimes.

JSON may be a plain list of candle objects or `{"symbol": "...", "candles": [...]}`.

Rows that violate `low <= open/close <= high`, have non-positive prices or
negative volume abort the run with the offending row number.

## CLI reference

### `scan PATHS... [options]`

Screens every file and prints a ranked table (score descending, then symbol).

| option                       | default | meaning |
|------------------------------|---------|---------|
| `--lookback`                 | 120     | bars searched for the base |
| `--reversal`                 | 3.0     | zig-zag reversal threshold (%) used to find swing highs/lows |
| `--min-contractions`         | 2       | minimum number of contraction legs |
| `--max-contractions`         | 6       | maximum number of contraction legs |
| `--contraction-ratio`        | 0.8     | each leg's depth must be <= previous depth x ratio |
| `--max-base-depth`           | 35.0    | max depth (%) of the first, deepest leg |
| `--max-final-depth`          | 10.0    | max depth (%) of the last, tightest leg |
| `--dry-up-ratio`             | 0.7     | mean volume of recent bars / 50-day average must be <= this |
| `--dry-up-bars`              | 5       | bars averaged for the dry-up check |
| `--max-pivot-distance`       | 10.0    | close may sit at most this far (%) below the pivot |
| `--trend` / `--no-trend`     | on      | require the Minervini trend template (needs 200+ bars) |
| `--all`, `-a`                | off     | show failing symbols with their reasons |
| `--min-score`                | 0       | hide results scoring below this |
| `--json`                     | off     | emit machine-readable JSON |
| `--mock`                     | off     | ignore PATHS and scan the built-in mock profiles |

```bash
vcp-screener scan data/ --json --min-score 50 > hits.json
vcp-screener scan data/AAPL.csv data/NVDA.csv --max-final-depth 6 --dry-up-ratio 0.5
vcp-screener scan data/ --no-trend --all          # short histories, show why each fails
```

Exit code is `0` on a successful scan (even with zero hits) and `2` on a data error.

### `inspect FILE [options] [--swings] [--json]`

Shows the detected contraction legs (dates, highs, lows, depth, bars, average
volume), the pivot, volume ratios and the trend verdict for one symbol.
`--swings` also lists every zig-zag pivot so you can see why legs were drawn
where they were. All `scan` thresholds apply.

```bash
vcp-screener inspect tests/fixtures/clean_contraction.csv --swings
```

### `mock OUT_DIR [-p PROFILE ...] [--bars 260] [--seed 42]`

Writes deterministic synthetic CSVs. Profiles (`vcp-screener profiles`):

| profile     | shape |
|-------------|-------|
| `vcp`       | long uptrend into a textbook 3-stage contraction (20% / 12% / 6%) with drying volume |
| `breakout`  | same base, but the last bars close above the pivot |
| `flat`      | sideways chop, no trend, steady volume |
| `volatile`  | expanding swings on heavy, rising volume |
| `downtrend` | persistent decline |

## How detection works

Everything lives in `src/vcp_screener/engine.py` and is pure Python over
lists of `Candle` dataclasses.

1. **Trend template** (`trend_template`): `close > SMA50 > SMA150 > SMA200`,
   SMA200 not falling over the last 20 bars, close >= 1.25 x 52-week low and
   >= 0.75 x 52-week high. Returns `None` with fewer than 200 bars; `--no-trend`
   drops the requirement.
2. **Swing detection** (`find_swing_points`): a percentage zig-zag. A swing high
   is confirmed once a later low trades `reversal` percent below it, and vice
   versa. The still-forming last extreme is returned as *unconfirmed* so the
   current pullback can count as the final leg.
3. **Contraction legs** (`detect_contractions`): starting at the highest swing
   high inside the lookback window, each swing-high -> swing-low pair becomes a
   `VCPStage` with `depth_pct = (high - low) / high`. If a later high exceeds
   the previous leg's high by more than 3%, or a leg fails to shrink by
   `contraction-ratio`, the chain restarts there, so the stages always describe
   the most recent valid sequence.
4. **Volume dry-up** (`volume_dry_up_ratio`): mean volume of the last
   `dry-up-bars` bars divided by the 50-day volume SMA. Average volume per leg
   is also reported, with a flag for whether it declines leg over leg.
5. **Pivot and status**: the pivot is the high of the final leg. The close must
   be within `max-pivot-distance` below it (`forming`), or at most 5% above it
   (`breakout`). Further above is `extended`; below the final leg's low or too
   far under the pivot is `too_far`.
6. **Score** (`compute_score`, 0-100): contraction count 25, final tightness
   20, volume dry-up 20, leg-volume decline 10, pivot proximity 15, trend 10.
   A symbol **passes** only when every hard criterion holds; the score ranks
   passers (and, with `--all`, everything else).

### Architecture

```
src/vcp_screener/
├── cli.py          Typer commands: scan, inspect, mock, profiles. Rich tables or JSON.
├── engine.py       Pure math: sma/ema/atr, trend template, zig-zag swings,
│                   contraction legs, dry-up ratio, scoring, screen()/screen_many().
├── models.py       Frozen dataclasses: Candle, SwingPoint, VCPStage, ScreeningResult.
└── data_loader.py  CSV/JSON readers with validation, CSV writer, seeded mock generator.
tests/
├── fixtures/       Synthetic CSVs produced by the mock generator (seed 42, 260 bars).
├── test_engine.py  Unit tests for the math and the full screen on each fixture.
└── test_cli.py     End-to-end CliRunner tests for every command.
```

### Library use

```python
from vcp_screener import ScreenerConfig, screen
from vcp_screener.data_loader import load_candles

candles = load_candles("data/AAPL.csv")
result = screen("AAPL", candles, ScreenerConfig(max_final_depth_pct=8.0))
print(result.passed, result.score, result.depths, result.reasons)
```

## License

MIT. See [LICENSE](LICENSE).

*Not investment advice. This tool describes price structure; it does not
predict anything.*
