#!/usr/bin/env Rscript
# Point-in-time financial statement panel analysis + anomaly detection, same
# as panel_anomaly_detection.R, but reading the SEC `num`/`sub` tables from
# Parquet files instead of querying PostgreSQL.
#
# Sources panel_anomaly_detection.R to reuse its panel-building and
# anomaly-scoring logic (Altman Z''-Score, Beneish M-Score, Benford's Law)
# so the two scripts run an identical analysis over the same data, differing
# only in the data-access layer being timed.
#
# Requires: arrow, dplyr, tidyr, optparse (plus DBI/RPostgres, transitively
# loaded from panel_anomaly_detection.R even though no DB connection is made)
#
# Usage:
#   Rscript panel_anomaly_detection_parquet.R \
#       --num-path ./sec_data/parquet/num --sub-path ./sec_data/parquet/sub \
#       --top-n 25
#
#   Wildcards are also supported:
#   Rscript panel_anomaly_detection_parquet.R \
#       --num-path "./sec_data/parquet/num/year=*/num.parquet" \
#       --sub-path "./sec_data/parquet/sub/*.parquet"

suppressPackageStartupMessages({
  library(arrow)
  library(dplyr)
  library(optparse)
})

get_script_dir <- function() {
  cmd_args <- commandArgs(trailingOnly = FALSE)
  file_arg <- grep("^--file=", cmd_args, value = TRUE)
  if (length(file_arg) == 1) return(dirname(normalizePath(sub("^--file=", "", file_arg))))
  getwd()
}

# brings in build_panel, compute_altman_z, compute_beneish_m, compute_benford,
# run_anomaly_detection, INSTANT_TAGS/DURATION_TAGS, etc. Its own main() is
# guarded by is_main() and will not run when sourced from here.
source(file.path(get_script_dir(), "panel_anomaly_detection.R"))

resolve_paths <- function(path_spec) {
  if (!grepl("[*?\\[]", path_spec)) return(path_spec)
  matches <- sort(Sys.glob(path_spec))
  if (length(matches) == 0) stop(sprintf("No files matched pattern: %s", path_spec))
  matches
}

read_parquet_multi <- function(path_spec, columns) {
  paths <- resolve_paths(path_spec)

  if (length(paths) == 1 && dir.exists(paths)) {
    ds <- arrow::open_dataset(paths)
    return(as.data.frame(dplyr::collect(dplyr::select(ds, dplyr::all_of(columns)))))
  }

  frames <- lapply(paths, function(p) {
    t <- arrow::read_parquet(p)
    as.data.frame(t[, columns, drop = FALSE])
  })
  do.call(rbind, frames)
}

parse_cli_args <- function() {
  option_list <- list(
    make_option("--num-path", type = "character",
                help = "Path to num Parquet data: a file, a directory, or a wildcard pattern (e.g. 'sec_data/num/year=*/num.parquet')"),
    make_option("--sub-path", type = "character",
                help = "Path to sub Parquet data: a file, a directory, or a wildcard pattern (e.g. 'sec_data/sub/*.parquet')"),
    make_option("--form", default = "10-K", help = "Filing form type to analyze"),
    make_option("--min-benford-obs", type = "integer", default = 30,
                help = "Minimum reported facts per company to score with Benford's Law"),
    make_option("--top-n", type = "integer", default = 25,
                help = "Number of top flagged filings to print"),
    make_option("--output", default = NULL, help = "Optional CSV path for flagged filings")
  )
  opt <- optparse::parse_args(OptionParser(option_list = option_list),
                               args = commandArgs(trailingOnly = TRUE))
  if (is.null(opt$`num-path`) || is.null(opt$`sub-path`)) {
    stop("--num-path and --sub-path are required")
  }
  opt
}

fetch_data <- function(opt) {
  start <- Sys.time()

  sub <- read_parquet_multi(opt$`sub-path`, columns = c("adsh", "cik", "name", "fy", "fp", "form"))
  sub <- sub[sub$form == opt$form, ]

  num <- read_parquet_multi(opt$`num-path`, columns = c("adsh", "tag", "ddate", "qtrs", "uom", "coreg", "value"))
  num <- num[num$coreg == "", ]

  is_instant <- num$tag %in% INSTANT_TAGS & num$qtrs == 0
  is_duration <- num$tag %in% DURATION_TAGS & num$qtrs == 4
  num <- num[is_instant | is_duration, ]

  df <- merge(num, sub, by = "adsh")[, c("adsh", "cik", "name", "fy", "fp", "tag", "value")]

  elapsed <- as.numeric(difftime(Sys.time(), start, units = "secs"))
  list(df = df, elapsed = elapsed)
}

main <- function() {
  opt <- parse_cli_args()

  fetched <- fetch_data(opt)
  ad <- run_anomaly_detection(fetched$df, opt$`min-benford-obs`)
  print_results(fetched$elapsed, nrow(fetched$df), ad, opt,
                query_label = "Parquet read/query time: ")
}

if (is_main("panel_anomaly_detection_parquet.R")) {
  main()
}
