#!/usr/bin/env python3
"""
Point-in-time financial statement panel analysis + anomaly detection, same
as panel_anomaly_detection.py, but reading the SEC `num`/`sub` tables from
Parquet files instead of querying PostgreSQL.

Reuses the panel-building and anomaly-scoring logic (Altman Z''-Score,
Beneish M-Score, Benford's Law) from panel_anomaly_detection.py so the two
scripts run an identical analysis over the same data, differing only in the
data-access layer being timed.

Usage:
  python panel_anomaly_detection_parquet.py \\
      --num-path ./sec_data/num.parquet --sub-path ./sec_data/sub.parquet \\
      --top-n 25
"""
import argparse
import glob
import re
import time

import pandas as pd

from panel_anomaly_detection import DURATION_TAGS, INSTANT_TAGS, run_anomaly_detection

GLOB_CHARS = set("*?[")


def resolve_paths(path_spec):
    """Expand a wildcard pattern (e.g. 'num/year=*/num.parquet') to a list of
    files; otherwise return the path unchanged (a single file or a directory,
    which pandas/pyarrow already knows how to read as-is)."""
    # Unlike a shell or R's Sys.glob(), Python's glob module does not treat
    # backslash as an escape character -- "\=" is matched as a literal
    # backslash followed by "=", which never matches real paths (e.g.
    # Hive-style "year=2020" partition directories have no backslash).
    # Job schedulers/shells commonly backslash-escape "=" defensively, so
    # unescape any "\X" -> "X" before matching, mirroring real glob(3).
    path_spec = re.sub(r"\\(.)", r"\1", path_spec)

    if not any(c in path_spec for c in GLOB_CHARS):
        return path_spec

    matches = sorted(glob.glob(path_spec, recursive=True))
    if not matches:
        raise FileNotFoundError(f"No files matched pattern: {path_spec}")
    return matches


def read_parquet_multi(path_spec, columns):
    paths = resolve_paths(path_spec)
    if isinstance(paths, list):
        return pd.concat(
            (pd.read_parquet(p, columns=columns) for p in paths),
            ignore_index=True,
        )
    return pd.read_parquet(paths, columns=columns)


def parse_args():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--num-path", required=True,
                    help="Path to num Parquet data: a file, a directory, or a "
                         "wildcard pattern (e.g. 'sec_data/num/year=*/num.parquet')")
    p.add_argument("--sub-path", required=True,
                    help="Path to sub Parquet data: a file, a directory, or a "
                         "wildcard pattern (e.g. 'sec_data/sub/*.parquet')")
    p.add_argument("--form", default="10-K", help="Filing form type to analyze")
    p.add_argument("--min-benford-obs", type=int, default=30,
                    help="Minimum reported facts per company to score with Benford's Law")
    p.add_argument("--top-n", type=int, default=25,
                    help="Number of top flagged filings to print")
    p.add_argument("--output", default=None, help="Optional CSV path for flagged filings")
    return p.parse_args()


def fetch_data(args):
    start = time.perf_counter()

    sub = read_parquet_multi(args.sub_path, columns=["adsh", "cik", "name", "fy", "fp", "form"])
    sub = sub[sub["form"] == args.form]

    num = read_parquet_multi(
        args.num_path,
        columns=["adsh", "tag", "ddate", "qtrs", "uom", "coreg", "value"],
    )
    num = num[num["coreg"] == ""]

    is_instant = num["tag"].isin(INSTANT_TAGS) & (num["qtrs"] == 0)
    is_duration = num["tag"].isin(DURATION_TAGS) & (num["qtrs"] == 4)
    num = num[is_instant | is_duration]

    df = num.merge(sub, on="adsh", how="inner")[
        ["adsh", "cik", "name", "fy", "fp", "tag", "value"]
    ]

    elapsed = time.perf_counter() - start
    return df, elapsed


def main():
    args = parse_args()

    df, query_time = fetch_data(args)
    print(f"Parquet read/query time:  {query_time:8.2f}s ({len(df):,} facts loaded)")

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
