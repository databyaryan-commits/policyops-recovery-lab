"""Record manual approval for policy snapshots awaiting review."""

import argparse
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

import psycopg

from policyops.db import get_connection


@dataclass(frozen=True)
class ApprovalRecord:
    run_id: UUID
    reviewer: str
    reason: str
    approved_at: datetime


def approve_snapshot(
    connection: psycopg.Connection,
    run_id: UUID,
    reviewer: str,
    reason: str,
) -> ApprovalRecord:
    """Record a reviewer's approval without promoting policy data."""

    if not isinstance(reviewer, str) or not reviewer.strip():
        raise ValueError("A reviewer name is required.")

    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("A written approval reason is required.")

    reviewer = reviewer.strip()
    reason = reason.strip()

    # Lock the run so two approval attempts cannot update it
    # at the same time.
    run = connection.execute(
        """
        SELECT status, approved_at
        FROM ingestion_runs
        WHERE run_id = %s
        FOR UPDATE
        """,
        (run_id,),
    ).fetchone()

    if run is None:
        raise ValueError("The requested ingestion run does not exist.")

    status, approved_at = run

    if status != "awaiting_review":
        raise ValueError(
            f"Cannot approve a run with status '{status}'."
        )

    if approved_at is not None:
        raise ValueError("This ingestion run has already been approved.")

    # Approval is recorded on the ingestion run itself.
    # This does not update current policies or audit history.
    recorded_approval = connection.execute(
        """
        UPDATE ingestion_runs
        SET approved_by = %s,
            approval_reason = %s,
            approved_at = NOW()
        WHERE run_id = %s
        RETURNING approved_at
        """,
        (
            reviewer,
            reason,
            run_id,
        ),
    ).fetchone()

    return ApprovalRecord(
        run_id=run_id,
        reviewer=reviewer,
        reason=reason,
        approved_at=recorded_approval[0],
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Record manual approval for a flagged policy snapshot"
    )

    parser.add_argument(
        "--run-id",
        required=True,
        type=UUID,
        help="Ingestion run ID requiring approval",
    )

    parser.add_argument(
        "--reviewer",
        required=True,
        help="Name of the person recording approval",
    )

    parser.add_argument(
        "--reason",
        required=True,
        help="Written explanation for approving the delivery",
    )

    args = parser.parse_args()

    try:
        with get_connection() as connection:
            approval = approve_snapshot(
                connection=connection,
                run_id=args.run_id,
                reviewer=args.reviewer,
                reason=args.reason,
            )

    except ValueError as error:
        print(f"Approval not recorded: {error}")
        return 1

    except psycopg.Error:
        print("Database error. The approval transaction was rolled back.")
        return 1

    print(f"Approval recorded for run {approval.run_id}")
    print(f"Reviewer: {approval.reviewer}")
    print(f"Reason: {approval.reason}")
    print(f"Approved at: {approval.approved_at}")
    print("Current policies and audit history were not changed.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())