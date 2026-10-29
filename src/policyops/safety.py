"""Check whether a policy snapshot needs manual deletion review."""

from dataclasses import dataclass
from decimal import Decimal
from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb

from policyops.changes import ChangeCounts, compare_snapshot


MAX_AUTOMATIC_DELETION_RATE = Decimal("0.20")


@dataclass(frozen=True)
class ReviewDecision:
    run_id: UUID
    changes: ChangeCounts
    active_before: int
    deletion_rate: Decimal
    needs_review: bool


def evaluate_deletion_risk(
    connection: psycopg.Connection,
    run_id: UUID,
) -> ReviewDecision:
    """Record change counts and flag unusually large deletions."""

    # Lock this ingestion run while its review decision is recorded.
    run = connection.execute(
        """
        SELECT status
        FROM ingestion_runs
        WHERE run_id = %s
        FOR UPDATE
        """,
        (run_id,),
    ).fetchone()

    if run is None:
        raise ValueError("The requested ingestion run does not exist.")

    if run[0] != "validated":
        raise ValueError(
            f"Cannot evaluate a run with status '{run[0]}'."
        )

    changes = compare_snapshot(connection, run_id)

    active_before = connection.execute(
        """
        SELECT COUNT(*)
        FROM policy_current
        WHERE is_deleted = FALSE
        """
    ).fetchone()[0]

    if active_before == 0:
        deletion_rate = Decimal("0")
    else:
        deletion_rate = (
            Decimal(changes.deleted) / Decimal(active_before)
        )

    needs_review = (
        deletion_rate > MAX_AUTOMATIC_DELETION_RATE
    )

    next_status = (
        "awaiting_review" if needs_review else "validated"
    )

    # Store the comparison result so every ingestion attempt
    # has a record of the changes it would make.
    connection.execute(
        """
        UPDATE ingestion_runs
        SET new_count = %s,
            changed_count = %s,
            deleted_count = %s,
            unchanged_count = %s,
            status = %s
        WHERE run_id = %s
        """,
        (
            changes.new,
            changes.changed,
            changes.deleted,
            changes.unchanged,
            next_status,
            run_id,
        ),
    )

    if needs_review:
        deletion_percentage = deletion_rate * 100

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
                "DELETION_RATE_REVIEW_REQUIRED",
                "warning",
                f"{deletion_percentage:.2f}%",
                "Deletion rates above 20% require manual approval.",
                Jsonb(
                    {
                        "active_before": active_before,
                        "deleted_count": changes.deleted,
                        "deletion_rate": str(deletion_rate),
                        "threshold": str(
                            MAX_AUTOMATIC_DELETION_RATE
                        ),
                    }
                ),
            ),
        )

    # The decision and its evidence are written together.
    # Final promotion will need to recheck the risk before
    # changing current policy records.
    return ReviewDecision(
        run_id=run_id,
        changes=changes,
        active_before=active_before,
        deletion_rate=deletion_rate,
        needs_review=needs_review,
    )