#!/usr/bin/env python3
"""
Point-in-time financial statement panel analysis + anomaly detection over
the SEC Financial Statement Data Sets loaded by setup/download_and_load.py.

For every 10-K filing (adsh) it reconstructs a wide company-year panel from
the long `num` fact table, then flags anomalous filings using three
independent signals:

  - Altman Z''-Score  (bankruptcy / distress risk)
  - Beneish M-Score   (earnings-manipulation risk, needs prior-year data)
  - Benford's Law     (chi-square goodness-of-fit on leading digits)

Reports the wall-clock time spent querying PostgreSQL separately from the
wall-clock time spent on the in-memory anomaly detection.

Usage:
  python panel_anomaly_detection.py --dbname sec_financial_statements --top-n 25
"""
import argparse
import time

import numpy as np
import pandas as pd
import psycopg2

INSTANT_TAGS = [
    "Assets", "AssetsCurrent", "LiabilitiesCurrent", "Liabilities",
    "StockholdersEquity", "RetainedEarningsAccumulatedDeficit",
    "ReceivablesNetCurrent", "PropertyPlantAndEquipmentNet",
    "LongTermDebtNoncurrent",
]

DURATION_TAGS = [
    "OperatingIncomeLoss", "Revenues", "SalesRevenueNet",
    "CostOfGoodsAndServicesSold", "CostOfRevenue", "NetIncomeLoss",
    "DepreciationDepletionAndAmortization",
    "SellingGeneralAndAdministrativeExpense",
    "NetCashProvidedByUsedInOperatingActivities",
]

QUERY = """
SELECT s.adsh, s.cik, s.name, s.fy, s.fp, s.period,
       n.tag, n.value
FROM num n
JOIN sub s ON s.adsh = n.adsh
WHERE s.form = %(form)s
  AND n.coreg = ''
  AND (
        (n.tag = ANY(%(instant_tags)s) AND n.qtrs = 0)
     OR (n.tag = ANY(%(duration_tags)s) AND n.qtrs = 4)
      )
"""

BENFORD_CHI2_CRITICAL_8DF = 15.51  # p = 0.05, 8 degrees of freedom
ALTMAN_DISTRESS_THRESHOLD = 1.23
BENEISH_MANIPULATION_THRESHOLD = -2.22


def parse_args():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--host", default="localhost")
    p.add_argument("--port", type=int, default=5432)
    p.add_argument("--user", default="postgres")
    p.add_argument("--password", default="postgres")
    p.add_argument("--dbname", default="sec_financial_statements")
    p.add_argument("--form", default="10-K", help="Filing form type to analyze")
    p.add_argument("--min-benford-obs", type=int, default=30,
                    help="Minimum reported facts per company to score with Benford's Law")
    p.add_argument("--top-n", type=int, default=25,
                    help="Number of top flagged filings to print")
    p.add_argument("--output", default=None, help="Optional CSV path for flagged filings")
    return p.parse_args()


def fetch_data(args):
    start = time.perf_counter()
    conn = psycopg2.connect(
        host=args.host, port=args.port, user=args.user,
        password=args.password, dbname=args.dbname,
    )
    try:
        with conn.cursor() as cur:
            cur.execute(
                QUERY,
                {
                    "form": args.form,
                    "instant_tags": INSTANT_TAGS,
                    "duration_tags": DURATION_TAGS,
                },
            )
            rows = cur.fetchall()
            columns = [d[0] for d in cur.description]
    finally:
        conn.close()
    df = pd.DataFrame(rows, columns=columns)
    elapsed = time.perf_counter() - start
    return df, elapsed


def col(frame, name):
    return frame[name] if name in frame.columns else pd.Series(np.nan, index=frame.index)


def build_panel(df):
    wide = df.pivot_table(
        index=["adsh", "cik", "name", "fy", "fp"],
        columns="tag", values="value", aggfunc="last",
    ).reset_index()

    wide["revenue"] = col(wide, "Revenues").fillna(col(wide, "SalesRevenueNet"))
    wide["cogs"] = col(wide, "CostOfGoodsAndServicesSold").fillna(col(wide, "CostOfRevenue"))
    return wide


def compute_altman_z(wide):
    assets = col(wide, "Assets").replace(0, np.nan)
    working_capital = col(wide, "AssetsCurrent") - col(wide, "LiabilitiesCurrent")
    retained_earnings = col(wide, "RetainedEarningsAccumulatedDeficit")
    ebit = col(wide, "OperatingIncomeLoss")
    equity = col(wide, "StockholdersEquity")
    liabilities = col(wide, "Liabilities").replace(0, np.nan)

    with np.errstate(divide="ignore", invalid="ignore"):
        wide["z_score"] = (
            0.717 * (working_capital / assets)
            + 0.847 * (retained_earnings / assets)
            + 3.107 * (ebit / assets)
            + 0.420 * (equity / liabilities)
            + 0.998 * (wide["revenue"] / assets)
        )
    return wide


def compute_beneish_m(wide):
    w = wide.sort_values(["cik", "fy"]).copy()
    prev = w.groupby("cik").shift(1)

    assets = col(w, "Assets").replace(0, np.nan)
    assets_prev = col(prev, "Assets").replace(0, np.nan)
    sales = w["revenue"]
    sales_prev = prev["revenue"]

    with np.errstate(divide="ignore", invalid="ignore"):
        dsri = (col(w, "ReceivablesNetCurrent") / sales) / (col(prev, "ReceivablesNetCurrent") / sales_prev)

        gm = (sales - w["cogs"]) / sales
        gm_prev = (sales_prev - prev["cogs"]) / sales_prev
        gmi = gm_prev / gm

        aqi_cur = 1 - (col(w, "AssetsCurrent").fillna(0) + col(w, "PropertyPlantAndEquipmentNet").fillna(0)) / assets
        aqi_prev = 1 - (col(prev, "AssetsCurrent").fillna(0) + col(prev, "PropertyPlantAndEquipmentNet").fillna(0)) / assets_prev
        aqi = aqi_cur / aqi_prev

        sgi = sales / sales_prev

        depi_cur = col(w, "DepreciationDepletionAndAmortization") / (
            col(w, "DepreciationDepletionAndAmortization") + col(w, "PropertyPlantAndEquipmentNet")
        )
        depi_prev = col(prev, "DepreciationDepletionAndAmortization") / (
            col(prev, "DepreciationDepletionAndAmortization") + col(prev, "PropertyPlantAndEquipmentNet")
        )
        depi = depi_prev / depi_cur

        sgai = (col(w, "SellingGeneralAndAdministrativeExpense") / sales) / (
            col(prev, "SellingGeneralAndAdministrativeExpense") / sales_prev
        )

        tata = (col(w, "NetIncomeLoss") - col(w, "NetCashProvidedByUsedInOperatingActivities")) / assets

        lvgi_cur = (col(w, "LiabilitiesCurrent").fillna(0) + col(w, "LongTermDebtNoncurrent").fillna(0)) / assets
        lvgi_prev = (col(prev, "LiabilitiesCurrent").fillna(0) + col(prev, "LongTermDebtNoncurrent").fillna(0)) / assets_prev
        lvgi = lvgi_cur / lvgi_prev

        w["m_score"] = (
            -4.84
            + 0.92 * dsri
            + 0.528 * gmi
            + 0.404 * aqi
            + 0.892 * sgi
            + 0.115 * depi
            - 0.172 * sgai
            + 4.679 * tata
            - 0.327 * lvgi
        )
    return w


def compute_benford(df, min_obs):
    d = df[["cik", "value"]].dropna()
    d = d[d["value"] != 0].copy()
    d["abs_value"] = d["value"].abs()
    d = d[d["abs_value"] >= 1]
    d["first_digit"] = (d["abs_value"] / 10 ** np.floor(np.log10(d["abs_value"]))).astype(int).clip(1, 9)

    counts = d.groupby(["cik", "first_digit"]).size().unstack(fill_value=0)
    for digit in range(1, 10):
        if digit not in counts.columns:
            counts[digit] = 0
    counts = counts[sorted(counts.columns)]

    totals = counts.sum(axis=1)
    expected_props = pd.Series({digit: np.log10(1 + 1 / digit) for digit in range(1, 10)})
    expected_counts = pd.DataFrame(
        np.outer(totals, expected_props[counts.columns]),
        index=counts.index, columns=counts.columns,
    )

    chi2 = ((counts - expected_counts) ** 2 / expected_counts.replace(0, np.nan)).sum(axis=1)
    result = pd.DataFrame({"cik": counts.index, "benford_chi2": chi2.values, "benford_n": totals.values})
    return result[result["benford_n"] >= min_obs]


def run_anomaly_detection(df, min_benford_obs):
    start = time.perf_counter()
    wide = build_panel(df)
    wide = compute_altman_z(wide)
    wide = compute_beneish_m(wide)
    benford = compute_benford(df, min_benford_obs)

    result = wide.merge(benford, on="cik", how="left")
    result["flag_distress"] = result["z_score"] < ALTMAN_DISTRESS_THRESHOLD
    result["flag_manipulation"] = result["m_score"] > BENEISH_MANIPULATION_THRESHOLD
    result["flag_benford"] = result["benford_chi2"] > BENFORD_CHI2_CRITICAL_8DF
    result["anomaly_count"] = result[["flag_distress", "flag_manipulation", "flag_benford"]].sum(axis=1)

    elapsed = time.perf_counter() - start
    return result, elapsed


def main():
    args = parse_args()

    df, query_time = fetch_data(args)
    print(f"Database query time:      {query_time:8.2f}s ({len(df):,} facts fetched)")

    result, analysis_time = run_anomaly_detection(df, args.min_benford_obs)
    print(f"Anomaly detection time:    {analysis_time:8.2f}s ({len(result):,} filings scored)")
    print(f"Total time:                {query_time + analysis_time:8.2f}s")

    flagged = result[result["anomaly_count"] > 0].sort_values(
        ["anomaly_count", "m_score"], ascending=[False, False]
    )
    print(f"\n{len(flagged):,} filings flagged by at least one signal\n")

    display_cols = ["adsh", "cik", "name", "fy", "fp", "z_score", "m_score", "benford_chi2", "anomaly_count"]
    print(flagged[display_cols].head(args.top_n).to_string(index=False))

    if args.output:
        flagged.to_csv(args.output, index=False)
        print(f"\nFlagged filings written to {args.output}")


if __name__ == "__main__":
    main()
