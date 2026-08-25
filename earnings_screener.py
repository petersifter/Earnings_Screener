"""
earnings_screener.py — daily run.

Pairs one after-close session with the following morning's pre-open reports,
since those two sets of names trade on the same overnight gap.

    python earnings_screener.py                  # tomorrow, interactive prompt
    python earnings_screener.py --tomorrow       # tomorrow, no prompt
    python earnings_screener.py --date 2026-08-25

All screening logic lives in screener_core. This file only decides which names
are in the universe and how the report is labelled.
"""

import argparse
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd

import screener_core as core
from report_utils import Report

ET = ZoneInfo("America/New_York")


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="Daily earnings screener.")
    p.add_argument("--date", help="AMC date to screen, YYYY-MM-DD")
    p.add_argument("--tomorrow", action="store_true",
                   help="screen the next weekday without prompting")
    return p.parse_args(argv)


def resolve_session(args):
    """Return (amc_day, bmo_day), prompting only if nothing was passed."""
    today = datetime.now(ET).replace(tzinfo=None)

    if args.date:
        amc = datetime.strptime(args.date, "%Y-%m-%d")
    elif args.tomorrow or not sys.stdin.isatty():
        amc = core.next_weekday(today + timedelta(days=1))
    else:
        print("Which day do you want to screen?")
        print("  1. Tomorrow")
        print("  2. Enter a specific date")
        choice = input("\nChoice (1 or 2): ").strip()
        if choice == "2":
            amc = datetime.strptime(input("Enter AMC date (YYYY-MM-DD): ").strip(),
                                    "%Y-%m-%d")
        else:
            amc = core.next_weekday(today + timedelta(days=1))

    return amc, core.next_weekday(amc + timedelta(days=1))


def build_universe(amc_day, bmo_day):
    """
    Confirmed after-close names on the AMC day plus confirmed pre-open names on
    the BMO day. Names with an unconfirmed reporting time are left to the
    weekly screener.

    Returns (entries, calendar_errors, n_amc, n_bmo).
    """
    from finance_calendars import finance_calendars as fc

    errors = []
    frames = {}
    for label, day, wanted in (("AMC", amc_day, "time-after-hours"),
                               ("BMO", bmo_day, "time-pre-market")):
        try:
            raw = fc.get_earnings_by_date(day)
            df = pd.DataFrame(raw) if len(raw) > 0 else pd.DataFrame()
        except Exception as e:
            errors.append(f"{label} {day.strftime('%Y-%m-%d')}: {type(e).__name__}: {e}")
            print(f"  {label} calendar fetch failed: {e}")
            df = pd.DataFrame()
        frames[label] = df[df["time"] == wanted] if len(df) > 0 else pd.DataFrame()

    entries = []
    seen = set()
    for label, day in (("AMC", amc_day), ("BMO", bmo_day)):
        df = frames[label]
        for ticker in df.index.tolist():
            if ticker in seen:
                continue
            seen.add(ticker)
            entries.append((ticker, df.loc[[ticker]].iloc[0]["name"], label, day))

    return entries, errors, len(frames["AMC"]), len(frames["BMO"])


def main(argv=None):
    args = parse_args(argv)
    amc_day, bmo_day = resolve_session(args)
    amc_str = amc_day.strftime("%Y-%m-%d")
    bmo_str = bmo_day.strftime("%Y-%m-%d")

    report = Report(f"daily_{amc_str}_AMC_{bmo_str}_BMO.txt")
    report.set_meta("SESSION", f"AMC {amc_str} / BMO {bmo_str}")

    print("\n=== Earnings Screener ===")
    print(f"AMC: {amc_str} | BMO: {bmo_str}\n")
    print("Fetching earnings calendar...")

    entries, errors, n_amc, n_bmo = build_universe(amc_day, bmo_day)
    print(f"Found {len(entries)} stocks ({n_amc} AMC, {n_bmo} BMO).")

    result = core.screen_universe(
        entries,
        calendar_errors=errors,
        incomplete_message=(
            "The earnings calendar failed:"
        ),
    )
    if errors:
        result.abort_reason += (
            ". The universe is incomplete, so the lists below mean nothing."
        )
    result.attempted_tickers = len(entries)

    today = datetime.now(ET).replace(tzinfo=None)
    core.write_report(
        report,
        result,
        "DAILY EARNINGS SCREENER",
        [f"AMC {amc_str}   \u00b7   BMO {bmo_str}",
         f"generated {today.strftime('%Y-%m-%d %H:%M')} ET"],
        [f"  UNIVERSE     {len(entries):>4} confirmed reports   "
         f"({n_amc} AMC \u00b7 {n_bmo} BMO)"],
        core.DAILY_COLUMNS,
    )

    report.set_meta("HEADLINE", core.headline_for(
        result, "daily",
        "DATA FAILURE \u2014 calendar unavailable. Not a screening result."))

    path = report.write()
    report.echo()
    print(f"\nReport saved to {path}")
    return 1 if result.aborted else 0


if __name__ == "__main__":
    sys.exit(main())
