#!/usr/bin/env Rscript
# Point-in-time financial statement panel analysis + anomaly detection over
# the SEC Financial Statement Data Sets loaded by setup/download_and_load.py.
#
# For every 10-K filing (adsh) it reconstructs a wide company-year panel from
# the long `num` fact table, then flags anomalous filings using three
# independent signals:
#
#   - Altman Z''-Score  (bankruptcy / distress risk)
#   - Beneish M-Score   (earnings-manipulation risk, needs prior-year data)
#   - Benford's Law     (chi-square goodness-of-fit on leading digits)
#
# Reports the wall-clock time spent querying PostgreSQL separately from the
# wall-clock time spent on the in-memory anomaly detection.
#
# Requires: DBI, RPostgres, dplyr, tidyr, optparse
#
# Usage:
#   Rscript panel_anomaly_detection.R --dbname sec_financial_statements --top-n 25

script_dir <- function() {
  cmd_args <- commandArgs(trailingOnly = FALSE)
  file_arg <- grep("^--file=", cmd_args, value = TRUE)
  if (length(file_arg) == 1) return(dirname(normalizePath(sub("^--file=", "", file_arg))))
  getwd()
}

# Install renv (if missing) and restore the exact package versions pinned in
# r/renv.lock before loading anything, so the analysis always runs against a
# known-good dependency set.
if (!requireNamespace("renv", quietly = TRUE)) {
  install.packages("renv", repos = "https://cloud.r-project.org")
}
renv::restore(
  project = script_dir(),
  lockfile = file.path(script_dir(), "renv.lock"),
  prompt = FALSE
)

suppressPackageStartupMessages({
  library(DBI)
  library(RPostgres)
  library(dplyr)
  library(tidyr)
  library(optparse)
})

INSTANT_TAGS <- c(
  "Assets", "AssetsCurrent", "LiabilitiesCurrent", "Liabilities",
  "StockholdersEquity", "RetainedEarningsAccumulatedDeficit",
  "ReceivablesNetCurrent", "PropertyPlantAndEquipmentNet",
  "LongTermDebtNoncurrent"
)

DURATION_TAGS <- c(
  "OperatingIncomeLoss", "Revenues", "SalesRevenueNet",
  "CostOfGoodsAndServicesSold", "CostOfRevenue", "NetIncomeLoss",
  "DepreciationDepletionAndAmortization",
  "SellingGeneralAndAdministrativeExpense",
  "NetCashProvidedByUsedInOperatingActivities"
)

BENFORD_CHI2_CRITICAL_8DF <- 15.51 # p = 0.05, 8 degrees of freedom
ALTMAN_DISTRESS_THRESHOLD <- 1.23
BENEISH_MANIPULATION_THRESHOLD <- -2.22

# TRUE only when this file is the one Rscript was invoked with (not sourced)
is_main <- function(expected_basename) {
  cmd_args <- commandArgs(trailingOnly = FALSE)
  file_arg <- grep("^--file=", cmd_args, value = TRUE)
  if (length(file_arg) != 1) return(FALSE)
  basename(sub("^--file=", "", file_arg)) == expected_basename
}

ensure_columns <- function(df, names) {
  for (n in names) {
    if (!(n %in% names(df))) df[[n]] <- rep(NA_real_, nrow(df))
  }
  df
}

coalesce_zero <- function(x) ifelse(is.na(x), 0, x)

parse_cli_args <- function() {
  option_list <- list(
    make_option("--host", default = Sys.getenv("PG_DB_HOST", unset = "localhost")),
    make_option("--port", type = "integer", default = 5432),
    make_option("--user", default = "postgres"),
    make_option("--password", default = Sys.getenv("PG_DB_PW", unset = "postgres")),
    make_option("--dbname", default = "sec_financial_statements"),
    make_option("--form", default = "10-K", help = "Filing form type to analyze"),
    make_option("--min-benford-obs", type = "integer", default = 30,
                help = "Minimum reported facts per company to score with Benford's Law"),
    make_option("--top-n", type = "integer", default = 25,
                help = "Number of top flagged filings to print"),
    make_option("--output", default = NULL, help = "Optional CSV path for flagged filings")
  )
  optparse::parse_args(OptionParser(option_list = option_list),
                        args = commandArgs(trailingOnly = TRUE))
}

build_query <- function(form) {
  quote_arr <- function(tags) paste(sprintf("'%s'", tags), collapse = ", ")
  sprintf("
    SELECT s.adsh, s.cik, s.name, s.fy, s.fp, s.period,
           n.tag, n.value
    FROM num n
    JOIN sub s ON s.adsh = n.adsh
    WHERE s.form = '%s'
      AND n.coreg = ''
      AND (
            (n.tag = ANY(ARRAY[%s]) AND n.qtrs = 0)
         OR (n.tag = ANY(ARRAY[%s]) AND n.qtrs = 4)
          )
  ", form, quote_arr(INSTANT_TAGS), quote_arr(DURATION_TAGS))
}

fetch_data <- function(opt) {
  start <- Sys.time()
  con <- dbConnect(
    RPostgres::Postgres(), host = opt$host, port = opt$port,
    user = opt$user, password = opt$password, dbname = opt$dbname
  )
  df <- dbGetQuery(con, build_query(opt$form))
  dbDisconnect(con)
  elapsed <- as.numeric(difftime(Sys.time(), start, units = "secs"))
  list(df = df, elapsed = elapsed)
}

build_panel <- function(df) {
  wide <- df %>%
    tidyr::pivot_wider(
      id_cols = c(adsh, cik, name, fy, fp),
      names_from = tag, values_from = value, values_fn = dplyr::last
    )
  wide <- ensure_columns(wide, c(INSTANT_TAGS, DURATION_TAGS))
  wide$revenue <- dplyr::coalesce(wide$Revenues, wide$SalesRevenueNet)
  wide$cogs <- dplyr::coalesce(wide$CostOfGoodsAndServicesSold, wide$CostOfRevenue)
  wide
}

compute_altman_z <- function(wide) {
  assets <- wide$Assets
  assets[assets == 0] <- NA
  working_capital <- wide$AssetsCurrent - wide$LiabilitiesCurrent
  retained_earnings <- wide$RetainedEarningsAccumulatedDeficit
  ebit <- wide$OperatingIncomeLoss
  equity <- wide$StockholdersEquity
  liabilities <- wide$Liabilities
  liabilities[liabilities == 0] <- NA

  wide$z_score <- (
    0.717 * (working_capital / assets)
    + 0.847 * (retained_earnings / assets)
    + 3.107 * (ebit / assets)
    + 0.420 * (equity / liabilities)
    + 0.998 * (wide$revenue / assets)
  )
  wide
}

compute_beneish_m <- function(wide) {
  w <- wide %>%
    arrange(cik, fy) %>%
    group_by(cik) %>%
    mutate(
      Assets_prev = dplyr::lag(Assets),
      revenue_prev = dplyr::lag(revenue),
      cogs_prev = dplyr::lag(cogs),
      ReceivablesNetCurrent_prev = dplyr::lag(ReceivablesNetCurrent),
      AssetsCurrent_prev = dplyr::lag(AssetsCurrent),
      PropertyPlantAndEquipmentNet_prev = dplyr::lag(PropertyPlantAndEquipmentNet),
      DepreciationDepletionAndAmortization_prev = dplyr::lag(DepreciationDepletionAndAmortization),
      SellingGeneralAndAdministrativeExpense_prev = dplyr::lag(SellingGeneralAndAdministrativeExpense),
      NetIncomeLoss_prev = dplyr::lag(NetIncomeLoss),
      NetCashProvidedByUsedInOperatingActivities_prev = dplyr::lag(NetCashProvidedByUsedInOperatingActivities),
      LiabilitiesCurrent_prev = dplyr::lag(LiabilitiesCurrent),
      LongTermDebtNoncurrent_prev = dplyr::lag(LongTermDebtNoncurrent)
    ) %>%
    ungroup() %>%
    as.data.frame()

  assets <- w$Assets; assets[assets == 0] <- NA
  assets_prev <- w$Assets_prev; assets_prev[assets_prev == 0] <- NA
  sales <- w$revenue
  sales_prev <- w$revenue_prev

  dsri <- (w$ReceivablesNetCurrent / sales) / (w$ReceivablesNetCurrent_prev / sales_prev)

  gm <- (sales - w$cogs) / sales
  gm_prev <- (sales_prev - w$cogs_prev) / sales_prev
  gmi <- gm_prev / gm

  aqi_cur <- 1 - (coalesce_zero(w$AssetsCurrent) + coalesce_zero(w$PropertyPlantAndEquipmentNet)) / assets
  aqi_prev <- 1 - (coalesce_zero(w$AssetsCurrent_prev) + coalesce_zero(w$PropertyPlantAndEquipmentNet_prev)) / assets_prev
  aqi <- aqi_cur / aqi_prev

  sgi <- sales / sales_prev

  depi_cur <- w$DepreciationDepletionAndAmortization / (w$DepreciationDepletionAndAmortization + w$PropertyPlantAndEquipmentNet)
  depi_prev <- w$DepreciationDepletionAndAmortization_prev / (w$DepreciationDepletionAndAmortization_prev + w$PropertyPlantAndEquipmentNet_prev)
  depi <- depi_prev / depi_cur

  sgai <- (w$SellingGeneralAndAdministrativeExpense / sales) / (w$SellingGeneralAndAdministrativeExpense_prev / sales_prev)

  tata <- (w$NetIncomeLoss - w$NetCashProvidedByUsedInOperatingActivities) / assets

  lvgi_cur <- (coalesce_zero(w$LiabilitiesCurrent) + coalesce_zero(w$LongTermDebtNoncurrent)) / assets
  lvgi_prev <- (coalesce_zero(w$LiabilitiesCurrent_prev) + coalesce_zero(w$LongTermDebtNoncurrent_prev)) / assets_prev
  lvgi <- lvgi_cur / lvgi_prev

  w$m_score <- (
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
  w
}

compute_benford <- function(df, min_obs) {
  d <- df[!is.na(df$value) & df$value != 0, c("cik", "value")]
  d$abs_value <- abs(d$value)
  d <- d[d$abs_value >= 1, ]
  d$first_digit <- pmin(pmax(as.integer(d$abs_value / 10 ^ floor(log10(d$abs_value))), 1), 9)

  counts <- d %>%
    dplyr::count(cik, first_digit) %>%
    tidyr::pivot_wider(names_from = first_digit, values_from = n, values_fill = 0)

  digit_cols <- as.character(1:9)
  for (col_name in digit_cols) {
    if (!(col_name %in% names(counts))) counts[[col_name]] <- 0
  }

  totals <- rowSums(counts[digit_cols])
  expected_props <- setNames(sapply(1:9, function(digit) log10(1 + 1 / digit)), digit_cols)
  expected_counts <- outer(totals, expected_props)

  obs <- as.matrix(counts[digit_cols])
  chi2 <- rowSums((obs - expected_counts) ^ 2 / expected_counts)

  result <- data.frame(cik = counts$cik, benford_chi2 = chi2, benford_n = totals)
  result[result$benford_n >= min_obs, ]
}

run_anomaly_detection <- function(df, min_benford_obs) {
  start <- Sys.time()

  wide <- build_panel(df)
  wide <- compute_altman_z(wide)
  wide <- compute_beneish_m(wide)
  benford <- compute_benford(df, min_benford_obs)

  result <- dplyr::left_join(wide, benford, by = "cik")
  result$flag_distress <- !is.na(result$z_score) & result$z_score < ALTMAN_DISTRESS_THRESHOLD
  result$flag_manipulation <- !is.na(result$m_score) & result$m_score > BENEISH_MANIPULATION_THRESHOLD
  result$flag_benford <- !is.na(result$benford_chi2) & result$benford_chi2 > BENFORD_CHI2_CRITICAL_8DF
  result$anomaly_count <- rowSums(cbind(result$flag_distress, result$flag_manipulation, result$flag_benford))

  elapsed <- as.numeric(difftime(Sys.time(), start, units = "secs"))
  list(result = result, elapsed = elapsed)
}

print_results <- function(query_time, n_facts, ad, opt, query_label = "Database query time:     ") {
  cat(sprintf("%s %8.2fs (%s facts fetched)\n",
              query_label, query_time, format(n_facts, big.mark = ",")))
  cat(sprintf("Anomaly detection time:    %8.2fs (%s filings scored)\n",
              ad$elapsed, format(nrow(ad$result), big.mark = ",")))
  cat(sprintf("Total time:                %8.2fs\n", query_time + ad$elapsed))

  flagged <- ad$result[ad$result$anomaly_count > 0, ]
  flagged <- flagged[order(-flagged$anomaly_count, -flagged$m_score), ]
  cat(sprintf("\n%s filings flagged by at least one signal\n\n", format(nrow(flagged), big.mark = ",")))

  display_cols <- c("adsh", "cik", "name", "fy", "fp", "z_score", "m_score", "benford_chi2", "anomaly_count")
  print(utils::head(flagged[, display_cols], opt$`top-n`), row.names = FALSE)

  if (!is.null(opt$output)) {
    utils::write.csv(flagged, opt$output, row.names = FALSE)
    cat(sprintf("\nFlagged filings written to %s\n", opt$output))
  }
}

main <- function() {
  opt <- parse_cli_args()

  fetched <- fetch_data(opt)
  ad <- run_anomaly_detection(fetched$df, opt$`min-benford-obs`)
  print_results(fetched$elapsed, nrow(fetched$df), ad, opt)
}

if (is_main("panel_anomaly_detection.R")) {
  main()
}
