"""Scheduled updater — fetch NSE EOD, verify it advanced, prune, commit, push.

Runs identically on your own machine (Task Scheduler / cron) and inside
GitHub Actions. The difference that matters is the IP: NSE blocks datacentre
ranges, so a CI runner may be refused where your home broadband is not.
This script does not pretend otherwise — it exits non-zero with a clear
reason so a failed CI run is visible rather than silently committing stale
data.

USAGE
    python auto_update.py                  # fetch, prune, commit, push
    python auto_update.py --no-push        # local test, no git write
    python auto_update.py --no-git         # fetch and prune only
    python auto_update.py --window 400     # days of history to retain

EXIT CODES
    0  snapshot advanced (or already current on a non-trading day)
    1  fetch failed / NSE refused the connection
    2  fetch succeeded but the data did not advance on a trading day
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

ROOT = Path(__file__).resolve().parent
IST = ZoneInfo("Asia/Kolkata")

META = ROOT / "nse_meta.json"
STOCK_EOD = ROOT / "nse_stock_eod.csv.gz"
INDEX_EOD = ROOT / "nse_index_eod.csv"

TRACKED = [
    "nse_meta.json",
    "nse_index_eod.csv",
    "nse_constituents.csv",
    "nse_stock_eod.csv.gz",
    "nse_marketcap.csv",
]


def log(msg: str) -> None:
    print(f"[{datetime.now(IST):%d-%b-%Y %H:%M:%S IST}] {msg}", flush=True)


def read_meta() -> dict:
    if not META.exists():
        return {}
    try:
        return json.loads(META.read_text())
    except Exception:
        return {}


def run(cmd: list[str], check: bool = True) -> subprocess.CompletedProcess:
    log("$ " + " ".join(cmd))
    proc = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
    if proc.stdout.strip():
        print(proc.stdout.rstrip())
    if proc.returncode != 0:
        if proc.stderr.strip():
            print(proc.stderr.rstrip(), file=sys.stderr)
        if check:
            raise RuntimeError(f"command failed ({proc.returncode}): {' '.join(cmd)}")
    return proc


# ---------------------------------------------------------------------------
# Trading-day heuristic
# ---------------------------------------------------------------------------
def last_expected_session(now: datetime) -> date:
    """Most recent session whose bhavcopy should be published by now.

    NSE closes 15:30 IST; the UDiFF bhavcopy lands around 20:00 IST. Before
    then, today does not count. Weekends never count. Exchange holidays are
    NOT known here — they surface as 'did not advance', which the caller
    downgrades to a warning rather than a failure.
    """
    d = now.date()
    if now.hour < 20:
        d -= timedelta(days=1)
    while d.weekday() >= 5:                 # Sat/Sun
        d -= timedelta(days=1)
    return d


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------
def fetch(window: int, skip_mcap: bool) -> bool:
    cmd = [sys.executable, "fetch_nse_eod.py", "--days", str(window)]
    if skip_mcap:
        cmd.append("--skip-mcap")
    proc = run(cmd, check=False)
    return proc.returncode == 0


def prune(window: int) -> None:
    """Keep the repo from growing without bound.

    A daily commit of the full stock file would add a few MB a day forever.
    Trim to the retention window plus a 30-day margin so the 1Y lookback
    never falls off the edge.
    """
    cutoff = pd.Timestamp(date.today() - timedelta(days=window + 30))
    for path, kwargs in ((STOCK_EOD, {"compression": "gzip"}), (INDEX_EOD, {})):
        if not path.exists():
            continue
        df = pd.read_csv(path, **kwargs)
        if "Date" not in df.columns:
            continue
        df["Date"] = pd.to_datetime(df["Date"])
        before = len(df)
        df = df[df["Date"] >= cutoff]
        if len(df) < before:
            df.to_csv(path, index=False, **kwargs)
            log(f"pruned {path.name}: {before:,} -> {len(df):,} rows "
                f"(cutoff {cutoff:%d-%b-%Y})")


def verify(previous: dict, now: datetime) -> int:
    meta = read_meta()
    old_date = previous.get("latest_index_date")
    new_date = meta.get("latest_index_date")
    expected = last_expected_session(now)

    log(f"latest index close: {old_date} -> {new_date} (expected {expected})")

    warnings = meta.get("warnings") or []
    if warnings:
        log(f"{len(warnings)} fetch warning(s):")
        for w in warnings[:10]:
            log(f"  - {w}")

    if not new_date:
        log("FAIL: no latest_index_date in nse_meta.json")
        return 1

    latest = pd.to_datetime(new_date).date()
    if latest >= expected:
        log("OK: snapshot is current")
        return 0
    if new_date == old_date:
        log(f"WARN: data did not advance. Either {expected} was an exchange "
            f"holiday, or NSE refused the request. Check the warnings above.")
        return 2
    log(f"WARN: snapshot advanced to {new_date} but expected {expected}")
    return 2


def commit_and_push(push: bool) -> None:
    existing = [f for f in TRACKED if (ROOT / f).exists()]
    if not existing:
        log("nothing to commit")
        return

    run(["git", "config", "user.name", "nse-auto-update"], check=False)
    run(["git", "config", "user.email", "nse-auto-update@users.noreply.github.com"],
        check=False)
    run(["git", "add", *existing], check=False)

    status = run(["git", "status", "--porcelain", "--", *existing], check=False)
    if not status.stdout.strip():
        log("snapshot unchanged — no commit")
        return

    meta = read_meta()
    msg = (f"NSE EOD snapshot {meta.get('latest_index_date', 'unknown')} "
           f"(built {meta.get('built_at', '')})")
    run(["git", "commit", "-m", msg], check=False)
    if push:
        run(["git", "push"], check=False)
        log("pushed — Streamlit Cloud will redeploy automatically")
    else:
        log("committed locally (--no-push)")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--window", type=int, default=400,
                    help="days of history to fetch and retain (default 400)")
    ap.add_argument("--skip-mcap", action="store_true",
                    help="skip market cap refresh (much faster)")
    ap.add_argument("--no-push", action="store_true")
    ap.add_argument("--no-git", action="store_true")
    args = ap.parse_args()

    now = datetime.now(IST)
    log(f"auto_update starting in {ROOT}")

    previous = read_meta()

    if not fetch(args.window, args.skip_mcap):
        log("FAIL: fetch_nse_eod.py exited non-zero")
        return 1

    prune(args.window)
    code = verify(previous, now)

    if not args.no_git:
        commit_and_push(push=not args.no_push)

    log(f"auto_update finished with code {code}")
    return code


if __name__ == "__main__":
    sys.exit(main())
