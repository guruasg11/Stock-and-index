"""NSE Index Returns Dashboard — v2, NSE EOD snapshot edition.

Reads flat snapshot files produced by fetch_nse_eod.py. Makes ZERO network
calls, so it cannot be rate-limited, blocked, or served a stale series by a
third-party vendor. What you see is what NSE published, as of the build
timestamp shown at the bottom of every page.

Required files in the repo root (all produced by fetch_nse_eod.py):
    nse_index_eod.csv       Date, Index, Close
    nse_constituents.csv    Index, Symbol, CompanyName, Industry, ISIN
    nse_stock_eod.csv.gz    Date, Symbol, Close                        (optional)
    nse_marketcap.csv       Symbol, TotalMcapCr, FreeFloatMcapCr, AsOf (optional)
    nse_meta.json           build metadata + warnings                  (optional)

Method notes that matter for interpretation:
  - Index closes are NSE's published index values, not a reconstruction.
  - Stock closes come from the UDiFF bhavcopy, adjusted for splits and
    bonuses but NOT dividends. NSE headline indices are price indices, so
    the stock column and the index column sit on the same basis.
  - 1D/3D are trading-day offsets; 1W and longer are calendar lookbacks
    resolved to the last close on or before the target date.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st
from pandas.tseries.offsets import DateOffset

st.set_page_config(page_title="NSE Index Returns Dashboard", page_icon="📈", layout="wide")

ROOT = Path(__file__).resolve().parent
IST = ZoneInfo("Asia/Kolkata")
POSITIVE, NEGATIVE, NEUTRAL = "#0f9d58", "#d93025", "#8a8a8a"

PERIODS: dict[str, dict] = {
    "1D": {"kind": "trading", "n": 1},
    "3D": {"kind": "trading", "n": 3},
    "1W": {"kind": "calendar", "offset": DateOffset(days=7)},
    "2W": {"kind": "calendar", "offset": DateOffset(days=14)},
    "1M": {"kind": "calendar", "offset": DateOffset(months=1)},
    "2M": {"kind": "calendar", "offset": DateOffset(months=2)},
    "3M": {"kind": "calendar", "offset": DateOffset(months=3)},
    "6M": {"kind": "calendar", "offset": DateOffset(months=6)},
    "1Y": {"kind": "calendar", "offset": DateOffset(years=1)},
}
PERIOD_LABELS = list(PERIODS)

PERIOD_HELP = {
    "1D": "Previous trading day close",
    "3D": "3 trading days ago",
    "1W": "7 calendar days ago (last close on/before)",
    "2W": "14 calendar days ago",
    "1M": "1 calendar month ago",
    "2M": "2 calendar months ago",
    "3M": "3 calendar months ago",
    "6M": "6 calendar months ago",
    "1Y": "1 calendar year ago",
}


# ---------------------------------------------------------------------------
# Snapshot loading
# ---------------------------------------------------------------------------
SNAPSHOT_FILES = (
    "nse_index_eod.csv", "nse_constituents.csv",
    "nse_stock_eod.csv.gz", "nse_marketcap.csv", "nse_meta.json",
)


def snapshot_fingerprint() -> tuple:
    """Size and mtime of every snapshot file.

    This is the cache key. Without it @st.cache_data would hold the first
    snapshot it ever read for the life of the session, so a fresh commit
    would deploy but the user would still be looking at yesterday's data.
    """
    out = []
    for name in SNAPSHOT_FILES:
        path = ROOT / name
        out.append((name, path.stat().st_mtime_ns, path.stat().st_size)
                   if path.exists() else (name, 0, 0))
    return tuple(out)


@st.cache_data(ttl=900, show_spinner=False)
def load_snapshot(fingerprint: tuple) -> dict:
    def read(name: str, **kw) -> pd.DataFrame | None:
        path = ROOT / name
        if not path.exists():
            return None
        df = pd.read_csv(path, **kw)
        if "Date" in df.columns:
            df["Date"] = pd.to_datetime(df["Date"])
        return df

    meta_path = ROOT / "nse_meta.json"
    return {
        "index_eod": read("nse_index_eod.csv"),
        "constituents": read("nse_constituents.csv"),
        "stock_eod": read("nse_stock_eod.csv.gz", compression="gzip"),
        "marketcap": read("nse_marketcap.csv"),
        "meta": json.loads(meta_path.read_text()) if meta_path.exists() else {},
    }


# ---------------------------------------------------------------------------
# Return maths
# ---------------------------------------------------------------------------
def compute_returns(series: pd.Series | None) -> dict[str, float | None]:
    empty = {label: None for label in PERIOD_LABELS}
    if series is None or len(series) == 0:
        return empty
    series = pd.Series(series).dropna().sort_index()
    series = series[series > 0]
    if len(series) < 2:
        return empty

    last = float(series.iloc[-1])
    last_date = series.index[-1]
    out: dict[str, float | None] = {}
    for label, spec in PERIODS.items():
        if spec["kind"] == "trading":
            n = spec["n"]
            base = float(series.iloc[-1 - n]) if len(series) > n else None
        else:
            target = last_date - spec["offset"]
            window = series.loc[:target]
            # Require real coverage: otherwise a recent listing reports a
            # fake 1Y return measured off its first available close.
            base = (float(window.iloc[-1])
                    if not window.empty and series.index[0] <= target else None)
        out[label] = None if not base else (last - base) / base * 100.0
    return out


def series_from_long(df: pd.DataFrame | None, key_col: str, key: str) -> pd.Series | None:
    if df is None or df.empty:
        return None
    sub = df[df[key_col] == key]
    if sub.empty:
        return None
    return sub.set_index("Date")["Close"].sort_index()


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------
def _colour(value) -> str:
    if pd.isna(value):
        return f"color: {NEUTRAL}"
    if value > 0:
        return f"color: {POSITIVE}; font-weight: 600"
    if value < 0:
        return f"color: {NEGATIVE}; font-weight: 600"
    return f"color: {NEUTRAL}"


def style_table(df: pd.DataFrame):
    ret_cols = [c for c in PERIOD_LABELS if c in df.columns]
    fmt = {c: "{:+.2f}%" for c in ret_cols}
    for col, spec in (("Mcap (₹ Cr)", "{:,.0f}"), ("Free Float (₹ Cr)", "{:,.0f}"),
                      ("Close", "{:,.2f}"), ("Wt %", "{:.2f}%")):
        if col in df.columns:
            fmt[col] = spec
    return df.style.map(_colour, subset=ret_cols).format(fmt, na_rep="N/A")


def column_config(name_label: str) -> dict:
    cfg = {
        "Name": st.column_config.TextColumn(name_label, width="medium"),
        "Close": st.column_config.TextColumn("Close", help="Last NSE EOD close"),
        "Mcap (₹ Cr)": st.column_config.TextColumn(
            "Mcap (₹ Cr)", help="Total market capitalisation, NSE, ₹ crore"),
        "Free Float (₹ Cr)": st.column_config.TextColumn(
            "Free Float (₹ Cr)", help="Free-float market capitalisation, ₹ crore"),
        "Wt %": st.column_config.TextColumn(
            "Wt %", help="Share of this index's total free-float mcap. "
                         "Approximation — NSE applies capping rules this ignores."),
    }
    for label in PERIOD_LABELS:
        cfg[label] = st.column_config.TextColumn(label, help=PERIOD_HELP[label])
    return cfg


def build_footer(snap: dict) -> None:
    meta = snap.get("meta") or {}
    viewed = datetime.now(IST).strftime("%d %b %Y, %H:%M IST")
    st.caption(
        f"Snapshot built: {meta.get('built_at', 'unknown')}  ·  "
        f"Latest NSE index close: {meta.get('latest_index_date', 'unknown')}  ·  "
        f"Latest NSE stock close: {meta.get('latest_stock_date', 'unknown')}  ·  "
        f"Viewed: {viewed}  ·  Source: NSE / NSE Indices EOD"
    )
    warns = meta.get("warnings") or []
    if warns:
        with st.expander(f"⚠ {len(warns)} fetch warning(s) recorded in this snapshot"):
            for w in warns:
                st.write("-", w)


def staleness_guard(snap: dict) -> None:
    """A silently stale snapshot is exactly the failure mode that broke the
    Yahoo build. Make it loud."""
    latest = (snap.get("meta") or {}).get("latest_index_date")
    if not latest:
        return
    age = (datetime.now(IST).date() - pd.to_datetime(latest).date()).days
    if age > 5:
        st.error(
            f"Snapshot is {age} days old (latest NSE close {latest}). "
            f"Re-run `python fetch_nse_eod.py` and commit the output."
        )
    elif age > 2:
        st.warning(f"Snapshot is {age} days old (latest NSE close {latest}).")


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------
def home_page(snap: dict) -> None:
    header, reload_col = st.columns([5, 1])
    with header:
        st.title("📈 NSE Index Returns Dashboard")
        st.caption("NSE end-of-day data. No live feeds, no third-party price vendors.")
    with reload_col:
        st.write("")
        if st.button("🔄 Reload snapshot", use_container_width=True):
            load_snapshot.clear()
            st.rerun()

    idx_eod = snap["index_eod"]
    if idx_eod is None or idx_eod.empty:
        st.error(
            "nse_index_eod.csv not found or empty. Run `python fetch_nse_eod.py` "
            "locally and commit the generated nse_* files."
        )
        return

    staleness_guard(snap)

    cons = snap["constituents"]
    counts = (cons.groupby("Index")["Symbol"].nunique().to_dict()
              if cons is not None and not cons.empty else {})

    rows = []
    for index_name in sorted(idx_eod["Index"].unique()):
        series = series_from_long(idx_eod, "Index", index_name)
        row = {"Name": index_name,
               "Close": float(series.iloc[-1]) if series is not None else None}
        row.update(compute_returns(series))
        row["Stocks"] = counts.get(index_name, 0)
        rows.append(row)

    df = pd.DataFrame(rows, columns=["Name", "Close", *PERIOD_LABELS, "Stocks"])

    query = st.text_input("Search index", placeholder="e.g. AUTO, BANK, MIDCAP").strip()
    view = df[df["Name"].str.contains(query, case=False, na=False)] if query else df
    view = view.reset_index(drop=True)

    st.caption("Click a row to open the index detail page. Click a column header to sort.")

    selection = st.dataframe(
        style_table(view),
        use_container_width=True,
        hide_index=True,
        height=min(700, 40 + 36 * max(len(view), 1)),
        column_config=column_config("Index"),
        on_select="rerun",
        selection_mode="single-row",
        key="index_table",
    )

    picked = selection.get("selection", {}).get("rows", []) if selection else []
    if picked:
        st.session_state["selected_index"] = view.iloc[picked[0]]["Name"]
        st.session_state["view"] = "detail"
        st.rerun()

    st.download_button(
        "⬇ Export index table to CSV",
        data=df.to_csv(index=False).encode("utf-8"),
        file_name="nse_index_returns.csv",
        mime="text/csv",
    )
    build_footer(snap)


def detail_page(snap: dict) -> None:
    if st.button("⬅ Back to all indices"):
        st.session_state["view"] = "home"
        st.rerun()

    idx_eod, cons = snap["index_eod"], snap["constituents"]
    names = sorted(idx_eod["Index"].unique()) if idx_eod is not None else []
    name = st.session_state.get("selected_index")
    if name not in names:
        name = st.selectbox("Pick an index", names)
        st.session_state["selected_index"] = name

    index_series = series_from_long(idx_eod, "Index", name)
    index_returns = compute_returns(index_series)

    st.subheader(name)
    if index_series is not None:
        st.caption(f"NSE close {index_series.iloc[-1]:,.2f} "
                   f"on {index_series.index[-1]:%d %b %Y}")

    cols = st.columns(len(PERIOD_LABELS))
    for col, label in zip(cols, PERIOD_LABELS):
        value = index_returns.get(label)
        text = "N/A" if value is None else f"{value:+.2f}%"
        colour = (NEUTRAL if value is None
                  else POSITIVE if value > 0 else NEGATIVE if value < 0 else NEUTRAL)
        col.markdown(
            f"<div style='text-align:center'>"
            f"<div style='font-size:0.75rem;color:{NEUTRAL}'>{label}</div>"
            f"<div style='font-size:1.15rem;font-weight:700;color:{colour}'>{text}</div>"
            f"</div>",
            unsafe_allow_html=True,
        )

    st.divider()

    if cons is None or cons.empty:
        st.info("nse_constituents.csv missing. Run fetch_nse_eod.py.")
        build_footer(snap)
        return

    members = cons[cons["Index"] == name]
    if members.empty:
        st.info(f"No constituents recorded for {name} in this snapshot.")
        build_footer(snap)
        return

    stock_eod, mcap = snap["stock_eod"], snap["marketcap"]
    if stock_eod is None or stock_eod.empty:
        st.warning(
            "nse_stock_eod.csv.gz missing — constituent returns unavailable. "
            "Re-run fetch_nse_eod.py without --skip-stocks."
        )
        st.dataframe(members[["Symbol", "CompanyName", "Industry"]],
                     use_container_width=True, hide_index=True)
        build_footer(snap)
        return

    mcap_map, ff_map = {}, {}
    if mcap is not None and not mcap.empty:
        mcap_map = dict(zip(mcap["Symbol"], mcap["TotalMcapCr"]))
        ff_map = dict(zip(mcap["Symbol"], mcap["FreeFloatMcapCr"]))

    rows = []
    for _, m in members.iterrows():
        symbol = m["Symbol"]
        series = series_from_long(stock_eod, "Symbol", symbol)
        row = {"Name": symbol,
               "Company": m.get("CompanyName", ""),
               "Close": float(series.iloc[-1]) if series is not None else None}
        row.update(compute_returns(series))
        row["Mcap (₹ Cr)"] = mcap_map.get(symbol)
        row["Free Float (₹ Cr)"] = ff_map.get(symbol)
        rows.append(row)

    df = pd.DataFrame(rows, columns=[
        "Name", "Company", "Close", *PERIOD_LABELS, "Mcap (₹ Cr)", "Free Float (₹ Cr)"
    ])

    # Free-float weight approximation. NSE caps several indices, so this
    # reconciles to 100% but not to NSE's published constituent weights.
    ff_total = df["Free Float (₹ Cr)"].sum(skipna=True)
    if ff_total and ff_total > 0:
        df["Wt %"] = df["Free Float (₹ Cr)"] / ff_total * 100.0

    st.markdown(f"**Constituents — {len(df)} stocks**")
    c1, c2 = st.columns([3, 2])
    query = c1.text_input("Search stock", placeholder="e.g. MARUTI, TATA").strip()
    sort_by = c2.selectbox("Sort by", ["Mcap (₹ Cr)", *PERIOD_LABELS, "Name"], index=0)

    view = df[
        df["Name"].str.contains(query, case=False, na=False)
        | df["Company"].str.contains(query, case=False, na=False)
    ] if query else df
    view = view.sort_values(sort_by, ascending=(sort_by == "Name"),
                            na_position="last").reset_index(drop=True)

    total_mcap = df["Mcap (₹ Cr)"].sum(skipna=True)
    covered = int(df["Mcap (₹ Cr)"].notna().sum())
    if total_mcap:
        st.caption(
            f"Aggregate market cap of listed constituents: ₹{total_mcap:,.0f} Cr "
            f"({covered}/{len(df)} symbols priced). Sum of parts — not the index's "
            f"own capped free-float base."
        )

    st.dataframe(
        style_table(view),
        use_container_width=True,
        hide_index=True,
        height=min(760, 40 + 36 * max(len(view), 1)),
        column_config=column_config("Symbol"),
    )

    st.download_button(
        "⬇ Export to CSV",
        data=df.to_csv(index=False).encode("utf-8"),
        file_name=f"{name.replace(' ', '_').lower()}_returns.csv",
        mime="text/csv",
    )

    missing = int(df[PERIOD_LABELS].isna().all(axis=1).sum())
    if missing:
        st.warning(
            f"{missing} of {len(df)} constituents have no EOD price in this snapshot. "
            "Usual cause: symbol not in the EQ series of the bhavcopy, or listed "
            "after the snapshot window opened."
        )
    build_footer(snap)


def main() -> None:
    snap = load_snapshot(snapshot_fingerprint())
    if st.session_state.get("view") == "detail":
        detail_page(snap)
    else:
        home_page(snap)


if __name__ == "__main__":
    main()
