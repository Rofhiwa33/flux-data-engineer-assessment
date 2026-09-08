"""
Rofhiwa Flux Full Circle – Data Engineer Assessment
Part B: Python Pipeline

AI assistance: Claude (Anthropic) used to help structure and review this pipeline.

Run:
  python pipeline.py

Outputs written to data/output/ and data/quarantine/.
"""

import logging
import sys
from pathlib import Path

import pandas as pd

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).parent
RAW_DIR  = BASE_DIR / "data" / "raw"
OUT_DIR  = BASE_DIR / "data" / "output"
QUA_DIR  = BASE_DIR / "data" / "quarantine"
OUT_DIR.mkdir(parents=True, exist_ok=True)
QUA_DIR.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# Normalisation maps
# ---------------------------------------------------------------------------
CURRENCY_MAP = {
    "usd": "USD", "us$": "USD", "$": "USD",
    "eur": "EUR", "euro": "EUR", "€": "EUR",
    "gbp": "GBP", "£": "GBP",
    "zar": "ZAR", "r": "ZAR",
}

CHANNEL_MAP = {
    "direct":        "Direct",
    "website":       "Website",
    "ota":           "OTA",
    "o.t.a":         "OTA",
    "travel agent":  "Travel Agent",
    "travel  agent": "Travel Agent",
}

MAX_NIGHTS = 90
MAX_GUESTS = 30

# ===========================================================================
# Stage 1 – Ingest
# ===========================================================================

def ingest(raw_dir: Path):
    log.info("STAGE 1 – Ingest")
    bookings   = pd.read_excel(raw_dir / "bookings.xlsx",   dtype=str)
    properties = pd.read_excel(raw_dir / "properties.xlsx", dtype=str)
    fx_rates   = pd.read_excel(raw_dir / "fx_rates.xlsx",   dtype=str)
    log.info(f"  bookings  : {len(bookings):>5} rows")
    log.info(f"  properties: {len(properties):>5} rows")
    log.info(f"  fx_rates  : {len(fx_rates):>5} rows")
    return bookings, properties, fx_rates

# ===========================================================================
# Stage 2 – Clean
# ===========================================================================

def parse_date_col(series: pd.Series) -> pd.Series:
    """
    Two formats are present in the raw data:
      - "YYYY-MM-DD HH:MM:SS"   (openpyxl converts Excel serials to this)
      - "DD/MM/YYYY"             (stored as a text string in the cell)
    Returns pd.Timestamp per cell, NaT where unparseable.
    """
    def _parse(val):
        if pd.isna(val) or str(val).strip() in ("", "nan", "<NA>"):
            return pd.NaT
        s = str(val).strip()
        if "/" in s:
            try:
                return pd.to_datetime(s, dayfirst=True, format="%d/%m/%Y")
            except Exception:
                return pd.NaT
        try:
            return pd.to_datetime(s)
        except Exception:
            return pd.NaT

    return series.map(_parse)


def clean_bookings(df: pd.DataFrame) -> pd.DataFrame:
    log.info("STAGE 2 – Clean")
    df = df.copy()

    # Strip whitespace on all columns
    for col in df.columns:
        df[col] = df[col].astype(str).str.strip().replace({"nan": None, "<NA>": None})

    # Dates
    log.info("  Parsing dates …")
    for col in ("check_in", "check_out", "created_at"):
        df[col] = parse_date_col(df[col])

    # Numerics – strip stray symbols then coerce
    log.info("  Coercing numerics …")
    for col in ("num_guests", "room_rate", "revenue"):
        df[col] = (
            df[col].astype(str)
            .str.replace(r"[£€$R,\s]", "", regex=True)
            .replace({"nan": None, "<NA>": None, "None": None})
        )
        df[col] = pd.to_numeric(df[col], errors="coerce")

    # Currency
    log.info("  Normalising currencies …")
    df["currency"] = (
        df["currency"].astype(str).str.strip().str.lower().map(CURRENCY_MAP)
    )

    # Booking channel
    log.info("  Normalising channels …")
    df["booking_channel"] = (
        df["booking_channel"].astype(str).str.strip().str.lower().map(CHANNEL_MAP)
    )

    log.info("  Clean done.")
    return df


def clean_properties(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    for col in df.columns:
        df[col] = df[col].astype(str).str.strip()
    # Standardise country name inconsistency
    df["country"] = df["country"].str.replace(r"^S\.\s*Africa$", "South Africa", regex=True)
    df["room_count"] = pd.to_numeric(df["room_count"], errors="coerce")
    return df


def clean_fx(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["currency"]      = df["currency"].astype(str).str.strip().str.upper()
    df["exchange_rate"] = pd.to_numeric(df["exchange_rate"], errors="coerce")
    return df

# ===========================================================================
# Stage 3 – Validate
# ===========================================================================

def validate(df: pd.DataFrame, valid_property_ids: set):
    """
    Flag problematic records and split into clean / quarantine.

    Per-issue decision:
      duplicate booking_id      → quarantine (second occurrence)
      unparseable date          → quarantine
      check_out <= check_in     → quarantine
      stay > MAX_NIGHTS         → quarantine (implausible, e.g. 212-night stay)
      revenue missing/<=0       → quarantine
      num_guests missing        → quarantine
      num_guests > MAX_GUESTS   → quarantine (99 guests is clearly bad data)
      orphaned property_id      → quarantine (no dimension to join)
      unknown currency          → quarantine (can't convert to ZAR)
      unknown booking_channel   → correct to 'Unknown' (minor; keep in clean)
    """
    log.info("STAGE 3 – Validate")

    # We'll accumulate (index, reason) pairs; first reason wins per row.
    reason: dict[int, str] = {}

    def flag(mask: pd.Series, msg: str):
        n = 0
        for idx in df.index[mask]:
            if idx not in reason:
                reason[idx] = msg
                n += 1
        total = int(mask.sum())
        if total:
            log.info(f"  [{total:>3} rows] {msg}")

    # 1. Duplicates
    flag(df.duplicated(subset=["booking_id"], keep="first"),
         "Duplicate booking_id – second occurrence removed")

    # 2. Unparseable dates
    flag(df["check_in"].isna(),  "check_in could not be parsed")
    flag(df["check_out"].isna(), "check_out could not be parsed")

    # 3. Logically invalid date range
    dates_ok = df["check_in"].notna() & df["check_out"].notna()
    flag(dates_ok & (df["check_out"] <= df["check_in"]),
         "check_out on or before check_in")

    # 4. Implausibly long stay
    nights = (df["check_out"] - df["check_in"]).dt.days
    flag(dates_ok & (nights > MAX_NIGHTS),
         f"Stay length > {MAX_NIGHTS} nights (implausible outlier)")

    # 5. Revenue issues
    flag(df["revenue"].isna(), "Revenue is missing")
    flag(df["revenue"].notna() & (df["revenue"] <= 0), "Revenue is zero or negative")

    # 6. num_guests issues
    flag(df["num_guests"].isna(), "num_guests is missing")
    flag(df["num_guests"].notna() & (df["num_guests"] > MAX_GUESTS),
         f"num_guests > {MAX_GUESTS} (implausible)")

    # 7. Orphaned property
    flag(~df["property_id"].isin(valid_property_ids),
         "property_id not found in properties table")

    # 8. Unknown currency
    flag(df["currency"].isna(), "Currency unknown or unmappable")

    # Split
    bad_idx   = set(reason.keys())
    clean_df  = df[~df.index.isin(bad_idx)].copy()
    qua_df    = df[df.index.isin(bad_idx)].copy()
    qua_df["rejection_reason"] = qua_df.index.map(reason)

    # 9. Unrecognised channel → keep but label 'Unknown'
    unk = clean_df["booking_channel"].isna()
    if unk.sum():
        log.info(f"  [{int(unk.sum()):>3} rows] Unknown booking_channel → labelled 'Unknown' (kept)")
        clean_df.loc[unk, "booking_channel"] = "Unknown"

    log.info(f"  Result: {len(clean_df)} clean, {len(qua_df)} quarantined")
    return clean_df, qua_df

# ===========================================================================
# Stage 4 – Transform
# ===========================================================================

def transform(clean_df: pd.DataFrame, properties_df: pd.DataFrame, fx_df: pd.DataFrame) -> pd.DataFrame:
    log.info("STAGE 4 – Transform")
    df = clean_df.copy()

    # Derived field
    df["num_nights"] = (df["check_out"] - df["check_in"]).dt.days

    # FX conversion to ZAR
    fx_lookup = fx_df.set_index("currency")["exchange_rate"].to_dict()
    df["fx_rate_to_zar"] = df["currency"].map(fx_lookup)
    df["revenue_zar"]    = (df["revenue"] * df["fx_rate_to_zar"]).round(2)
    log.info(f"  FX applied. Currencies: {sorted(df['currency'].unique())}")

    # Enrich with property dimension
    prop = properties_df[["property_id", "property_name", "country", "region"]]
    df   = df.merge(prop, on="property_id", how="left")
    log.info("  Property enrichment done.")

    # Reorder
    cols = [
        "booking_id", "property_id", "property_name", "country", "region",
        "check_in", "check_out", "num_nights", "num_guests",
        "booking_channel", "currency", "room_rate", "revenue",
        "fx_rate_to_zar", "revenue_zar", "created_at",
    ]
    return df[[c for c in cols if c in df.columns]]

# ===========================================================================
# Stage 5 – Load
# ===========================================================================

def build_dq_summary(raw_df, clean_df, qua_df) -> pd.DataFrame:
    rows = [
        {"issue": "Total raw rows ingested",      "count": len(raw_df),   "resolution": "—"},
        {"issue": "Rows in clean dataset",        "count": len(clean_df), "resolution": "Kept"},
        {"issue": "Rows quarantined (total)",     "count": len(qua_df),   "resolution": "Quarantined"},
    ]
    if "rejection_reason" in qua_df.columns:
        for reason, cnt in qua_df["rejection_reason"].value_counts().items():
            rows.append({"issue": reason, "count": int(cnt), "resolution": "Quarantined"})
    return pd.DataFrame(rows)


def load(clean_df, qua_df, dq_df, out_dir, qua_dir):
    log.info("STAGE 5 – Load")
    clean_df.to_csv(out_dir / "bookings_clean.csv",          index=False)
    qua_df.to_csv(  qua_dir / "bookings_quarantine.csv",     index=False)
    dq_df.to_csv(   out_dir / "data_quality_summary.csv",    index=False)
    log.info(f"  bookings_clean.csv       → {len(clean_df)} rows")
    log.info(f"  bookings_quarantine.csv  → {len(qua_df)} rows")
    log.info(f"  data_quality_summary.csv → {len(dq_df)} rows")

# ===========================================================================
# Main
# ===========================================================================

def run_pipeline():
    log.info("=" * 62)
    log.info("  Flux Full Circle – Booking Data Pipeline")
    log.info("=" * 62)

    raw_b, raw_p, raw_fx = ingest(RAW_DIR)

    bookings   = clean_bookings(raw_b)
    properties = clean_properties(raw_p)
    fx         = clean_fx(raw_fx)

    valid_ids  = set(properties["property_id"].unique())
    clean_bk, qua_bk = validate(bookings, valid_ids)

    modelled  = transform(clean_bk, properties, fx)
    dq        = build_dq_summary(raw_b, modelled, qua_bk)

    load(modelled, qua_bk, dq, OUT_DIR, QUA_DIR)

    log.info("=" * 62)
    log.info("  Pipeline complete.")
    log.info("=" * 62)

    print("\n── Data Quality Summary ──────────────────────────────────────────")
    print(dq.to_string(index=False))
    print()


if __name__ == "__main__":
    run_pipeline()
