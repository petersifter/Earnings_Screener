"""
selftest.py — end-to-end regression check for the screener suite.

Runs both screeners against stubbed data and asserts the things that have
actually broken before: report filenames, the .last_run handshake the email
step depends on, the abort path, and that a failed calendar is loud rather
than silent.

    python selftest.py

Exits 0 if everything passes. Run it before any push.
"""

import os
import subprocess
import sys
import tempfile

REPO = os.path.dirname(os.path.abspath(__file__))

STUBS = r'''
import sys, types, random
sys.path.insert(0, %r)
import pandas as pd, numpy as np
from datetime import timedelta
random.seed(1)
MODE = %r
NAMES = {"AAA": "Alpha Corp", "BBB": "Beta Inc", "CCC": "Gamma Ltd",
         "DDD": "Delta Co", "EEE": "Epsilon SA"}
TK = list(NAMES)

def get_earnings_by_date(day):
    if MODE == "calendar_fail":
        raise ConnectionError("simulated NASDAQ outage")
    if MODE == "empty":
        return pd.DataFrame()
    return pd.DataFrame({"name": [NAMES[t] for t in TK],
                         "time": ["time-after-hours"] * len(TK)}, index=TK)

fcm = types.ModuleType("finance_calendars")
inner = types.ModuleType("finance_calendars.finance_calendars")
inner.get_earnings_by_date = get_earnings_by_date
fcm.finance_calendars = inner
sys.modules["finance_calendars"] = fcm
sys.modules["finance_calendars.finance_calendars"] = inner

class FT:
    def __init__(self, s): self.s = s
    def get_earnings_dates(self, limit=24, offset=0):
        if MODE == "nodata": return None
        idx = pd.DatetimeIndex([pd.Timestamp("2026-07-20", tz="America/New_York")
                                - pd.Timedelta(days=91*i) for i in range(10)])
        est = np.linspace(1.0, 2.0, 10)
        return pd.DataFrame({"EPS Estimate": est, "Reported EPS": est + 0.09}, index=idx)
    def history(self, start=None, end=None, period=None):
        if MODE == "nodata": return pd.DataFrame()
        s0 = start.replace(tzinfo=None) if getattr(start, "tzinfo", None) else start
        n = max(40, (end.replace(tzinfo=None) - s0).days) if end is not None else 90
        idx = pd.date_range(start=s0, periods=n, freq="D", tz="America/New_York")
        return pd.DataFrame({"Close": 100 + np.cumsum(np.full(n, 0.4))}, index=idx)

yfm = types.ModuleType("yfinance"); yfm.Ticker = FT
yfm.__version__ = "stub"
exc = types.ModuleType("yfinance.exceptions")
class YFRateLimitError(Exception): pass
exc.YFRateLimitError = YFRateLimitError
yfm.exceptions = exc
sys.modules["yfinance"] = yfm
sys.modules["yfinance.exceptions"] = exc

imm = types.ModuleType("implied_move")
imm.get_implied_move = lambda t, d, h, daily_vol_pct=None, timing="AMC": {
    "implied_move_pct": 2.0, "ratio": 1.0, "flag": "FAIR", "dte": 0, "error": None}
imm.realized_daily_vol = lambda p, e=None, lookback=60: 1.5
sys.modules["implied_move"] = imm
oam = types.ModuleType("options_activity")
oam.get_options_activity = lambda t, d, timing="AMC", min_volume=250: {
    "put_call_ratio": 0.8, "vol_oi_ratio": 0.5, "total_volume": 5000,
    "thin": False, "error": None}
sys.modules["options_activity"] = oam
'''


def run(script, mode, answer):
    d = tempfile.mkdtemp()
    runner = os.path.join(d, "r.py")
    with open(runner, "w", encoding="utf-8") as f:
        f.write(STUBS % (REPO, mode))
        f.write(f"\nexec(open({script!r}, encoding='utf-8').read())\n")
    p = subprocess.run([sys.executable, runner], input=answer, text=True,
                       cwd=REPO, capture_output=True, encoding="utf-8",
                       errors="replace")
    meta = {}
    if os.path.exists(".last_run"):
        for line in open(".last_run", encoding="utf-8"):
            if "=" in line:
                k, v = line.strip().split("=", 1)
                meta[k] = v
    return p, meta


def check(label, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {label}" + (f"   {detail}" if not cond else ""))
    return cond


def main():
    ok = True
    for script, answer, kind in (("earnings_screener.py", "1\n", "daily"),
                                 ("weekly_screener.py", "2\n", "weekly")):
        print(f"\n{kind}: normal run")
        for f in (".last_run",):
            if os.path.exists(f):
                os.remove(f)
        p, meta = run(script, "ok", answer)
        ok &= check("exits 0", p.returncode == 0, p.stderr[-300:])
        ok &= check("writes .last_run", set(meta) >= {
            "REPORT_FILE", "REPORT_NAME", "SESSION", "HEADLINE"}, str(meta))
        rf = meta.get("REPORT_FILE", "")
        ok &= check("report file exists", bool(rf) and os.path.exists(rf), rf)
        ok &= check("filename names the session",
                    kind == "weekly" and "_to_" in rf or kind == "daily" and "_AMC_" in rf, rf)
        if rf and os.path.exists(rf):
            body = open(rf, encoding="utf-8").read()
            ok &= check("has both lists",
                        "BUY LIST" in body and "SHORT LIST" in body)
            ok &= check("no per-ticker progress spam", "Analyzing" not in body)
            ok &= check("found candidates", "QUALIFIED" in body and " 0 BUY   \u00b7   0 SHORT" not in body)

        print(f"{kind}: data source down")
        p, meta = run(script, "nodata", answer)
        ok &= check("exits non-zero", p.returncode != 0, str(p.returncode))
        ok &= check("headline says DATA FAILURE",
                    "DATA FAILURE" in meta.get("HEADLINE", ""), meta.get("HEADLINE", ""))

        print(f"{kind}: calendar down")
        p, meta = run(script, "calendar_fail", answer)
        ok &= check("does not report a clean empty list",
                    "DATA FAILURE" in meta.get("HEADLINE", ""), meta.get("HEADLINE", ""))

    print("\n" + ("ALL PASSED" if ok else "FAILURES ABOVE"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
