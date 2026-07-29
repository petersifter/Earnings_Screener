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

from yf_fetch import yahoo_symbol
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")


def get_implied_move(ticker, earnings_date, historical_avg_move_pct,
                     daily_vol_pct=None, timing='AMC'):
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

    Args (continued):
        timing: 'AMC' or 'BMO'. Decides whether a same-day expiry captures the
            release. Getting this wrong inflates the reading badly.

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
            dte: int, days from earnings date to the expiration used
            strike_offset_pct: float, how far the strike sits from spot, in %
            warning: str or None — set when the reading is unreliable
            error: str or None — error message on failure, None on success

    Note on intrinsic value: straddle/spot is only a valid expected-move proxy
    when the strike sits at spot. On sparse chains the nearest common strike
    can be several percent away, and the in-the-money leg then carries
    intrinsic value that has nothing to do with expected movement. Example
    seen live: CBOE spot 307.81, nearest common strike 300.00, straddle 20.45.
    Raw gives 6.64%; stripping the 7.81 of intrinsic gives 4.11%.
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
        'dte': None,
        'event_move_pct': None,
        'event_share': None,
        'strike_offset_pct': None,
        'warning': None,
        'error': None,
    }

    try:
        # Normalize earnings_date to a date object
        if isinstance(earnings_date, str):
            earnings_date = datetime.strptime(earnings_date, '%Y-%m-%d').date()
        elif hasattr(earnings_date, 'date') and callable(earnings_date.date):
            earnings_date = earnings_date.date()

        tk = yf.Ticker(yahoo_symbol(ticker))

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

        # Pick the first expiration that actually contains the release.
        #
        # AMC: the report lands after the close, so a same-day expiry settles
        #      BEFORE it. We need the next one.
        # BMO: the report lands before the open, so a same-day expiry settles
        #      AFTER it and captures the move exactly. Include it.
        #
        # Treating BMO like AMC pushed CBOE from a 0-day straddle to a 7-day
        # one, loading the reading with a week of unrelated time value.
        same_day_ok = str(timing).upper() == 'BMO'
        target_exp = None
        for exp_str in expirations:
            exp_date = datetime.strptime(exp_str, '%Y-%m-%d').date()
            if exp_date > earnings_date or (same_day_ok and exp_date == earnings_date):
                target_exp = exp_str
                break

        if target_exp is None:
            result['error'] = 'no expiration after earnings date'
            return result
        result['expiration'] = target_exp
        result['dte'] = (datetime.strptime(target_exp, '%Y-%m-%d').date()
                         - earnings_date).days

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

        # Strip intrinsic value. Only the in-the-money leg carries any, and it
        # equals the strike-to-spot distance.
        intrinsic = abs(spot - atm_strike)
        extrinsic = straddle - intrinsic
        if extrinsic <= 0:
            result['error'] = 'straddle is all intrinsic value'
            return result

        offset_pct = (intrinsic / spot) * 100.0
        result['strike_offset_pct'] = round(offset_pct, 2)

        implied_move_pct = (extrinsic / spot) * 100.0
        result['implied_move_pct'] = round(implied_move_pct, 2)

        # Separate the earnings event from ordinary time value.
        #
        # A straddle expiring well after the report prices T days of normal
        # drift PLUS the event. In variance space:
        #     total^2 = diffusive^2 + event^2
        # Recovering the event means subtracting two nearly-equal numbers when
        # diffusive dominates, so the estimate becomes worthless exactly when
        # the expiry is far out. Measured on IMO: shifting the daily-vol input
        # from 1.2% to 1.6% moved the ratio from 3.58 to zero. We therefore
        # gate the ratio rather than publish a number we can't stand behind.
        # A straddle expiring the same day as (or the day after) the report is
        # the event, near enough. Decomposing it means subtracting a full day of
        # trailing realized vol, which is inflated precisely when the stock has
        # just run — i.e. on the momentum names this screener selects for. Below
        # this DTE we take the total and skip the decomposition.
        EVENT_DOMINANT_DTE = 1

        event_move = None
        if result['dte'] is not None and result['dte'] <= EVENT_DOMINANT_DTE:
            pass
        elif daily_vol_pct and daily_vol_pct > 0:
            days_out = (datetime.strptime(target_exp, '%Y-%m-%d').date()
                        - datetime.now(ET).date()).days
            trading_days = max(1.0, days_out * 252.0 / 365.0)
            diffusive = daily_vol_pct * (trading_days ** 0.5)
            ev_var = implied_move_pct ** 2 - diffusive ** 2
            if ev_var > 0:
                event_move = ev_var ** 0.5
                result['event_move_pct'] = round(event_move, 2)
            share = (event_move / implied_move_pct) ** 2 if event_move else 0.0
            result['event_share'] = round(share, 3)

        warnings_found = []
        if offset_pct > 2.0:
            warnings_found.append(
                f'nearest strike is {offset_pct:.1f}% from spot')
        if result['dte'] is not None and result['dte'] > 10:
            warnings_found.append(
                f"expiry is {result['dte']}d after earnings, so this covers "
                f"more than the event")
        result['warning'] = '; '.join(warnings_found) or None

        MIN_EVENT_SHARE = 0.30   # event must be >~55% of the implied move

        if not historical_avg_move_pct or historical_avg_move_pct <= 0:
            return result

        if result['dte'] is not None and result['dte'] <= EVENT_DOMINANT_DTE:
            basis = implied_move_pct
        elif result['event_share'] is None:
            # No vol estimate supplied. Only trust a near-dated expiry.
            if result['dte'] is not None and result['dte'] > 3:
                warnings_found.append(
                    f"expiry is {result['dte']}d after earnings and no vol "
                    f"estimate was supplied, so the event cannot be isolated")
                result['warning'] = '; '.join(warnings_found) or None
                return result
            basis = implied_move_pct
        elif result['event_share'] < MIN_EVENT_SHARE:
            warnings_found.append(
                f"only {result['event_share'] * 100:.0f}% of the straddle's "
                f"variance is the event ({result['dte']}d expiry) — too little "
                f"to separate signal from ordinary time value")
            result['warning'] = '; '.join(warnings_found) or None
            return result
        else:
            basis = event_move

        ratio = basis / historical_avg_move_pct
        result['ratio'] = round(ratio, 2)
        if ratio > 1.15:
            result['flag'] = 'RICH'
        elif ratio < 0.85:
            result['flag'] = 'CHEAP'
        else:
            result['flag'] = 'FAIR'

        result['warning'] = '; '.join(warnings_found) or None
        return result

    except Exception as e:
        result['error'] = f'{type(e).__name__}: {e}'
        return result


def format_implied_move_line(im):
    """Compact one-line summary suitable for console output."""
    if im.get('error'):
        return f"Implied move: N/A ({im['error']})"
    suffix = f"  [!] {im['warning']}" if im.get('warning') else ""
    impl = im['implied_move_pct']
    hist = im['historical_avg_pct']
    ratio = im['ratio']
    flag = im['flag']
    if ratio is None:
        return f"Implied move: {impl}% (no historical avg to compare)" + suffix
    return (f"Implied move: {impl}% | Hist avg: {hist:.2f}% | "
            f"Ratio: {ratio} [{flag}]" + suffix)


def realized_daily_vol(prices, earnings_dates=None, lookback=60):
    """
    Close-to-close daily volatility in percent, for the diffusive component.

    Days immediately following an earnings report are dropped, since those
    contain the event we are trying to isolate and would otherwise inflate the
    baseline. Returns None if there isn't enough clean data.
    """
    try:
        closes = prices['Close'].dropna()
        if len(closes) < 25:
            return None
        rets = closes.pct_change().dropna() * 100.0

        if earnings_dates:
            drop = set()
            for ed in earnings_dates:
                ed_naive = ed.tz_localize(None) if getattr(ed, 'tzinfo', None) else ed
                after = rets.index[rets.index > ed_naive]
                if len(after):
                    drop.add(after[0])
            rets = rets[~rets.index.isin(drop)]

        rets = rets.tail(lookback)
        if len(rets) < 20:
            return None
        return float(rets.std())
    except Exception:
        return None
