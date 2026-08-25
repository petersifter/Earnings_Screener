"""
weekly_screener.py — Monday-to-Friday run.

Covers a whole week and, unlike the daily screener, includes names whose
reporting time is still TBD, since a week out the calendar often hasn't
confirmed them yet.

    python weekly_screener.py                    # this week, interactive
    python weekly_screener.py --next-week
    python weekly_screener.py --monday 2026-08-24

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

TIMING = {"time-after-hours": "AMC", "time-pre-market": "BMO"}


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="Weekly earnings screener.")
    p.add_argument("--monday", help="Monday of the week to screen, YYYY-MM-DD")
    p.add_argument("--next-week", action="store_true",
                   help="screen next week without prompting")
    p.add_argument("--this-week", action="store_true",
                   help="screen the current week without prompting")
    return p.parse_args(argv)


def resolve_week(args):
    """Return the Monday of the week to screen, prompting only if needed."""
    today = datetime.now(ET).replace(tzinfo=None)
    this_monday = today - timedelta(days=today.weekday())

    def next_monday():
        delta = (7 - today.weekday()) % 7 or 7
        return today + timedelta(days=delta)

    if args.monday:
        return datetime.strptime(args.monday, "%Y-%m-%d")
    if args.next_week:
        return next_monday()
    if args.this_week or not sys.stdin.isatty():
        return this_monday

    print("Which week do you want to screen?")
    print("  1. This week")
    print("  2. Next week")
    print("  3. Enter a specific Monday date")
    choice = input("\nChoice (1, 2, or 3): ").strip()
    if choice == "2":
        return next_monday()
    if choice == "3":
        return datetime.strptime(input("Enter Monday date (YYYY-MM-DD): ").strip(),
                                 "%Y-%m-%d")
    return this_monday


def build_universe(week_days):
    """
    Every name reporting Monday through Friday, deduplicated, with its
    reporting time mapped to AMC/BMO/TBD.

    Returns (entries, calendar_errors, day_counts).
    """
    from finance_calendars import finance_calendars as fc

    errors = []
    day_counts = []
    entries = []
    seen = set()

    for day in week_days:
        label = day.strftime("%A %Y-%m-%d")
        try:
            raw = fc.get_earnings_by_date(day)
            df = pd.DataFrame(raw) if len(raw) > 0 else pd.DataFrame()
        except Exception as e:
            day_counts.append((label, 0))
            errors.append(f"{day.strftime('%Y-%m-%d')}: {type(e).__name__}")
            print(f"  {label}: Error \u2014 {e}")
            continue

        day_counts.append((label, len(df)))
        print(f"  {label}: {len(df)} stocks")

        for ticker in df.index.tolist():
            if ticker in seen:
                continue
            seen.add(ticker)
            row = df.loc[[ticker]].iloc[0]
            entries.append((
                ticker,
                row["name"],
                TIMING.get(row["time"], "TBD"),
                day.strftime("%Y-%m-%d"),
            ))

    return entries, errors, day_counts


def main(argv=None):
    args = parse_args(argv)
    monday = resolve_week(args)
    week_days = [monday + timedelta(days=i) for i in range(5)]
    monday_str = monday.strftime("%Y-%m-%d")
    friday_str = week_days[-1].strftime("%Y-%m-%d")

    report = Report(f"weekly_{monday_str}_to_{friday_str}.txt")
    report.set_meta("SESSION", f"week of {monday_str} to {friday_str}")

    print("\n=== Weekly Earnings Screener ===")
    print(f"Week of {monday_str} to {friday_str}\n")
    print("Fetching earnings calendar for the week...")

    entries, errors, day_counts = build_universe(week_days)
    print(f"\nTotal unique stocks: {len(entries)}")

    result = core.screen_universe(
        entries,
        calendar_errors=errors,
        incomplete_message="Calendar failed for",
    )
    if errors:
        result.abort_reason += ". The week is incompletely covered."
    result.attempted_tickers = len(entries)

    for i, r in enumerate(result.buy, start=1):
        r["#"] = i
    for i, r in enumerate(result.short, start=1):
        r["#"] = i

    today = datetime.now(ET).replace(tzinfo=None)
    universe_lines = [f"  UNIVERSE     {len(entries):>4} unique names across the week"]
    universe_lines += [f"                 {label:<22} {n:>4}" for label, n in day_counts]
    universe_lines.append("")

    core.write_report(
        report,
        result,
        "WEEKLY EARNINGS SCREENER",
        [f"week of {monday_str}  \u2192  {friday_str}",
         f"generated {today.strftime('%Y-%m-%d %H:%M')} ET"],
        universe_lines,
        core.WEEKLY_COLUMNS,
        list_suffix=", strongest first",
        extra_notes=[
            "",
            "  Weekly runs include names whose reporting time is still TBD;",
            "  the daily screener only takes confirmed AMC and BMO.",
        ],
    )

    report.set_meta("HEADLINE", core.headline_for(
        result, "weekly",
        "DATA FAILURE \u2014 calendar incomplete. Not a screening result."))

    path = report.write()
    report.echo()
    print(f"\nReport saved to {path}")
    return 1 if result.aborted else 0


if __name__ == "__main__":
    sys.exit(main())
