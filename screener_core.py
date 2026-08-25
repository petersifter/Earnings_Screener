"""
screener_core.py — the screening logic and report assembly shared by the daily
and weekly screeners.

Both screeners previously carried their own copy of this: the same filters, the
same move computation, the same row construction, the same report sections.
Roughly 300 duplicated lines, which meant every filter change had to be made
twice and a fix applied to one screener silently left the other wrong.

The two entry points now differ only in how they build the universe and how
they label the output. Everything below this line is common.
"""

from datetime import datetime, timedelta

import config
from implied_move import get_implied_move, realized_daily_vol
from options_activity import get_options_activity
from report_utils import LEGEND, progress, progress_done, truncate
from yf_fetch import CircuitBreaker, environment_report, fetch_eps_history, fetch_history


# Columns shared by both reports. The weekly adds "#" and "Date" around these.
BASE_COLUMNS = [
    ("Ticker", "Ticker", "<"),
    ("Company", "Company", "<"),
    ("Time", "Time", "<"),
    ("EPS", "EPS", ">"),
    ("React", "React", ">"),
    ("AvgMove", "Avg", ">"),
    ("AbsMove", "|Avg|", ">"),
    ("Mom30d", "Mom", ">"),
    ("IM", "IM", ">"),
    ("DTE", "DTE", ">"),
    ("IM/H", "IM/H", ">"),
    ("Flag", "Flag", "<"),
    ("OptVol", "OptVol", ">"),
    ("P/C", "P/C", ">"),
    ("Vol/OI", "Vol/OI", ">"),
]

DAILY_COLUMNS = BASE_COLUMNS
WEEKLY_COLUMNS = (
    [("#", "#", ">")]
    + BASE_COLUMNS[:2]
    + [("Date", "Date", "<")]
    + BASE_COLUMNS[2:]
)


def next_weekday(d):
    """Roll a date forward to the next Mon-Fri if it lands on a weekend."""
    while d.weekday() >= 5:
        d = d + timedelta(days=1)
    return d


class ScreenResult:
    """Everything a report needs to know about one screening run."""

    def __init__(self):
        self.buy = []
        self.short = []
        self.moves_detail = {}
        self.skipped = {
            "failed filters": 0,
            "under 8 quarters": 0,
            "eps fetch failed": 0,
            "price fetch failed": 0,
        }
        self.breaker = CircuitBreaker(check_after=config.CIRCUIT_BREAKER_CHECK_AFTER)
        self.aborted = False
        self.abort_reason = None
        self.attempted_tickers = 0

    @property
    def qualified(self):
        return self.buy + self.short

    def sort(self):
        self.buy.sort(key=lambda r: r["_sort"], reverse=True)
        self.short.sort(key=lambda r: r["_sort"])


def post_earnings_moves(prices, earnings_dates):
    """
    Next-day close-to-close move for each earnings date, in percent.
    Returns a list the same length as earnings_dates, with None where the
    price series doesn't straddle the date.
    """
    moves = []
    for edate in earnings_dates:
        naive = (
            edate.tz_localize(None)
            if hasattr(edate, "tz_localize") and edate.tzinfo
            else edate
        )
        future = prices.index[prices.index > naive]
        current = prices.index[prices.index <= naive]
        if len(future) == 0 or len(current) == 0:
            moves.append(None)
            continue
        before = prices.loc[current[-1], "Close"]
        after = prices.loc[future[0], "Close"]
        moves.append(round((after - before) / before * 100, 2))
    return moves


def screen_one(ticker, company_name, timing, report_date, result):
    """
    Run one ticker through every filter.

    Returns (row, moves_string) when the name qualifies, or (None, None) when
    it doesn't. Increments the appropriate counter in result.skipped either
    way, and records the fetch outcome against the circuit breaker.
    """
    ed, reason, stock = fetch_eps_history(ticker, limit=24)
    result.breaker.record(reason)

    if result.breaker.tripped:
        return None, None

    if reason is not None:
        result.skipped["eps fetch failed"] += 1
        return None, None

    # Sort explicitly. yfinance returns newest-first today, but nothing
    # documents that, and a silent flip would screen the OLDEST quarters while
    # still looking completely normal.
    reported = ed[ed["Reported EPS"].notna()].sort_index(ascending=False).copy()
    if len(reported) < config.MIN_QUARTERS:
        result.skipped["under 8 quarters"] += 1
        return None, None

    recent = reported.head(config.MIN_QUARTERS)
    n = config.MIN_QUARTERS
    eps_beats = int((recent["Reported EPS"] > recent["EPS Estimate"]).sum())
    eps_misses = int((recent["Reported EPS"] < recent["EPS Estimate"]).sum())

    # Cheap filter before spending calls on price history.
    if eps_beats < config.MIN_BEATS and eps_misses < config.MIN_BEATS:
        result.skipped["failed filters"] += 1
        return None, None

    earnings_dates = recent.index.tolist()

    # Single fetch covering the earnings window AND the momentum window. Two
    # separate calls per ticker was roughly 40 extra requests per run.
    start = min(earnings_dates) - timedelta(days=5)
    prices, reason = fetch_history(stock, start=start, end=datetime.now())
    if reason is not None:
        result.skipped["price fetch failed"] += 1
        return None, None

    prices.index = prices.index.tz_localize(None)

    moves = post_earnings_moves(prices, earnings_dates)
    valid = [m for m in moves if m is not None]
    if len(valid) < config.MIN_QUARTERS:
        result.skipped["price fetch failed"] += 1
        return None, None

    positive = sum(1 for m in valid if m > 0)
    negative = sum(1 for m in valid if m < 0)
    avg_move = round(sum(valid) / len(valid), 2)
    avg_abs_move = round(sum(abs(m) for m in valid) / len(valid), 2)

    if len(prices) < config.MOMENTUM_LOOKBACK_DAYS + 1:
        result.skipped["price fetch failed"] += 1
        return None, None

    then = prices["Close"].iloc[-(config.MOMENTUM_LOOKBACK_DAYS + 1)]
    now = prices["Close"].iloc[-1]
    momentum = round((now - then) / then * 100, 2)

    is_buy = (
        eps_beats >= config.MIN_BEATS
        and positive >= config.MIN_CONSISTENT_REACTIONS
        and avg_move > 0
        and momentum > 0
    )
    is_short = (
        eps_misses >= config.MIN_BEATS
        and negative >= config.MIN_CONSISTENT_REACTIONS
        and avg_move < 0
        and momentum < 0
    )
    if not (is_buy or is_short):
        result.skipped["failed filters"] += 1
        return None, None

    dvol = realized_daily_vol(prices, earnings_dates)
    im = get_implied_move(
        ticker, report_date, avg_abs_move, daily_vol_pct=dvol, timing=timing
    )
    oa = get_options_activity(ticker, report_date, timing=timing)

    # Optional fourth filter: refuse names where the straddle is already
    # pricing a bigger move than history. Off by default -- see config.
    if config.REQUIRE_CHEAP_OR_FAIR and im.get("flag") == "RICH":
        result.skipped["failed filters"] += 1
        return None, None

    row = {
        "Ticker": ticker,
        "Company": truncate(company_name, 18),
        "Time": timing,
        "EPS": f"{eps_beats}/{n}" if is_buy else f"{eps_misses}/{n}",
        "React": f"{positive}/{n}" if is_buy else f"{negative}/{n}",
        "AvgMove": f"{avg_move:+.2f}%",
        "AbsMove": f"{avg_abs_move:.2f}%",
        "Mom30d": f"{momentum:+.2f}%",
        "IM": f"{im['implied_move_pct']:.2f}%" if im["implied_move_pct"] is not None else "N/A",
        "DTE": im["dte"] if im.get("dte") is not None else "-",
        "IM/H": f"{im['ratio']}" if im["ratio"] is not None else "N/A",
        "Flag": im["flag"] or "-",
        "OptVol": f"{oa['total_volume']:,}" if oa.get("total_volume") is not None else "N/A",
        "P/C": (
            "thin" if oa.get("thin")
            else f"{oa['put_call_ratio']}" if oa["put_call_ratio"] is not None
            else "N/A"
        ),
        "Vol/OI": (
            "thin" if oa.get("thin")
            else f"{oa['vol_oi_ratio']}" if oa["vol_oi_ratio"] is not None
            else "N/A"
        ),
        "_buy": is_buy,
        "_sort": avg_move,
        "_abs": avg_abs_move,
    }
    moves_str = ", ".join(f"{m:+.2f}%" if m is not None else "N/A" for m in moves)
    return row, moves_str


def screen_universe(entries, calendar_errors=None, incomplete_message=""):
    """
    Run every entry through screen_one.

    entries: list of (ticker, company_name, timing, report_date) tuples, where
             timing is "AMC", "BMO" or "TBD" and report_date is what the
             options helpers should treat as the announcement date.

    A failed calendar call is fatal before screening even starts. Without that,
    an empty universe and a quiet day produce identical reports -- the same
    silent-failure trap that hid the missing lxml for three months.
    """
    result = ScreenResult()

    if calendar_errors:
        result.aborted = True
        result.abort_reason = (
            incomplete_message + " " + "; ".join(calendar_errors)
        ).strip()

    total = len(entries)
    if total:
        print(f"Screening {total} stocks...\n")

    for count, (ticker, company_name, timing, report_date) in enumerate(entries, start=1):
        progress(f"  Analyzing {count}/{total}: {ticker}...", count, total)
        result.attempted_tickers = count

        row, moves_str = screen_one(ticker, company_name, timing, report_date, result)

        if result.breaker.tripped:
            progress_done()
            print(
                f"\n!! ABORTING after {result.breaker.attempted} tickers: every "
                f"request returned no data."
            )
            print(f"   {result.breaker.diagnosis()}")
            result.aborted = True
            break

        if row is None:
            continue

        result.moves_detail[row["Ticker"]] = moves_str
        (result.buy if row["_buy"] else result.short).append(row)

    progress_done("Screening complete.")

    # Catches the case the early-abort threshold misses: a universe smaller
    # than check_after where every fetch still failed.
    if not result.aborted and result.breaker.total_failure:
        result.aborted = True

    result.sort()
    return result


def write_report(report, result, title, subtitles, universe_lines, columns,
                 list_suffix="", extra_notes=()):
    """
    Assemble the shared report body. The caller supplies the banner text and
    the universe block; everything from the BUY list down is identical between
    the two screeners.
    """
    report.banner(title, subtitles)
    report.blank()

    if result.aborted:
        report.add("  " + "!" * 74)
        report.add("  !!  DATA FAILURE \u2014 THIS IS NOT A SCREENING RESULT")
        if result.abort_reason:
            reason = result.abort_reason
            for chunk in [reason[i:i + 68] for i in range(0, len(reason), 68)]:
                report.add(f"  !!  {chunk}")
        else:
            report.add(
                f"  !!  Aborted after {result.breaker.attempted} tickers, all "
                f"returning no data."
            )
            report.add("  !!  The lists below are empty because the data source failed,")
            report.add("  !!  not because nothing qualified. See SCREENING NOTES.")
        report.add("  " + "!" * 74)
        report.blank()

    for line in universe_lines:
        report.add(line)

    report.add(f"  QUALIFIED    {len(result.buy):>4} BUY   \u00b7   {len(result.short)} SHORT")
    if result.buy:
        report.add(f"  BUY          {', '.join(r['Ticker'] for r in result.buy)}")
    if result.short:
        report.add(f"  SHORT        {', '.join(r['Ticker'] for r in result.short)}")
    if not result.qualified and not result.aborted:
        report.add("  RESULT       No stocks qualified.")

    report.section(f"\u25b2  BUY LIST \u2014 {len(result.buy)} names{list_suffix}")
    report.table(result.buy, columns)

    report.section(f"\u25bc  SHORT LIST \u2014 {len(result.short)} names{list_suffix}")
    report.table(result.short, columns)

    low = [r for r in result.qualified if r.get("_abs", 99) < config.LOW_MOVER_PCT]
    if low:
        report.blank()
        report.add(f"  LOW MOVERS \u2014 average absolute move under {config.LOW_MOVER_PCT}%:")
        for r in low:
            report.add(
                f"    {r['Ticker']:<8}{r['_abs']:.2f}%   little room for an "
                f"earnings move to pay for the risk"
            )
        report.add("  Informational only. Not filtered out.")

    if result.moves_detail:
        report.section("NEXT-DAY MOVE HISTORY \u2014 newest first")
        for r in result.qualified:
            report.add(f"  {r['Ticker']:<8}{result.moves_detail[r['Ticker']]}")

    report.section("HOW TO READ THIS")
    for line in LEGEND:
        report.add(line)

    report.section("SCREENING NOTES")
    report.add(f"  Of {result.attempted_tickers} names in the universe:")
    report.add(f"    {result.skipped['failed filters']:>5}  did not meet the filters")
    report.add(f"    {result.skipped['under 8 quarters']:>5}  had fewer than "
               f"{config.MIN_QUARTERS} reported quarters available")
    report.add(f"    {result.skipped['eps fetch failed']:>5}  EPS fetch failed")
    report.add(f"    {result.skipped['price fetch failed']:>5}  price fetch failed")
    report.blank()

    breaker = result.breaker
    if breaker.attempted:
        report.add(
            f"  Data fetch failure rate: {breaker.failure_rate * 100:.0f}%"
            f"  ({breaker.fetch_failures} of {breaker.attempted} attempted)"
        )
        if breaker.rate_limited:
            report.add(f"  Rate-limited responses: {breaker.rate_limited}")

    if result.aborted:
        report.blank()
        report.add("  *** RUN ABORTED \u2014 THIS IS NOT A SCREENING RESULT ***")
        report.add(f"  {result.abort_reason or breaker.diagnosis()}")
    elif breaker.failure_rate > config.INCOMPLETE_RUN_FAILURE_RATE:
        report.blank()
        report.add(
            f"  Fetch failures above "
            f"{config.INCOMPLETE_RUN_FAILURE_RATE * 100:.0f}% mean this list is "
            f"incomplete."
        )
        report.add("  Names may have been dropped for lack of data, not merit.")

    for note in extra_notes:
        report.add(note)
    if extra_notes:
        report.blank()

    report.add("  ENVIRONMENT")
    for line in environment_report():
        report.add(f"    {line}")


def headline_for(result, kind, calendar_failed_message):
    """The one-line summary the workflow puts in the email subject and body."""
    if result.aborted:
        if result.abort_reason:
            headline = calendar_failed_message
        else:
            headline = (
                f"DATA FAILURE \u2014 aborted after {result.breaker.attempted} "
                f"tickers, no usable EPS data. Not a screening result."
            )
    else:
        headline = (
            f"{result.attempted_tickers} scanned \u00b7 {len(result.buy)} BUY "
            f"\u00b7 {len(result.short)} SHORT"
        )
    if result.buy:
        headline += " \u00b7 BUY: " + ", ".join(r["Ticker"] for r in result.buy)
    if result.short:
        headline += " \u00b7 SHORT: " + ", ".join(r["Ticker"] for r in result.short)
    return headline
