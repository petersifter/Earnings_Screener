"""
report_utils.py — shared formatting and file-writing helpers for the
earnings screener suite.

Used by earnings_screener.py and weekly_screener.py.

Why this exists
---------------
Previously the report filename was invented twice: once by the shell inside
the workflow (`date +%Y-%m-%d`) and once implicitly by the screener's content.
Those two disagreed whenever a run straddled UTC midnight, which is why the
weekly email arrived with no attachment.

Now Python owns the filename. It writes the report itself and drops a
`.last_run` file containing KEY=value lines that the workflow feeds straight
into $GITHUB_ENV. One source of truth, no shell date arithmetic.

Console chatter (progress counters) goes through progress() and never reaches
the report file.
"""

import os
import sys

WIDTH = 78

# Console progress ----------------------------------------------------------


IS_TTY = sys.stdout.isatty()


def progress(msg, i=None, total=None, every=25):
    """
    Console-only progress. Never enters the report file.

    Interactive terminal -> overwriting single line.
    CI (no TTY) -> a plain line every `every` tickers, so the Actions log
    stays readable but still shows the run is alive.
    """
    if IS_TTY:
        sys.stdout.write("\r" + msg.ljust(WIDTH)[:WIDTH])
        sys.stdout.flush()
    elif i is None or i == 1 or i == total or i % every == 0:
        print(msg, flush=True)


def progress_done(msg=""):
    """Clear the progress line and optionally print a final message."""
    sys.stdout.write("\r" + " " * WIDTH + "\r")
    sys.stdout.flush()
    if msg:
        print(msg)


# Formatting primitives ----------------------------------------------------


def rule(title, char="\u2501"):
    """Centred section rule, e.g. ---- BUY LIST ----"""
    label = "  " + title + "  "
    if len(label) >= WIDTH:
        return label
    total = WIDTH - len(label)
    left = total // 2
    return char * left + label + char * (total - left)


def render_table(rows, columns):
    """
    rows    : list of dicts
    columns : list of (key, header, align) where align is '<' or '>'
    Returns a list of strings with columns aligned to their widest cell.
    """
    if not rows:
        return ["  No stocks qualified."]

    widths = {}
    for key, header, _ in columns:
        w = len(header)
        for r in rows:
            w = max(w, len(str(r.get(key, ""))))
        widths[key] = w

    out = []
    out.append("  " + "  ".join(
        f"{h:<{widths[k]}}" if a == "<" else f"{h:>{widths[k]}}"
        for k, h, a in columns))
    out.append("  " + "  ".join("\u2500" * widths[k] for k, _, _ in columns))
    for r in rows:
        out.append("  " + "  ".join(
            f"{str(r.get(k, '')):<{widths[k]}}" if a == "<"
            else f"{str(r.get(k, '')):>{widths[k]}}"
            for k, _, a in columns))
    return out


def truncate(text, n):
    text = str(text)
    return text if len(text) <= n else text[:n - 1] + "\u2026"


# Report object ------------------------------------------------------------


class Report:
    """Accumulates report lines, then writes the file and the .last_run sidecar."""

    def __init__(self, filename, reports_dir="reports"):
        self.filename = filename
        self.reports_dir = reports_dir
        self.lines = []
        self.meta = {}

    # -- building --
    def add(self, text=""):
        self.lines.append(text)

    def blank(self):
        self.lines.append("")

    def banner(self, title, subtitles=()):
        inner = WIDTH - 2
        self.add("\u2554" + "\u2550" * inner + "\u2557")
        self.add("\u2551" + title.center(inner) + "\u2551")
        for s in subtitles:
            self.add("\u2551" + s.center(inner) + "\u2551")
        self.add("\u255a" + "\u2550" * inner + "\u255d")

    def section(self, title):
        self.blank()
        self.add(rule(title))
        self.blank()

    def table(self, rows, columns):
        for line in render_table(rows, columns):
            self.add(line)

    def set_meta(self, key, value):
        """KEY=value pair exported to the workflow. Single line only."""
        self.meta[key] = " ".join(str(value).split())

    # -- output --
    def write(self):
        os.makedirs(self.reports_dir, exist_ok=True)
        path = os.path.join(self.reports_dir, self.filename)
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(self.lines).rstrip() + "\n")

        self.meta["REPORT_FILE"] = path.replace(os.sep, "/")
        self.meta["REPORT_NAME"] = self.filename

        with open(".last_run", "w", encoding="utf-8") as f:
            for k, v in self.meta.items():
                f.write(f"{k}={v}\n")

        return path

    def echo(self):
        """Print the report to console too, for interactive local runs."""
        print("\n".join(self.lines))


# Legend -------------------------------------------------------------------

LEGEND = [
    "  FILTERS \u2014 all three must pass for a name to appear above",
    "",
    "    1. EPS        6+/8 beats  (BUY)   \u00b7  6+/8 misses  (SHORT)",
    "    2. Reaction   6+/8 next-day closes up (BUY) or down (SHORT),",
    "                  and the average next-day move pointing the same way",
    "    3. Momentum   30-day pre-earnings price change positive (BUY)",
    "                  or negative (SHORT)",
    "",
    "  INFORMATIONAL \u2014 shown for context, does not filter anything",
    "",
    "    AvgMove   average signed next-day move over the last 8 reports",
    "    Mom30d    price change over the ~21 trading days into earnings",
    "    IM        implied move from the ATM straddle, as % of spot",
    "    IM/H      implied move \u00f7 historical average absolute move",
    "                RICH   > 1.15   options pricing a bigger move than history",
    "                FAIR   0.85\u20131.15",
    "                CHEAP  < 0.85   options pricing a smaller move than history",
    "    P/C       put volume \u00f7 call volume",
    "                < 0.70 bullish  \u00b7  0.70\u20131.00 neutral  \u00b7  > 1.00 bearish",
    "    Vol/OI    option volume \u00f7 open interest",
    "                > 1.00 fresh positioning  \u00b7  < 0.50 quiet",
    "",
    "  Straddle pricing comes from the first expiration strictly after the",
    "  report date, so an AMC name is measured against the expiry that",
    "  actually carries the event.",
]
