#!/usr/bin/env python3
"""
Create a PostgreSQL database and schema for the SEC Financial Statement
Data Sets (https://www.sec.gov/dera/data/financial-statement-data-sets).

The source data ships as one ZIP per fiscal quarter, each containing four
tab-delimited files: sub.txt, tag.txt, num.txt, pre.txt. This script creates
matching tables:

  sub  - one row per XBRL submission (filing)
  tag  - one row per unique (tag, version) XBRL element definition
  num  - one row per reported numeric fact, RANGE partitioned by ddate (year)
  pre  - one row per line item as presented in a filing's statements

`num` is partitioned by year so large date-range queries and per-quarter
loads only touch relevant partitions.

Usage:
  python create_database.py --start-year 1994 --end-year 2035
"""
import argparse
import sys

import psycopg2
from psycopg2 import sql
from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT


def parse_args():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--host", default="localhost")
    p.add_argument("--port", type=int, default=5432)
    p.add_argument("--user", default="postgres")
    p.add_argument("--password", default="postgres")
    p.add_argument("--dbname", default="sec_financial_statements",
                    help="Database to create/use")
    p.add_argument("--maintenance-db", default="postgres",
                    help="Existing database to connect to in order to run CREATE DATABASE")
    p.add_argument("--start-year", type=int, default=1994,
                    help="First year to pre-create a num partition for")
    p.add_argument("--end-year", type=int, default=2035,
                    help="Last year to pre-create a num partition for")
    p.add_argument("--drop-existing", action="store_true",
                    help="Drop the database first if it already exists")
    return p.parse_args()


def create_database(args):
    conn = psycopg2.connect(
        host=args.host, port=args.port, user=args.user,
        password=args.password, dbname=args.maintenance_db,
    )
    conn.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
    try:
        with conn.cursor() as cur:
            if args.drop_existing:
                cur.execute(
                    sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                        sql.Identifier(args.dbname)
                    )
                )
            cur.execute(
                "SELECT 1 FROM pg_database WHERE datname = %s", (args.dbname,)
            )
            if cur.fetchone() is None:
                cur.execute(
                    sql.SQL("CREATE DATABASE {}").format(sql.Identifier(args.dbname))
                )
                print(f"Created database '{args.dbname}'")
            else:
                print(f"Database '{args.dbname}' already exists, reusing it")
    finally:
        conn.close()


SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS sub (
    adsh        VARCHAR(20)  PRIMARY KEY,
    cik         BIGINT       NOT NULL,
    name        VARCHAR(150),
    sic         INTEGER,
    countryba   VARCHAR(2),
    stprba      VARCHAR(2),
    cityba      VARCHAR(30),
    zipba       VARCHAR(10),
    bas1        VARCHAR(40),
    bas2        VARCHAR(40),
    baph        VARCHAR(20),
    countryma   VARCHAR(2),
    stprma      VARCHAR(2),
    cityma      VARCHAR(30),
    zipma       VARCHAR(10),
    mas1        VARCHAR(40),
    mas2        VARCHAR(40),
    countryinc  VARCHAR(3),
    stprinc     VARCHAR(2),
    ein         VARCHAR(10),
    former      VARCHAR(150),
    changed     VARCHAR(8),
    afs         VARCHAR(5),
    wksi        BOOLEAN,
    fye         VARCHAR(4),
    form        VARCHAR(10),
    period      DATE,
    fy          INTEGER,
    fp          VARCHAR(2),
    filed       DATE,
    accepted    TIMESTAMP,
    prevrpt     BOOLEAN,
    detail      BOOLEAN,
    instance    VARCHAR(40),
    nciks       INTEGER,
    aciks       TEXT
);

CREATE TABLE IF NOT EXISTS tag (
    tag         VARCHAR(256) NOT NULL,
    version     VARCHAR(20)  NOT NULL,
    custom      BOOLEAN,
    abstract    BOOLEAN,
    datatype    VARCHAR(20),
    iord        VARCHAR(1),
    crdr        VARCHAR(1),
    tlabel      VARCHAR(512),
    doc         TEXT,
    PRIMARY KEY (tag, version)
);

CREATE TABLE IF NOT EXISTS num (
    adsh        VARCHAR(20)   NOT NULL REFERENCES sub(adsh),
    tag         VARCHAR(256)  NOT NULL,
    version     VARCHAR(20)   NOT NULL,
    ddate       DATE          NOT NULL,
    qtrs        SMALLINT      NOT NULL,
    uom         VARCHAR(20)   NOT NULL,
    segments    TEXT,
    coreg       VARCHAR(256)  NOT NULL DEFAULT '',
    value       NUMERIC,
    footnote    TEXT,
    PRIMARY KEY (adsh, tag, version, ddate, qtrs, uom, coreg),
    FOREIGN KEY (tag, version) REFERENCES tag(tag, version)
) PARTITION BY RANGE (ddate);

CREATE TABLE IF NOT EXISTS pre (
    adsh        VARCHAR(20)   NOT NULL REFERENCES sub(adsh),
    report      SMALLINT      NOT NULL,
    line        INTEGER       NOT NULL,
    stmt        VARCHAR(2),
    inpth       BOOLEAN,
    rfile       VARCHAR(1),
    tag         VARCHAR(256)  NOT NULL,
    version     VARCHAR(20)   NOT NULL,
    plabel      VARCHAR(512),
    negating    BOOLEAN,
    PRIMARY KEY (adsh, report, line)
);

CREATE INDEX IF NOT EXISTS idx_num_adsh ON num (adsh);
CREATE INDEX IF NOT EXISTS idx_num_tag_version ON num (tag, version);
CREATE INDEX IF NOT EXISTS idx_pre_adsh ON pre (adsh);
CREATE INDEX IF NOT EXISTS idx_sub_cik ON sub (cik);
CREATE INDEX IF NOT EXISTS idx_sub_period ON sub (period);
"""


def create_schema(args):
    conn = psycopg2.connect(
        host=args.host, port=args.port, user=args.user,
        password=args.password, dbname=args.dbname,
    )
    try:
        with conn.cursor() as cur:
            cur.execute(SCHEMA_DDL)
            # backfill for databases created before the `segments` column existed
            cur.execute("ALTER TABLE num ADD COLUMN IF NOT EXISTS segments TEXT")

            # One partition per calendar year, plus a catch-all default
            # partition for any ddate outside the configured range.
            for year in range(args.start_year, args.end_year + 1):
                part_name = f"num_y{year}"
                cur.execute(
                    sql.SQL(
                        "CREATE TABLE IF NOT EXISTS {part} PARTITION OF num "
                        "FOR VALUES FROM (%s) TO (%s)"
                    ).format(part=sql.Identifier(part_name)),
                    (f"{year}-01-01", f"{year + 1}-01-01"),
                )

            cur.execute(
                sql.SQL(
                    "CREATE TABLE IF NOT EXISTS num_default "
                    "PARTITION OF num DEFAULT"
                )
            )
        conn.commit()
        print(
            f"Schema ready: sub, tag, pre, and num "
            f"({args.end_year - args.start_year + 1} yearly partitions + default)"
        )
    finally:
        conn.close()


def main():
    args = parse_args()
    if args.start_year > args.end_year:
        sys.exit("--start-year must be <= --end-year")
    create_database(args)
    create_schema(args)


if __name__ == "__main__":
    main()
