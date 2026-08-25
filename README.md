# Earnings Event Screener

Screens the daily and weekly US earnings calendar for names with a consistent
history of beating estimates and moving in a predictable direction afterwards,
then prices each candidate's at-the-money straddle against that name's own
post-earnings history to see whether the options market is already charging for
the move.

Two entry points — a daily screener that pairs an after-close session with the
following morning's pre-open reports, and a weekly one that covers Monday to
Friday. Both write a plain-text report and run either locally or on GitHub
Actions.

## What this is not

It is not backtested (NEXT STEP!). The filters are a conjunction of conditions that look
sensible; nothing here measures whether they predict the next quarter, and
screening on eight quarters of realised beats is close to selecting on the
outcome. Treat the output as a watchlist, not a signal.

The options overlay is informational and filters nothing. A name can appear on
the BUY list with a straddle flagged RICH, which is arguably the setup you'd
want to skip.

## Quick start

```bash
pip install -r requirements.txt
python earnings_screener.py     # tomorrow's AMC + next morning's BMO
python weekly_screener.py       # a full Mon-Fri week
python ticker_analysis.py       # one name, in detail
python selftest.py              # regression checks, run before pushing
```

Reports land in `reports/`. If EPS fetches come back empty, run `diagnose.py`
before assuming it's throttling — see Failure handling below.

## The filters

All three must pass. A name qualifies for BUY or SHORT, never both.

| | BUY | SHORT |
|---|---|---|
| EPS, last 8 quarters | 6+ beats | 6+ misses |
| Next-day reaction | 6+ closes up, and average signed move positive | 6+ closes down, and average signed move negative |
| Momentum into earnings | 21 trading days positive | 21 trading days negative |

Fewer than eight reported quarters and the name is skipped rather than judged
on a shorter record.

## The options overlay

Shown for context on every qualifying name.

**IM** — implied move from the ATM straddle as a percentage of spot, with
intrinsic value removed first. Straddle divided by spot is only a valid
expected-move proxy when the strike sits at spot. On a sparse chain the nearest
common strike can be several percent away and the in-the-money leg then carries
intrinsic value that has nothing to do with expected movement. Seen live: CBOE
spot 307.81, nearest common strike 300.00, straddle 20.45. Raw gives 6.64%;
stripping the 7.81 of intrinsic gives 4.11%.

**DTE** — days from the report to the expiry used. Expiry selection depends on
report timing, and getting it backwards inflates the reading badly. An
after-close report lands after a same-day expiry has already settled, so the
next expiry is the first one that contains the event. A pre-open report lands
before the same-day expiry settles, so that chain captures it exactly. Treating
a pre-open name like an after-close one pushed CBOE from a 0-day straddle to a
7-day one and loaded the reading with a week of unrelated time value.

**IM/H** — implied move divided by the historical average absolute move. Above
1.15 reads RICH, below 0.85 CHEAP.

A straddle expiring well after the report prices ordinary drift *plus* the
event, so the two are separated in variance space:

```
total² = diffusive² + event²
```

That subtraction is unstable exactly when it matters most. When the diffusive
term dominates, recovering the event means subtracting two nearly equal
numbers, and small errors in the volatility input blow up the answer. Measured
on IMO: moving the daily-vol input from 1.2% to 1.6% took the ratio from 3.58
to zero. So the ratio is suppressed rather than published whenever the event
accounts for less than 30% of implied variance. **A blank IM/H is not a neutral
reading — it means no reading was possible.**

Below one day to expiry the decomposition is skipped entirely and the total is
used. Subtracting a day of trailing realised vol is worst on names that have
just run, which is precisely what the momentum filter selects for.

**P/C and Vol/OI** — put/call and volume/open-interest ratios on the same
expiry. Both read `thin` below 250 contracts. Under that floor the ratio is
arithmetic on a handful of trades: CBOE's chain once carried 74 calls and 120
puts and produced a confident-looking 1.62 "BEARISH" out of 194 contracts.

## Failure handling

yfinance 1.x doesn't raise when a fetch fails. It returns `None` and logs
"symbol may be delisted", which is indistinguishable from a genuinely delisted
ticker. Wrapped in a bare `except`, 160 throttled requests become "160 names
had no EPS history" and the run emails a clean-looking empty report.

Everything in `yf_fetch.py` therefore returns `(data, reason)`, where `reason`
is `None` on success and a classified string otherwise, so the report can say
*why* a name dropped out. On top of that:

- Retry with exponential backoff and jitter on rate limits and empty results.
- `ImportError` is never retried — a missing parser fails identically every
  time, and retrying 160 tickers turns a one-line fix into a 20-minute run.
- A circuit breaker aborts the run when every request is failing, and the
  report is stamped `DATA FAILURE — THIS IS NOT A SCREENING RESULT`. An empty
  list and a broken data source must never look the same.
- Ticker symbols are normalised for Yahoo's share-class convention (`MOG.A`
  from the NASDAQ calendar becomes `MOG-A`).

**lxml is the dependency that will bite you.** yfinance parses the earnings
table with `pandas.read_html`, which needs lxml, but lxml is not one of
yfinance's declared dependencies. A clean install produces a yfinance that
fetches prices fine and raises `ImportError` on every EPS lookup. That was the
"160 names returned no EPS history" failure — not throttling, not a blocked IP,
just a missing parser. It's pinned in `requirements.txt` for that reason.

## Layout

```
earnings_screener.py   daily run: after-close session + next morning's pre-open
weekly_screener.py     Monday-to-Friday run
ticker_analysis.py     single name, full detail
implied_move.py        straddle pricing, intrinsic stripping, variance split
options_activity.py    put/call and volume/OI, with the liquidity floor
yf_fetch.py            retries, error classification, circuit breaker
report_utils.py        report building, .last_run handshake, console progress
diagnose.py            why are EPS fetches empty — run locally and in CI
selftest.py            end-to-end regression checks against stubbed data
```

`report_utils.py` owns report filenames. They used to be generated twice —
once by the shell in the workflow and once implicitly by the screener — and the
two disagreed whenever a run straddled UTC midnight, which is why one weekly
email arrived with no attachment. Python now writes the file and drops a
`.last_run` sidecar of `KEY=value` lines that the workflow reads straight into
`$GITHUB_ENV`.

## Data sources

NASDAQ earnings calendar via `finance_calendars`; prices, EPS history and
option chains via `yfinance`. Both are unofficial and free, and neither
guarantees availability. Yahoo tolerates a steady trickle far better than a
burst, so set `SCREENER_SPACING=0.5` in CI.
