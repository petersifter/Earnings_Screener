from finance_calendars import finance_calendars as fc
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
ET = ZoneInfo("America/New_York")
import yfinance as yf
import pandas as pd
import warnings
warnings.filterwarnings("ignore")

from implied_move import get_implied_move
from options_activity import get_options_activity
from report_utils import Report, LEGEND, progress, progress_done, truncate
from yf_fetch import (fetch_eps_history, fetch_history, CircuitBreaker,
                      environment_report, RATE_LIMITED)


# -------------------------------------------
# Which week?
# -------------------------------------------
print("Which week do you want to screen?")
print("  1. This week")
print("  2. Next week")
print("  3. Enter a specific Monday date")
choice = input("\nChoice (1, 2, or 3): ").strip()

today = datetime.now(ET).replace(tzinfo=None)

if choice == "2":
    days_until_monday = (7 - today.weekday()) % 7
    if days_until_monday == 0:
        days_until_monday = 7
    monday = today + timedelta(days=days_until_monday)
elif choice == "3":
    date_str = input("Enter Monday date (YYYY-MM-DD): ").strip()
    monday = datetime.strptime(date_str, "%Y-%m-%d")
else:
    monday = today - timedelta(days=today.weekday())

week_days = [monday + timedelta(days=i) for i in range(5)]
friday = week_days[-1]

monday_str = monday.strftime("%Y-%m-%d")
friday_str = friday.strftime("%Y-%m-%d")

report = Report(f"weekly_{monday_str}_to_{friday_str}.txt")
report.set_meta("SESSION", f"week of {monday_str} to {friday_str}")

print(f"\n=== Weekly Earnings Screener ===")
print(f"Week of {monday_str} to {friday_str}\n")

# -------------------------------------------
# Step 1: Fetch the week's calendar
# -------------------------------------------
print("Fetching earnings calendar for the week...")

all_earnings = []
day_counts = []

for day in week_days:
    label = day.strftime("%A %Y-%m-%d")
    try:
        earnings = fc.get_earnings_by_date(day)
        df = pd.DataFrame(earnings) if len(earnings) > 0 else pd.DataFrame()
        if len(df) > 0:
            df["report_date"] = day.strftime("%Y-%m-%d")
            all_earnings.append(df)
        day_counts.append((label, len(df)))
        print(f"  {label}: {len(df)} stocks")
    except Exception as e:
        day_counts.append((label, 0))
        print(f"  {label}: Error \u2014 {e}")

if len(all_earnings) > 0:
    universe = pd.concat(all_earnings)
    tickers = list(dict.fromkeys(universe.index.tolist()))
else:
    universe = pd.DataFrame()
    tickers = []

print(f"\nTotal unique stocks: {len(tickers)}")

buy_list = []
short_list = []
moves_detail = {}
skipped = {"failed filters": 0, "under 8 quarters": 0,
           "eps fetch failed": 0, "price fetch failed": 0}
breaker = CircuitBreaker(check_after=20)
aborted = False

# -------------------------------------------
# Step 2: Analyze
# -------------------------------------------
if len(tickers) > 0:
    print(f"Screening {len(tickers)} stocks...\n")

for count, ticker in enumerate(tickers, start=1):
    progress(f"  Analyzing {count}/{len(tickers)}: {ticker}...", count, len(tickers))

    rows = universe.loc[[ticker]]
    company_name = rows.iloc[0]["name"]
    timing = rows.iloc[0]["time"]
    report_date = rows.iloc[0]["report_date"]

    if timing == "time-after-hours":
        timing_str = "AMC"
    elif timing == "time-pre-market":
        timing_str = "BMO"
    else:
        timing_str = "TBD"

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

    reported = ed[ed["Reported EPS"].notna()].copy()
    if len(reported) < 8:
        skipped["under 8 quarters"] += 1
        continue

    last_8 = reported.head(8)
    eps_beats = int((last_8["Reported EPS"] > last_8["EPS Estimate"]).sum())
    eps_misses = int((last_8["Reported EPS"] < last_8["EPS Estimate"]).sum())

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

    im = get_implied_move(ticker, report_date, avg_abs_move)
    oa = get_options_activity(ticker, report_date)

    row = {
        "Ticker": ticker,
        "Company": truncate(company_name, 22),
        "Date": report_date,
        "Time": timing_str,
        "EPS": f"{eps_beats}/8" if is_buy else f"{eps_misses}/8",
        "React": f"{positive_count}/8" if is_buy else f"{negative_count}/8",
        "AvgMove": f"{avg_move:+.2f}%",
        "Mom30d": f"{momentum_30d:+.2f}%",
        "IM": f"{im['implied_move_pct']:.2f}%" if im["implied_move_pct"] is not None else "N/A",
        "IM/H": f"{im['ratio']}" if im["ratio"] is not None else "N/A",
        "Flag": im["flag"] or "-",
        "P/C": f"{oa['put_call_ratio']}" if oa["put_call_ratio"] is not None else "N/A",
        "Vol/OI": f"{oa['vol_oi_ratio']}" if oa["vol_oi_ratio"] is not None else "N/A",
        "_sort": avg_move,
    }
    moves_detail[ticker] = moves_str

    if is_buy:
        buy_list.append(row)
    else:
        short_list.append(row)

progress_done("Screening complete.")

buy_list.sort(key=lambda x: x["_sort"], reverse=True)
short_list.sort(key=lambda x: x["_sort"])

for i, r in enumerate(buy_list, start=1):
    r["#"] = i
for i, r in enumerate(short_list, start=1):
    r["#"] = i

# -------------------------------------------
# Build the report
# -------------------------------------------
COLUMNS = [
    ("#", "#", ">"),
    ("Ticker", "Ticker", "<"),
    ("Company", "Company", "<"),
    ("Date", "Date", "<"),
    ("Time", "Time", "<"),
    ("EPS", "EPS", ">"),
    ("React", "React", ">"),
    ("AvgMove", "AvgMove", ">"),
    ("Mom30d", "Mom30d", ">"),
    ("IM", "IM", ">"),
    ("IM/H", "IM/H", ">"),
    ("Flag", "Flag", "<"),
    ("P/C", "P/C", ">"),
    ("Vol/OI", "Vol/OI", ">"),
]

report.banner(
    "WEEKLY EARNINGS SCREENER",
    [
        f"week of {monday_str}  \u2192  {friday_str}",
        f"generated {today.strftime('%Y-%m-%d %H:%M')} ET",
    ],
)
report.blank()
if aborted:
    report.add("  " + "!" * 74)
    report.add("  !!  DATA FAILURE \u2014 THIS IS NOT A SCREENING RESULT")
    report.add(f"  !!  Aborted after {breaker.attempted} tickers, all returning no data.")
    report.add("  !!  The lists below are empty because the data source failed,")
    report.add("  !!  not because nothing qualified. See SCREENING NOTES.")
    report.add("  " + "!" * 74)
    report.blank()
report.add(f"  UNIVERSE     {len(tickers):>4} unique names across the week")
for label, n in day_counts:
    report.add(f"                 {label:<22} {n:>4}")
report.blank()
report.add(f"  QUALIFIED    {len(buy_list):>4} BUY   \u00b7   {len(short_list)} SHORT")
if buy_list:
    report.add(f"  BUY          {', '.join(r['Ticker'] for r in buy_list)}")
if short_list:
    report.add(f"  SHORT        {', '.join(r['Ticker'] for r in short_list)}")
if not buy_list and not short_list and not aborted:
    report.add(f"  RESULT       No stocks qualified this week.")

report.section(f"\u25b2  BUY LIST \u2014 {len(buy_list)} names, strongest first")
report.table(buy_list, COLUMNS)

report.section(f"\u25bc  SHORT LIST \u2014 {len(short_list)} names, strongest first")
report.table(short_list, COLUMNS)

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
    report.add("  *** RUN ABORTED \u2014 THIS IS NOT A SCREENING RESULT ***")
    report.add(f"  {breaker.diagnosis()}")
elif breaker.failure_rate > 0.25:
    report.blank()
    report.add("  Fetch failures above 25% mean this list is incomplete.")
    report.add("  Names may have been dropped for lack of data, not merit.")
report.blank()
report.add("  Weekly runs include names whose reporting time is still TBD;")
report.add("  the daily screener only takes confirmed AMC and BMO.")
report.blank()
report.add("  ENVIRONMENT")
for line in environment_report():
    report.add(f"    {line}")

# -------------------------------------------
# Write + export
# -------------------------------------------
if aborted:
    headline = (f"DATA FAILURE \u2014 aborted after {breaker.attempted} tickers, "
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
