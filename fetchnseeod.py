"""NSE EOD fetcher — run LOCALLY, commit the output, deploy the app.

WHY THIS IS A SEPARATE SCRIPT
-----------------------------
NSE blocks datacentre IP ranges. Streamlit Community Cloud runs on GCP.
An app that calls NSE at request time works on your laptop and 401s on
deploy. So: fetch here, write flat snapshot files, commit them, and let
streamlit_app.py read the snapshot with zero network calls.

WHAT IT WRITES (flat files, repo root — no folders)
---------------------------------------------------
  nse_index_eod.csv      Date, Index, Close          (NSE index history)
  nse_stock_eod.csv.gz   Date, Symbol, Close         (bhavcopy, CA-adjusted)
  nse_constituents.csv   Index, Symbol, CompanyName  (NSE index CSVs)
  nse_marketcap.csv      Symbol, TotalMcapCr, FreeFloatMcapCr, AsOf
  nse_meta.json          build time, row counts, source URLs, warnings

USAGE
-----
    pip install requests pandas
    python fetch_nse_eod.py                 # incremental, ~400 days on first run
    python fetch_nse_eod.py --days 400      # force a full rebuild
    python fetch_nse_eod.py --skip-mcap     # faster; keeps existing mcap file

SOURCES
-------
  Constituents  https://nsearchives.nseindia.com/content/indices/<file>.csv
  Stock EOD     https://nsearchives.nseindia.com/content/cm/
                BhavCopy_NSE_CM_0_0_0_YYYYMMDD_F_0000.csv.zip   (UDiFF)
                Old cm<DD><MMM><YYYY>bhav.csv.zip was discontinued
                08-Jul-2024 (NSE circular 62424).
  Index EOD     https://www.niftyindices.com/Backpage.aspx/
                getHistoricaldatatabletoString   (POST, cookie-primed)
  Market cap    https://www.nseindia.com/api/quote-equity?symbol=X
                &section=trade_info   -> values in Rs lakh

ADJUSTMENT POLICY
-----------------
Bhavcopy closes are UNADJUSTED. Splits and bonuses are adjusted here using
NSE's corporate actions feed. Dividends are deliberately NOT adjusted:
NSE headline indices are PRICE indices, so leaving dividends out makes the
stock column methodologically consistent with the index column. This is the
opposite of yfinance auto_adjust=True, which silently mixed the two.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import re
import sys
import time
import zipfile
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
import requests

ROOT = Path(__file__).resolve().parent

NSE_HOME = "https://www.nseindia.com/"
ARCHIVES = "https://nsearchives.nseindia.com/content/"
NIFTY_HOME = "https://www.niftyindices.com"
NIFTY_HIST = f"{NIFTY_HOME}/Backpage.aspx/getHistoricaldatatabletoString"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

# Index display name -> (NSE constituent CSV, niftyindices history name)
INDEX_SOURCES: dict[str, tuple[str, str]] = {
    "NIFTY 50":            ("ind_nifty50list.csv",            "NIFTY 50"),
    "NIFTY NEXT 50":       ("ind_niftynext50list.csv",        "NIFTY NEXT 50"),
    "NIFTY 100":           ("ind_nifty100list.csv",           "NIFTY 100"),
    "NIFTY 200":           ("ind_nifty200list.csv",           "NIFTY 200"),
    "NIFTY 500":           ("ind_nifty500list.csv",           "NIFTY 500"),
    "NIFTY MIDCAP 100":    ("ind_niftymidcap100list.csv",     "NIFTY MIDCAP 100"),
    "NIFTY SMALLCAP 100":  ("ind_niftysmallcap100list.csv",   "NIFTY SMALLCAP 100"),
    "NIFTY BANK":          ("ind_niftybanklist.csv",          "NIFTY BANK"),
    "NIFTY AUTO":          ("ind_niftyautolist.csv",          "NIFTY AUTO"),
    "NIFTY IT":            ("ind_niftyitlist.csv",            "NIFTY IT"),
    "NIFTY FMCG":          ("ind_niftyfmcglist.csv",          "NIFTY FMCG"),
    "NIFTY PHARMA":        ("ind_niftypharmalist.csv",        "NIFTY PHARMA"),
    "NIFTY METAL":         ("ind_niftymetallist.csv",         "NIFTY METAL"),
    "NIFTY ENERGY":        ("ind_niftyenergylist.csv",        "NIFTY ENERGY"),
    "NIFTY FIN SERVICE":   ("ind_niftyfinancelist.csv",       "NIFTY FINANCIAL SERVICES"),
    "NIFTY REALTY":        ("ind_niftyrealtylist.csv",        "NIFTY REALTY"),
    "NIFTY PSU BANK":      ("ind_niftypsubanklist.csv",       "NIFTY PSU BANK"),
    "NIFTY MEDIA":         ("ind_niftymedialist.csv",         "NIFTY MEDIA"),
    "NIFTY INFRA":         ("ind_niftyinfralist.csv",         "NIFTY INFRASTRUCTURE"),
    "NIFTY CONSUMER DURABLES": ("ind_niftyconsumerdurableslist.csv", "NIFTY CONSUMER DURABLES"),
    "NIFTY OIL AND GAS":   ("ind_niftyoilgaslist.csv",        "NIFTY OIL & GAS"),
    "NIFTY HEALTHCARE":    ("ind_niftyhealthcarelist.csv",    "NIFTY HEALTHCARE INDEX"),
}

WARNINGS: list[str] = []


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------
def nse_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/json,*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.nseindia.com/market-data/live-market-indices",
    })
    s.get(NSE_HOME, timeout=20)  # prime cookies
    return s


def nifty_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"})
    s.get(NIFTY_HOME, timeout=20)
    return s


# ---------------------------------------------------------------------------
# 1. Constituents
# ---------------------------------------------------------------------------
def fetch_constituents(s: requests.Session) -> pd.DataFrame:
    rows = []
    for index_name, (filename, _) in INDEX_SOURCES.items():
        try:
            r = s.get(ARCHIVES + "indices/" + filename, timeout=25)
            r.raise_for_status()
            reader = csv.DictReader(io.StringIO(r.text))
            n = 0
            for row in reader:
                symbol = (row.get("Symbol") or "").strip().upper()
                if not symbol:
                    continue
                rows.append({
                    "Index": index_name,
                    "Symbol": symbol,
                    "CompanyName": (row.get("Company Name") or "").strip(),
                    "Industry": (row.get("Industry") or "").strip(),
                    "ISIN": (row.get("ISIN Code") or "").strip(),
                })
                n += 1
            print(f"  {index_name:<26} {n:>4} constituents")
        except Exception as exc:
            WARNINGS.append(f"constituents {index_name}: {exc}")
            print(f"  {index_name:<26}  FAILED  {exc}")
        time.sleep(0.6)
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# 2. Index EOD history (niftyindices)
# ---------------------------------------------------------------------------
def fetch_index_history(s: requests.Session, nifty_name: str,
                        start: date, end: date) -> pd.DataFrame:
    payload = {
        "name": nifty_name,
        "startDate": start.strftime("%d-%b-%Y"),
        "endDate": end.strftime("%d-%b-%Y"),
    }
    r = s.post(NIFTY_HIST, json=payload, timeout=40)
    r.raise_for_status()
    inner = r.json().get("d")
    records = json.loads(inner) if isinstance(inner, str) else inner
    if not records:
        raise ValueError("empty payload")

    out = []
    for rec in records:
        raw_date = rec.get("HistoricalDate") or rec.get("Date")
        close = rec.get("CLOSE") or rec.get("Close")
        if not raw_date or close in (None, "", "-"):
            continue
        out.append({
            "Date": pd.to_datetime(raw_date, dayfirst=True, errors="coerce"),
            "Close": float(str(close).replace(",", "")),
        })
    df = pd.DataFrame(out).dropna().sort_values("Date")
    return df


def build_index_eod(start: date, end: date) -> pd.DataFrame:
    s = nifty_session()
    frames = []
    for index_name, (_, nifty_name) in INDEX_SOURCES.items():
        try:
            df = fetch_index_history(s, nifty_name, start, end)
            df["Index"] = index_name
            frames.append(df)
            print(f"  {index_name:<26} {len(df):>4} sessions  "
                  f"last {df['Date'].max():%d-%b-%Y} = {df['Close'].iloc[-1]:,.2f}")
        except Exception as exc:
            WARNINGS.append(f"index history {index_name}: {exc}")
            print(f"  {index_name:<26}  FAILED  {exc}")
        time.sleep(1.0)
    if not frames:
        return pd.DataFrame(columns=["Date", "Index", "Close"])
    return pd.concat(frames)[["Date", "Index", "Close"]].reset_index(drop=True)


# ---------------------------------------------------------------------------
# 3. Stock EOD (UDiFF bhavcopy)
# ---------------------------------------------------------------------------
def fetch_bhavcopy(s: requests.Session, d: date) -> pd.DataFrame | None:
    url = (ARCHIVES + "cm/BhavCopy_NSE_CM_0_0_0_"
           f"{d:%Y%m%d}_F_0000.csv.zip")
    r = s.get(url, timeout=40)
    if r.status_code != 200 or len(r.content) < 1000:
        return None                      # holiday / not yet published
    with zipfile.ZipFile(io.BytesIO(r.content)) as z:
        name = z.namelist()[0]
        df = pd.read_csv(z.open(name))
    df.columns = [c.strip() for c in df.columns]

    sym = "TckrSymb" if "TckrSymb" in df.columns else "SYMBOL"
    ser = "SctySrs" if "SctySrs" in df.columns else "SERIES"
    cls = "ClsPric" if "ClsPric" in df.columns else "CLOSE"
    if sym not in df.columns or cls not in df.columns:
        raise ValueError(f"unexpected bhavcopy columns: {list(df.columns)[:12]}")

    df = df[df[ser].astype(str).str.strip() == "EQ"]
    out = pd.DataFrame({
        "Date": pd.Timestamp(d),
        "Symbol": df[sym].astype(str).str.strip().str.upper(),
        "Close": pd.to_numeric(df[cls], errors="coerce"),
    }).dropna()
    return out[out["Close"] > 0]


def build_stock_eod(s: requests.Session, start: date, end: date,
                    existing: pd.DataFrame | None) -> pd.DataFrame:
    have = set()
    frames = []
    if existing is not None and not existing.empty:
        have = set(pd.to_datetime(existing["Date"]).dt.date)
        frames.append(existing)

    day, fetched, misses = start, 0, 0
    while day <= end:
        if day.weekday() < 5 and day not in have:
            try:
                df = fetch_bhavcopy(s, day)
            except Exception as exc:
                WARNINGS.append(f"bhavcopy {day}: {exc}")
                df = None
            if df is not None and not df.empty:
                frames.append(df)
                fetched += 1
                if fetched % 20 == 0:
                    print(f"  … {fetched} sessions fetched (latest {day})")
            else:
                misses += 1
            time.sleep(0.4)
        day += timedelta(days=1)

    print(f"  fetched {fetched} new sessions, {misses} non-trading/missing days")
    if not frames:
        return pd.DataFrame(columns=["Date", "Symbol", "Close"])
    out = pd.concat(frames, ignore_index=True)
    out["Date"] = pd.to_datetime(out["Date"])
    return out.drop_duplicates(["Date", "Symbol"]).sort_values(["Symbol", "Date"])


# ---------------------------------------------------------------------------
# 4. Corporate actions -> split/bonus adjustment
# ---------------------------------------------------------------------------
SPLIT_RE = re.compile(r"FROM\s*RS?\.?\s*([\d.]+)\s*(?:/-)?\s*TO\s*RS?\.?\s*([\d.]+)", re.I)
BONUS_RE = re.compile(r"BONUS\s*(\d+)\s*[:\-/]\s*(\d+)", re.I)


def parse_action(subject: str) -> float | None:
    """Return the price-multiplier applied to pre-ex-date closes, or None.

    Split 10 -> 2  : old prices multiplied by 2/10 = 0.2
    Bonus 1:1      : old prices multiplied by 1/(1+1) = 0.5
    """
    subject = (subject or "").upper()
    if "SPLIT" in subject or "SUB-DIVISION" in subject or "SUBDIVISION" in subject:
        m = SPLIT_RE.search(subject)
        if m:
            old_fv, new_fv = float(m.group(1)), float(m.group(2))
            if old_fv > 0 and new_fv > 0 and new_fv < old_fv:
                return new_fv / old_fv
    if "BONUS" in subject:
        m = BONUS_RE.search(subject)
        if m:
            new_sh, held = float(m.group(1)), float(m.group(2))
            if held > 0:
                return held / (held + new_sh)
    return None


def fetch_corporate_actions(s: requests.Session, start: date, end: date) -> pd.DataFrame:
    url = ("https://www.nseindia.com/api/corporates-corporateActions"
           f"?index=equities&from_date={start:%d-%m-%Y}&to_date={end:%d-%m-%Y}")
    try:
        r = s.get(url, timeout=40)
        r.raise_for_status()
        data = r.json()
    except Exception as exc:
        WARNINGS.append(f"corporate actions: {exc} — closes are UNADJUSTED")
        return pd.DataFrame(columns=["Symbol", "ExDate", "Factor"])

    rows = []
    for rec in data if isinstance(data, list) else []:
        factor = parse_action(rec.get("subject", ""))
        if factor is None:
            continue
        ex = pd.to_datetime(rec.get("exDate"), dayfirst=True, errors="coerce")
        if pd.isna(ex):
            continue
        rows.append({
            "Symbol": str(rec.get("symbol", "")).strip().upper(),
            "ExDate": ex,
            "Factor": factor,
            "Subject": rec.get("subject", ""),
        })
    df = pd.DataFrame(rows)
    for _, r_ in df.iterrows():
        print(f"  {r_['Symbol']:<14} ex {r_['ExDate']:%d-%b-%Y}  x{r_['Factor']:.4f}  "
              f"{r_['Subject'][:52]}")
    return df


def apply_adjustments(prices: pd.DataFrame, actions: pd.DataFrame) -> pd.DataFrame:
    """Scale every close strictly BEFORE each ex-date by that action's factor.

    Without this a 1:1 bonus shows as a -50% one-day return.
    """
    if actions.empty or prices.empty:
        return prices
    prices = prices.copy()
    for symbol, grp in actions.groupby("Symbol"):
        mask_sym = prices["Symbol"] == symbol
        if not mask_sym.any():
            continue
        for _, act in grp.iterrows():
            mask = mask_sym & (prices["Date"] < act["ExDate"])
            prices.loc[mask, "Close"] *= act["Factor"]
    return prices


# ---------------------------------------------------------------------------
# 5. Market capitalisation
# ---------------------------------------------------------------------------
def fetch_marketcap(s: requests.Session, symbols: list[str]) -> pd.DataFrame:
    """NSE trade_info gives total and free-float mcap in Rs LAKH. Converted
    to Rs CRORE here (1 crore = 100 lakh)."""
    rows, failed = [], 0
    for i, symbol in enumerate(symbols, 1):
        url = ("https://www.nseindia.com/api/quote-equity"
               f"?symbol={requests.utils.quote(symbol)}&section=trade_info")
        try:
            r = s.get(url, timeout=25)
            r.raise_for_status()
            info = (r.json().get("marketDeptOrderBook") or {}).get("tradeInfo") or {}
            total = info.get("totalMarketCap")
            ffmc = info.get("ffmc")
            if total in (None, "", "-"):
                raise ValueError("no totalMarketCap")
            rows.append({
                "Symbol": symbol,
                "TotalMcapCr": float(str(total).replace(",", "")) / 100.0,
                "FreeFloatMcapCr": (float(str(ffmc).replace(",", "")) / 100.0
                                    if ffmc not in (None, "", "-") else None),
                "AsOf": datetime.now().strftime("%Y-%m-%d"),
            })
        except Exception:
            failed += 1
        if i % 25 == 0:
            print(f"  … {i}/{len(symbols)} symbols, {failed} failed")
        time.sleep(0.5)
    if failed:
        WARNINGS.append(f"market cap: {failed}/{len(symbols)} symbols returned no data")
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=400,
                    help="calendar days of history (default 400, covers 1Y)")
    ap.add_argument("--skip-mcap", action="store_true")
    ap.add_argument("--skip-stocks", action="store_true")
    args = ap.parse_args()

    end = date.today()
    start = end - timedelta(days=args.days)
    print(f"NSE EOD fetch  {start:%d-%b-%Y} -> {end:%d-%b-%Y}\n")

    s = nse_session()

    print("[1/5] Constituents")
    cons = fetch_constituents(s)
    if not cons.empty:
        cons.to_csv(ROOT / "nse_constituents.csv", index=False)

    print("\n[2/5] Index EOD")
    idx = build_index_eod(start, end)
    if not idx.empty:
        idx.to_csv(ROOT / "nse_index_eod.csv", index=False)

    stocks = pd.DataFrame()
    if not args.skip_stocks:
        print("\n[3/5] Stock EOD (bhavcopy)")
        path = ROOT / "nse_stock_eod.csv.gz"
        prior = pd.read_csv(path) if path.exists() else None
        stocks = build_stock_eod(s, start, end, prior)

        print("\n[4/5] Corporate actions (split/bonus)")
        actions = fetch_corporate_actions(s, start, end)
        stocks = apply_adjustments(stocks, actions)
        stocks.to_csv(path, index=False, compression="gzip")
    else:
        print("\n[3/5] [4/5] skipped")

    if not args.skip_mcap and not cons.empty:
        print("\n[5/5] Market capitalisation")
        symbols = sorted(cons["Symbol"].unique())
        mcap = fetch_marketcap(s, symbols)
        if not mcap.empty:
            mcap.to_csv(ROOT / "nse_marketcap.csv", index=False)
    else:
        print("\n[5/5] skipped")

    meta = {
        "built_at": datetime.now().isoformat(timespec="seconds"),
        "window": {"from": str(start), "to": str(end)},
        "rows": {
            "constituents": int(len(cons)),
            "index_eod": int(len(idx)),
            "stock_eod": int(len(stocks)),
        },
        "latest_index_date": (str(idx["Date"].max().date()) if not idx.empty else None),
        "latest_stock_date": (str(pd.to_datetime(stocks["Date"]).max().date())
                              if not stocks.empty else None),
        "adjustment_policy": "splits and bonuses adjusted; dividends NOT adjusted "
                             "(NSE headline indices are price indices)",
        "warnings": WARNINGS,
    }
    (ROOT / "nse_meta.json").write_text(json.dumps(meta, indent=2))

    print("\n" + "=" * 60)
    print(json.dumps(meta["rows"], indent=2))
    print(f"latest index date: {meta['latest_index_date']}")
    print(f"latest stock date: {meta['latest_stock_date']}")
    if WARNINGS:
        print(f"\n{len(WARNINGS)} warning(s):")
        for w in WARNINGS[:20]:
            print("  -", w)
    print("\nCommit nse_*.csv, nse_*.csv.gz and nse_meta.json, then redeploy.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
