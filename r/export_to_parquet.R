#!/usr/bin/env Rscript
# Query the PostgreSQL SEC Financial Statement Data Sets database (see
# setup/create_database.py) and dump it to Parquet files, mirroring the
# database's own date partitioning.
#
# `num` is RANGE partitioned by year on `ddate` (num_y1994, num_y1995, ...,
# num_default). Each partition is queried and written independently into a
# Hive-style directory:
#
#   <output-dir>/num/year=1994/num.parquet
#   <output-dir>/num/year=1995/num.parquet
#   ...
#   <output-dir>/num/year=default/num.parquet   (catch-all partition)
#
# `sub`, `tag`, and `pre` are not date-partitioned in the database, so each
# is written as a single file:
#
#   <output-dir>/sub/sub.parquet
#   <output-dir>/tag/tag.parquet
#   <output-dir>/pre/pre.parquet
#
# This directory layout can be read directly by
# panel_anomaly_detection_parquet.R via --num-path/--sub-path pointing at the
# num/ and sub/ subdirectories.
#
# Total wall time spent querying PostgreSQL and total wall time spent
# writing Parquet files are printed separately on stdout.
#
# Requires: DBI, RPostgres, arrow, optparse
#
# Usage:
#   Rscript export_to_parquet.R --dbname sec_financial_statements \
#       --output-dir ./sec_data/parquet

suppressPackageStartupMessages({
  library(DBI)
  library(RPostgres)
  library(arrow)
  library(optparse)
})

NON_PARTITIONED_TABLES <- c("sub", "tag", "pre")

parse_cli_args <- function() {
  option_list <- list(
    make_option("--host", default = Sys.getenv("PG_DB_HOST", unset = "localhost")),
    make_option("--port", type = "integer", default = 5432),
    make_option("--user", default = "postgres"),
    make_option("--password", default = Sys.getenv("PG_DB_PW", unset = "postgres")),
    make_option("--dbname", default = "sec_financial_statements"),
    make_option("--output-dir", type = "character",
                help = "Root directory to write Parquet files into"),
    make_option("--tables", default = "sub,tag,num,pre",
                help = "Comma-separated subset of tables to export")
  )
  opt <- optparse::parse_args(OptionParser(option_list = option_list),
                               args = commandArgs(trailingOnly = TRUE))
  if (is.null(opt$`output-dir`)) stop("--output-dir is required")
  opt
}

get_num_partitions <- function(con) {
  query <- "
    SELECT child.relname AS partition_name
    FROM pg_inherits
    JOIN pg_class parent ON pg_inherits.inhparent = parent.oid
    JOIN pg_class child  ON pg_inherits.inhrelid  = child.oid
    WHERE parent.relname = 'num'
    ORDER BY child.relname
  "
  dbGetQuery(con, query)$partition_name
}

partition_year_label <- function(name) {
  m <- regmatches(name, regexec("^num_y(\\d{4})$", name))[[1]]
  if (length(m) == 2) m[2] else "default"
}

export_num <- function(con, output_dir) {
  query_time <- 0
  write_time <- 0
  total_rows <- 0

  for (partition in get_num_partitions(con)) {
    year_label <- partition_year_label(partition)

    t0 <- Sys.time()
    df <- dbGetQuery(con, sprintf("SELECT * FROM %s", dbQuoteIdentifier(con, partition)))
    query_time <- query_time + as.numeric(difftime(Sys.time(), t0, units = "secs"))

    if (nrow(df) == 0) {
      cat(sprintf("  num partition %-14s -> year=%-8s 0 rows (skipped)\n", partition, year_label))
      next
    }

    t0 <- Sys.time()
    part_dir <- file.path(output_dir, "num", sprintf("year=%s", year_label))
    dir.create(part_dir, recursive = TRUE, showWarnings = FALSE)
    arrow::write_parquet(df, file.path(part_dir, "num.parquet"))
    write_time <- write_time + as.numeric(difftime(Sys.time(), t0, units = "secs"))

    total_rows <- total_rows + nrow(df)
    cat(sprintf("  num partition %-14s -> year=%-8s %10s rows\n",
                partition, year_label, format(nrow(df), big.mark = ",")))
  }

  list(query_time = query_time, write_time = write_time, total_rows = total_rows)
}

export_table <- function(con, table, output_dir) {
  t0 <- Sys.time()
  df <- dbGetQuery(con, sprintf("SELECT * FROM %s", dbQuoteIdentifier(con, table)))
  query_time <- as.numeric(difftime(Sys.time(), t0, units = "secs"))

  t0 <- Sys.time()
  table_dir <- file.path(output_dir, table)
  dir.create(table_dir, recursive = TRUE, showWarnings = FALSE)
  arrow::write_parquet(df, file.path(table_dir, sprintf("%s.parquet", table)))
  write_time <- as.numeric(difftime(Sys.time(), t0, units = "secs"))

  cat(sprintf("  %-10s %10s rows\n", table, format(nrow(df), big.mark = ",")))
  list(query_time = query_time, write_time = write_time, total_rows = nrow(df))
}

main <- function() {
  opt <- parse_cli_args()
  tables <- trimws(strsplit(opt$tables, ",")[[1]])

  con <- dbConnect(
    RPostgres::Postgres(), host = opt$host, port = opt$port,
    user = opt$user, password = opt$password, dbname = opt$dbname
  )
  on.exit(dbDisconnect(con))

  total_query_time <- 0
  total_write_time <- 0
  total_rows <- 0

  for (table in tables) {
    if (table == "num") {
      cat("Exporting num (by year partition):\n")
      res <- export_num(con, opt$`output-dir`)
    } else if (table %in% NON_PARTITIONED_TABLES) {
      cat(sprintf("Exporting %s:\n", table))
      res <- export_table(con, table, opt$`output-dir`)
    } else {
      cat(sprintf("Skipping unknown table '%s'\n", table))
      next
    }
    total_query_time <- total_query_time + res$query_time
    total_write_time <- total_write_time + res$write_time
    total_rows <- total_rows + res$total_rows
  }

  cat(sprintf("\nRows exported:              %12s\n", format(total_rows, big.mark = ",")))
  cat(sprintf("Database query time:       %8.2fs\n", total_query_time))
  cat(sprintf("Parquet file creation time:%8.2fs\n", total_write_time))
  cat(sprintf("Total time:                %8.2fs\n", total_query_time + total_write_time))
}

is_main <- function(expected_basename) {
  cmd_args <- commandArgs(trailingOnly = FALSE)
  file_arg <- grep("^--file=", cmd_args, value = TRUE)
  if (length(file_arg) != 1) return(FALSE)
  basename(sub("^--file=", "", file_arg)) == expected_basename
}

if (is_main("export_to_parquet.R")) {
  main()
}
