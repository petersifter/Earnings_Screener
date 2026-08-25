"""
selftest.py — end-to-end regression check for the screener suite.

Runs both screeners against stubbed data and asserts the things that have
actually broken before: report filenames, the .last_run handshake the email
step depends on, the abort path, and that a failed calendar is loud rather
than silent.

    python selftest.py

Exits 0 if everything passes. Run it before any push.

Both screeners now expose main(argv), so this imports and calls them in-process
instead of exec()-ing the file and feeding answers to input(). That means a
failure produces a real traceback pointing at a real line.
"""

import importlib
import os
import sys
import types

import numpy as np
import pandas as pd

REPO = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, REPO)

NAMES = {"AAA": "Alpha Corp", "BBB": "Beta Inc", "CCC": "Gamma Ltd",
         "DDD": "Delta Co", "EEE": "Epsilon SA"}


# --- Stubs -----------------------------------------------------------------

def install_stubs(mode):
    """Replace the network-facing modules with deterministic fakes."""

    def get_earnings_by_date(day):
        if mode == "calendar_fail":
            raise ConnectionError("simulated NASDAQ outage")
        if mode == "empty":
            return pd.DataFrame()
        return pd.DataFrame(
            {"name": [NAMES[t] for t in NAMES],
             "time": ["time-after-hours"] * len(NAMES)},
            index=list(NAMES),
        )

    fcm = types.ModuleType("finance_calendars")
    inner = types.ModuleType("finance_calendars.finance_calendars")
    inner.get_earnings_by_date = get_earnings_by_date
    fcm.finance_calendars = inner
    sys.modules["finance_calendars"] = fcm
    sys.modules["finance_calendars.finance_calendars"] = inner

    class FakeTicker:
        def __init__(self, symbol):
            self.symbol = symbol
            self.info = {"longName": NAMES.get(symbol, symbol)}

        def get_earnings_dates(self, limit=24, offset=0):
            if mode == "nodata":
                return None
            idx = pd.DatetimeIndex([
                pd.Timestamp("2026-07-20", tz="America/New_York")
                - pd.Timedelta(days=91 * i) for i in range(10)
            ])
            est = np.linspace(1.0, 2.0, 10)
            return pd.DataFrame(
                {"EPS Estimate": est, "Reported EPS": est + 0.09}, index=idx)

        def history(self, start=None, end=None, period=None):
            if mode == "nodata":
                return pd.DataFrame()
            s0 = start.replace(tzinfo=None) if getattr(start, "tzinfo", None) else start
            n = max(40, (end.replace(tzinfo=None) - s0).days) if end is not None else 90
            idx = pd.date_range(start=s0, periods=n, freq="D", tz="America/New_York")
            return pd.DataFrame({"Close": 100 + np.cumsum(np.full(n, 0.4))}, index=idx)

    yfm = types.ModuleType("yfinance")
    yfm.Ticker = FakeTicker
    yfm.__version__ = "stub"
    exc = types.ModuleType("yfinance.exceptions")

    class YFRateLimitError(Exception):
        pass

    exc.YFRateLimitError = YFRateLimitError
    yfm.exceptions = exc
    sys.modules["yfinance"] = yfm
    sys.modules["yfinance.exceptions"] = exc

    imm = types.ModuleType("implied_move")
    imm.get_implied_move = lambda t, d, h, daily_vol_pct=None, timing="AMC": {
        "implied_move_pct": 2.0, "ratio": 1.0, "flag": "FAIR", "dte": 0,
        "error": None}
    imm.realized_daily_vol = lambda p, e=None, lookback=None: 1.5
    sys.modules["implied_move"] = imm

    oam = types.ModuleType("options_activity")
    oam.get_options_activity = lambda t, d, timing="AMC", min_volume=None: {
        "put_call_ratio": 0.8, "vol_oi_ratio": 0.5, "total_volume": 5000,
        "thin": False, "error": None}
    sys.modules["options_activity"] = oam

    # Anything already imported against the real modules must be reloaded.
    for name in ("config", "yf_fetch", "report_utils", "screener_core",
                 "earnings_screener", "weekly_screener"):
        if name in sys.modules:
            importlib.reload(sys.modules[name])


def run(module_name, argv, mode):
    """Import the screener under stubs and call main(argv). Returns (code, meta)."""
    if os.path.exists(".last_run"):
        os.remove(".last_run")

    install_stubs(mode)
    module = importlib.import_module(module_name)
    importlib.reload(module)

    try:
        code = module.main(argv)
    except SystemExit as e:
        code = e.code if isinstance(e.code, int) else 1

    meta = {}
    if os.path.exists(".last_run"):
        for line in open(".last_run", encoding="utf-8"):
            if "=" in line:
                k, v = line.strip().split("=", 1)
                meta[k] = v
    return code, meta


# --- Assertions ------------------------------------------------------------

def check(label, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {label}" + (f"   {detail}" if not cond else ""))
    return bool(cond)


def main():
    ok = True
    cases = (
        ("earnings_screener", ["--tomorrow"], "daily", "_AMC_"),
        ("weekly_screener", ["--this-week"], "weekly", "_to_"),
    )

    for module_name, argv, kind, name_marker in cases:
        print(f"\n{kind}: normal run")
        code, meta = run(module_name, argv, "ok")
        ok &= check("exits 0", code == 0, str(code))
        ok &= check("writes .last_run", set(meta) >= {
            "REPORT_FILE", "REPORT_NAME", "SESSION", "HEADLINE"}, str(meta))

        rf = meta.get("REPORT_FILE", "")
        ok &= check("report file exists", bool(rf) and os.path.exists(rf), rf)
        ok &= check("filename names the session", name_marker in rf, rf)

        if rf and os.path.exists(rf):
            body = open(rf, encoding="utf-8").read()
            ok &= check("has both lists",
                        "BUY LIST" in body and "SHORT LIST" in body)
            ok &= check("no per-ticker progress spam", "Analyzing" not in body)
            ok &= check("found candidates",
                        "QUALIFIED" in body
                        and " 0 BUY   \u00b7   0 SHORT" not in body)

        print(f"{kind}: data source down")
        code, meta = run(module_name, argv, "nodata")
        ok &= check("exits non-zero", code != 0, str(code))
        ok &= check("headline says DATA FAILURE",
                    "DATA FAILURE" in meta.get("HEADLINE", ""),
                    meta.get("HEADLINE", ""))

        print(f"{kind}: calendar down")
        code, meta = run(module_name, argv, "calendar_fail")
        ok &= check("does not report a clean empty list",
                    "DATA FAILURE" in meta.get("HEADLINE", ""),
                    meta.get("HEADLINE", ""))

    print("\n" + ("ALL PASSED" if ok else "FAILURES ABOVE"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
