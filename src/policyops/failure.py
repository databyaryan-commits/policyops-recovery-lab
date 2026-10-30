
"""Record failed ingestion attempts after their transactions roll back."""

import re
from datetime import date
from pathlib import Path
from uuid import UUID, uuid4

import psycopg
from psycopg.types.json import Jsonb

from policyops.load import read_manifest_details


def read_failure_source(manifest_path: Path) -> dict:
    """Extract usable source metadata without trusting the manifest."""

    manifest = read_manifest_details(manifest_path)

    raw_date = manifest.get("snapshot_date")
    snapshot_date = None

    if isinstance(raw_date, str):
        try:
            snapshot_date = date.fromisoformat(raw_date)
        except ValueError:
            pass

    source_file = manifest.get("csv_file")

    if not (
        isinstance(source_file, str)
        and source_file.endswith(".csv")
        and Path(source_file).name == source_file
    ):
        source_file = None

    checksum = manifest.get("sha256")

    if not (
        isinstance(checksum, str)
        and re.fullmatch(r"[0-9a-f]{64}", checksum)
    ):
        checksum = None

    schema_version = manifest.get("schema_version")

    if not isinstance(schema_version, str):
        schema_version = None

    complete_snapshot = manifest.get("complete_snapshot")

    if not isinstance(complete_snapshot, bool):
        complete_snapshot = None

    expected_count = manifest.get("expected_row_count")

    if (
        isinstance(expected_count, bool)
        or not isinstance(expected_count, int)
        or expected_count < 0
    ):
        expected_count = None

    return {
        "snapshot_date": snapshot_date,
        "source_file": source_file,
        "checksum_sha256": checksum,
        "schema_version": schema_version,
        "complete_snapshot": complete_snapshot,
        "expected_row_count": expected_count,
    }


def record_failed_attempt(
    connection: psycopg.Connection,
    manifest_path: Path,
    reason: str,
    *,
    stage: str = "ingestion",
) -> UUID:
    """Store a failed attempt and its quality event together.

    Call this using a fresh connection after the failed
    processing transaction has been rolled back. The caller
    must commit this recording transaction.
    """

    if connection.autocommit:
        raise ValueError(
            "Failure recording requires a database transaction."
        )

    if not isinstance(reason, str) or not reason.strip():
        raise ValueError(
            "A meaningful failure reason is required."
        )

    if stage not in {"ingestion", "promotion"}:
        raise ValueError(
            "Failure stage must be 'ingestion' or 'promotion'."
        )

    source = read_failure_source(manifest_path)
    run_id = uuid4()
    failure_reason = reason.strip()

    # Failed attempts are separate from successful snapshot
    # commits. They do not stage or modify business records.
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
            status,
            finished_at,
            failure_reason
        )
        VALUES (
            %s, %s, %s, %s, %s, %s, %s,
            'failed', NOW(), %s
        )
        """,
        (
            run_id,
            source["snapshot_date"],
            source["source_file"],
            source["checksum_sha256"],
            source["schema_version"],
            source["complete_snapshot"],
            source["expected_row_count"],
            failure_reason,
        ),
    )

    # The run and its incident evidence are part of this
    # recording transaction, so they commit or roll back
    # together.
    connection.execute(
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
        (
            run_id,
            "PROCESSING_TRANSACTION_FAILED",
            "error",
            failure_reason,
            (
                "Failed processing must not leave partial "
                "business changes or audit history."
            ),
            Jsonb(
                {
                    "manifest": manifest_path.name,
                    "stage": stage,
                }
            ),
        ),
    )

    return run_id
