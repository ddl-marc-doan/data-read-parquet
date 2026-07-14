#!/usr/bin/env python3
"""
Download SEC Financial Statement Data Sets quarterly ZIP files and load
them into the PostgreSQL schema created by create_database.py.

Each quarter is published at:
  https://www.sec.gov/files/dera/data/financial-statement-data-sets/{year}q{q}.zip

and contains four tab-delimited files with a header row: sub.txt, tag.txt,
num.txt, pre.txt. Each file is COPY-loaded into a temporary staging table,
then merged into the target table with ON CONFLICT DO NOTHING so re-running
a quarter (or a quarter with rows appearing in more than one release) is
safe and idempotent.

Usage:
  python download_and_load.py --start-year 2020 --end-year 2023 \\
      --user-agent "Your Name your.email@example.com"
"""
import argparse
import io
import os
import sys
import zipfile
from datetime import date

import psycopg2
import requests

SEC_URL_TEMPLATE = (
    "https://www.sec.gov/files/dera/data/financial-statement-data-sets/{year}q{q}.zip"
)

# column order must match the header row of each *.txt file
TABLE_COLUMNS = {
    "sub": [
        "adsh", "cik", "name", "sic", "countryba", "stprba", "cityba", "zipba",
        "bas1", "bas2", "baph", "countryma", "stprma", "cityma", "zipma",
        "mas1", "mas2", "countryinc", "stprinc", "ein", "former", "changed",
        "afs", "wksi", "fye", "form", "period", "fy", "fp", "filed",
        "accepted", "prevrpt", "detail", "instance", "nciks", "aciks",
    ],
    "tag": [
        "tag", "version", "custom", "abstract", "datatype", "iord", "crdr",
        "tlabel", "doc",
    ],
    "num": [
        "adsh", "tag", "version", "ddate", "qtrs", "uom", "segments", "coreg",
        "value", "footnote",
    ],
    "pre": [
        "adsh", "report", "line", "stmt", "inpth", "rfile", "tag", "version",
        "plabel", "negating",
    ],
}

# load order matters: sub/tag before num/pre because of foreign keys
LOAD_ORDER = ["sub", "tag", "num", "pre"]


def parse_args():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--host", default="localhost")
    p.add_argument("--port", type=int, default=5432)
    p.add_argument("--user", default="postgres")
    p.add_argument("--password", default="postgres")
    p.add_argument("--dbname", default="sec_financial_statements")
    p.add_argument("--start-year", type=int, required=True)
    p.add_argument("--end-year", type=int, required=True)
    p.add_argument("--start-quarter", type=int, default=1, choices=[1, 2, 3, 4])
    p.add_argument("--end-quarter", type=int, default=4, choices=[1, 2, 3, 4])
    p.add_argument("--data-dir", default="./sec_data",
                    help="Directory to download/cache the quarterly zip files in")
    p.add_argument("--keep-zip", action="store_true",
                    help="Keep downloaded zip files instead of re-downloading each run")
    p.add_argument(
        "--user-agent",
        default=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
        help="User-Agent header to send with download requests (defaults to a Chrome UA string)",
    )
    return p.parse_args()


def iter_quarters(args):
    today = date.today()
    for year in range(args.start_year, args.end_year + 1):
        q_start = args.start_quarter if year == args.start_year else 1
        q_end = args.end_quarter if year == args.end_year else 4
        for q in range(q_start, q_end + 1):
            # skip quarters that haven't been filed/published yet
            if date(year, q * 3, 1) > today:
                continue
            yield year, q


def download_quarter(year, q, data_dir, user_agent):
    zip_path = os.path.join(data_dir, f"{year}q{q}.zip")
    if os.path.exists(zip_path):
        print(f"  {year}q{q}: zip already downloaded, skipping download")
        return zip_path

    url = SEC_URL_TEMPLATE.format(year=year, q=q)
    print(f"  {year}q{q}: downloading {url}")
    resp = requests.get(url, headers={"User-Agent": user_agent}, stream=True, timeout=60)
    if resp.status_code == 404:
        print(f"  {year}q{q}: not published, skipping")
        return None
    resp.raise_for_status()

    os.makedirs(data_dir, exist_ok=True)
    tmp_path = zip_path + ".part"
    with open(tmp_path, "wb") as f:
        for chunk in resp.iter_content(chunk_size=1024 * 1024):
            f.write(chunk)
    os.rename(tmp_path, zip_path)
    return zip_path


def load_table(cur, table, fileobj):
    columns = TABLE_COLUMNS[table]
    staging = f"staging_{table}"

    cur.execute(
        f"CREATE TEMP TABLE {staging} (LIKE {table} INCLUDING DEFAULTS) ON COMMIT DROP"
    )
    # staging has no partitioning/constraints issues since it's a plain copy of columns
    col_list = ", ".join(columns)
    cur.copy_expert(
        f"COPY {staging} ({col_list}) FROM STDIN WITH (FORMAT text, "
        f"DELIMITER E'\\t', NULL '', HEADER true)",
        fileobj,
    )
    cur.execute(
        f"INSERT INTO {table} ({col_list}) SELECT {col_list} FROM {staging} "
        f"ON CONFLICT DO NOTHING"
    )
    cur.execute(f"SELECT count(*) FROM {staging}")
    return cur.fetchone()[0]


def load_quarter(conn, zip_path):
    with zipfile.ZipFile(zip_path) as zf:
        with conn.cursor() as cur:
            for table in LOAD_ORDER:
                member = f"{table}.txt"
                if member not in zf.namelist():
                    print(f"    {member} not found in {os.path.basename(zip_path)}, skipping")
                    continue
                with zf.open(member) as raw:
                    text_stream = io.TextIOWrapper(raw, encoding="latin-1")
                    n = load_table(cur, table, text_stream)
                    print(f"    loaded {n:>7} rows from {member}")
        conn.commit()


def main():
    args = parse_args()
    if args.start_year > args.end_year:
        sys.exit("--start-year must be <= --end-year")

    conn = psycopg2.connect(
        host=args.host, port=args.port, user=args.user,
        password=args.password, dbname=args.dbname,
    )

    try:
        for year, q in iter_quarters(args):
            print(f"Quarter {year}Q{q}:")
            zip_path = download_quarter(year, q, args.data_dir, args.user_agent)
            if zip_path is None:
                continue
            load_quarter(conn, zip_path)
            if not args.keep_zip:
                os.remove(zip_path)
    finally:
        conn.close()

    print("Done.")


if __name__ == "__main__":
    main()
