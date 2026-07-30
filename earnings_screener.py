from finance_calendars import finance_calendars as fc
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
ET = ZoneInfo("America/New_York")
import yfinance as yf
import pandas as pd
import warnings
warnings.filterwarnings("ignore")

from implied_move import get_implied_move, realized_daily_vol
from options_activity import get_options_activity
from report_utils import Report, LEGEND, progress, progress_done, truncate
from yf_fetch import (fetch_eps_history, fetch_history, CircuitBreaker,
                      environment_report, RATE_LIMITED)


def next_weekday(d):
    """Roll a date forward to the next Mon-Fri if it lands on a weekend."""
    while d.weekday() >= 5:
        d = d + timedelta(days=1)
    return d


# -------------------------------------------
# Which session are we screening?
# -------------------------------------------
print("Which day do you want to invest for?")
print("  1. Tomorrow")
print("  2. Enter a specific date")
choice = input("\nChoice (1 or 2): ").strip()

today_et = datetime.now(ET).replace(tzinfo=None)

if choice == "2":
    date_str = input("Enter AMC date (YYYY-MM-DD): ").strip()
    amc_day = datetime.strptime(date_str, "%Y-%m-%d")
else:
    amc_day = next_weekday(today_et + timedelta(days=1))

bmo_day = next_weekday(amc_day + timedelta(days=1))

amc_str = amc_day.strftime("%Y-%m-%d")
bmo_str = bmo_day.strftime("%Y-%m-%d")

report = Report(f"daily_{amc_str}_AMC_{bmo_str}_BMO.txt")
report.set_meta("SESSION", f"AMC {amc_str} / BMO {bmo_str}")

print(f"\n=== Earnings Screener ===")
print(f"AMC: {amc_str} | BMO: {bmo_str}\n")

# -------------------------------------------
# Step 1: Fetch earnings calendar
# -------------------------------------------
print("Fetching earnings calendar...")

calendar_errors = []

try:
    earnings_amc = fc.get_earnings_by_date(amc_day)
except Exception as e:
    calendar_errors.append(f"AMC {amc_str}: {type(e).__name__}: {e}")
    print(f"  AMC calendar fetch failed: {e}")
    earnings_amc = []

try:
    earnings_bmo = fc.get_earnings_by_date(bmo_day)
except Exception as e:
    calendar_errors.append(f"BMO {bmo_str}: {type(e).__name__}: {e}")
    print(f"  BMO calendar fetch failed: {e}")
    earnings_bmo = []

df_amc = pd.DataFrame(earnings_amc) if len(earnings_amc) > 0 else pd.DataFrame()
df_bmo = pd.DataFrame(earnings_bmo) if len(earnings_bmo) > 0 else pd.DataFrame()

amc = df_amc[df_amc["time"] == "time-after-hours"] if len(df_amc) > 0 else pd.DataFrame()
bmo = df_bmo[df_bmo["time"] == "time-pre-market"] if len(df_bmo) > 0 else pd.DataFrame()

universe = pd.concat([amc, bmo])
tickers = list(dict.fromkeys(universe.index.tolist()))

n_amc, n_bmo = len(amc), len(bmo)
print(f"Found {len(tickers)} stocks ({n_amc} AMC, {n_bmo} BMO).")

buy_list = []
short_list = []
moves_detail = {}
skipped = {"failed filters": 0, "under 8 quarters": 0,
           "eps fetch failed": 0, "price fetch failed": 0}
breaker = CircuitBreaker(check_after=20)
aborted = False
abort_reason = None

# An empty universe after a failed calendar call is a broken run, not a quiet
# day. Without this the report reads "No stocks qualified" either way -- the
# same silent-failure trap that hid the missing lxml for three months.
if calendar_errors:
    aborted = True
    abort_reason = ("The earnings calendar failed: " + "; ".join(calendar_errors)
                    + ". The universe is incomplete, so the lists below mean "
                      "nothing.")

# -------------------------------------------
# Step 2: Analyze
# -------------------------------------------
if len(tickers) > 0:
    print(f"Screening {len(tickers)} stocks...\n")

for count, ticker in enumerate(tickers, start=1):
    rows = universe.loc[[ticker]]
    company_name = rows.iloc[0]["name"]
    timing = "AMC" if rows.iloc[0]["time"] == "time-after-hours" else "BMO"

    progress(f"  Analyzing {count}/{len(tickers)}: {ticker}...", count, len(tickers))

    ed, reason, stock = fetch_eps_history(ticker, limit=24)
    breaker.record(reason)

    if breaker.tripped:
        progress_done()
        print(f"\n!! ABORTING after {breaker.attempted} tickers: every request "
              f"returned no data.")
        print(f"   {breaker.diagnosis()}")
        aborted = True
        break

    if reason is not None:
        skipped["eps fetch failed"] += 1
        continue

    # Sort explicitly. yfinance returns newest-first today, but nothing
    # documents that, and a silent flip would screen the OLDEST 8 quarters
    # while still looking completely normal.
    reported = ed[ed["Reported EPS"].notna()].sort_index(ascending=False).copy()
    if len(reported) < 8:
        skipped["under 8 quarters"] += 1
        continue

    last_8 = reported.head(8)
    eps_beats = int((last_8["Reported EPS"] > last_8["EPS Estimate"]).sum())
    eps_misses = int((last_8["Reported EPS"] < last_8["EPS Estimate"]).sum())

    # Cheap filter before spending calls on price history
    if eps_beats < 6 and eps_misses < 6:
        skipped["failed filters"] += 1
        continue

    earnings_dates_list = last_8.index.tolist()

    # Single fetch covering the whole earnings window AND the momentum window.
    # Two separate calls per ticker was roughly 40 extra requests per run.
    min_date = min(earnings_dates_list) - timedelta(days=5)
    prices, reason = fetch_history(stock, start=min_date, end=datetime.now())
    if reason is not None:
        skipped["price fetch failed"] += 1
        continue

    prices.index = prices.index.tz_localize(None)

    moves = []
    for edate in earnings_dates_list:
        ed_naive = edate.tz_localize(None) if hasattr(edate, "tz_localize") and edate.tzinfo else edate

        future = prices.index[prices.index > ed_naive]
        current = prices.index[prices.index <= ed_naive]

        if len(future) == 0 or len(current) == 0:
            moves.append(None)
            continue

        close_before = prices.loc[current[-1], "Close"]
        close_after = prices.loc[future[0], "Close"]
        moves.append(round((close_after - close_before) / close_before * 100, 2))

    valid_moves = [m for m in moves if m is not None]
    if len(valid_moves) < 8:
        skipped["price fetch failed"] += 1
        continue

    positive_count = sum(1 for m in valid_moves if m > 0)
    negative_count = sum(1 for m in valid_moves if m < 0)
    avg_move = round(sum(valid_moves) / len(valid_moves), 2)
    avg_abs_move = round(sum(abs(m) for m in valid_moves) / len(valid_moves), 2)
    moves_str = ", ".join(f"{m:+.2f}%" if m is not None else "N/A" for m in moves)

    # Filter 3: pre-earnings momentum. period= instead of datetime.now() so a
    # local run and a cloud run of the same session use the same window.
    if len(prices) < 22:
        skipped["price fetch failed"] += 1
        continue

    price_30d_ago = prices["Close"].iloc[-21]
    price_now = prices["Close"].iloc[-1]
    momentum_30d = round((price_now - price_30d_ago) / price_30d_ago * 100, 2)

    is_buy = eps_beats >= 6 and positive_count >= 6 and avg_move > 0 and momentum_30d > 0
    is_short = eps_misses >= 6 and negative_count >= 6 and avg_move < 0 and momentum_30d < 0

    if not (is_buy or is_short):
        skipped["failed filters"] += 1
        continue

    report_date = amc_day if timing == "AMC" else bmo_day
    dvol = realized_daily_vol(prices, earnings_dates_list)
    im = get_implied_move(ticker, report_date, avg_abs_move,
                          daily_vol_pct=dvol, timing=timing)
    oa = get_options_activity(ticker, report_date, timing=timing)

    row = {
        "Ticker": ticker,
        "Company": truncate(company_name, 18),
        "Time": timing,
        "EPS": f"{eps_beats}/8" if is_buy else f"{eps_misses}/8",
        "React": f"{positive_count}/8" if is_buy else f"{negative_count}/8",
        "AvgMove": f"{avg_move:+.2f}%",
        "AbsMove": f"{avg_abs_move:.2f}%",
        "Mom30d": f"{momentum_30d:+.2f}%",
        "IM": f"{im['implied_move_pct']:.2f}%" if im["implied_move_pct"] is not None else "N/A",
        "DTE": im["dte"] if im.get("dte") is not None else "-",
        "IM/H": f"{im['ratio']}" if im["ratio"] is not None else "N/A",
        "Flag": im["flag"] or "-",
        "OptVol": (f"{oa['total_volume']:,}" if oa.get("total_volume") is not None
                   else "N/A"),
        "P/C": ("thin" if oa.get("thin")
                else f"{oa['put_call_ratio']}" if oa["put_call_ratio"] is not None
                else "N/A"),
        "Vol/OI": ("thin" if oa.get("thin")
                   else f"{oa['vol_oi_ratio']}" if oa["vol_oi_ratio"] is not None
                   else "N/A"),
        "_sort": avg_move,
        "_abs": avg_abs_move,
    }
    moves_detail[ticker] = moves_str

    if is_buy:
        buy_list.append(row)
    else:
        short_list.append(row)

progress_done("Screening complete.")

# Catches the case the early-abort threshold misses: a universe smaller than
# check_after where every fetch still failed.
if not aborted and breaker.total_failure:
    aborted = True

buy_list.sort(key=lambda x: x["_sort"], reverse=True)
short_list.sort(key=lambda x: x["_sort"])

# -------------------------------------------
# Build the report
# -------------------------------------------
COLUMNS = [
    ("Ticker", "Ticker", "<"),
    ("Company", "Company", "<"),
    ("Time", "Time", "<"),
    ("EPS", "EPS", ">"),
    ("React", "React", ">"),
    ("AvgMove", "Avg", ">"),
    ("AbsMove", "|Avg|", ">"),
    ("Mom30d", "Mom", ">"),
    ("IM", "IM", ">"),
    ("DTE", "DTE", ">"),
    ("IM/H", "IM/H", ">"),
    ("Flag", "Flag", "<"),
    ("OptVol", "OptVol", ">"),
    ("P/C", "P/C", ">"),
    ("Vol/OI", "Vol/OI", ">"),
]

report.banner(
    "DAILY EARNINGS SCREENER",
    [
        f"AMC {amc_str}   \u00b7   BMO {bmo_str}",
        f"generated {today_et.strftime('%Y-%m-%d %H:%M')} ET",
    ],
)
report.blank()
if aborted:
    report.add("  " + "!" * 74)
    report.add("  !!  DATA FAILURE \u2014 THIS IS NOT A SCREENING RESULT")
    if abort_reason:
        for chunk in [abort_reason[i:i + 68] for i in range(0, len(abort_reason), 68)]:
            report.add(f"  !!  {chunk}")
    else:
        report.add(f"  !!  Aborted after {breaker.attempted} tickers, all returning no data.")
        report.add("  !!  The lists below are empty because the data source failed,")
        report.add("  !!  not because nothing qualified. See SCREENING NOTES.")
    report.add("  " + "!" * 74)
    report.blank()
report.add(f"  UNIVERSE     {len(tickers):>4} confirmed reports   ({n_amc} AMC \u00b7 {n_bmo} BMO)")
report.add(f"  QUALIFIED    {len(buy_list):>4} BUY   \u00b7   {len(short_list)} SHORT")
if buy_list:
    report.add(f"  BUY          {', '.join(r['Ticker'] for r in buy_list)}")
if short_list:
    report.add(f"  SHORT        {', '.join(r['Ticker'] for r in short_list)}")
if not buy_list and not short_list and not aborted:
    report.add(f"  RESULT       No stocks qualified for this session.")

report.section(f"\u25b2  BUY LIST \u2014 {len(buy_list)} names")
report.table(buy_list, COLUMNS)

report.section(f"\u25bc  SHORT LIST \u2014 {len(short_list)} names")
report.table(short_list, COLUMNS)

low = [r for r in buy_list + short_list if r.get("_abs", 99) < 1.5]
if low:
    report.blank()
    report.add("  LOW MOVERS \u2014 average absolute move under 1.5%:")
    for r in low:
        report.add(f"    {r['Ticker']:<8}{r['_abs']:.2f}%   little room for an "
                   f"earnings move to pay for the risk")
    report.add("  Informational only. Not filtered out.")

if moves_detail:
    report.section("NEXT-DAY MOVE HISTORY \u2014 newest first")
    for r in buy_list + short_list:
        t = r["Ticker"]
        report.add(f"  {t:<8}{moves_detail[t]}")

report.section("HOW TO READ THIS")
for line in LEGEND:
    report.add(line)

report.section("SCREENING NOTES")
report.add(f"  Of {len(tickers)} names in the universe:")
report.add(f"    {skipped['failed filters']:>5}  did not meet the filters")
report.add(f"    {skipped['under 8 quarters']:>5}  had fewer than 8 reported quarters available")
report.add(f"    {skipped['eps fetch failed']:>5}  EPS fetch failed")
report.add(f"    {skipped['price fetch failed']:>5}  price fetch failed")
report.blank()
if breaker.attempted:
    report.add(f"  Data fetch failure rate: {breaker.failure_rate * 100:.0f}%"
               f"  ({breaker.fetch_failures} of {breaker.attempted} attempted)")
    if breaker.rate_limited:
        report.add(f"  Rate-limited responses: {breaker.rate_limited}")
if aborted:
    report.blank()
    report.add("  *** RUN ABORTED — THIS IS NOT A SCREENING RESULT ***")
    report.add(f"  {abort_reason or breaker.diagnosis()}")
elif breaker.failure_rate > 0.25:
    report.blank()
    report.add("  Fetch failures above 25% mean this list is incomplete.")
    report.add("  Names may have been dropped for lack of data, not merit.")

report.blank()
report.add("  ENVIRONMENT")
for line in environment_report():
    report.add(f"    {line}")

# -------------------------------------------
# Write + export
# -------------------------------------------
if aborted:
    headline = ("DATA FAILURE \u2014 calendar unavailable. Not a screening result."
                if abort_reason else
                f"DATA FAILURE \u2014 aborted after {breaker.attempted} tickers, "
                f"no usable EPS data. Not a screening result.")
else:
    headline = f"{len(tickers)} scanned \u00b7 {len(buy_list)} BUY \u00b7 {len(short_list)} SHORT"
if buy_list:
    headline += " \u00b7 BUY: " + ", ".join(r["Ticker"] for r in buy_list)
if short_list:
    headline += " \u00b7 SHORT: " + ", ".join(r["Ticker"] for r in short_list)
report.set_meta("HEADLINE", headline)

path = report.write()
report.echo()
print(f"\nReport saved to {path}")

if aborted:
    raise SystemExit(1)
