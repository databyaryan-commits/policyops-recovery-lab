
"""Register, validate, and stage daily policy snapshots."""

import argparse
import json
import re
from datetime import date
from pathlib import Path
from uuid import uuid4

import psycopg
from psycopg.types.json import Jsonb

from policyops.db import get_connection
from policyops.validate import validate_snapshot


def read_manifest_details(manifest_path: Path) -> dict:
    """Read available metadata without assuming the manifest is valid."""

    try:
        manifest = json.loads(
            manifest_path.read_text(encoding="utf-8")
        )
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}

    return manifest if isinstance(manifest, dict) else {}


def stage_snapshot(
    connection: psycopg.Connection,
    manifest_path: Path,
):
    """Validate a delivery and record its outcome in PostgreSQL."""

    report = validate_snapshot(manifest_path)
    manifest = read_manifest_details(manifest_path)
    run_id = uuid4()

    checksum = manifest.get("sha256")

    if not (
        isinstance(checksum, str)
        and re.fullmatch(r"[0-9a-f]{64}", checksum)
    ):
        checksum = None

    source_file = manifest.get("csv_file")

    if not (
        isinstance(source_file, str)
        and source_file.endswith(".csv")
        and Path(source_file).name == source_file
    ):
        source_file = None

    schema_version = manifest.get("schema_version")
    if not isinstance(schema_version, str):
        schema_version = None

    complete_snapshot = manifest.get("complete_snapshot")
    if not isinstance(complete_snapshot, bool):
        complete_snapshot = None

    snapshot_date = (
        date.fromisoformat(report.snapshot_date)
        if report.snapshot_date is not None
        else None
    )

    connection.execute(
        """
        INSERT INTO ingestion_runs (
            run_id,
            snapshot_date,
            source_file,
            checksum_sha256,
            schema_version,
            complete_snapshot,
            expected_row_count,
            source_count,
            accepted_count,
            status
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'registered')
        """,
        (
            run_id,
            snapshot_date,
            source_file,
            checksum,
            schema_version,
            complete_snapshot,
            report.expected_count,
            report.source_count,
            report.accepted_count,
        ),
    )

    if not report.passed:
        quality_rows = [
            (
                run_id,
                issue.check_id,
                issue.severity,
                issue.observed_value,
                issue.expected_rule,
                Jsonb({"manifest": manifest_path.name}),
            )
            for issue in report.issues
        ]

        with connection.cursor() as cursor:
            cursor.executemany(
                """
                INSERT INTO quality_events (
                    run_id,
                    check_id,
                    severity,
                    observed_value,
                    expected_rule,
                    evidence
                )
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                quality_rows,
            )

        connection.execute(
            """
            UPDATE ingestion_runs
            SET status = 'rejected',
                failure_reason = %s,
                finished_at = NOW()
            WHERE run_id = %s
            """,
            (
                f"{len(report.issues)} validation check(s) failed",
                run_id,
            ),
        )

        return run_id, report

    staged_rows = [
        (
            run_id,
            record.policy_id,
            record.customer_id,
            record.status,
            record.premium_amount,
            record.effective_date,
            record.source_updated_at,
        )
        for record in report.records
    ]

    if staged_rows:
        with connection.cursor() as cursor:
            cursor.executemany(
                """
                INSERT INTO policy_staging (
                    run_id,
                    policy_id,
                    customer_id,
                    status,
                    premium_amount,
                    effective_date,
                    source_updated_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                staged_rows,
            )

    connection.execute(
        """
        UPDATE ingestion_runs
        SET status = 'validated'
        WHERE run_id = %s
        """,
        (run_id,),
    )

    return run_id, report


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Validate and stage a policy snapshot"
    )
    parser.add_argument(
        "--manifest",
        required=True,
        type=Path,
        help="Path to the snapshot manifest",
    )

    args = parser.parse_args()

    try:
        with get_connection() as connection:
            run_id, report = stage_snapshot(
                connection,
                args.manifest,
            )
    except psycopg.Error:
        print(
            "Database staging failed. The current transaction "
            "was rolled back."
        )
        return 1

    if not report.passed:
        print(
            f"Rejected run {run_id}: "
            f"{len(report.issues)} validation check(s) failed"
        )
        return 1

    print(
        f"Validated and staged {report.accepted_count:,} policies "
        f"(run {run_id})"
    )
    print("Current policy state and history were not changed.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
