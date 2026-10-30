
"""Run validation, staging, review checks, and safe policy promotion."""

import argparse
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Literal
from uuid import UUID

import psycopg

from policyops.changes import ChangeCounts
from policyops.db import get_connection
from policyops.load import read_manifest_details, stage_snapshot
from policyops.promote import promote_snapshot
from policyops.replay import check_snapshot_identity
from policyops.safety import evaluate_deletion_risk
from policyops.validate import validate_snapshot


IngestionOutcome = Literal[
    "committed",
    "replayed",
    "awaiting_review",
    "rejected",
    "date_conflict",
    "out_of_order",
]


@dataclass(frozen=True)
class IngestionResult:
    outcome: IngestionOutcome
    run_id: UUID | None
    accepted_count: int = 0
    changes: ChangeCounts | None = None
    detail: str = ""


def ingest_snapshot(
    connection: psycopg.Connection,
    manifest_path: Path,
) -> IngestionResult:
    """Process a delivery without repeating committed business changes."""

    if connection.autocommit:
        raise ValueError(
            "Ingestion requires a database transaction."
        )

    # Validate the source before checking its logical identity.
    # An invalid manifest must never bypass validation just
    # because it resembles an earlier committed delivery.
    initial_report = validate_snapshot(manifest_path)

    with connection.transaction():
        # Use the same table lock as promotion. This serializes
        # ingestion and promotion while the source identity,
        # current policies, and audit writes are checked.
        connection.execute(
            """
            LOCK TABLE policy_current, policy_staging
            IN SHARE ROW EXCLUSIVE MODE
            """
        )

        expected_identity = None

        if initial_report.passed:
            manifest = read_manifest_details(manifest_path)

            snapshot_date = date.fromisoformat(
                initial_report.snapshot_date
            )
            checksum = manifest["sha256"]

            expected_identity = (snapshot_date, checksum)

            replay = check_snapshot_identity(
                connection,
                snapshot_date,
                checksum,
            )

            if replay.outcome == "already_committed":
                return IngestionResult(
                    outcome="replayed",
                    run_id=replay.existing_run_id,
                    accepted_count=initial_report.accepted_count,
                    detail=(
                        "This exact snapshot was already committed. "
                        "No new ingestion run or business changes "
                        "were created."
                    ),
                )

            if replay.outcome == "date_conflict":
                return IngestionResult(
                    outcome="date_conflict",
                    run_id=replay.existing_run_id,
                    detail=(
                        "A different snapshot has already been "
                        "committed for this date."
                    ),
                )

            if replay.outcome == "out_of_order":
                return IngestionResult(
                    outcome="out_of_order",
                    run_id=None,
                    detail=(
                        "A newer snapshot has already been committed "
                        f"on {replay.last_committed_date}."
                    ),
                )

        # The original staging function remains responsible
        # for registration, validation evidence, and staging.
        run_id, report = stage_snapshot(
            connection,
            manifest_path,
        )

        if not report.passed:
            return IngestionResult(
                outcome="rejected",
                run_id=run_id,
                accepted_count=report.accepted_count,
                detail=(
                    f"{len(report.issues)} validation check(s) failed."
                ),
            )

        # Check that the source identity did not change
        # between the preliminary check and registration.
        staged_identity = connection.execute(
            """
            SELECT snapshot_date, checksum_sha256
            FROM ingestion_runs
            WHERE run_id = %s
            """,
            (run_id,),
        ).fetchone()

        if staged_identity != expected_identity:
            raise ValueError(
                "Source identity changed during ingestion. "
                "Retry with unchanged source files."
            )

        review = evaluate_deletion_risk(
            connection,
            run_id,
        )

        if review.needs_review:
            # The staged data and approval requirement are saved.
            # Nothing has been promoted to current or history.
            return IngestionResult(
                outcome="awaiting_review",
                run_id=run_id,
                accepted_count=report.accepted_count,
                changes=review.changes,
                detail=(
                    "The deletion rate exceeds 20%. "
                    "Manual approval is required before promotion."
                ),
            )

        # For normal deliveries, the existing promotion function
        # applies current records and audit history together.
        promoted = promote_snapshot(
            connection,
            run_id,
        )

        return IngestionResult(
            outcome="committed",
            run_id=run_id,
            accepted_count=report.accepted_count,
            changes=promoted.changes,
            detail="Snapshot committed successfully.",
        )


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Ingest a policy snapshot with replay detection, "
            "deletion review, and transactional promotion"
        )
    )

    parser.add_argument(
        "--manifest",
        required=True,
        type=Path,
        help="Path to the source snapshot manifest",
    )

    args = parser.parse_args()

    try:
        with get_connection() as connection:
            result = ingest_snapshot(
                connection,
                args.manifest,
            )

    except ValueError as error:
        print(f"Ingestion blocked: {error}")
        return 1

    except psycopg.Error:
        print(
            "Database ingestion failed. "
            "The transaction was rolled back."
        )
        return 1

    if result.outcome == "replayed":
        print(f"Already committed as run {result.run_id}")
        print(result.detail)
        return 0

    if result.outcome in {"date_conflict", "out_of_order"}:
        print(f"Ingestion blocked: {result.detail}")
        return 1

    if result.outcome == "rejected":
        print(f"Rejected run {result.run_id}")
        print(result.detail)
        return 1

    if result.outcome == "awaiting_review":
        print(f"Staged run {result.run_id}")
        print(f"Accepted policies: {result.accepted_count:,}")
        print(result.detail)
        print(
            "Record approval with policyops.approve, "
            "then promote this run with policyops.promote."
        )
        return 0

    print(f"Committed run {result.run_id}")

    if result.changes is not None:
        print(f"New policies: {result.changes.new:,}")
        print(f"Changed policies: {result.changes.changed:,}")
        print(f"Deleted policies: {result.changes.deleted:,}")
        print(f"Unchanged policies: {result.changes.unchanged:,}")

    print(result.detail)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
