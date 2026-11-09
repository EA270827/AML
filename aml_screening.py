"""
AML Behavioural Watchlist Screening — daily pipeline
====================================================

Runs once per night (cron / Task Scheduler). For each account on a
watchlist of known reference accounts, it finds the accounts whose
transaction *behaviour* is closest to the reference, explains why, bands
them by priority, and writes a dated snapshot to SQL Server for Power BI.

Design decisions locked during model development:
  * RobustScaler (median/IQR) — the book's robust treatment for
    fraud-setting outliers; StandardScaler collapsed the ratios.
  * Curated 9-feature behavioural set (size/magnitude/mirror columns dropped
    for multicollinearity).
  * Interpretable winsorising caps on the two fat-tailed features.
  * Thresholds are FROZEN in the config table on first run, not recomputed
    nightly — so an account's "why flagged" text and trend stay meaningful
    over time. Bump model_version to recalibrate deliberately + auditably.

Computation functions (build_profiles, score_reference, generate_reason,
assign_band) are pure and DB-free so they can be unit-tested offline.
DB access is isolated in the *_db / write_* / main functions.

Environment variables required at runtime:
  AML_READ_URL   SQLAlchemy URL for the Finacle read replica
  AML_WRITE_URL  SQLAlchemy URL for the reporting DB (results land here)
Example URL:
  mssql+pyodbc://USER:PWD@HOST,PORT/DB?driver=ODBC+Driver+17+for+SQL+Server
"""

from __future__ import annotations

import os
import sys
import uuid
import json
import hashlib
import logging
from datetime import datetime, timezone

import numpy as np
import pandas as pd
from sklearn.preprocessing import RobustScaler

# --------------------------------------------------------------------------
# Configuration constants (structural; tunable params live in the DB config)
# --------------------------------------------------------------------------

MODEL_VERSION = "v2.13"  # v2.13: notifications = one email per RM with personnel CC'd; test-recipient override in sender

# The nine locked behavioural features used for the distance metric.
BEHAVIOUR_COLS = [
    "flow_through_ratio",
    "largest_credit_share",
    "largest_debit_share",
    "top3_credit_share",
    "top3_debit_share",
    "median_hours_to_next_debit",
    "frac_credits_followed_24h",
    "account_age_days",
    "log_turnover_per_day",
]

# Interpretable winsorising caps applied before scaling.
CLIPS = {
    "flow_through_ratio": (0.0, 2.0),          # outflows >= 2x inflows -> "equally high"
    "median_hours_to_next_debit": (0.0, 720.0),  # cap slowness at 30 days
}

# Default tunable parameters. On first run these are written to aml_config
# under MODEL_VERSION (with the two percentile values filled from the data)
# and thereafter READ from the DB so they stay frozen.
DEFAULT_PARAMS = {
    "reason_age_days_max": 365,
    "reason_flow_through_min": 0.90,
    "reason_median_hours_max": 72,
    "reason_frac24h_min": 0.30,
    "reason_largest_credit_max": None,   # p25: mules DISPERSE inflows (low concentration)
    "reason_largest_debit_max": None,    # p25: mules DISPERSE outflows (low concentration)
    "reason_turnover_day_min": None,     # filled from population p90 on bootstrap
    # Priority bands are RANK-based within each reference account, so they are
    # comparable across seeds regardless of neighbourhood density (v2.1).
    # A seed's closest matches are High, next tier Medium, rest Low.
    "band_high_max_rank": 3,        # ranks 1-3   -> High
    "band_medium_max_rank": 10,     # ranks 4-10  -> Medium; 11+ -> Low
    "max_surface_distance": 0.85,   # accounts beyond this distance are not surfaced
    "top_n": 30,                    # max candidates returned per reference account
}

# Fee / non-behavioural transaction filter (kept identical to development).
# v2.6: fees are filtered by RPT_CODE ('CHG','INT') in clean_transactions, not by
# narration substrings. The old FEE_PATTERNS word-list deleted real payments and
# has been removed.

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("aml_screening")


def _utcnow():
    """Naive UTC timestamp. SQL Server DATETIME2 has no tz; a tz-aware value
    binds as an over-long string and truncates, so strip the tzinfo here."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


# --------------------------------------------------------------------------
# Extraction query (keep this identical to the validated development query)
# --------------------------------------------------------------------------

EXTRACT_QUERY = """
SELECT
    gm.ACID, gm.FORACID, gm.ACCT_NAME, gm.CIF_ID, gm.SOL_ID,
    gm.ACCT_OPN_DATE, gm.SCHM_TYPE, gm.SCHM_CODE, gm.ACCT_CRNCY_CODE, gm.ACCT_CLS_FLG,
    hd.TRAN_DATE, hd.TRAN_ID, hd.TRAN_AMT, hd.TRAN_PARTICULAR, hd.TRAN_RMKS,
    hd.PART_TRAN_TYPE, hd.TRAN_TYPE, hd.TRAN_SUB_TYPE, hd.RCRE_TIME, hd.RPT_CODE
FROM RSVRRealtimeFinacleUGPRD.TBAADM.HIST_TRAN_DTL_TABLE hd
JOIN RSVRRealtimeFinacleUGPRD.TBAADM.GENERAL_ACCT_MAST_TABLE gm
    ON hd.ACID = gm.ACID
WHERE gm.ACCT_CRNCY_CODE = 'USD'
    AND gm.ACCT_OPN_DATE >= '2024-01-01'
    -- v2.0: closed accounts are now included (tagged via ACCT_CLS_FLG), not excluded
"""


# ==========================================================================
# PURE COMPUTATION  (no DB — unit-testable)
# ==========================================================================

def clean_transactions(df: pd.DataFrame) -> pd.DataFrame:
    """Remove bank charges, interest, and internal GL transfers, then normalise
    counterparty text.

    v2.6: fees/interest are identified by RPT_CODE ('CHG','INT'), NOT by matching
    words in TRAN_PARTICULAR. The old substring match deleted ~half a million real
    payments narrated with words like 'consultancy fee', badly understating credits
    and inflating flow_through_ratio. Internal GL legs ('Ac xfr from gl NNNN to
    NNNN') are non-economic plumbing and are also excluded.
    """
    import re

    particular = df["TRAN_PARTICULAR"].fillna("").str.upper()
    rpt = df["RPT_CODE"].fillna("").str.upper()

    is_charge = rpt.isin(["CHG", "INT"])                 # bank-levied charges / interest
    is_gl_leg = particular.str.contains(r"\bGL\s*\d", regex=True)  # 'Ac xfr from gl 0603 ...'

    txns = df.loc[~(is_charge | is_gl_leg)].copy()

    def _clean(text):
        if pd.isna(text):
            return None
        text = re.sub(r"^\d+/", "", str(text).strip().upper())
        return re.sub(r"\s+", " ", text)

    txns["COUNTERPARTY"] = txns["TRAN_PARTICULAR"].apply(_clean)
    # Ensure datetime dtypes (defensive — driver may return strings)
    for col in ("TRAN_DATE", "ACCT_OPN_DATE", "RCRE_TIME"):
        txns[col] = pd.to_datetime(txns[col], errors="coerce")
    return txns


def _velocity(sub: pd.DataFrame) -> pd.Series:
    sub = sub.sort_values("RCRE_TIME")
    credits = sub[sub["PART_TRAN_TYPE"] == "C"][["RCRE_TIME"]].copy()
    debits = sub[sub["PART_TRAN_TYPE"] == "D"][["RCRE_TIME"]].copy()
    if credits.empty or debits.empty:
        return pd.Series({"median_hours_to_next_debit": np.nan, "credits_followed_24h": 0})
    debits["DEBIT_TIME"] = debits["RCRE_TIME"]
    merged = pd.merge_asof(
        credits.sort_values("RCRE_TIME"),
        debits.sort_values("RCRE_TIME"),
        on="RCRE_TIME", direction="forward",
    )
    hours = (merged["DEBIT_TIME"] - merged["RCRE_TIME"]).dt.total_seconds() / 3600
    return pd.Series({
        "median_hours_to_next_debit": hours.median(),
        "credits_followed_24h": int((hours <= 24).sum()),
    })


def _top3(sub: pd.DataFrame) -> pd.Series:
    def share(side):
        s = sub[sub["PART_TRAN_TYPE"] == side]["TRAN_AMT"]
        tot = s.sum()
        return (s.sort_values(ascending=False).head(3).sum() / tot) if tot else np.nan
    return pd.Series({"top3_credit_share": share("C"), "top3_debit_share": share("D")})


def build_profiles(txns: pd.DataFrame) -> pd.DataFrame:
    """One row per account with the locked behavioural feature set."""
    credits = txns[txns["PART_TRAN_TYPE"] == "C"]
    debits = txns[txns["PART_TRAN_TYPE"] == "D"]

    base = pd.DataFrame(index=txns["ACID"].unique())
    base.index.name = "ACID"
    base["total_credit"] = credits.groupby("ACID")["TRAN_AMT"].sum()
    base["total_debit"] = debits.groupby("ACID")["TRAN_AMT"].sum()
    base["credit_count"] = credits.groupby("ACID")["TRAN_AMT"].size()
    base["debit_count"] = debits.groupby("ACID")["TRAN_AMT"].size()
    base["max_credit"] = credits.groupby("ACID")["TRAN_AMT"].max()
    base["max_debit"] = debits.groupby("ACID")["TRAN_AMT"].max()
    base = base.fillna({"total_credit": 0, "total_debit": 0,
                        "credit_count": 0, "debit_count": 0})

    vel = txns.groupby("ACID")[txns.columns].apply(_velocity)
    con = txns.groupby("ACID")[txns.columns].apply(_top3)

    meta = txns.groupby("ACID").agg(
        FORACID=("FORACID", "first"), ACCT_NAME=("ACCT_NAME", "first"),
        SOL_ID=("SOL_ID", "first"), SCHM_TYPE=("SCHM_TYPE", "first"),
        ACCT_CRNCY_CODE=("ACCT_CRNCY_CODE", "first"),
        ACCT_OPN_DATE=("ACCT_OPN_DATE", "first"),
        ACCT_CLS_FLG=("ACCT_CLS_FLG", "first"),          # v2.0: Open/Closed tag
        CIF_ID=("CIF_ID", "first"),                      # v2.3: customer id
        LAST_TRAN_DATE=("TRAN_DATE", "max"),             # v2.0: per-account active end
    )

    p = base.join(vel).join(con).join(meta).reset_index()

    # --- derived behavioural features ---
    safe = p["total_credit"].replace(0, np.nan)
    p["flow_through_ratio"] = (p["total_debit"] / safe).replace([np.inf, -np.inf], np.nan)
    p["largest_credit_share"] = (p["max_credit"] / safe).replace([np.inf, -np.inf], np.nan)
    p["largest_debit_share"] = (
        p["max_debit"] / p["total_debit"].replace(0, np.nan)
    ).replace([np.inf, -np.inf], np.nan)
    p["frac_credits_followed_24h"] = (
        p["credits_followed_24h"] / p["credit_count"].replace(0, np.nan)
    ).replace([np.inf, -np.inf], np.nan)

    # v2.0: age = account's active lifespan (last txn - open), not span to today.
    # For a closed account, span-to-today would wildly overstate its age.
    p["account_age_days"] = (p["LAST_TRAN_DATE"] - p["ACCT_OPN_DATE"]).dt.days
    p["account_status"] = np.where(p["ACCT_CLS_FLG"].astype(str).str.upper() == "Y",
                                   "Closed", "Open")
    # v2.3: category from CIF_ID leading digit (2=business, 1=personal, else unknown).
    # NULL/other CIFs -> "Unknown" so nothing is silently mis-bucketed or dropped.
    cif_first = p["CIF_ID"].astype(str).str.strip().str[0]
    p["account_category"] = np.select(
        [cif_first == "2", cif_first == "1"],
        ["Business", "Personal"],
        default="Unknown",
    )
    p["turnover"] = p["total_credit"] + p["total_debit"]
    p["turnover_per_day"] = (
        p["turnover"] / p["account_age_days"].replace(0, np.nan)
    ).replace([np.inf, -np.inf], np.nan)
    p["log_turnover_per_day"] = np.log1p(p["turnover_per_day"])

    return p.reset_index(drop=True)


def scale_features(profiles: pd.DataFrame):
    """Clip -> median-impute -> RobustScale. Returns the scaled matrix."""
    Xb = profiles[BEHAVIOUR_COLS].copy()
    for col, (lo, hi) in CLIPS.items():
        Xb[col] = Xb[col].clip(lo, hi)
    Xb = Xb.fillna(Xb.median())
    return RobustScaler().fit_transform(Xb)


def resolve_params(profiles: pd.DataFrame, stored: dict | None) -> dict:
    """Use stored (frozen) params if present; else fill the data-driven ones.
    Stored values win, but any newly-added default keys are backfilled so a
    config frozen under an older version can't raise KeyError."""
    if stored is not None:
        return {**DEFAULT_PARAMS, **stored}
    params = dict(DEFAULT_PARAMS)
    params["reason_largest_credit_max"] = float(profiles["largest_credit_share"].quantile(0.25))
    params["reason_largest_debit_max"] = float(profiles["largest_debit_share"].quantile(0.25))
    params["reason_turnover_day_min"] = float(profiles["turnover_per_day"].quantile(0.90))
    return params


def generate_reason(row: pd.Series, ref: pd.Series, params: dict) -> tuple[str, str]:
    """Return (technical, plain) 'why flagged' strings for the same findings.

    Both registers cover the identical set of findings in the same order — the
    plain version is a translation, not a reduction. Numbers live in the
    technical version; the plain version keeps qualitative magnitude only.
    Compliance-safe: the reference is named as a neutral comparison anchor,
    never characterised (v2.4)."""
    def pct(x):
        return "n/a" if pd.isna(x) else f"{x * 100:.0f}%"

    # each finding is a (technical, plain) pair so the two registers can't drift
    hits = []
    if pd.notna(row["account_age_days"]) and row["account_age_days"] <= params["reason_age_days_max"]:
        hits.append((
            f"recently opened ({int(row['account_age_days'])}d; ref {int(ref['account_age_days'])}d)",
            "account opened recently",
        ))
    if pd.notna(row["flow_through_ratio"]) and row["flow_through_ratio"] >= params["reason_flow_through_min"]:
        hits.append((
            f"high fund flow-through (outflows {row['flow_through_ratio']:.2f}x inflows)",
            "almost everything paid in was paid straight back out",
        ))
    if pd.notna(row["median_hours_to_next_debit"]) and row["median_hours_to_next_debit"] <= params["reason_median_hours_max"]:
        hits.append((
            f"rapid onward movement (~{row['median_hours_to_next_debit']:.0f}h median to next debit)",
            "money left within hours of arriving",
        ))
    if pd.notna(row["frac_credits_followed_24h"]) and row["frac_credits_followed_24h"] >= params["reason_frac24h_min"]:
        hits.append((
            f"inflows commonly moved within 24h ({pct(row['frac_credits_followed_24h'])} of credits)",
            "most incoming money moved on within a day",
        ))
    if pd.notna(row["largest_credit_share"]) and row["largest_credit_share"] <= params["reason_largest_credit_max"]:
        hits.append((
            f"incoming value dispersed across many small credits ({pct(row['largest_credit_share'])} in largest)",
            "money came in from many small sources rather than one",
        ))
    if pd.notna(row["largest_debit_share"]) and row["largest_debit_share"] <= params["reason_largest_debit_max"]:
        hits.append((
            f"outgoing value dispersed across many small debits ({pct(row['largest_debit_share'])} in largest)",
            "money went out to many small destinations rather than one",
        ))
    if pd.notna(row["turnover_per_day"]) and row["turnover_per_day"] >= params["reason_turnover_day_min"]:
        hits.append((
            f"high daily turnover vs peers ({row['turnover_per_day']:,.0f}/day)",
            "very high daily activity for how new the account is",
        ))

    ref_name = str(ref["ACCT_NAME"])
    if not hits:
        tech = f"Behavioural profile close to reference account {ref_name} (no single indicator dominant)"
        plain = f"Behaves broadly like reference account {ref_name}, with no single stand-out pattern"
        return tech, plain

    tech = (f"Similar transaction behaviour to reference account {ref_name}: "
            + "; ".join(h[0] for h in hits))
    plain = (f"Behaves like reference account {ref_name} in these ways: "
             + "; ".join(h[1] for h in hits))
    return tech, plain


def assign_band(rank: int, distance: float, params: dict) -> str | None:
    """Rank-based band within a reference account. Distance only gates whether
    an account is surfaced at all, so bands stay comparable across seeds of
    differing neighbourhood density."""
    if distance > params["max_surface_distance"]:
        return None  # too far to be a meaningful match — don't surface
    if rank <= params["band_high_max_rank"]:
        return "High"
    if rank <= params["band_medium_max_rank"]:
        return "Medium"
    return "Low"


def score_reference(profiles, X_scaled, ref_acid, params) -> pd.DataFrame:
    """Rank all accounts by behavioural distance to one reference account."""
    idx = profiles.index[profiles["ACID"] == ref_acid]
    if len(idx) == 0:
        log.warning("Reference account %s not found in profiles — skipped", ref_acid)
        return pd.DataFrame()
    ref_i = idx[0]
    ref_row = profiles.loc[ref_i]

    dist = np.linalg.norm(X_scaled - X_scaled[ref_i], axis=1)
    order = np.argsort(dist)
    order = order[order != ref_i][: params["top_n"]]

    rows = []
    for rank, i in enumerate(order, start=1):
        d = float(dist[i])
        band = assign_band(rank, d, params)
        if band is None:
            continue
        r = profiles.loc[i]
        reason_tech, reason_plain = generate_reason(r, ref_row, params)
        rows.append({
            "reference_acid": ref_acid,
            "reference_name": ref_row["ACCT_NAME"],
            "match_acid": r["ACID"],
            "match_foracid": r["FORACID"],
            "match_name": r["ACCT_NAME"],
            "sol_id": r["SOL_ID"],
            "cif_id": str(r["CIF_ID"]) if pd.notna(r["CIF_ID"]) else None,   # v2.3
            "account_category": r["account_category"],                       # v2.3
            "schm_type": r["SCHM_TYPE"],
            "account_status": r["account_status"],       # v2.0: Open / Closed
            "distance": round(d, 4),
            "rank_within_ref": rank,
            "priority_band": band,
            "risk_reason": reason_plain,               # plain (dashboard)
            "risk_reason_technical": reason_tech,       # technical (analyst/audit)
            "flow_through_ratio": _f(r["flow_through_ratio"]),
            "retention_pct": _f(1 - r["flow_through_ratio"]) if pd.notna(r["flow_through_ratio"]) else None,
            "largest_credit_share": _f(r["largest_credit_share"]),
            "largest_debit_share": _f(r["largest_debit_share"]),
            "top3_credit_share": _f(r["top3_credit_share"]),
            "top3_debit_share": _f(r["top3_debit_share"]),
            "median_hours_to_next_debit": _f(r["median_hours_to_next_debit"]),
            "frac_credits_followed_24h": _f(r["frac_credits_followed_24h"]),
            "account_age_days": _i(r["account_age_days"]),
            "total_credit": _f(r["total_credit"]),        # v2.5
            "total_debit": _f(r["total_debit"]),          # v2.5
            "turnover": _f(r["turnover"]),
            "turnover_per_day": _f(r["turnover_per_day"]),
        })
    return pd.DataFrame(rows)


def _f(x):
    return None if pd.isna(x) else float(x)


def _i(x):
    return None if pd.isna(x) else int(x)


def screen_watchlist(profiles, watchlist, params) -> pd.DataFrame:
    X_scaled = scale_features(profiles)
    frames = [score_reference(profiles, X_scaled, acid, params) for acid in watchlist]
    frames = [f for f in frames if not f.empty]
    if not frames:
        return pd.DataFrame()
    results = pd.concat(frames, ignore_index=True)
    # seed_overlap: how many distinct reference accounts surfaced this match.
    # An account near several confirmed-bad seeds is a stronger lead (v2.2).
    overlap = results.groupby("match_acid")["reference_acid"].nunique()
    results["seed_overlap"] = results["match_acid"].map(overlap).astype(int)
    return results


# Sub-unit rule: only ENTERPRISE is "Enterprise"; every other sector is
# "Commercial"; a CIF absent from the map is "Unclassified" (never dropped).
def _sub_unit(sector):
    if pd.isna(sector) or str(sector).strip() == "":
        return "Unclassified"
    return "Enterprise" if str(sector).strip().upper() == "ENTERPRISE" else "Commercial"


def enrich_with_rm_sector(results: pd.DataFrame, rm_map: pd.DataFrame) -> pd.DataFrame:
    """Left-join RM/sector enrichment onto results by CIF (pure; DB-free).

    rm_map is expected to have columns CIF, Sector, RM_NAME, Email,
    Segment_Manager. Left join so accounts whose CIF isn't in the map keep
    their row with 'Unclassified'/blank rather than disappearing (v2.7).
    """
    if results.empty:
        return results
    out = results.copy()
    if rm_map is None or rm_map.empty:
        # no map available — degrade gracefully, everything Unclassified
        out["sector"] = None
        out["sub_unit"] = "Unclassified"
        out["rm_name"] = None
        out["rm_email"] = None
        out["segment_manager"] = None
        return out

    m = rm_map.copy()
    m["CIF"] = m["CIF"].astype(str).str.strip()
    m = m.drop_duplicates(subset="CIF", keep="first")  # defensive: one row per CIF
    lookup = m.set_index("CIF")

    key = out["cif_id"].astype(str).str.strip()
    out["sector"] = key.map(lookup["Sector"]) if "Sector" in lookup else None
    out["sub_unit"] = out["sector"].map(_sub_unit)
    out["rm_name"] = key.map(lookup["RM_NAME"]) if "RM_NAME" in lookup else None
    out["rm_email"] = key.map(lookup["Email"]) if "Email" in lookup else None
    out["segment_manager"] = (key.map(lookup["Segment_Manager"])
                              if "Segment_Manager" in lookup else None)
    return out


def apply_exclusions(results: pd.DataFrame, exclusions: pd.DataFrame) -> pd.DataFrame:
    """Remove known-legitimate entities from surfaced results (v2.8).

    exclusions has active rows with either cif_id or acid set. A result row is
    dropped if its cif_id matches an excluded CIF OR its match_acid matches an
    excluded ACID. Excluded entities remain in the population (they still shape
    distances); they are only suppressed from the leads list. Returns the count
    removed alongside the filtered frame for logging."""
    if results.empty or exclusions is None or exclusions.empty:
        return results, 0

    ex = exclusions.copy()
    excl_cifs = set(ex.loc[ex["cif_id"].notna(), "cif_id"].astype(str).str.strip())
    excl_acids = set(ex.loc[ex["acid"].notna(), "acid"].astype(str).str.strip())

    cif_key = results["cif_id"].astype(str).str.strip()
    acid_key = results["match_acid"].astype(str).str.strip()
    drop_mask = cif_key.isin(excl_cifs) | acid_key.isin(excl_acids)

    removed = int(drop_mask.sum())
    return results.loc[~drop_mask].copy(), removed


# ==========================================================================
# DB ACCESS  (thin wrappers — isolated from computation above)
# ==========================================================================

def _engine(prefix):
    """Build a SQLAlchemy engine for the given side ('READ' or 'WRITE').

    Preferred: supply credentials as separate environment variables so no
    URL-encoding is ever needed (SQLAlchemy escapes them):
        AML_{prefix}_USER, AML_{prefix}_PWD, AML_{prefix}_HOST,
        AML_{prefix}_PORT (default 1433), AML_{prefix}_DB
        AML_ODBC_DRIVER   (default 'ODBC Driver 17 for SQL Server')

    Fallback: if AML_{prefix}_URL is set, that full URL is used as-is.
    """
    from sqlalchemy import create_engine, URL

    full_url = os.environ.get(f"AML_{prefix}_URL")
    if full_url:
        return create_engine(full_url, fast_executemany=True)

    def req(name):
        val = os.environ.get(f"AML_{prefix}_{name}")
        if not val:
            raise RuntimeError(
                f"Set AML_{prefix}_{name} (or provide a full AML_{prefix}_URL)"
            )
        return val

    url = URL.create(
        "mssql+pyodbc",
        username=req("USER"),
        password=req("PWD"),                       # escaped automatically
        host=req("HOST"),
        port=int(os.environ.get(f"AML_{prefix}_PORT", "1433")),
        database=req("DB"),
        query={"driver": os.environ.get("AML_ODBC_DRIVER",
                                        "ODBC Driver 17 for SQL Server")},
    )
    return create_engine(url, fast_executemany=True)


def load_rm_sector_map_db():
    """Load the CIF -> RM/sector enrichment map from the curated interface DB.

    Uses the AML_RM_* connection env vars (same scheme as READ/WRITE). If the
    map can't be loaded, returns None so the pipeline degrades gracefully to
    'Unclassified' rather than failing the whole run (v2.7)."""
    try:
        eng = _engine("RM")
        df = pd.read_sql(
            "SELECT CIF, Sector, RM_NAME, Email, Segment_Manager "
            "FROM dbo.bcc_rm_sector_map", eng)
        log.info("Loaded RM/sector map: %s rows", f"{len(df):,}")
        return df
    except Exception as exc:  # noqa: BLE001 — enrichment is best-effort
        log.warning("RM/sector map unavailable (%s) — results will be Unclassified", exc)
        return None


def load_credit_cifs(cif_ids):
    """Return the SET of CIFs (from the given list) that have an active credit
    relationship — at least one loan with book_bal_lcy_eq < 0 (customer owes the
    bank) on the latest loan-book snapshot (v2.10).

    Targeted: queries only the supplied CIFs against the latest date, so it never
    scans the 193M-row archive. Uses AML_LOAN_* connection env vars. Best-effort:
    on failure returns an empty set (no accounts flagged) rather than failing."""
    cifs = [str(c).strip() for c in cif_ids if pd.notna(c) and str(c).strip()]
    if not cifs:
        return set()
    try:
        eng = _engine("LOAN")
        placeholders = ",".join(f"'{c}'" for c in set(cifs))
        q = f"""
            SELECT DISTINCT cif_id
            FROM dbo.DAILY_LOAN_BOOK_ARCH
            WHERE run_for_date = (SELECT MAX(run_for_date) FROM dbo.DAILY_LOAN_BOOK_ARCH)
              AND book_bal_lcy_eq < 0
              AND cif_id IN ({placeholders})
        """
        df = pd.read_sql(q, eng)
        credit = set(df["cif_id"].astype(str).str.strip())
        log.info("Credit relationship: %s of %s screened CIFs have active credit",
                 len(credit), len(set(cifs)))
        return credit
    except Exception as exc:  # noqa: BLE001 — enrichment is best-effort
        log.warning("Loan book unavailable (%s) — no credit flags set", exc)
        return set()


def extract_transactions_db():
    eng = _engine("READ")
    log.info("Extracting transactions from Finacle replica ...")
    df = pd.read_sql(EXTRACT_QUERY, eng)
    log.info("Extracted %s transaction rows", f"{len(df):,}")
    return df


def load_watchlist_db(eng) -> list[str]:
    df = pd.read_sql("SELECT acid FROM aml_watchlist WHERE active = 1", eng)
    return df["acid"].tolist()


def load_exclusions_db(eng) -> pd.DataFrame:
    """Load active exclusion rows. Returns empty frame if the table is absent
    or empty, so the pipeline runs normally when nothing is excluded (v2.8)."""
    try:
        return pd.read_sql(
            "SELECT cif_id, acid, entity_name, reason FROM aml_exclusions WHERE active = 1",
            eng)
    except Exception as exc:  # noqa: BLE001 — exclusions are optional
        log.warning("Exclusion list unavailable (%s) — no entities suppressed", exc)
        return pd.DataFrame()


def discover_related_accounts(reference_profiles, results):
    """Find OPEN accounts held by confirmed-mule customers that are NOT already
    on the flagged list — a known mule's live footprint (v2.9).

    reference_profiles: profiles rows for the watchlist accounts (gives us the
        confirmed-mule CIFs and names).
    results: the flagged results (to anti-join, so we surface only NEW accounts).

    Queries the Finacle master broadly — all currencies and vintages, open
    accounts only — because the whole point is to catch a mule's other accounts
    that the narrow USD/2024 screening scope would miss. Returns a DataFrame.
    """
    if reference_profiles is None or reference_profiles.empty:
        return pd.DataFrame()

    ref_cifs = (reference_profiles["CIF_ID"].dropna().astype(str).str.strip().unique().tolist())
    if not ref_cifs:
        return pd.DataFrame()

    # accounts already flagged (any account of these seeds already on the list)
    flagged_acids = set(results["match_acid"].astype(str)) if not results.empty else set()
    # the seed accounts themselves are not "discoveries"
    seed_acids = set(reference_profiles["ACID"].astype(str))

    read_eng = _engine("READ")
    # broad master query: every OPEN account for these CIFs, any currency/vintage
    placeholders = ",".join(f"'{c}'" for c in ref_cifs)
    q = f"""
        SELECT gm.ACID, gm.FORACID, gm.ACCT_NAME, gm.CIF_ID,
               gm.ACCT_CRNCY_CODE, gm.ACCT_OPN_DATE
        FROM RSVRRealtimeFinacleUGPRD.TBAADM.GENERAL_ACCT_MAST_TABLE gm
        WHERE gm.CIF_ID IN ({placeholders})
          AND gm.ACCT_CLS_FLG = 'N'
    """
    master = pd.read_sql(q, read_eng)
    if master.empty:
        return pd.DataFrame()

    master["ACID"] = master["ACID"].astype(str)
    master["CIF_ID"] = master["CIF_ID"].astype(str).str.strip()

    # discovery = open, not already flagged, not a seed itself
    mask = (~master["ACID"].isin(flagged_acids)) & (~master["ACID"].isin(seed_acids))
    disc = master.loc[mask].copy()
    if disc.empty:
        return disc

    # attach which confirmed mule (name/acid) each CIF traces back to
    ref_by_cif = (reference_profiles.assign(CIF_ID=reference_profiles["CIF_ID"].astype(str).str.strip())
                  .drop_duplicates("CIF_ID").set_index("CIF_ID"))
    disc["reference_acid"] = disc["CIF_ID"].map(ref_by_cif["ACID"])
    disc["reference_name"] = disc["CIF_ID"].map(ref_by_cif["ACCT_NAME"])

    return disc.rename(columns={
        "ACID": "related_acid", "FORACID": "related_foracid",
        "ACCT_NAME": "related_name", "CIF_ID": "cif_id",
        "ACCT_CRNCY_CODE": "related_crncy", "ACCT_OPN_DATE": "related_opn_date",
    })[["reference_acid", "reference_name", "cif_id", "related_acid",
        "related_foracid", "related_name", "related_crncy", "related_opn_date"]]


# Strong relationship types to traverse (v2.12). Introducer/Contact excluded as
# too weak/noisy. One hop only: mule -> shared party -> that party's accounts.
NETWORK_RELATIONSHIPS = ["Beneficial Owner", "ShareHolder", "CorpRep", "Guarantor"]


def discover_network_accounts(reference_profiles, results):
    """Find OPEN accounts linked to a confirmed mule through a SHARED PARTY
    (beneficial owner / shareholder / corp rep / guarantor), not already flagged.

    Two hops via the CRM relationship graph, strong types only, one level deep:
        mule CIF --[relationship]--> linked party CIF --> that party's open accounts
    Catches controllers CIF-discovery misses. Returns a DataFrame; each row is
    one discovered account with the full path (mule -> relationship -> party).
    """
    if reference_profiles is None or reference_profiles.empty:
        return pd.DataFrame()
    mule_cifs = reference_profiles["CIF_ID"].dropna().astype(str).str.strip().unique().tolist()
    if not mule_cifs:
        return pd.DataFrame()

    read_eng = _engine("READ")
    cif_ph = ",".join(f"'{c}'" for c in mule_cifs)
    rel_ph = ",".join(f"'{r}'" for r in NETWORK_RELATIONSHIPS)

    # Hop 1: parties related to the mule CIFs via strong relationship types.
    # Relationship is directional (parent->child); we look both ways so a mule
    # appearing as either parent or child still yields its linked parties.
    rel_q = f"""
        SELECT PARENTCIFID, CHILDCIFID, RELATIONSHIP
        FROM [RSVRRealtimeFinacleUGPRD].[CRMUSER].[ENTITYRELATIONSHIP]
        WHERE RELATIONSHIP IN ({rel_ph})
          AND PARENTCIFID IS NOT NULL AND CHILDCIFID IS NOT NULL
          AND PARENTCIFID <> CHILDCIFID
          AND (PARENTCIFID IN ({cif_ph}) OR CHILDCIFID IN ({cif_ph}))
    """
    rels = pd.read_sql(rel_q, read_eng)
    if rels.empty:
        return pd.DataFrame()

    rels["PARENTCIFID"] = rels["PARENTCIFID"].astype(str).str.strip()
    rels["CHILDCIFID"] = rels["CHILDCIFID"].astype(str).str.strip()
    mule_set = set(mule_cifs)

    # normalise each relationship to (mule_cif, party_cif, relationship)
    links = []
    for _, r in rels.iterrows():
        if r["PARENTCIFID"] in mule_set and r["CHILDCIFID"] not in mule_set:
            links.append((r["PARENTCIFID"], r["CHILDCIFID"], r["RELATIONSHIP"]))
        elif r["CHILDCIFID"] in mule_set and r["PARENTCIFID"] not in mule_set:
            links.append((r["CHILDCIFID"], r["PARENTCIFID"], r["RELATIONSHIP"]))
    if not links:
        return pd.DataFrame()
    link_df = pd.DataFrame(links, columns=["mule_cif", "linked_party_cif", "relationship"]).drop_duplicates()

    # Hop 2: open accounts held by those linked parties (broad — any currency/vintage)
    party_cifs = link_df["linked_party_cif"].unique().tolist()
    party_ph = ",".join(f"'{c}'" for c in party_cifs)
    acct_q = f"""
        SELECT gm.ACID, gm.FORACID, gm.ACCT_NAME, gm.CIF_ID,
               gm.ACCT_CRNCY_CODE, gm.ACCT_OPN_DATE
        FROM RSVRRealtimeFinacleUGPRD.TBAADM.GENERAL_ACCT_MAST_TABLE gm
        WHERE gm.CIF_ID IN ({party_ph}) AND gm.ACCT_CLS_FLG = 'N'
    """
    master = pd.read_sql(acct_q, read_eng)
    if master.empty:
        return pd.DataFrame()
    master["ACID"] = master["ACID"].astype(str)
    master["CIF_ID"] = master["CIF_ID"].astype(str).str.strip()

    flagged_acids = set(results["match_acid"].astype(str)) if not results.empty else set()
    seed_acids = set(reference_profiles["ACID"].astype(str))
    master = master[~master["ACID"].isin(flagged_acids) & ~master["ACID"].isin(seed_acids)]
    if master.empty:
        return pd.DataFrame()

    # join party accounts back to the mule->party link, then attach mule identity
    out = master.merge(link_df, left_on="CIF_ID", right_on="linked_party_cif", how="inner")
    ref_by_cif = (reference_profiles.assign(CIF_ID=reference_profiles["CIF_ID"].astype(str).str.strip())
                  .drop_duplicates("CIF_ID").set_index("CIF_ID"))
    out["reference_acid"] = out["mule_cif"].map(ref_by_cif["ACID"])
    out["reference_name"] = out["mule_cif"].map(ref_by_cif["ACCT_NAME"])

    return out.rename(columns={
        "ACID": "linked_acid", "FORACID": "linked_foracid", "ACCT_NAME": "linked_name",
        "ACCT_CRNCY_CODE": "linked_crncy", "ACCT_OPN_DATE": "linked_opn_date",
    })[["reference_acid", "reference_name", "mule_cif", "relationship",
        "linked_party_cif", "linked_acid", "linked_foracid", "linked_name",
        "linked_crncy", "linked_opn_date"]]


def load_params_db(eng, model_version) -> dict | None:
    df = pd.read_sql(
        "SELECT param_name, param_value FROM aml_config WHERE model_version = ?",
        eng, params=(model_version,),
    )
    if df.empty:
        return None
    out = {}
    for name, val in zip(df["param_name"], df["param_value"]):
        try:
            out[name] = json.loads(val)
        except (TypeError, json.JSONDecodeError):
            out[name] = val
    return out


def persist_params_db(eng, model_version, params):
    from sqlalchemy import text
    log.info("Bootstrapping config for %s (first run) ...", model_version)
    rows = pd.DataFrame(
        [{"model_version": model_version, "param_name": k,
          "param_value": json.dumps(v), "created_at": _utcnow()}
         for k, v in params.items()]
    )
    with eng.begin() as cx:
        cx.execute(text("DELETE FROM aml_config WHERE model_version = :v"),
                   {"v": model_version})
        rows.to_sql("aml_config", cx, if_exists="append", index=False)


def write_results_db(eng, results, run_id, run_date, model_version):
    from sqlalchemy import text
    results = results.copy()
    results.insert(0, "run_id", run_id)
    results.insert(1, "run_date", run_date)
    results.insert(2, "model_version", model_version)
    results["loaded_at"] = _utcnow()
    with eng.begin() as cx:
        # idempotent: a re-run for the same date replaces that date's snapshot
        cx.execute(text("DELETE FROM aml_screening_results WHERE run_date = :d"),
                   {"d": run_date})
        results.to_sql("aml_screening_results", cx, if_exists="append", index=False)
    log.info("Wrote %s result rows for %s", len(results), run_date)


def write_discovery_db(eng, disc, run_id, run_date):
    from sqlalchemy import text
    with eng.begin() as cx:
        cx.execute(text("DELETE FROM aml_related_discovery WHERE run_date = :d"),
                   {"d": run_date})
        if disc is not None and not disc.empty:
            d = disc.copy()
            d.insert(0, "run_id", run_id)
            d.insert(1, "run_date", run_date)
            d["loaded_at"] = _utcnow()
            d.to_sql("aml_related_discovery", cx, if_exists="append", index=False)
    n = 0 if disc is None else len(disc)
    log.info("Wrote %s related-discovery rows for %s", n, run_date)


def write_network_db(eng, net, run_id, run_date):
    from sqlalchemy import text
    with eng.begin() as cx:
        cx.execute(text("DELETE FROM aml_network_discovery WHERE run_date = :d"),
                   {"d": run_date})
        if net is not None and not net.empty:
            d = net.copy()
            d.insert(0, "run_id", run_id)
            d.insert(1, "run_date", run_date)
            d["loaded_at"] = _utcnow()
            d.to_sql("aml_network_discovery", cx, if_exists="append", index=False)
    n = 0 if net is None else len(net)
    log.info("Wrote %s network-discovery rows for %s", n, run_date)


# --- Notifications (v2.11): detect newly-added accounts, stage messages ------

# Compliance framing — reviewed wording. The 'do not contact' line is the
# explicit anti-tipping-off instruction and must remain in RM messages.
_RM_PREAMBLE = (
    "As part of routine behavioural monitoring, the account(s) below in your "
    "portfolio have been flagged for review based on transaction-pattern "
    "similarity to reference accounts. This is a prompt for review through "
    "standard procedures and does NOT indicate confirmed suspicious activity. "
    "Please do not contact the customer regarding this notification."
)
_MGMT_PREAMBLE = (
    "Daily behavioural screening summary. The counts below reflect accounts "
    "newly flagged today based on transaction-pattern similarity to reference "
    "accounts — a risk-based prioritisation aid, not confirmation of suspicious "
    "activity. Detail is available in the screening dashboard."
)


def find_new_accounts(eng, results, run_date):
    """Return the subset of today's results whose match_acid was NOT on the
    most recent prior run — i.e. genuinely newly-added accounts (v2.11)."""
    if results.empty:
        return results.iloc[0:0]
    prior = pd.read_sql(
        "SELECT DISTINCT match_acid FROM aml_screening_results "
        "WHERE run_date = (SELECT MAX(run_date) FROM aml_screening_results "
        "WHERE run_date < ?)", eng, params=(run_date,))
    prior_set = set(prior["match_acid"].astype(str)) if not prior.empty else set()
    if not prior_set:
        # first-ever run (no prior): treat nothing as "new" to avoid a mass alert
        log.info("No prior run found — no accounts treated as newly-added")
        return results.iloc[0:0]
    is_new = ~results["match_acid"].astype(str).isin(prior_set)
    return results.loc[is_new].copy()


def build_notifications(new_accounts, extra_cc=None):
    """Compose staged notifications (v2.13 RM+CC model): one email per RM, with
    the relevant personnel (segment manager, plus any always-CC management from
    config) in CC — rather than a separate management summary. Pure/DB-free.

    extra_cc: optional list of addresses to CC on every message (e.g. a standing
    management distribution list). Segment-manager CC is derived per-RM from the
    data when a segment_manager email is available."""
    if new_accounts.empty:
        return []
    extra_cc = extra_cc or []

    def _acct_line(r):
        bits = [f"  - {r['match_name']} ({r.get('match_foracid','')})"]
        extra = []
        if r.get("sub_unit"):
            extra.append(str(r["sub_unit"]))
        if r.get("sector"):
            extra.append(str(r["sector"]))
        if r.get("has_credit_relationship"):
            extra.append("has active credit relationship")
        if extra:
            bits.append("    [" + "; ".join(extra) + "]")
        if r.get("risk_reason"):
            bits.append(f"    {r['risk_reason']}")
        return "\n".join(bits)

    rows = []
    have_rm = new_accounts[new_accounts["rm_email"].notna()
                           & (new_accounts["rm_email"].astype(str).str.strip() != "")]
    for (email, name), grp in have_rm.groupby(["rm_email", "rm_name"], dropna=False):
        # CC = segment manager(s) for this RM's accounts (if present) + standing extra_cc
        cc = []
        if "segment_manager" in grp:
            # segment_manager holds a name in the map; only CC if it looks like an email
            seg = [s for s in grp["segment_manager"].dropna().astype(str).unique()
                   if "@" in s]
            cc.extend(seg)
        cc.extend(extra_cc)
        cc = sorted(set(a.strip() for a in cc if a and a.strip() and a.strip() != email))

        body = (_RM_PREAMBLE + "\n\n"
                + "\n".join(_acct_line(r) for _, r in grp.iterrows())
                + "\n\n(Behavioural similarity screening — for review via standard "
                  "procedures only.)")
        rows.append({
            "recipient_type": "RM", "recipient_name": name, "recipient_email": email,
            "cc_email": ",".join(cc) if cc else None,
            "subject": f"[Review] {len(grp)} account(s) flagged in your portfolio",
            "body": body, "n_accounts": int(len(grp)), "status": "pending",
        })
    return rows


def write_notifications_db(eng, notif_rows, run_id, run_date):
    from sqlalchemy import text
    with eng.begin() as cx:
        cx.execute(text("DELETE FROM aml_notifications WHERE run_date = :d AND status='pending'"),
                   {"d": run_date})
        if notif_rows:
            df = pd.DataFrame(notif_rows)
            df.insert(0, "run_id", run_id)
            df.insert(1, "run_date", run_date)
            df["loaded_at"] = _utcnow()
            df.to_sql("aml_notifications", cx, if_exists="append", index=False)
    log.info("Staged %s notification(s) for %s (status=pending, NOT sent)",
             len(notif_rows), run_date)


def log_run_db(eng, **fields):
    from sqlalchemy import text
    cols = ", ".join(fields)
    ph = ", ".join(f":{k}" for k in fields)
    with eng.begin() as cx:
        cx.execute(text(f"INSERT INTO aml_run_log ({cols}) VALUES ({ph})"), fields)


# ==========================================================================
# ORCHESTRATION
# ==========================================================================

def main():
    run_id = str(uuid.uuid4())
    run_date = _utcnow().date()
    started = _utcnow()
    write_eng = _engine("WRITE")
    log.info("=== AML screening run %s (%s) ===", run_id, run_date)

    try:
        watchlist = load_watchlist_db(write_eng)
        if not watchlist:
            raise RuntimeError("Watchlist is empty — nothing to screen")
        log.info("Watchlist: %s reference accounts", len(watchlist))

        raw = extract_transactions_db()
        txns = clean_transactions(raw)
        profiles = build_profiles(txns)
        log.info("Profiled %s accounts", f"{len(profiles):,}")

        stored = load_params_db(write_eng, MODEL_VERSION)
        params = resolve_params(profiles, stored)
        if stored is None:
            persist_params_db(write_eng, MODEL_VERSION, params)

        results = screen_watchlist(profiles, watchlist, params)
        rm_map = load_rm_sector_map_db()
        results = enrich_with_rm_sector(results, rm_map)
        # v2.10: flag customers with an active credit relationship (owes the bank)
        if not results.empty:
            credit_cifs = load_credit_cifs(results["cif_id"].tolist())
            key = results["cif_id"].astype(str).str.strip()
            results["has_credit_relationship"] = key.isin(credit_cifs)
        exclusions = load_exclusions_db(write_eng)
        results, n_excluded = apply_exclusions(results, exclusions)
        if n_excluded:
            log.info("Suppressed %s result rows via exclusion list", n_excluded)
        write_results_db(write_eng, results, run_id, run_date, MODEL_VERSION)

        # v2.9: discover confirmed-mule customers' other OPEN accounts (not flagged)
        try:
            ref_profiles = profiles[profiles["ACID"].isin(watchlist)]
            disc = discover_related_accounts(ref_profiles, results)
            write_discovery_db(write_eng, disc, run_id, run_date)
            # v2.12: network discovery — accounts linked via shared party
            net = discover_network_accounts(ref_profiles, results)
            write_network_db(write_eng, net, run_id, run_date)
        except Exception as disc_exc:  # noqa: BLE001 — discovery is best-effort
            log.warning("Related-account discovery failed (%s) — main results unaffected", disc_exc)

        # v2.11: stage notifications for NEWLY-added accounts. Staging only —
        # nothing is sent here; a separate gated sender delivers pending rows.
        try:
            new_accts = find_new_accounts(write_eng, results, run_date)
            standing_cc = [e.strip() for e in
                           os.environ.get("AML_MGMT_RECIPIENTS", "").split(",") if e.strip()]
            notif_rows = build_notifications(new_accts, extra_cc=standing_cc)
            write_notifications_db(write_eng, notif_rows, run_id, run_date)
        except Exception as ntf_exc:  # noqa: BLE001 — notifications are best-effort
            log.warning("Notification staging failed (%s) — main results unaffected", ntf_exc)

        phash = hashlib.sha256(json.dumps(params, sort_keys=True).encode()).hexdigest()[:16]
        log_run_db(
            write_eng, run_id=run_id, run_date=run_date, model_version=MODEL_VERSION,
            started_at=started, finished_at=_utcnow(), status="SUCCESS",
            n_accounts_profiled=int(len(profiles)), n_reference_accounts=len(watchlist),
            n_results=int(len(results)), params_hash=phash, error_message=None,
        )
        log.info("=== SUCCESS: %s results ===", len(results))

    except Exception as exc:  # noqa: BLE001 — top-level guard, logged to run table
        log.exception("Run failed")
        try:
            log_run_db(
                write_eng, run_id=run_id, run_date=run_date, model_version=MODEL_VERSION,
                started_at=started, finished_at=_utcnow(), status="FAILED",
                n_accounts_profiled=0, n_reference_accounts=0, n_results=0,
                params_hash=None, error_message=str(exc)[:1000],
            )
        finally:
            sys.exit(1)


if __name__ == "__main__":
    main()