"""End-to-end CLI tests using Typer's CliRunner."""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from vcp_screener import __version__
from vcp_screener.cli import app

FIXTURES = Path(__file__).parent / "fixtures"
runner = CliRunner(env={"COLUMNS": "200"})  # keep Rich tables from truncating


def _run(*args: str):
    return runner.invoke(app, list(args))


class TestScan:
    def test_scan_directory_table(self):
        res = _run("scan", str(FIXTURES))
        assert res.exit_code == 0, res.output
        assert "CLEAN_CONTRACTION" in res.output
        assert "BREAKOUT" in res.output
        assert "FLAT" not in res.output  # failures hidden by default
        assert "2 passed" in res.output

    def test_scan_all_shows_failures_and_reasons(self):
        res = _run("scan", str(FIXTURES), "--all")
        assert res.exit_code == 0, res.output
        assert "FLAT" in res.output
        assert "HIGH_VOLATILITY" in res.output
        assert "Reasons" in res.output

    def test_scan_json(self):
        res = _run("scan", str(FIXTURES), "--json", "--all")
        assert res.exit_code == 0, res.output
        payload = json.loads(res.stdout)
        assert payload["scanned"] == 5
        assert payload["passed"] == 2
        symbols = {r["symbol"]: r for r in payload["results"]}
        assert symbols["CLEAN_CONTRACTION"]["passed"] is True
        assert symbols["CLEAN_CONTRACTION"]["contractions"] == 3
        assert symbols["FLAT"]["passed"] is False
        assert payload["config"]["lookback"] == 120

    def test_scan_single_file_and_threshold_override(self):
        f = str(FIXTURES / "clean_contraction.csv")
        ok = _run("scan", f, "--json")
        assert json.loads(ok.stdout)["passed"] == 1
        strict = _run("scan", f, "--json", "--max-final-depth", "5")
        assert json.loads(strict.stdout)["passed"] == 0

    def test_scan_min_score_filters(self):
        res = _run("scan", str(FIXTURES), "--all", "--json", "--min-score", "55")
        payload = json.loads(res.stdout)
        assert payload["scanned"] == 5
        assert all(r["score"] >= 55 for r in payload["results"])
        assert len(payload["results"]) < 5

    def test_scan_mock_fallback(self):
        res = _run("scan", "--mock", "--json")
        assert res.exit_code == 0, res.output
        payload = json.loads(res.stdout)
        assert payload["scanned"] == 5
        assert {r["symbol"] for r in payload["results"]} == {"MOCK_VCP", "MOCK_BREAKOUT"}

    def test_scan_no_args_uses_mock_data(self):
        res = _run("scan")
        assert res.exit_code == 0, res.output
        assert "MOCK_VCP" in res.output

    def test_scan_missing_path_errors(self):
        res = _run("scan", str(FIXTURES / "does_not_exist.csv"))
        assert res.exit_code == 2
        assert "no such file" in res.output

    def test_scan_bad_csv_errors(self, tmp_path: Path):
        bad = tmp_path / "BAD.csv"
        bad.write_text("date,open,high,low,close,volume\n2024-01-01,10,9,11,10,100\n")
        res = _run("scan", str(bad))
        assert res.exit_code == 2
        assert "row 2" in res.output

    def test_scan_json_input(self, tmp_path: Path):
        src = (FIXTURES / "clean_contraction.csv").read_text().strip().splitlines()[1:]
        rows = []
        for line in src:
            d, o, h, l, c, v = line.split(",")
            rows.append({"date": d, "open": o, "high": h, "low": l, "close": c, "volume": v})
        target = tmp_path / "JSONSYM.json"
        target.write_text(json.dumps({"symbol": "JSONSYM", "candles": rows}))
        res = _run("scan", str(target), "--json")
        payload = json.loads(res.stdout)
        assert payload["passed"] == 1
        assert payload["results"][0]["symbol"] == "JSONSYM"

    def test_scan_is_deterministic(self):
        a = _run("scan", str(FIXTURES), "--json", "--all").stdout
        b = _run("scan", str(FIXTURES), "--json", "--all").stdout
        assert a == b


class TestInspect:
    def test_inspect_table(self):
        res = _run("inspect", str(FIXTURES / "clean_contraction.csv"), "--swings")
        assert res.exit_code == 0, res.output
        assert "PASS" in res.output
        assert "Contraction legs" in res.output
        assert "Swing points" in res.output

    def test_inspect_json(self):
        res = _run("inspect", str(FIXTURES / "flat.csv"), "--json", "--swings")
        assert res.exit_code == 0, res.output
        payload = json.loads(res.stdout)
        assert payload["symbol"] == "FLAT"
        assert payload["passed"] is False
        assert payload["reasons"]
        assert isinstance(payload["swings"], list)

    def test_inspect_missing_file(self):
        res = _run("inspect", str(FIXTURES / "nope.csv"))
        assert res.exit_code != 0


class TestMockAndMisc:
    def test_mock_writes_files(self, tmp_path: Path):
        res = _run("mock", str(tmp_path), "-p", "vcp", "-p", "flat", "--bars", "120")
        assert res.exit_code == 0, res.output
        files = sorted(p.name for p in tmp_path.iterdir())
        assert files == ["MOCK_FLAT.csv", "MOCK_VCP.csv"]
        assert len((tmp_path / "MOCK_VCP.csv").read_text().splitlines()) == 121

    def test_mock_rejects_unknown_profile(self, tmp_path: Path):
        res = _run("mock", str(tmp_path), "-p", "sideways")
        assert res.exit_code != 0
        assert "unknown profile" in res.output

    def test_mock_then_scan_roundtrip(self, tmp_path: Path):
        assert _run("mock", str(tmp_path)).exit_code == 0
        res = _run("scan", str(tmp_path), "--json")
        payload = json.loads(res.stdout)
        assert payload["scanned"] == 5
        assert payload["passed"] == 2

    def test_profiles_lists_all(self):
        res = _run("profiles")
        assert res.exit_code == 0
        for name in ("vcp", "flat", "volatile", "downtrend", "breakout"):
            assert name in res.output

    def test_version(self):
        res = _run("--version")
        assert res.exit_code == 0
        assert __version__ in res.output

    def test_help(self):
        res = _run("--help")
        assert res.exit_code == 0
        assert "scan" in res.output and "inspect" in res.output
