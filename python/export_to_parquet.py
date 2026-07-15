#!/usr/bin/env python3
"""
Query the PostgreSQL SEC Financial Statement Data Sets database (see
setup/create_database.py) and dump it to Parquet files, mirroring the
database's own date partitioning.

`num` is RANGE partitioned by year on `ddate` (num_y1994, num_y1995, ...,
num_default). Each partition is queried and written independently into a
Hive-style directory:

  <output-dir>/num/year=1994/num.parquet
  <output-dir>/num/year=1995/num.parquet
  ...
  <output-dir>/num/year=default/num.parquet   (catch-all partition)

`sub`, `tag`, and `pre` are not date-partitioned in the database, so each is
written as a single file:

  <output-dir>/sub/sub.parquet
  <output-dir>/tag/tag.parquet
  <output-dir>/pre/pre.parquet

This directory layout can be read directly by
panel_anomaly_detection_parquet.py via --num-path/--sub-path pointing at the
num/ and sub/ subdirectories.

Total wall time spent querying PostgreSQL and total wall time spent writing
Parquet files are printed separately on stdout.

Usage:
  python export_to_parquet.py --dbname sec_financial_statements \\
      --output-dir ./sec_data/parquet
"""
import argparse
import os
import re
import time

import pandas as pd
import psycopg2
from psycopg2 import sql

NON_PARTITIONED_TABLES = ["sub", "tag", "pre"]


def parse_args():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--host", default=os.environ.get("PG_DB_HOST", "localhost"))
    p.add_argument("--port", type=int, default=5432)
    p.add_argument("--user", default="postgres")
    p.add_argument("--password", default=os.environ.get("PG_DB_PW", "postgres"))
    p.add_argument("--dbname", default="sec_financial_statements")
    p.add_argument("--output-dir", required=True, help="Root directory to write Parquet files into")
    p.add_argument("--tables", default="sub,tag,num,pre",
                    help="Comma-separated subset of tables to export")
    return p.parse_args()


def get_num_partitions(conn):
    query = """
        SELECT child.relname AS partition_name
        FROM pg_inherits
        JOIN pg_class parent ON pg_inherits.inhparent = parent.oid
        JOIN pg_class child  ON pg_inherits.inhrelid  = child.oid
        WHERE parent.relname = 'num'
        ORDER BY child.relname
    """
    with conn.cursor() as cur:
        cur.execute(query)
        return [row[0] for row in cur.fetchall()]


def partition_year_label(partition_name):
    match = re.match(r"^num_y(\d{4})$", partition_name)
    return match.group(1) if match else "default"


def fetch_table(conn, identifier_sql):
    with conn.cursor() as cur:
        cur.execute(identifier_sql)
        rows = cur.fetchall()
        columns = [d[0] for d in cur.description]
    return pd.DataFrame(rows, columns=columns)


def export_num(conn, output_dir):
    query_time = 0.0
    write_time = 0.0
    total_rows = 0

    for partition in get_num_partitions(conn):
        year_label = partition_year_label(partition)

        t0 = time.perf_counter()
        df = fetch_table(conn, sql.SQL("SELECT * FROM {}").format(sql.Identifier(partition)))
        query_time += time.perf_counter() - t0

        if df.empty:
            print(f"  num partition {partition:14s} -> year={year_label:8s} 0 rows (skipped)")
            continue

        t0 = time.perf_counter()
        part_dir = os.path.join(output_dir, "num", f"year={year_label}")
        os.makedirs(part_dir, exist_ok=True)
        df.to_parquet(os.path.join(part_dir, "num.parquet"), engine="pyarrow", index=False)
        write_time += time.perf_counter() - t0

        total_rows += len(df)
        print(f"  num partition {partition:14s} -> year={year_label:8s} {len(df):>10,} rows")

    return query_time, write_time, total_rows


def export_table(conn, table, output_dir):
    t0 = time.perf_counter()
    df = fetch_table(conn, sql.SQL("SELECT * FROM {}").format(sql.Identifier(table)))
    query_time = time.perf_counter() - t0

    t0 = time.perf_counter()
    table_dir = os.path.join(output_dir, table)
    os.makedirs(table_dir, exist_ok=True)
    df.to_parquet(os.path.join(table_dir, f"{table}.parquet"), engine="pyarrow", index=False)
    write_time = time.perf_counter() - t0

    print(f"  {table:10s} {len(df):>10,} rows")
    return query_time, write_time, len(df)


def main():
    args = parse_args()
    tables = [t.strip() for t in args.tables.split(",") if t.strip()]

    conn = psycopg2.connect(
        host=args.host, port=args.port, user=args.user,
        password=args.password, dbname=args.dbname,
    )

    total_query_time = 0.0
    total_write_time = 0.0
    total_rows = 0

    try:
        for table in tables:
            if table == "num":
                print("Exporting num (by year partition):")
                q, w, n = export_num(conn, args.output_dir)
            elif table in NON_PARTITIONED_TABLES:
                print(f"Exporting {table}:")
                q, w, n = export_table(conn, table, args.output_dir)
            else:
                print(f"Skipping unknown table '{table}'")
                continue
            total_query_time += q
            total_write_time += w
            total_rows += n
    finally:
        conn.close()

    print(f"\nRows exported:             {total_rows:>12,}")
    print(f"Database query time:       {total_query_time:8.2f}s")
    print(f"Parquet file creation time:{total_write_time:8.2f}s")
    print(f"Total time:                {total_query_time + total_write_time:8.2f}s")


if __name__ == "__main__":
    main()
