import yfinance as yf
import pandas as pd
from datetime import datetime, timedelta
import warnings
warnings.filterwarnings("ignore")

from implied_move import get_implied_move
from options_activity import get_options_activity

ticker = input("Enter ticker: ").strip().upper()

print(f"\nAnalyzing {ticker}...\n")

try:
    stock = yf.Ticker(ticker)
    info = stock.info
    company_name = info.get("longName", info.get("shortName", ticker))
except Exception:
    company_name = ticker

# -------------------------------------------
# EPS History
# -------------------------------------------
try:
    ed = stock.earnings_dates
except Exception:
    print("Could not fetch earnings data.")
    exit()

if ed is None or ed.empty:
    print("No earnings data available.")
    exit()

reported = ed[ed["Reported EPS"].notna()].copy()

if len(reported) == 0:
    print("No reported earnings found.")
    exit()

quarters_available = min(len(reported), 8)
last_n = reported.head(quarters_available)

eps_beats = (last_n["Reported EPS"] > last_n["EPS Estimate"]).sum()
eps_misses = (last_n["Reported EPS"] < last_n["EPS Estimate"]).sum()
eps_meets = quarters_available - eps_beats - eps_misses

# -------------------------------------------
# Post-earnings price moves
# -------------------------------------------
earnings_dates_list = last_n.index.tolist()

try:
    min_date = min(earnings_dates_list) - timedelta(days=5)
    max_date = max(earnings_dates_list) + timedelta(days=5)
    prices = stock.history(start=min_date, end=max_date)
except Exception:
    print("Could not fetch price history.")
    exit()

if prices is None or prices.empty:
    print("No price data available.")
    exit()

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
positive_count = sum(1 for m in valid_moves if m > 0)
negative_count = sum(1 for m in valid_moves if m < 0)
avg_move = round(sum(valid_moves) / len(valid_moves), 2) if valid_moves else 0
avg_abs_move = round(sum(abs(m) for m in valid_moves) / len(valid_moves), 2) if valid_moves else 0

# -------------------------------------------
# 30-day momentum
# -------------------------------------------
try:
    end_date = datetime.now()
    start_date = end_date - timedelta(days=45)
    recent_prices = stock.history(start=start_date, end=end_date)
    recent_prices.index = recent_prices.index.tz_localize(None)
    if len(recent_prices) >= 20:
        price_30d_ago = recent_prices["Close"].iloc[-21]
        price_now = recent_prices["Close"].iloc[-1]
        momentum_30d = round((price_now - price_30d_ago) / price_30d_ago * 100, 2)
    else:
        momentum_30d = None
except Exception:
    momentum_30d = None

# -------------------------------------------
# Next earnings date
# -------------------------------------------
upcoming = ed[ed["Reported EPS"].isna()]
next_earnings = upcoming.index[0].strftime("%Y-%m-%d") if len(upcoming) > 0 else "Unknown"

# -------------------------------------------
# Output
# -------------------------------------------
print(f"{'='*60}")
print(f"  {company_name} ({ticker})")
print(f"{'='*60}")
print(f"\n  Next earnings date: {next_earnings}")
if momentum_30d is not None:
    print(f"  30-day momentum: {momentum_30d:+.2f}%")

print(f"\n  EPS HISTORY (last {quarters_available} quarters):")
print(f"    Beats: {eps_beats}/{quarters_available}")
print(f"    Misses: {eps_misses}/{quarters_available}")
print(f"    Meets: {eps_meets}/{quarters_available}")

print(f"\n  EPS DETAIL (newest first):")
for i, (idx, row) in enumerate(last_n.iterrows()):
    date_str = idx.strftime("%Y-%m-%d")
    est = row["EPS Estimate"]
    actual = row["Reported EPS"]

    if pd.notna(est) and pd.notna(actual):
        diff = actual - est
        result = "BEAT" if diff > 0 else "MISS" if diff < 0 else "MET"
        move_str = f"{moves[i]:+.2f}%" if i < len(moves) and moves[i] is not None else "N/A"
        print(f"    {date_str}  Est: {est:>8.2f}  Actual: {actual:>8.2f}  {result:>4} ({diff:+.2f})  Next-day: {move_str}")

print(f"\n  POST-EARNINGS PRICE REACTION (last {len(valid_moves)} reports):")
print(f"    Positive: {positive_count}/{len(valid_moves)}")
print(f"    Negative: {negative_count}/{len(valid_moves)}")
print(f"    Avg move (signed): {avg_move:+.2f}%")
print(f"    Avg |move|: {avg_abs_move:.2f}%")

# -------------------------------------------
# Implied move (from options chain)
# -------------------------------------------
print(f"\n  IMPLIED MOVE (from ATM straddle):")
if next_earnings == "Unknown":
    print(f"    N/A — no upcoming earnings date available")
elif not valid_moves:
    print(f"    N/A — no historical moves to compare against")
else:
    im = get_implied_move(ticker, next_earnings, avg_abs_move)
    if im['error']:
        print(f"    N/A — {im['error']}")
    else:
        print(f"    Expiration used: {im['expiration']}")
        print(f"    Spot: ${im['spot']:.2f}  |  ATM strike: ${im['atm_strike']:.2f}")
        print(f"    Straddle price: ${im['straddle_price']:.2f}")
        print(f"    Implied move: {im['implied_move_pct']:.2f}%")
        print(f"    Historical avg |move|: {avg_abs_move:.2f}%")
        if im['ratio'] is not None:
            print(f"    Ratio (IM / Hist): {im['ratio']}  [{im['flag']}]")
            if im['flag'] == 'RICH':
                print(f"      → Straddle is pricing in a larger move than history suggests")
            elif im['flag'] == 'CHEAP':
                print(f"      → Straddle is pricing in a smaller move than history suggests")
            else:
                print(f"      → Straddle is roughly in line with historical moves")

# -------------------------------------------
# Options activity (put/call ratio + volume/OI)
# -------------------------------------------
print(f"\n  OPTIONS ACTIVITY (front-month chain):")
if next_earnings == "Unknown":
    print(f"    N/A — no upcoming earnings date available")
else:
    oa = get_options_activity(ticker, next_earnings)
    if oa['error']:
        print(f"    N/A — {oa['error']}")
    else:
        print(f"    Expiration used: {oa['expiration']}")
        print(f"    Call volume: {oa['total_call_volume']:,}  |  Put volume: {oa['total_put_volume']:,}")
        if oa['put_call_ratio'] is not None:
            print(f"    Put/Call ratio: {oa['put_call_ratio']}  [{oa['pc_signal']}]")
            if oa['pc_signal'] == 'BULLISH':
                print(f"      → Calls dominate — traders positioning for upside")
            elif oa['pc_signal'] == 'BEARISH':
                print(f"      → Puts dominate — traders positioning for downside or hedging")
            else:
                print(f"      → Roughly balanced positioning")
        if oa['vol_oi_ratio'] is not None:
            print(f"    Volume/OI: {oa['vol_oi_ratio']}  [{oa['voi_signal']}]")
            if oa['voi_signal'] == 'FRESH':
                print(f"      → Today's volume exceeds existing OI — lots of new positioning")
            elif oa['voi_signal'] == 'ELEVATED':
                print(f"      → Above-average activity relative to existing positions")
            else:
                print(f"      → Quiet relative to existing positions")

# Signal
print(f"\n  SIGNAL:")
buy_signal = eps_beats >= 6 and positive_count >= 6 and avg_move > 0
short_signal = eps_misses >= 6 and negative_count >= 6 and avg_move < 0

if buy_signal and momentum_30d is not None and momentum_30d > 0:
    print(f"    >>> STRONG BUY — beats + positive reactions + positive momentum")
elif buy_signal:
    print(f"    >>> BUY — beats + positive reactions (momentum neutral/negative)")
elif short_signal and momentum_30d is not None and momentum_30d < 0:
    print(f"    >>> STRONG SHORT — misses + negative reactions + negative momentum")
elif short_signal:
    print(f"    >>> SHORT — misses + negative reactions (momentum neutral/positive)")
else:
    print(f"    No clear signal.")

print()
