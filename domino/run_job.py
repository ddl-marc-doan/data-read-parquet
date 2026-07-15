#!/usr/bin/env python3
"""
Kick off one of this repo's scripts as a Domino Job via the Domino API,
using the official python-domino client (`pip install dominodatalab`).

Follows the same pattern as job_trainer_xgb.py in
https://github.com/ddl-marc-doan/Fraud-Detection-Workshop/tree/main/exercises/c_TrainingAndEvaluation
(connect with `Domino(...)`, then `domino.job_start(...)`), adapted to launch
any script in this repo (export_to_parquet.py, panel_anomaly_detection.py,
their .R equivalents, etc.) with arbitrary arguments.

Connection info is read from the same environment variables Domino
auto-injects into every workspace/job:
  DOMINO_PROJECT_OWNER, DOMINO_PROJECT_NAME, DOMINO_USER_API_KEY,
  DOMINO_API_HOST
These can be overridden with --project-owner/--project-name/--api-key/--host
for launching a job from outside a Domino execution (e.g. a laptop).

Usage:
  # Run inside a Domino workspace/job in this project:
  python domino/run_job.py --script python/panel_anomaly_detection.py \\
      --script-args "--dbname sec_financial_statements --top-n 25" \\
      --hardware-tier Medium --wait

  # Run from outside Domino:
  python domino/run_job.py --project-owner myuser --project-name my-project \\
      --api-key $DOMINO_USER_API_KEY --host https://mydomino.example.com \\
      --script python/export_to_parquet.py \\
      --script-args "--output-dir /mnt/data/parquet"
"""
import argparse
import os
import sys
import time

from domino import Domino


def parse_args():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--project-owner", default=os.environ.get("DOMINO_PROJECT_OWNER"),
                    help="Defaults to $DOMINO_PROJECT_OWNER")
    p.add_argument("--project-name", default=os.environ.get("DOMINO_PROJECT_NAME"),
                    help="Defaults to $DOMINO_PROJECT_NAME")
    p.add_argument("--api-key", default=os.environ.get("DOMINO_USER_API_KEY"),
                    help="Defaults to $DOMINO_USER_API_KEY")
    p.add_argument("--host", default=os.environ.get("DOMINO_API_HOST"),
                    help="Defaults to $DOMINO_API_HOST")
    p.add_argument("--script", required=True,
                    help="Path (relative to the project root) of the script to run, "
                         "e.g. python/panel_anomaly_detection.py or r/export_to_parquet.R")
    p.add_argument("--script-args", default="",
                    help="Arguments to pass to --script, as a single string, "
                         "e.g. \"--dbname sec_financial_statements --top-n 25\"")
    p.add_argument("--title", default=None,
                    help="Job title shown in the Domino UI (defaults to the script name)")
    p.add_argument("--hardware-tier", default=None,
                    help="Hardware tier name to run the job on (e.g. Small, Medium, Large)")
    p.add_argument("--commit-id", default=None,
                    help="Git commit to run the job against (defaults to latest)")
    p.add_argument("--wait", action="store_true",
                    help="Poll until the job reaches a terminal status before exiting")
    p.add_argument("--poll-interval", type=float, default=10.0,
                    help="Seconds between status checks when --wait is set")
    return p.parse_args()


def build_command(args):
    interpreter = "Rscript" if args.script.endswith(".R") else "python"
    command = f"{interpreter} {args.script}"
    if args.script_args:
        command = f"{command} {args.script_args}"
    return command


def main():
    args = parse_args()

    missing = [
        name for name, value in [
            ("--project-owner", args.project_owner),
            ("--project-name", args.project_name),
            ("--api-key", args.api_key),
            ("--host", args.host),
        ] if not value
    ]
    if missing:
        sys.exit(f"Missing required connection info: {', '.join(missing)}")

    domino = Domino(
        f"{args.project_owner}/{args.project_name}",
        api_key=args.api_key,
        host=args.host,
    )

    command = build_command(args)
    title = args.title or f"Run {os.path.basename(args.script)}"

    print(f"Starting Domino job: {command}")
    run = domino.job_start(
        command=command,
        title=title,
        commit_id=args.commit_id,
        hardware_tier_name=args.hardware_tier,
    )
    job_id = run["id"]
    print(f"Job started: id={job_id}")

    if not args.wait:
        return

    print("Waiting for job to complete...")
    while True:
        status = domino.job_status(job_id)
        statuses = status.get("statuses", {})
        execution_status = statuses.get("executionStatus")
        print(f"  status={execution_status}")
        if statuses.get("isCompleted"):
            break
        time.sleep(args.poll_interval)

    if execution_status != "Succeeded":
        sys.exit(f"Job {job_id} finished with status {execution_status}")
    print(f"Job {job_id} succeeded")


if __name__ == "__main__":
    main()
