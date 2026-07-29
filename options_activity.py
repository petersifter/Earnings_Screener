"""
Options activity helper — put/call ratio and volume/OI ratio.

Pulls the front-month expiration (first one strictly after earnings date) and
computes two simple positioning metrics:

  - Put/Call ratio: total put volume / total call volume
      < 0.7  -> BULLISH (calls dominate)
      0.7-1.0 -> NEUTRAL
      > 1.0  -> BEARISH (puts dominate)

  - Volume/OI ratio: (call vol + put vol) / total open interest
      > 1.0  -> FRESH      (today's volume exceeds existing positions =
                            lots of new positioning being opened)
      0.5-1.0 -> ELEVATED
      < 0.5  -> QUIET

Note: the volume/OI ratio is most informative when run during or after the
trading day. Pre-market it reflects yesterday's final volume.
"""

import yfinance as yf
from datetime import datetime

from yf_fetch import yahoo_symbol


def get_options_activity(ticker, earnings_date, timing='AMC',
                         min_volume=250):
    """
    Compute put/call and volume/OI ratios for the front-month expiration.

    Args:
        ticker: stock symbol
        earnings_date: date of upcoming earnings (datetime, date, or "YYYY-MM-DD")

    Returns:
        dict with put_call_ratio, pc_signal, vol_oi_ratio, voi_signal,
        total_call_volume, total_put_volume, total_oi, expiration, error
    """
    result = {
        'put_call_ratio': None,
        'pc_signal': None,
        'vol_oi_ratio': None,
        'voi_signal': None,
        'total_call_volume': None,
        'total_put_volume': None,
        'total_oi': None,
        'total_volume': None,
        'thin': False,
        'expiration': None,
        'error': None,
    }

    try:
        if isinstance(earnings_date, str):
            earnings_date = datetime.strptime(earnings_date, '%Y-%m-%d').date()
        elif hasattr(earnings_date, 'date') and callable(earnings_date.date):
            earnings_date = earnings_date.date()

        tk = yf.Ticker(yahoo_symbol(ticker))
        expirations = tk.options
        if not expirations:
            result['error'] = 'no options chain available'
            return result

        # AMC settles before a same-day expiry, BMO settles after it. Same
        # rule as implied_move.py — a BMO name needs the same-day chain.
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

        chain = tk.option_chain(target_exp)
        calls, puts = chain.calls, chain.puts

        if calls.empty and puts.empty:
            result['error'] = 'empty calls and puts'
            return result

        # Sum volume and open interest, treating NaN as zero
        call_vol = int(calls['volume'].fillna(0).sum()) if not calls.empty else 0
        put_vol = int(puts['volume'].fillna(0).sum()) if not puts.empty else 0
        call_oi = int(calls['openInterest'].fillna(0).sum()) if not calls.empty else 0
        put_oi = int(puts['openInterest'].fillna(0).sum()) if not puts.empty else 0

        result['total_call_volume'] = call_vol
        result['total_put_volume'] = put_vol
        result['total_oi'] = call_oi + put_oi
        result['total_volume'] = call_vol + put_vol

        # Below this, the ratios are arithmetic on a handful of contracts.
        # Observed live: CBOE's chain carried 74 calls and 120 puts, which
        # produced a confident-looking 1.62 "BEARISH" out of 194 contracts.
        # A ratio is only reported once there is enough volume to mean anything.
        if result['total_volume'] < min_volume:
            result['thin'] = True
            result['error'] = (f"only {result['total_volume']} contracts traded "
                               f"(floor {min_volume}) \u2014 ratios not meaningful")
            return result

        # Put/Call ratio
        if call_vol > 0:
            pc = put_vol / call_vol
            result['put_call_ratio'] = round(pc, 2)
            if pc < 0.7:
                result['pc_signal'] = 'BULLISH'
            elif pc > 1.0:
                result['pc_signal'] = 'BEARISH'
            else:
                result['pc_signal'] = 'NEUTRAL'

        # Volume / OI ratio
        total_vol = call_vol + put_vol
        total_oi = call_oi + put_oi
        if total_oi > 0:
            voi = total_vol / total_oi
            result['vol_oi_ratio'] = round(voi, 2)
            if voi > 1.0:
                result['voi_signal'] = 'FRESH'
            elif voi > 0.5:
                result['voi_signal'] = 'ELEVATED'
            else:
                result['voi_signal'] = 'QUIET'

        # Surface a soft error if neither metric was computable
        if result['put_call_ratio'] is None and result['vol_oi_ratio'] is None:
            result['error'] = 'no usable volume or OI data'

        return result

    except Exception as e:
        result['error'] = f'{type(e).__name__}: {e}'
        return result
