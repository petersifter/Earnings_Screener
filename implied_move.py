"""
Implied move / straddle comparison helper for the earnings screener suite.

Pulls the ATM straddle from the first expiration after the earnings date,
computes the implied move as a % of spot, and compares it to the historical
average post-earnings move that the screener already calculates.

Used by earnings_screener.py, weekly_screener.py, and ticker_analysis.py.
Informational only — does not filter tickers.
"""

import yfinance as yf
from datetime import datetime


def get_implied_move(ticker, earnings_date, historical_avg_move_pct):
    """
    Pull the ATM straddle and compare implied move to historical average.

    Args:
        ticker: stock symbol, e.g. "AAPL"
        earnings_date: date of the earnings announcement (datetime, date, or
            "YYYY-MM-DD" string). For AMC this is the announcement day; the
            function targets the first expiration strictly after this date.
        historical_avg_move_pct: average absolute next-day post-earnings move
            from prior quarters, in percent (e.g. 5.2 for 5.2%). The screener
            already computes this in filter 2 — pass it in.

    Returns:
        dict with keys:
            implied_move_pct: float, implied move from straddle (% of spot)
            historical_avg_pct: float, echoed back from input
            ratio: float, implied / historical (None if not computable)
            straddle_price: float, dollar cost of ATM straddle
            atm_strike: float, the strike used
            spot: float, current stock price used
            expiration: str, the expiration date used (YYYY-MM-DD)
            flag: "RICH" if ratio > 1.15, "CHEAP" if < 0.85, else "FAIR"
            error: str or None — error message on failure, None on success
    """
    result = {
        'implied_move_pct': None,
        'historical_avg_pct': historical_avg_move_pct,
        'ratio': None,
        'straddle_price': None,
        'atm_strike': None,
        'spot': None,
        'expiration': None,
        'flag': None,
        'error': None,
    }

    try:
        # Normalize earnings_date to a date object
        if isinstance(earnings_date, str):
            earnings_date = datetime.strptime(earnings_date, '%Y-%m-%d').date()
        elif hasattr(earnings_date, 'date') and callable(earnings_date.date):
            earnings_date = earnings_date.date()

        tk = yf.Ticker(ticker)

        # Spot price
        hist = tk.history(period='1d')
        if hist.empty:
            result['error'] = 'no price data'
            return result
        spot = float(hist['Close'].iloc[-1])
        result['spot'] = round(spot, 2)

        # Available expirations
        expirations = tk.options
        if not expirations:
            result['error'] = 'no options chain available'
            return result

        # First expiration strictly after earnings date.
        # (For AMC announcements, same-day weekly expires before the release,
        # so the next expiration is what's actually pricing in earnings.)
        target_exp = None
        for exp_str in expirations:
            exp_date = datetime.strptime(exp_str, '%Y-%m-%d').date()
            if exp_date > earnings_date:
                target_exp = exp_str
                break

        if target_exp is None:
            result['error'] = 'no expiration after earnings date'
            return result
        result['expiration'] = target_exp

        # Option chain for that expiration
        chain = tk.option_chain(target_exp)
        calls, puts = chain.calls, chain.puts
        if calls.empty or puts.empty:
            result['error'] = 'empty calls or puts'
            return result

        # ATM strike: closest to spot, present in BOTH calls and puts
        common_strikes = set(calls['strike']).intersection(set(puts['strike']))
        if not common_strikes:
            result['error'] = 'no overlapping strikes between calls and puts'
            return result
        atm_strike = min(common_strikes, key=lambda s: abs(s - spot))
        result['atm_strike'] = float(atm_strike)

        call_row = calls[calls['strike'] == atm_strike].iloc[0]
        put_row = puts[puts['strike'] == atm_strike].iloc[0]

        # Mid price with fallback to last if bid is zero/missing
        def fair_price(bid, ask, last):
            try:
                if bid is not None and ask is not None and bid > 0 and ask > 0:
                    return (float(bid) + float(ask)) / 2.0
            except (TypeError, ValueError):
                pass
            try:
                if last is not None and float(last) > 0:
                    return float(last)
            except (TypeError, ValueError):
                pass
            return None

        call_price = fair_price(call_row.get('bid'), call_row.get('ask'), call_row.get('lastPrice'))
        put_price = fair_price(put_row.get('bid'), put_row.get('ask'), put_row.get('lastPrice'))

        if call_price is None or put_price is None:
            result['error'] = 'invalid option prices (zero bid/ask, no last)'
            return result

        straddle = call_price + put_price
        result['straddle_price'] = round(straddle, 2)

        implied_move_pct = (straddle / spot) * 100.0
        result['implied_move_pct'] = round(implied_move_pct, 2)

        if historical_avg_move_pct and historical_avg_move_pct > 0:
            ratio = implied_move_pct / historical_avg_move_pct
            result['ratio'] = round(ratio, 2)
            if ratio > 1.15:
                result['flag'] = 'RICH'
            elif ratio < 0.85:
                result['flag'] = 'CHEAP'
            else:
                result['flag'] = 'FAIR'

        return result

    except Exception as e:
        result['error'] = f'{type(e).__name__}: {e}'
        return result


def format_implied_move_line(im):
    """Compact one-line summary suitable for console output."""
    if im.get('error'):
        return f"Implied move: N/A ({im['error']})"
    impl = im['implied_move_pct']
    hist = im['historical_avg_pct']
    ratio = im['ratio']
    flag = im['flag']
    if ratio is None:
        return f"Implied move: {impl}% (no historical avg to compare)"
    return f"Implied move: {impl}% | Hist avg: {hist:.2f}% | Ratio: {ratio} [{flag}]"
