"""
ticker_analysis.py — one name, in full detail.

Same filters and same options overlay as the screeners, printed rather than
written to a report. Useful for checking why a name did or didn't qualify.

    python ticker_analysis.py                 # prompts
    python ticker_analysis.py AAPL --timing AMC

Uses yf_fetch like everything else, so a throttled request is reported as
throttling rather than silently becoming "no earnings data available".
"""

import argparse
import sys
import warnings
from datetime import datetime, timedelta

warnings.filterwarnings("ignore")

import config
from implied_move import get_implied_move, realized_daily_vol
from options_activity import get_options_activity
from screener_core import post_earnings_moves
from yf_fetch import fetch_eps_history, fetch_history


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="Analyze one ticker in detail.")
    p.add_argument("ticker", nargs="?", help="stock symbol, e.g. AAPL")
    p.add_argument("--timing", choices=["AMC", "BMO"], default=None,
                   help="does it report after close or before open")
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)

    ticker = args.ticker or input("Enter ticker: ").strip()
    ticker = ticker.strip().upper()
    if not ticker:
        print("No ticker given.")
        return 2

    timing = args.timing
    if timing is None:
        timing = (input("Reports AMC or BMO? [AMC]: ").strip().upper() or "AMC")
    if timing not in ("AMC", "BMO"):
        timing = "AMC"

    print(f"\nAnalyzing {ticker}...\n")

    # --- EPS history -------------------------------------------------------
    ed, reason, stock = fetch_eps_history(ticker, limit=24)
    if reason is not None:
        print(f"Could not fetch earnings data: {reason}")
        return 1

    try:
        company_name = stock.info.get("longName", stock.info.get("shortName", ticker))
    except Exception:
        company_name = ticker

    reported = ed[ed["Reported EPS"].notna()].sort_index(ascending=False).copy()
    if len(reported) == 0:
        print("No reported earnings found.")
        return 1

    n = min(len(reported), config.MIN_QUARTERS)
    recent = reported.head(n)
    if n < config.MIN_QUARTERS:
        print(f"  [!] Only {n} reported quarters available; the screeners "
              f"require {config.MIN_QUARTERS} and would skip this name.\n")

    beats = int((recent["Reported EPS"] > recent["EPS Estimate"]).sum())
    misses = int((recent["Reported EPS"] < recent["EPS Estimate"]).sum())
    meets = n - beats - misses

    # --- Prices ------------------------------------------------------------
    earnings_dates = recent.index.tolist()
    start = min(earnings_dates) - timedelta(days=5)
    prices, reason = fetch_history(stock, start=start, end=datetime.now())
    if reason is not None:
        print(f"Could not fetch price history: {reason}")
        return 1
    prices.index = prices.index.tz_localize(None)

    moves = post_earnings_moves(prices, earnings_dates)
    valid = [m for m in moves if m is not None]
    positive = sum(1 for m in valid if m > 0)
    negative = sum(1 for m in valid if m < 0)
    avg_move = round(sum(valid) / len(valid), 2) if valid else 0
    avg_abs_move = round(sum(abs(m) for m in valid) / len(valid), 2) if valid else 0

    momentum = None
    if len(prices) >= config.MOMENTUM_LOOKBACK_DAYS + 1:
        then = prices["Close"].iloc[-(config.MOMENTUM_LOOKBACK_DAYS + 1)]
        now = prices["Close"].iloc[-1]
        momentum = round((now - then) / then * 100, 2)

    upcoming = ed[ed["Reported EPS"].isna()]
    next_earnings = (upcoming.index[0].strftime("%Y-%m-%d")
                     if len(upcoming) > 0 else "Unknown")

    # --- Output ------------------------------------------------------------
    print("=" * 60)
    print(f"  {company_name} ({ticker})")
    print("=" * 60)
    print(f"\n  Next earnings date: {next_earnings}")
    if momentum is not None:
        print(f"  {config.MOMENTUM_LOOKBACK_DAYS}-day momentum: {momentum:+.2f}%")

    print(f"\n  EPS HISTORY (last {n} quarters):")
    print(f"    Beats:  {beats}/{n}")
    print(f"    Misses: {misses}/{n}")
    print(f"    Meets:  {meets}/{n}")

    print("\n  EPS DETAIL (newest first):")
    for i, (idx, row) in enumerate(recent.iterrows()):
        est, actual = row["EPS Estimate"], row["Reported EPS"]
        if est != est or actual != actual:      # NaN check without importing pandas
            continue
        diff = actual - est
        verdict = "BEAT" if diff > 0 else "MISS" if diff < 0 else "MET"
        move = f"{moves[i]:+.2f}%" if i < len(moves) and moves[i] is not None else "N/A"
        print(f"    {idx.strftime('%Y-%m-%d')}  Est: {est:>8.2f}  "
              f"Actual: {actual:>8.2f}  {verdict:>4} ({diff:+.2f})  Next-day: {move}")

    print(f"\n  POST-EARNINGS PRICE REACTION (last {len(valid)} reports):")
    print(f"    Positive: {positive}/{len(valid)}")
    print(f"    Negative: {negative}/{len(valid)}")
    print(f"    Avg move (signed): {avg_move:+.2f}%")
    print(f"    Avg |move|: {avg_abs_move:.2f}%")

    # --- Implied move ------------------------------------------------------
    print(f"\n  IMPLIED MOVE (from ATM straddle, {timing}):")
    if next_earnings == "Unknown":
        print("    N/A \u2014 no upcoming earnings date available")
    elif not valid:
        print("    N/A \u2014 no historical moves to compare against")
    else:
        dvol = realized_daily_vol(prices, earnings_dates)
        im = get_implied_move(ticker, next_earnings, avg_abs_move,
                              daily_vol_pct=dvol, timing=timing)
        if im["error"]:
            print(f"    N/A \u2014 {im['error']}")
        else:
            dte = f"  ({im['dte']}d after earnings)" if im.get("dte") is not None else ""
            print(f"    Expiration used: {im['expiration']}{dte}")
            print(f"    Spot: ${im['spot']:.2f}  |  ATM strike: ${im['atm_strike']:.2f}")
            if im.get("strike_offset_pct"):
                print(f"    Strike offset from spot: {im['strike_offset_pct']:.2f}%")
            print(f"    Straddle price: ${im['straddle_price']:.2f}"
                  f"  (intrinsic stripped before the move calc)")
            print(f"    Implied move: {im['implied_move_pct']:.2f}%  (total, to expiry)")
            if dvol:
                print(f"    Baseline daily vol: {dvol:.2f}%  (earnings days excluded)")
            if im.get("event_move_pct") is not None:
                print(f"    Event-only move: {im['event_move_pct']:.2f}%"
                      f"  ({im['event_share'] * 100:.0f}% of variance)")
            if im.get("warning"):
                print(f"    [!] {im['warning']}")
                print("        Treat the ratio below as soft.")
            print(f"    Historical avg |move|: {avg_abs_move:.2f}%")
            if im["ratio"] is None:
                print("    Ratio: not reported \u2014 see warning above")
            else:
                print(f"    Ratio (IM / Hist): {im['ratio']}  [{im['flag']}]")

    # --- Options activity --------------------------------------------------
    print("\n  OPTIONS ACTIVITY (front-month chain):")
    if next_earnings == "Unknown":
        print("    N/A \u2014 no upcoming earnings date available")
    else:
        oa = get_options_activity(ticker, next_earnings, timing=timing)
        if oa["error"]:
            print(f"    N/A \u2014 {oa['error']}")
        else:
            print(f"    Expiration used: {oa['expiration']}")
            print(f"    Call volume: {oa['total_call_volume']:,}  |  "
                  f"Put volume: {oa['total_put_volume']:,}")
            if oa["put_call_ratio"] is not None:
                print(f"    Put/Call ratio: {oa['put_call_ratio']}  [{oa['pc_signal']}]")
            if oa["vol_oi_ratio"] is not None:
                print(f"    Volume/OI: {oa['vol_oi_ratio']}  [{oa['voi_signal']}]")

    # --- Signal ------------------------------------------------------------
    print("\n  SIGNAL:")
    buy = (beats >= config.MIN_BEATS
           and positive >= config.MIN_CONSISTENT_REACTIONS and avg_move > 0)
    short = (misses >= config.MIN_BEATS
             and negative >= config.MIN_CONSISTENT_REACTIONS and avg_move < 0)

    if buy and momentum is not None and momentum > 0:
        print("    >>> STRONG BUY \u2014 beats + positive reactions + positive momentum")
    elif buy:
        print("    >>> BUY \u2014 beats + positive reactions (momentum neutral/negative)")
    elif short and momentum is not None and momentum < 0:
        print("    >>> STRONG SHORT \u2014 misses + negative reactions + negative momentum")
    elif short:
        print("    >>> SHORT \u2014 misses + negative reactions (momentum neutral/positive)")
    else:
        print("    No clear signal.")

    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
