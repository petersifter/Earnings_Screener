"""
yf_fetch.py — resilient yfinance access with retries and honest error reporting.

The problem this solves
-----------------------
yfinance 1.x does not raise when a fetch fails. get_earnings_dates() returns
None and logs "No earnings dates found, symbol may be delisted" — which looks
identical to a genuinely delisted ticker. Wrapped in a bare except, 160
throttled requests become "160 names had no EPS history", and the screener
emails a clean-looking empty report.

Every function here returns (data, reason). reason is None on success and a
short classified string on failure, so the report can say *why* a name dropped
out instead of guessing.
"""

import os
import time
import random

import yfinance as yf

# Seconds to wait between tickers. Yahoo tolerates a steady trickle far better
# than a burst of several hundred requests. Set SCREENER_SPACING=0.5 in CI.
REQUEST_SPACING = float(os.environ.get("SCREENER_SPACING", "0"))

try:
    from yfinance.exceptions import YFRateLimitError
except Exception:  # older versions
    class YFRateLimitError(Exception):
        pass

RATE_LIMITED = "rate limited"
NO_DATA = "no data returned"
ERROR = "fetch error"


def _sleep_backoff(attempt, base=1.5):
    """Exponential backoff with jitter, so parallel retries don't sync up."""
    time.sleep(base * (2 ** attempt) + random.uniform(0, 0.75))


def _retry(fn, retries=3, base=1.5):
    """
    Call fn(). Returns (result, reason).
    Retries on rate limits and on empty results, since yfinance reports
    throttling as an empty result rather than an exception.
    """
    last = NO_DATA
    for attempt in range(retries):
        try:
            result = fn()
        except YFRateLimitError:
            last = RATE_LIMITED
            if attempt < retries - 1:
                _sleep_backoff(attempt, base * 2)
            continue
        except Exception as e:
            last = f"{ERROR}: {type(e).__name__}"
            if attempt < retries - 1:
                _sleep_backoff(attempt, base)
            continue

        if result is None or (hasattr(result, "empty") and result.empty):
            last = NO_DATA
            if attempt < retries - 1:
                _sleep_backoff(attempt, base)
            continue

        return result, None

    return None, last


def pace():
    """Spacing between tickers, so a 160-name run is a trickle not a burst."""
    if REQUEST_SPACING > 0:
        time.sleep(REQUEST_SPACING + random.uniform(0, REQUEST_SPACING / 2))


def fetch_eps_history(ticker, limit=24, retries=3):
    """
    Earnings history for a ticker.

    The default limit of 12 includes upcoming unreported quarters, which can
    leave fewer than the 8 reported ones the screener needs. Ask for more.

    Returns (DataFrame, reason).
    """
    pace()
    stock = yf.Ticker(ticker)

    def call():
        try:
            return stock.get_earnings_dates(limit=limit)
        except TypeError:
            return stock.earnings_dates

    df, reason = _retry(call, retries=retries)
    return df, reason, stock


def fetch_history(stock, retries=2, **kwargs):
    """Price history. Returns (DataFrame, reason)."""
    return _retry(lambda: stock.history(**kwargs), retries=retries)


class CircuitBreaker:
    """
    Aborts a run when the data source is clearly down rather than letting the
    screener emit an empty report that reads like "nothing qualified today".

    Trips when the first `check_after` tickers ALL failed to return data.
    """

    def __init__(self, check_after=20, threshold=1.0):
        self.check_after = check_after
        self.threshold = threshold
        self.attempted = 0
        self.fetch_failures = 0
        self.rate_limited = 0

    def record(self, reason):
        self.attempted += 1
        if reason is not None:
            self.fetch_failures += 1
            if reason == RATE_LIMITED:
                self.rate_limited += 1

    @property
    def tripped(self):
        if self.attempted < self.check_after:
            return False
        return (self.fetch_failures / self.attempted) >= self.threshold

    @property
    def failure_rate(self):
        if not self.attempted:
            return 0.0
        return self.fetch_failures / self.attempted

    def diagnosis(self):
        if self.rate_limited:
            return ("Yahoo is rate limiting this host. On GitHub Actions the "
                    "runner IP is shared and often already throttled.")
        return ("Every request returned empty. Either the host is blocked, or "
                "the installed yfinance version cannot reach the endpoint.")


def environment_report():
    """Version fingerprint, for comparing a working local run against CI."""
    import sys
    import platform

    lines = [
        f"python      {sys.version.split()[0]} on {platform.system()}",
        f"yfinance    {getattr(yf, '__version__', 'unknown')}",
    ]
    for mod in ("curl_cffi", "pandas", "requests"):
        try:
            m = __import__(mod)
            lines.append(f"{mod:<11} {getattr(m, '__version__', 'unknown')}")
        except Exception:
            lines.append(f"{mod:<11} NOT INSTALLED")
    return lines
