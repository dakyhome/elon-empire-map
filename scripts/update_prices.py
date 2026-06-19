#!/usr/bin/env python3
"""
Rewrites prices.json once a day for the Elon Empire Map.

This file is OWNED by the GitHub Action. Do not edit prices.json by hand and
do not re-upload a local copy over it. The Action is the only writer.

What it does:
  1. Fetches TSLA daily close (Alpha Vantage primary, Stooq fallback).
  2. Fetches SPCX once it lists on June 12, 2026 (same two sources).
  3. Records the real trading date of that close (for the per-bubble label).
  4. Computes an approximate net worth headline from a few fixed constants.
  5. Stamps "checked" with the run date in New York time (US market clock,
     so it never drifts to the runner's UTC day or anyone's local day).
  6. Writes prices.json, keeping yesterday's values if a fetch fails.

prices.json shape consumed by index.html:
  {
    "checked":   "2026-06-09",   -> footer "Last checked on ..."
    "networth":  "$814B",        -> Elon hub headline
    "tickers":   {"TSLA": 408.95, "SPCX": null},
    "close_date":"2026-06-08"    -> per-bubble "Closing: 8 Jun 2026"
  }

Net worth is deliberately approximate. The goal is a figure that sits close to
the reliable public numbers, not an exact valuation. See the constants below.
"""

import csv
import io
import json
import os
import sys
import urllib.request
from datetime import datetime, date

try:
    from zoneinfo import ZoneInfo
    NY = ZoneInfo("America/New_York")
except Exception:
    NY = None  # falls back to UTC date if tz data is missing

# ----------------------------------------------------------------------------
# Paths
# ----------------------------------------------------------------------------
HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
PRICES_PATH = os.path.join(REPO, "prices.json")

# ----------------------------------------------------------------------------
# Net worth model (approximate, calibrated to ~$834B at TSLA $433)
#
#   networth = TSLA_close * ELON_TSLA_SH
#            + (SPCX_close * ELON_SPCX_SH   if SPCX is listed
#               else SPACEX_PREIPO_B)
#            + PRIVATE_REST_B
#
# All *_SH are billions of "share-equivalents". They bake in options and the
# economic stake, so they are not raw share counts. All *_B are billions of $.
# These constants rarely change.
# ----------------------------------------------------------------------------
# Share counts in billions. Net worth = live price * count, so the figure
# tracks both stocks daily and scales correctly at any price.
ELON_TSLA_SH = 0.70        # Tesla shares (beneficial), billions: 699,580,882 per
                           # the June 2026 Schedule 13G/A, after he exercised the
                           # 2018 option package on June 16, 2026 (now real shares).
ELON_SPCX_SH = 5.22        # SpaceX Class B shares, billions: ~91.6% of the
                           # 5,695,668,265 Class B shares per the S-1/A (primary
                           # source). ~1.3B of these are milestone-restricted.
SPACEX_PREIPO_B = 420.0    # fixed SpaceX contribution before it lists
PRIVATE_REST_B = 59.0      # Boring, Neuralink and the rest

# SPCX starts trading on this date. Before it, the ticker stays null.
SPCX_LIST_DATE = date(2026, 6, 12)

# Manual SPCX pin. SPCX listed June 12, 2026, but neither Alpha Vantage nor Stooq
# carries the new ticker yet, so the live fetch returns nothing. Until a source
# lists it, pin the latest real close by hand here. When SPCX appears in a source,
# the live fetch wins automatically; to force the live pull, set both to None.
# Update SPCX_MANUAL to the newest close and bump SPCX_MANUAL_DATE to its date.
SPCX_MANUAL = 185.00              # latest real SPCX close, set by hand
SPCX_MANUAL_DATE = "2026-06-18"   # the trading day of SPCX_MANUAL

ALPHA_KEY = os.environ.get("ALPHAVANTAGE_KEY", "").strip()
UA = {"User-Agent": "elon-empire-map/1.0 (+github action)"}
TIMEOUT = 25


def _get(url):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return r.read().decode("utf-8", "replace")


def from_alpha(symbol):
    """(close, 'YYYY-MM-DD') from Alpha Vantage's latest daily bar. None on failure."""
    if not ALPHA_KEY:
        return None
    try:
        url = (
            "https://www.alphavantage.co/query?function=TIME_SERIES_DAILY"
            f"&symbol={symbol}&outputsize=compact&apikey={ALPHA_KEY}"
        )
        data = json.loads(_get(url))
        series = data.get("Time Series (Daily)")
        if not series:
            # Covers rate-limit notes, invalid symbol, not-yet-listed, etc.
            return None
        day = sorted(series.keys())[-1]                # latest trading date
        close = float(series[day]["4. close"])
        return (close, day) if close > 0 else None
    except Exception as e:
        print(f"  alpha {symbol}: {e}", file=sys.stderr)
        return None


def from_stooq(symbol):
    """(close, 'YYYY-MM-DD') from Stooq (no key). None on failure."""
    try:
        url = f"https://stooq.com/q/l/?s={symbol.lower()}.us&f=sd2t2ohlcv&h&e=csv"
        rows = list(csv.DictReader(io.StringIO(_get(url))))
        if not rows:
            return None
        row = rows[0]
        close = row.get("Close", "")
        day = row.get("Date", "")                      # already YYYY-MM-DD
        if close in ("", "N/D") or not day:
            return None
        close = float(close)
        return (close, day) if close > 0 else None
    except Exception as e:
        print(f"  stooq {symbol}: {e}", file=sys.stderr)
        return None


def fetch_close(symbol):
    """Alpha Vantage first, then Stooq. Returns the LATEST available daily
    close as (close, 'YYYY-MM-DD'), or None if both sources fail.

    We deliberately accept whatever the most recent completed daily bar is.
    Daily endpoints only ever return finished bars, so the worst case is we
    carry yesterday's official close for a few more hours until today's posts.
    That keeps the page on the freshest real close at all times instead of
    freezing whenever a vendor is briefly slow to publish.
    """
    return from_alpha(symbol) or from_stooq(symbol)


def fmt_networth(value_b):
    """814.0 -> '$814B'; 1284.0 -> '$1.28T'."""
    if value_b >= 1000:
        return f"${value_b / 1000:.2f}T"
    return f"${round(value_b)}B"


def main():
    # Start from yesterday's file so a failed fetch keeps the last good value.
    prev = {}
    if os.path.exists(PRICES_PATH):
        try:
            with open(PRICES_PATH) as f:
                prev = json.load(f)
        except Exception:
            pass
    prev_tickers = prev.get("tickers", {}) or {}

    # "Checked" date follows the US market clock (New York), not UTC or local.
    now_ny = datetime.now(NY) if NY else datetime.utcnow()
    checked = now_ny.date()

    # No post-close guard: every run takes the latest available daily close and
    # writes it. Daily endpoints only return completed bars, so running early
    # just carries the prior official close until today's posts -- it never
    # writes a partial/intraday number. This makes the job self-healing instead
    # of freezing when a vendor is slow to publish the close.

    close_date = prev.get("close_date")  # real trading date of the shown close

    # --- TSLA --- (latest available close)
    res = fetch_close("TSLA")
    if res:
        tsla, close_date = res
        print(f"TSLA close: {tsla} ({close_date})")
    else:
        tsla = prev_tickers.get("TSLA")  # keep last good close and its date
        print("TSLA: no close from any source, kept previous", file=sys.stderr)

    # --- SPCX (null until it lists, then live close, else the manual pin) ---
    if checked < SPCX_LIST_DATE:
        spcx = None
        print(f"SPCX: not listed until {SPCX_LIST_DATE}, null")
    else:
        res = fetch_close("SPCX")
        if res:
            # A source now carries SPCX. Live data wins over the manual pin.
            spcx, close_date = res
            print(f"SPCX close (live): {spcx} ({close_date})")
        elif SPCX_MANUAL is not None:
            # No source has SPCX yet. Use the hand-set pin and its own date.
            spcx = SPCX_MANUAL
            if SPCX_MANUAL_DATE:
                close_date = SPCX_MANUAL_DATE
            print(f"SPCX: no live source, using manual pin {spcx} ({SPCX_MANUAL_DATE})")
        else:
            spcx = prev_tickers.get("SPCX")  # no pin set, keep last good
            print("SPCX: no close from any source, kept previous", file=sys.stderr)

    # --- Net worth ---
    if tsla is not None:
        total = tsla * ELON_TSLA_SH + PRIVATE_REST_B
        total += (spcx * ELON_SPCX_SH) if spcx else SPACEX_PREIPO_B
        networth = fmt_networth(total)
    else:
        networth = prev.get("networth", "")  # nothing live, keep prior
    print(f"Net worth: {networth}")

    out = {
        "checked": checked.isoformat(),
        "networth": networth,
        "tickers": {"TSLA": tsla, "SPCX": spcx},
        "close_date": close_date,
    }

    with open(PRICES_PATH, "w") as f:
        json.dump(out, f, indent=2)
        f.write("\n")
    print(f"Wrote {PRICES_PATH}")


if __name__ == "__main__":
    main()
