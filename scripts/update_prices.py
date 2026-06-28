#!/usr/bin/env python3
"""
Rewrites prices.json once a day for the Elon Empire Map.

Fetch order:
  TSLA: Alpha Vantage (keyed) → Yahoo Finance → Stooq
  SPCX: Yahoo Finance → manual pin  (Stooq does not carry SPCX)

Net worth is computed dynamically in index.html from the live tickers plus the
static fields below. The "networth" string written here is a last-resort fallback
shown only when both tickers fail to load client-side.

prices.json shape consumed by index.html:
  {
    "checked":           "2026-06-26",  -> footer "Last checked on ..."
    "private_b":         60,            -> Boring + Neuralink + other private ($B)
    "pledged_tsla_m":    236,           -> TSLA shares pledged as collateral (millions)
    "restricted_tsla_m": 286,           -> TSLA shares locked from Jun-16 exercise (millions)
    "networth":          "$950B",       -> fallback only; index.html recalculates from tickers
    "tickers":           {"TSLA": 379.71, "SPCX": 153.23},
    "close_date":        "2026-06-26"   -> per-bubble "Closing: 26 Jun 2026"
  }

Net worth formula (mirrors index.html liveUpdate):
  pubEq      = (TSLA x tsla_shares_b x 0.199) + (SPCX x spcx_shares_b x 0.42)
  tslaDeduct = TSLA x (pledged_tsla_m + restricted_tsla_m) / 1000
  networth   = pubEq + private_b - tslaDeduct
"""

import csv
import io
import json
import os
import sys
import urllib.request
from datetime import datetime, date, timezone

try:
    from zoneinfo import ZoneInfo
    NY = ZoneInfo("America/New_York")
except Exception:
    NY = None

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
PRICES_PATH = os.path.join(REPO, "prices.json")

# ---- Static fields written into prices.json on every run ---------------------
# Changing these here changes what index.html uses to compute net worth.
PRIVATE_B         = 60    # Boring ~$6B + Neuralink ~$4.5B + other private assets ($B)
PLEDGED_TSLA_M    = 236   # Shares pledged as collateral — Tesla 2025 proxy statement (millions)
RESTRICTED_TSLA_M = 286   # Net shares from Jun 16 2026 options exercise, locked to 2033 (millions)

# Shares outstanding used in the net worth fallback string (mirrors data.csv shares_b)
TSLA_SHARES_B = 3.21   # total TSLA shares outstanding, billions
SPCX_SHARES_B = 13.08  # total SPCX shares outstanding, billions

# SPCX starts trading on this date. Before it, the ticker stays null.
SPCX_LIST_DATE = date(2026, 6, 12)

# Manual SPCX pin — used only if Yahoo Finance fetch fails.
# Update to the newest real close when bumping by hand.
SPCX_MANUAL      = 153.23        # latest real SPCX close
SPCX_MANUAL_DATE = "2026-06-26"  # trading date of SPCX_MANUAL

ALPHA_KEY = os.environ.get("ALPHAVANTAGE_KEY", "").strip()
UA = {"User-Agent": "elon-empire-map/1.0 (+github action)"}
TIMEOUT = 25


def _get(url):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return r.read().decode("utf-8", "replace")


def from_alpha(symbol):
    """Alpha Vantage TIME_SERIES_DAILY — requires ALPHAVANTAGE_KEY secret."""
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
            return None
        day = sorted(series.keys())[-1]
        close = float(series[day]["4. close"])
        return (close, day) if close > 0 else None
    except Exception as e:
        print(f"  alpha {symbol}: {e}", file=sys.stderr)
        return None


def from_yahoo(symbol):
    """Yahoo Finance v8 — no key required. Uses last bar in the 5-day window."""
    try:
        url = (
            f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
            f"?interval=1d&range=5d"
        )
        data = json.loads(_get(url))
        result = data.get("chart", {}).get("result", [])
        if not result:
            return None
        r = result[0]
        timestamps = r.get("timestamp", [])
        closes = r.get("indicators", {}).get("quote", [{}])[0].get("close", [])
        # Walk back from the last entry to find the most recent non-null close
        for ts, cl in reversed(list(zip(timestamps, closes))):
            if cl is not None:
                day = datetime.utcfromtimestamp(ts).strftime("%Y-%m-%d")
                return (round(float(cl), 2), day)
        return None
    except Exception as e:
        print(f"  yahoo {symbol}: {e}", file=sys.stderr)
        return None


def from_stooq(symbol):
    """Stooq CSV feed — no key required. Does not carry SPCX."""
    try:
        url = f"https://stooq.com/q/l/?s={symbol.lower()}.us&f=sd2t2ohlcv&h&e=csv"
        rows = list(csv.DictReader(io.StringIO(_get(url))))
        if not rows:
            return None
        row = rows[0]
        close = row.get("Close", "")
        day = row.get("Date", "")
        if close in ("", "N/D") or not day:
            return None
        close = float(close)
        return (close, day) if close > 0 else None
    except Exception as e:
        print(f"  stooq {symbol}: {e}", file=sys.stderr)
        return None


def fetch_tsla():
    """Alpha Vantage → Yahoo Finance → Stooq."""
    return from_alpha("TSLA") or from_yahoo("TSLA") or from_stooq("TSLA")


def fetch_spcx():
    """Yahoo Finance → manual pin. Stooq does not carry SPCX."""
    return from_yahoo("SPCX") or from_alpha("SPCX")


def fmt_networth(value_b):
    if value_b >= 1000:
        return f"${value_b / 1000:.2f}T"
    return f"${round(value_b)}B"


def calc_networth(tsla, spcx):
    """Mirror the index.html liveUpdate formula exactly."""
    tsla_eq   = tsla * TSLA_SHARES_B * 0.199
    spcx_eq   = spcx * SPCX_SHARES_B * 0.42
    deduct    = tsla * (PLEDGED_TSLA_M + RESTRICTED_TSLA_M) / 1000
    return tsla_eq + spcx_eq + PRIVATE_B - deduct


def main():
    # Start from the previous file so failed fetches keep the last good values.
    prev = {}
    if os.path.exists(PRICES_PATH):
        try:
            with open(PRICES_PATH) as f:
                prev = json.load(f)
        except Exception:
            pass
    prev_tickers = prev.get("tickers", {}) or {}

    now_ny = datetime.now(NY) if NY else datetime.utcnow()
    checked = now_ny.date()
    close_date = prev.get("close_date")

    # --- TSLA ---
    res = fetch_tsla()
    if res:
        tsla, close_date = res
        print(f"TSLA close: {tsla} ({close_date})")
    else:
        tsla = prev_tickers.get("TSLA")
        print("TSLA: no close from any source, kept previous", file=sys.stderr)

    # --- SPCX ---
    if checked < SPCX_LIST_DATE:
        spcx = None
        print(f"SPCX: not listed until {SPCX_LIST_DATE}, null")
    else:
        res = fetch_spcx()
        if res:
            spcx, spcx_date = res
            # Only advance close_date, never backtrack it
            if not close_date or spcx_date >= close_date:
                close_date = spcx_date
            print(f"SPCX close (live): {spcx} ({spcx_date})")
        elif SPCX_MANUAL is not None:
            spcx = SPCX_MANUAL
            # Manual pin NEVER updates close_date — TSLA's fetched date is always fresher
            print(f"SPCX: Yahoo failed, using manual pin {spcx} (close_date unchanged: {close_date})")
        else:
            spcx = prev_tickers.get("SPCX")
            print("SPCX: no close from any source, kept previous", file=sys.stderr)

    # --- Hard guard: close_date must never go backwards from what was previously committed ---
    prev_close = prev.get("close_date", "")
    if prev_close and close_date and close_date < prev_close:
        print(f"  close_date would backtrack {close_date} < {prev_close}, keeping {prev_close}", file=sys.stderr)
        close_date = prev_close

    # --- Fallback networth string (index.html computes dynamically; this is backup) ---
    if tsla is not None and spcx is not None:
        networth = fmt_networth(calc_networth(tsla, spcx))
    else:
        networth = prev.get("networth", "")
    print(f"Net worth: {networth}")

    out = {
        "checked":           checked.isoformat(),
        "private_b":         PRIVATE_B,
        "pledged_tsla_m":    PLEDGED_TSLA_M,
        "restricted_tsla_m": RESTRICTED_TSLA_M,
        "networth":          networth,
        "tickers":           {"TSLA": tsla, "SPCX": spcx},
        "close_date":        close_date,
    }

    with open(PRICES_PATH, "w") as f:
        json.dump(out, f, indent=2)
        f.write("\n")
    print(f"Wrote {PRICES_PATH}")


if __name__ == "__main__":
    main()
