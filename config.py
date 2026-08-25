"""
config.py — every tunable threshold in the screener suite, in one place.

These were previously hardcoded, most of them in two files at once, which meant
changing a filter required remembering both screeners used it. It also made
parameter sweeps impossible: you cannot sweep a number you have to grep for.

Nothing here is calibrated. The values are plausible starting points, not
results of any optimisation, and that is exactly why they belong somewhere a
backtest can reach them.
"""

# --- Screening filters -----------------------------------------------------

# Quarters of reported EPS required before a name is judged at all. Fewer than
# this and the name is skipped rather than assessed on a shorter record.
MIN_QUARTERS = 8

# Of those quarters, how many must be beats (BUY) or misses (SHORT).
MIN_BEATS = 6

# Of those quarters, how many next-day closes must move the same direction.
MIN_CONSISTENT_REACTIONS = 6

# Trading days of pre-earnings momentum. 21 is roughly a calendar month.
MOMENTUM_LOOKBACK_DAYS = 21

# Require the straddle NOT to be pricing a bigger move than history before a
# name qualifies. With this False the options overlay is informational only and
# a name can appear on the BUY list with a RICH straddle -- arguably the exact
# setup to skip. With it True, direction comes from the fundamental pattern and
# entry is conditioned on the option not already charging for it.
REQUIRE_CHEAP_OR_FAIR = False

# --- Implied move ----------------------------------------------------------

# Above this the straddle is pricing a bigger move than history: RICH.
RICH_RATIO = 1.15

# Below this it is pricing a smaller one: CHEAP.
CHEAP_RATIO = 0.85

# A straddle expiring this soon after the report IS the event, near enough, so
# the variance decomposition is skipped and the total implied move is used.
# Decomposing at 0-1 DTE means subtracting a full day of trailing realized vol,
# which is inflated precisely on the momentum names this screener selects for.
EVENT_DOMINANT_DTE = 1

# Minimum share of implied VARIANCE the earnings event must account for before
# the ratio is published at all. Below this, recovering the event means
# subtracting two nearly-equal numbers and the answer is dominated by error in
# the vol input. Measured on IMO: moving daily vol from 1.2% to 1.6% took the
# ratio from 3.58 to zero.
MIN_EVENT_SHARE = 0.30

# Warn when the nearest common strike sits this far from spot, in percent.
STRIKE_OFFSET_WARN_PCT = 2.0

# Warn when the expiry used sits this many days past the report.
DTE_WARN_DAYS = 10

# Trailing window for the diffusive baseline, in trading days.
VOL_LOOKBACK_DAYS = 60

# --- Options activity ------------------------------------------------------

# Below this many contracts traded, put/call and volume/OI are arithmetic on a
# handful of trades and are reported as 'thin' instead of as a number. Seen
# live: 74 calls and 120 puts produced a confident-looking 1.62 "BEARISH".
MIN_OPTION_VOLUME = 250

PUT_CALL_BULLISH = 0.70
PUT_CALL_BEARISH = 1.00
VOL_OI_FRESH = 1.00
VOL_OI_ELEVATED = 0.50

# --- Reporting -------------------------------------------------------------

# Flagged as informational. An average absolute move under this leaves little
# room for an earnings move to pay for the risk, and it is the divisor behind
# IM/H, so a small value inflates that ratio.
LOW_MOVER_PCT = 1.5

# Fetch failure rate above this means the list is incomplete and says so.
INCOMPLETE_RUN_FAILURE_RATE = 0.25

# --- Data fetching ---------------------------------------------------------

# Consecutive tickers all returning nothing before the run is declared broken.
CIRCUIT_BREAKER_CHECK_AFTER = 20

# A missing parser fails deterministically, so stop after this many rather than
# retrying 160 tickers three times each.
MISSING_DEP_ABORT_AFTER = 3

FETCH_RETRIES = 3
BACKOFF_BASE_SECONDS = 1.5
