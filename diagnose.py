"""
diagnose.py — 30-second check to find out why EPS fetches are returning nothing.

Run it in BOTH places and compare:

    local:  python diagnose.py
    cloud:  Actions tab -> Screener Diagnostic -> Run workflow

If local passes and cloud fails, the runner IP is being blocked or throttled.
If both fail, it is the installed yfinance version or the machine's network.
If the version numbers differ, pin requirements.txt to whichever one works.
"""

from yf_fetch import (fetch_eps_history, environment_report, RATE_LIMITED,
                      MISSING_DEP)

TEST_TICKERS = ["AAPL", "MSFT", "KO", "JPM", "WMT"]

print("=" * 62)
print("  ENVIRONMENT")
print("=" * 62)
for line in environment_report():
    print("  " + line)

print()
print("=" * 62)
print("  EARNINGS CALENDAR (finance_calendars -> NASDAQ)")
print("=" * 62)
try:
    from finance_calendars import finance_calendars as fc
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo

    day = datetime.now(ZoneInfo("America/New_York")).replace(tzinfo=None) + timedelta(days=1)
    while day.weekday() >= 5:
        day += timedelta(days=1)
    cal = fc.get_earnings_by_date(day)
    print(f"  {day.strftime('%Y-%m-%d')}: {len(cal)} names returned  ->  OK")
except Exception as e:
    print(f"  FAILED: {type(e).__name__}: {e}")

print()
print("=" * 62)
print("  EPS HISTORY (yfinance)")
print("=" * 62)

results = []
for t in TEST_TICKERS:
    df, reason, _ = fetch_eps_history(t, limit=24, retries=2)
    if reason is None:
        reported = df[df["Reported EPS"].notna()]
        status = f"OK    {len(df):>2} rows, {len(reported):>2} reported"
        if len(reported) < 8:
            status += "   <-- under the 8 the screener needs"
    else:
        status = f"FAIL  {reason}"
    results.append(reason)
    print(f"  {t:<6} {status}")

print()
print("=" * 62)
print("  VERDICT")
print("=" * 62)

failures = [r for r in results if r is not None]
missing = [r for r in failures if r.startswith(MISSING_DEP)]

if missing:
    print("  A required library is missing. This is NOT a network or rate-limit")
    print("  problem — the fetch never got as far as parsing a response.")
    print(f"    {missing[0]}")
    print()
    print("  yfinance parses the earnings table with pandas.read_html, which")
    print("  needs lxml. lxml is not a declared yfinance dependency, so a clean")
    print("  install omits it. Add this to requirements.txt and re-run:")
    print("      lxml>=5.0")
elif not failures:
    print("  yfinance is working here. If the cloud run fails with the same")
    print("  code, the GitHub runner IP is the problem, not your code.")
elif len(failures) == len(results):
    if RATE_LIMITED in failures:
        print("  Every request was rate limited. Yahoo is refusing this host.")
    else:
        print("  Every request returned empty with no rate-limit error.")
        print("  Compare the yfinance version above against your working venv:")
        print("      pip show yfinance")
        print("  If they differ, pin requirements.txt to the working version.")
else:
    print(f"  Partial failure: {len(failures)} of {len(results)}.")
    print("  Consistent with throttling under load. The retry/backoff in")
    print("  yf_fetch.py plus a slower pace should recover most names.")
print()
