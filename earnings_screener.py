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

# -------------------------------------------
# Ask user which day to invest
# -------------------------------------------
print("Which day do you want to invest for?")
print("  1. Tomorrow")
print("  2. Enter a specific date")
choice = input("\nChoice (1 or 2): ").strip()

if choice == "2":
    date_str = input("Enter date (YYYY-MM-DD): ").strip()
    invest_day = datetime.now(ET).replace(tzinfo=None) + timedelta(days=1)
else:
    invest_day = datetime.now() + timedelta(days=1)

# Find the next trading day after invest_day (skip weekends)
next_day = invest_day + timedelta(days=1)
while next_day.weekday() >= 5:
    next_day = next_day + timedelta(days=1)

tomorrow = invest_day
day_after = next_day

print(f"\n=== Earnings Screener ===")
print(f"AMC: {tomorrow.strftime('%Y-%m-%d')} | BMO: {day_after.strftime('%Y-%m-%d')}\n")

# -------------------------------------------
# Step 1: Fetch earnings calendar
# -------------------------------------------
print("Fetching earnings calendar...")

earnings_tomorrow = fc.get_earnings_by_date(tomorrow)
earnings_day_after = fc.get_earnings_by_date(day_after)

df_tom = pd.DataFrame(earnings_tomorrow) if len(earnings_tomorrow) > 0 else pd.DataFrame()
df_day = pd.DataFrame(earnings_day_after) if len(earnings_day_after) > 0 else pd.DataFrame()

amc = df_tom[df_tom["time"] == "time-after-hours"] if len(df_tom) > 0 else pd.DataFrame()
bmo = df_day[df_day["time"] == "time-pre-market"] if len(df_day) > 0 else pd.DataFrame()

universe = pd.concat([amc, bmo])
tickers = universe.index.tolist()

print(f"Found {len(tickers)} stocks ({len(amc)} AMC, {len(bmo)} BMO).")

if len(tickers) == 0:
    print("No confirmed stocks found. Exiting.")
    exit()

# -------------------------------------------
# Step 2: Analyze all stocks
# -------------------------------------------
print(f"Screening {len(tickers)} stocks...\n")

buy_list = []
short_list = []
count = 0

for ticker in tickers:
    count += 1
    rows = universe.loc[[ticker]]
    company_name = rows.iloc[0]["name"]
    timing = "AMC" if rows.iloc[0]["time"] == "time-after-hours" else "BMO"

    print(f"  Analyzing {count}/{len(tickers)}: {ticker}...", end="\r")

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

    # Quick filter before fetching prices
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

        next_day_idx = future[0]
        earn_day = current[-1]

        close_before = prices.loc[earn_day, "Close"]
        close_after = prices.loc[next_day_idx, "Close"]

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
        upcoming_earnings = tomorrow if timing == "AMC" else day_after
        im = get_implied_move(ticker, upcoming_earnings, avg_abs_move)
        oa = get_options_activity(ticker, upcoming_earnings)

        buy_list.append({
            "Ticker": ticker,
            "Company": company_name,
            "Timing": timing,
            "EPS Beats": f"{eps_beats}/8",
            "Positive": f"{positive_count}/8",
            "Avg Move": f"{avg_move:+.2f}%",
            "30d Momentum": f"{momentum_30d:+.2f}%",
            "IM": f"{im['implied_move_pct']:.2f}%" if im['implied_move_pct'] is not None else "N/A",
            "IM/Hist": f"{im['ratio']}" if im['ratio'] is not None else "N/A",
            "Flag": im['flag'] or "-",
            "P/C": f"{oa['put_call_ratio']}" if oa['put_call_ratio'] is not None else "N/A",
            "Vol/OI": f"{oa['vol_oi_ratio']}" if oa['vol_oi_ratio'] is not None else "N/A",
            "Moves (newest first)": moves_str,
        })
    elif eps_misses >= 6 and negative_count >= 6 and avg_move < 0 and momentum_30d < 0:
        upcoming_earnings = tomorrow if timing == "AMC" else day_after
        im = get_implied_move(ticker, upcoming_earnings, avg_abs_move)
        oa = get_options_activity(ticker, upcoming_earnings)

        short_list.append({
            "Ticker": ticker,
            "Company": company_name,
            "Timing": timing,
            "EPS Misses": f"{eps_misses}/8",
            "Negative": f"{negative_count}/8",
            "Avg Move": f"{avg_move:+.2f}%",
            "30d Momentum": f"{momentum_30d:+.2f}%",
            "IM": f"{im['implied_move_pct']:.2f}%" if im['implied_move_pct'] is not None else "N/A",
            "IM/Hist": f"{im['ratio']}" if im['ratio'] is not None else "N/A",
            "Flag": im['flag'] or "-",
            "P/C": f"{oa['put_call_ratio']}" if oa['put_call_ratio'] is not None else "N/A",
            "Vol/OI": f"{oa['vol_oi_ratio']}" if oa['vol_oi_ratio'] is not None else "N/A",
            "Moves (newest first)": moves_str,
        })

print(f"Screening complete.                    ")

# -------------------------------------------
# Headline summary
# -------------------------------------------
print(f"\n{'#'*60}")
print(f"#  HEADLINE — {tomorrow.strftime('%Y-%m-%d')}")
print(f"#  Scanned {len(tickers)} stocks. Qualified: {len(buy_list)} BUY / {len(short_list)} SHORT")
if buy_list:
    print(f"#  BUY:   {', '.join([s['Ticker'] for s in buy_list])}")
if short_list:
    print(f"#  SHORT: {', '.join([s['Ticker'] for s in short_list])}")
if not buy_list and not short_list:
    print(f"#  No stocks qualified today.")
print(f"{'#'*60}")
# -------------------------------------------
# Output
# -------------------------------------------
pd.set_option("display.width", 400)
pd.set_option("display.max_colwidth", 80)

print(f"\n{'='*60}")
print(f"  BUY LIST — {len(buy_list)} stocks")
print(f"  (6+/8 EPS beats, 6+/8 positive reactions,")
print(f"   avg move > 0, 30d momentum > 0)")
print(f"  IM = implied move from straddle; IM/Hist > 1.15 = RICH, < 0.85 = CHEAP")
print(f"  P/C = put/call vol ratio (<0.7 bullish, >1.0 bearish)")
print(f"  Vol/OI = options volume / open interest (>1.0 fresh positioning)")
print(f"{'='*60}\n")

if len(buy_list) > 0:
    print(pd.DataFrame(buy_list).to_string(index=False))
else:
    print("  No stocks qualified.")

print(f"\n{'='*60}")
print(f"  SHORT LIST — {len(short_list)} stocks")
print(f"  (6+/8 EPS misses, 6+/8 negative reactions,")
print(f"   avg move < 0, 30d momentum < 0)")
print(f"  IM = implied move from straddle; IM/Hist > 1.15 = RICH, < 0.85 = CHEAP")
print(f"  P/C = put/call vol ratio (<0.7 bullish, >1.0 bearish)")
print(f"  Vol/OI = options volume / open interest (>1.0 fresh positioning)")
print(f"{'='*60}\n")

if len(short_list) > 0:
    print(pd.DataFrame(short_list).to_string(index=False))
else:
    print("  No stocks qualified.")
