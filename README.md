# data-read-parquet

Tooling to load the [SEC Financial Statement Data Sets](https://www.sec.gov/dera/data/financial-statement-data-sets)
into PostgreSQL, export that data to date-partitioned Parquet files, and run
a point-in-time financial-statement panel / anomaly-detection analysis
(Altman Z''-Score, Beneish M-Score, Benford's Law) against either data
source.

## Repo layout

### `setup/`
Scripts to provision the source database.

- `create_database.py` — creates the PostgreSQL database and schema for the
  SEC data set (`sub`, `tag`, `pre` tables, plus `num` — the numeric-facts
  table — RANGE partitioned by year on `ddate`).
- `download_and_load.py` — downloads the quarterly SEC ZIP files and
  COPY-loads `sub.txt`/`tag.txt`/`num.txt`/`pre.txt` into the schema above,
  idempotently (safe to re-run).

Run these once, in order, to stand up the database.

### `python/`
Python implementations of the data-export and analysis pipeline.

- `panel_anomaly_detection.py` — queries PostgreSQL directly, builds a wide
  company-fiscal-year panel from the long `num` fact table, and flags
  anomalous 10-K filings using Altman Z''-Score, Beneish M-Score, and a
  Benford's Law chi-square test. Prints the wall time for the database query
  separately from the wall time for the in-memory analysis.
- `panel_anomaly_detection_parquet.py` — the same analysis, but reads
  `num`/`sub` data from Parquet files (single file, directory, or wildcard
  pattern) instead of the database. Shares its panel-building/scoring logic
  with `panel_anomaly_detection.py` via a plain import.
- `export_to_parquet.py` — queries PostgreSQL and dumps `sub`/`tag`/`num`/`pre`
  to Parquet, mirroring the database's own partitioning: `num` is written
  one file per year partition in a Hive-style `num/year=YYYY/num.parquet`
  layout so it can be read back with the wildcard/glob support in
  `panel_anomaly_detection_parquet.py`. Prints wall time for querying vs.
  wall time for Parquet file creation.

### `r/`
R ports of the three scripts in `python/`, with the same CLI flags,
behavior, and timing output:

- `panel_anomaly_detection.R` — PostgreSQL-querying version.
- `panel_anomaly_detection_parquet.R` — Parquet-reading version; sources
  `panel_anomaly_detection.R` to reuse its analysis functions, the same way
  the Python parquet script imports from the Python DB script.
- `export_to_parquet.R` — database-to-partitioned-Parquet exporter.

Requires the `DBI`, `RPostgres`, `arrow`, `dplyr`, `tidyr`, and `optparse`
packages.
