from finance_calendars import finance_calendars as fc
from datetime import datetime, timedelta
import yfinance as yf
import pandas as pd
import warnings
warnings.filterwarnings("ignore")

from implied_move import get_implied_move
from options_activity import get_options_activity

# -------------------------------------------
# Ask user which week
# -------------------------------------------
print("Which week do you want to screen?")
print("  1. This week")
print("  2. Next week")
print("  3. Enter a specific Monday date")
choice = input("\nChoice (1, 2, or 3): ").strip()

today = datetime.now()

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

# Build weekdays Monday-Friday
week_days = []
for i in range(5):
    day = monday + timedelta(days=i)
    week_days.append(day)

friday = week_days[-1]
print(f"\n=== Weekly Earnings Screener ===")
print(f"Week of {monday.strftime('%Y-%m-%d')} to {friday.strftime('%Y-%m-%d')}\n")

# -------------------------------------------
# Step 1: Fetch earnings for the entire week
# -------------------------------------------
print("Fetching earnings calendar for the week...")

all_earnings = []

for day in week_days:
    try:
        earnings = fc.get_earnings_by_date(day)
        df = pd.DataFrame(earnings) if len(earnings) > 0 else pd.DataFrame()
        if len(df) > 0:
            df["report_date"] = day.strftime("%Y-%m-%d")
            all_earnings.append(df)
            print(f"  {day.strftime('%A %Y-%m-%d')}: {len(df)} stocks")
        else:
            print(f"  {day.strftime('%A %Y-%m-%d')}: 0 stocks")
    except Exception as e:
        print(f"  {day.strftime('%A %Y-%m-%d')}: Error — {e}")

if len(all_earnings) == 0:
    print("\nNo earnings found this week. Exiting.")
    exit()

universe = pd.concat(all_earnings)
tickers = universe.index.tolist()
tickers = list(dict.fromkeys(tickers))

print(f"\nTotal unique stocks: {len(tickers)}")

# -------------------------------------------
# Step 2: Analyze all stocks
# -------------------------------------------
print(f"Screening {len(tickers)} stocks...\n")

buy_list = []
short_list = []
count = 0

for ticker in tickers:
    count += 1
    print(f"  Analyzing {count}/{len(tickers)}: {ticker}...", end="\r")

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

    try:
        stock = yf.Ticker(ticker)
        ed = stock.earnings_dates
    except Exception:
        continue

    if ed is None or ed.empty:
        continue

    reported = ed[ed["Reported EPS"].notna()].copy()

    if len(reported) < 8:
        continue

    last_8 = reported.head(8)
    eps_beats = (last_8["Reported EPS"] > last_8["EPS Estimate"]).sum()
    eps_misses = (last_8["Reported EPS"] < last_8["EPS Estimate"]).sum()

    if eps_beats < 6 and eps_misses < 6:
        continue

    earnings_dates_list = last_8.index.tolist()

    try:
        min_date = min(earnings_dates_list) - timedelta(days=5)
        max_date = max(earnings_dates_list) + timedelta(days=5)
        prices = stock.history(start=min_date, end=max_date)
    except Exception:
        continue

    if prices is None or prices.empty:
        continue

    prices.index = prices.index.tz_localize(None)

    moves = []

    for edate in earnings_dates_list:
        ed_naive = edate.tz_localize(None) if hasattr(edate, 'tz_localize') and edate.tzinfo else edate

        future = prices.index[prices.index > ed_naive]
        current = prices.index[prices.index <= ed_naive]

        if len(future) == 0 or len(current) == 0:
            moves.append(None)
            continue

        next_day = future[0]
        earn_day = current[-1]

        close_before = prices.loc[earn_day, "Close"]
        close_after = prices.loc[next_day, "Close"]

        pct = round((close_after - close_before) / close_before * 100, 2)
        moves.append(pct)

    valid_moves = [m for m in moves if m is not None]
    if len(valid_moves) < 8:
        continue

    positive_count = sum(1 for m in valid_moves if m > 0)
    negative_count = sum(1 for m in valid_moves if m < 0)
    avg_move = round(sum(valid_moves) / len(valid_moves), 2)
    avg_abs_move = round(sum(abs(m) for m in valid_moves) / len(valid_moves), 2)
    moves_str = ", ".join([f"{m:+.2f}%" if m is not None else "N/A" for m in moves])

    # Filter 3: Pre-earnings momentum (last 30 days)
    try:
        end_date = datetime.now()
        start_date = end_date - timedelta(days=45)
        recent_prices = stock.history(start=start_date, end=end_date)
    except Exception:
        continue

    if recent_prices is None or len(recent_prices) < 20:
        continue

    recent_prices.index = recent_prices.index.tz_localize(None)
    price_30d_ago = recent_prices["Close"].iloc[-21]
    price_now = recent_prices["Close"].iloc[-1]
    momentum_30d = round((price_now - price_30d_ago) / price_30d_ago * 100, 2)

    if eps_beats >= 6 and positive_count >= 6 and avg_move > 0 and momentum_30d > 0:
        im = get_implied_move(ticker, report_date, avg_abs_move)
        oa = get_options_activity(ticker, report_date)

        buy_list.append({
            "Ticker": ticker,
            "Company": company_name,
            "Date": report_date,
            "Timing": timing_str,
            "EPS Beats": f"{eps_beats}/8",
            "Positive": f"{positive_count}/8",
            "Avg Move": avg_move,
            "30d Momentum": momentum_30d,
            "IM": im['implied_move_pct'],
            "IM/Hist": im['ratio'],
            "Flag": im['flag'] or "-",
            "P/C": oa['put_call_ratio'],
            "Vol/OI": oa['vol_oi_ratio'],
            "Moves (newest first)": moves_str,
        })
    elif eps_misses >= 6 and negative_count >= 6 and avg_move < 0 and momentum_30d < 0:
        im = get_implied_move(ticker, report_date, avg_abs_move)
        oa = get_options_activity(ticker, report_date)

        short_list.append({
            "Ticker": ticker,
            "Company": company_name,
            "Date": report_date,
            "Timing": timing_str,
            "EPS Misses": f"{eps_misses}/8",
            "Negative": f"{negative_count}/8",
            "Avg Move": avg_move,
            "30d Momentum": momentum_30d,
            "IM": im['implied_move_pct'],
            "IM/Hist": im['ratio'],
            "Flag": im['flag'] or "-",
            "P/C": oa['put_call_ratio'],
            "Vol/OI": oa['vol_oi_ratio'],
            "Moves (newest first)": moves_str,
        })

print(f"Screening complete.                    ")

# -------------------------------------------
# Headline summary
# -------------------------------------------
# Sort first so headline shows strongest names first
buy_list_sorted = sorted(buy_list, key=lambda x: x["Avg Move"], reverse=True)
short_list_sorted = sorted(short_list, key=lambda x: x["Avg Move"])

print(f"\n{'#'*60}")
print(f"#  HEADLINE — Week of {monday.strftime('%Y-%m-%d')}")
print(f"#  Scanned {len(tickers)} stocks. Qualified: {len(buy_list)} BUY / {len(short_list)} SHORT")
if buy_list_sorted:
    print(f"#  BUY:   {', '.join([s['Ticker'] for s in buy_list_sorted])}")
if short_list_sorted:
    print(f"#  SHORT: {', '.join([s['Ticker'] for s in short_list_sorted])}")
if not buy_list and not short_list:
    print(f"#  No stocks qualified this week.")
print(f"{'#'*60}")
# -------------------------------------------
# Sort and output
# -------------------------------------------
pd.set_option("display.width", 400)
pd.set_option("display.max_colwidth", 80)

# Sort buys by avg move descending (strongest first)
buy_list.sort(key=lambda x: x["Avg Move"], reverse=True)
# Sort shorts by avg move ascending (most negative first)
short_list.sort(key=lambda x: x["Avg Move"])


def _fmt_im(v):
    return f"{v:.2f}%" if v is not None else "N/A"


def _fmt_ratio(v):
    return f"{v}" if v is not None else "N/A"


print(f"\n{'='*60}")
print(f"  BUY LIST — {len(buy_list)} stocks (strongest first)")
print(f"  (6+/8 EPS beats, 6+/8 positive reactions,")
print(f"   avg move > 0, 30d momentum > 0)")
print(f"  IM = implied move from straddle; IM/Hist > 1.15 = RICH, < 0.85 = CHEAP")
print(f"  P/C = put/call vol ratio (<0.7 bullish, >1.0 bearish)")
print(f"  Vol/OI = options volume / open interest (>1.0 fresh positioning)")
print(f"{'='*60}\n")

if len(buy_list) > 0:
    buy_df = pd.DataFrame(buy_list)
    buy_df["Rank"] = range(1, len(buy_df) + 1)
    buy_df["Avg Move"] = buy_df["Avg Move"].apply(lambda x: f"{x:+.2f}%")
    buy_df["30d Momentum"] = buy_df["30d Momentum"].apply(lambda x: f"{x:+.2f}%")
    buy_df["IM"] = buy_df["IM"].apply(_fmt_im)
    buy_df["IM/Hist"] = buy_df["IM/Hist"].apply(_fmt_ratio)
    buy_df["P/C"] = buy_df["P/C"].apply(_fmt_ratio)
    buy_df["Vol/OI"] = buy_df["Vol/OI"].apply(_fmt_ratio)
    cols = ["Rank"] + [c for c in buy_df.columns if c != "Rank"]
    print(buy_df[cols].to_string(index=False))
else:
    print("  No stocks qualified.")

print(f"\n{'='*60}")
print(f"  SHORT LIST — {len(short_list)} stocks (strongest first)")
print(f"  (6+/8 EPS misses, 6+/8 negative reactions,")
print(f"   avg move < 0, 30d momentum < 0)")
print(f"  IM = implied move from straddle; IM/Hist > 1.15 = RICH, < 0.85 = CHEAP")
print(f"  P/C = put/call vol ratio (<0.7 bullish, >1.0 bearish)")
print(f"  Vol/OI = options volume / open interest (>1.0 fresh positioning)")
print(f"{'='*60}\n")

if len(short_list) > 0:
    short_df = pd.DataFrame(short_list)
    short_df["Rank"] = range(1, len(short_df) + 1)
    short_df["Avg Move"] = short_df["Avg Move"].apply(lambda x: f"{x:+.2f}%")
    short_df["30d Momentum"] = short_df["30d Momentum"].apply(lambda x: f"{x:+.2f}%")
    short_df["IM"] = short_df["IM"].apply(_fmt_im)
    short_df["IM/Hist"] = short_df["IM/Hist"].apply(_fmt_ratio)
    short_df["P/C"] = short_df["P/C"].apply(_fmt_ratio)
    short_df["Vol/OI"] = short_df["Vol/OI"].apply(_fmt_ratio)
    cols = ["Rank"] + [c for c in short_df.columns if c != "Rank"]
    print(short_df[cols].to_string(index=False))
else:
    print("  No stocks qualified.")
