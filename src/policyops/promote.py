"""Apply a validated policy snapshot in one database transaction."""

import argparse
from dataclasses import dataclass
from decimal import Decimal
from uuid import UUID

import psycopg

from policyops.changes import ChangeCounts, compare_snapshot
from policyops.db import get_connection
from policyops.safety import MAX_AUTOMATIC_DELETION_RATE


@dataclass(frozen=True)
class PromotionResult:
    run_id: UUID
    changes: ChangeCounts
    active_after: int


def write_change_history(
    connection: psycopg.Connection,
    run_id: UUID,
) -> int:
    """Record business changes before updating current policy state."""

    cursor = connection.execute(
        """
        WITH incoming AS (
            SELECT *
            FROM policy_staging
            WHERE run_id = %s
        ),
        differences AS (
            SELECT
                COALESCE(
                    incoming.policy_id,
                    current_policy.policy_id
                ) AS policy_id,

                CASE
                    WHEN incoming.policy_id IS NULL
                        THEN 'delete'

                    WHEN current_policy.policy_id IS NULL
                         OR current_policy.is_deleted = TRUE
                        THEN 'insert'

                    WHEN ROW(
                        incoming.customer_id,
                        incoming.status,
                        incoming.premium_amount,
                        incoming.effective_date
                    ) IS DISTINCT FROM ROW(
                        current_policy.customer_id,
                        current_policy.status,
                        current_policy.premium_amount,
                        current_policy.effective_date
                    )
                        THEN 'update'

                    ELSE NULL
                END AS change_type,

                CASE
                    WHEN current_policy.policy_id IS NULL
                        THEN NULL
                    ELSE jsonb_build_object(
                        'policy_id', current_policy.policy_id,
                        'customer_id', current_policy.customer_id,
                        'status', current_policy.status,
                        'premium_amount', current_policy.premium_amount,
                        'effective_date', current_policy.effective_date,
                        'source_updated_at',
                            current_policy.source_updated_at,
                        'is_deleted', current_policy.is_deleted
                    )
                END AS old_values,

                CASE
                    WHEN incoming.policy_id IS NULL
                        THEN NULL
                    ELSE jsonb_build_object(
                        'policy_id', incoming.policy_id,
                        'customer_id', incoming.customer_id,
                        'status', incoming.status,
                        'premium_amount', incoming.premium_amount,
                        'effective_date', incoming.effective_date,
                        'source_updated_at', incoming.source_updated_at,
                        'is_deleted', FALSE
                    )
                END AS new_values

            FROM incoming

            FULL OUTER JOIN policy_current AS current_policy
                ON incoming.policy_id = current_policy.policy_id

            WHERE incoming.policy_id IS NOT NULL
               OR current_policy.is_deleted = FALSE
        )
        INSERT INTO policy_history (
            run_id,
            policy_id,
            change_type,
            old_values,
            new_values
        )
        SELECT
            %s,
            policy_id,
            change_type,
            old_values,
            new_values
        FROM differences
        WHERE change_type IS NOT NULL
        """,
        (run_id, run_id),
    )

    return cursor.rowcount


def update_current_policies(
    connection: psycopg.Connection,
    run_id: UUID,
    snapshot_date,
) -> int:
    """Insert new policies and refresh policies present in the snapshot."""

    cursor = connection.execute(
        """
        INSERT INTO policy_current (
            policy_id,
            customer_id,
            status,
            premium_amount,
            effective_date,
            source_updated_at,
            is_deleted,
            latest_run_id,
            last_seen_snapshot_date
        )
        SELECT
            policy_id,
            customer_id,
            status,
            premium_amount,
            effective_date,
            source_updated_at,
            FALSE,
            run_id,
            %s
        FROM policy_staging
        WHERE run_id = %s

        ON CONFLICT (policy_id)
        DO UPDATE SET
            customer_id = EXCLUDED.customer_id,
            status = EXCLUDED.status,
            premium_amount = EXCLUDED.premium_amount,
            effective_date = EXCLUDED.effective_date,
            source_updated_at = EXCLUDED.source_updated_at,
            is_deleted = FALSE,
            latest_run_id = EXCLUDED.latest_run_id,
            last_seen_snapshot_date =
                EXCLUDED.last_seen_snapshot_date
        """,
        (snapshot_date, run_id),
    )

    return cursor.rowcount


def mark_missing_policies_deleted(
    connection: psycopg.Connection,
    run_id: UUID,
) -> int:
    """Soft-delete active policies missing from the incoming snapshot."""

    cursor = connection.execute(
        """
        UPDATE policy_current AS current_policy
        SET is_deleted = TRUE,
            latest_run_id = %s
        WHERE current_policy.is_deleted = FALSE
          AND NOT EXISTS (
              SELECT 1
              FROM policy_staging AS incoming
              WHERE incoming.run_id = %s
                AND incoming.policy_id = current_policy.policy_id
          )
        """,
        (run_id, run_id),
    )

    return cursor.rowcount


def promote_snapshot(
    connection: psycopg.Connection,
    run_id: UUID,
) -> PromotionResult:
    """Recheck the safeguards, then apply one snapshot atomically."""

    if connection.autocommit:
        raise ValueError(
            "Promotion requires a database transaction."
        )

    # Serialize changes to current policy state and staging
    # while this promotion is checked and applied.
    connection.execute(
        """
        LOCK TABLE policy_current, policy_staging
        IN SHARE ROW EXCLUSIVE MODE
        """
    )

    run = connection.execute(
        """
        SELECT
            status,
            snapshot_date,
            complete_snapshot,
            new_count,
            changed_count,
            deleted_count,
            unchanged_count,
            approved_at
        FROM ingestion_runs
        WHERE run_id = %s
        FOR UPDATE
        """,
        (run_id,),
    ).fetchone()

    if run is None:
        raise ValueError("The requested ingestion run does not exist.")

    (
        status,
        snapshot_date,
        complete_snapshot,
        new_count,
        changed_count,
        deleted_count,
        unchanged_count,
        approved_at,
    ) = run

    if status not in {"validated", "awaiting_review"}:
        raise ValueError(
            f"Cannot promote a run with status '{status}'."
        )

    if snapshot_date is None or complete_snapshot is not True:
        raise ValueError(
            "Promotion requires a dated, complete snapshot."
        )

    recorded_counts = (
        new_count,
        changed_count,
        deleted_count,
        unchanged_count,
    )

    if any(count is None for count in recorded_counts):
        raise ValueError(
            "Run the deletion safety assessment before promotion."
        )

    previous_date = connection.execute(
        """
        SELECT MAX(snapshot_date)
        FROM ingestion_runs
        WHERE status = 'committed'
        """
    ).fetchone()[0]

    if previous_date is not None and snapshot_date <= previous_date:
        raise ValueError(
            "The snapshot must be newer than the last committed snapshot."
        )

    # Do not rely only on the earlier safety assessment.
    # Recompute the comparison while current policy state is locked.
    changes = compare_snapshot(connection, run_id)

    if changes != ChangeCounts(*recorded_counts):
        raise ValueError(
            "Policy changes differ from the recorded safety assessment."
        )

    active_before = connection.execute(
        """
        SELECT COUNT(*)
        FROM policy_current
        WHERE is_deleted = FALSE
        """
    ).fetchone()[0]

    deletion_rate = (
        Decimal(changes.deleted) / Decimal(active_before)
        if active_before
        else Decimal("0")
    )

    approval_required = (
        deletion_rate > MAX_AUTOMATIC_DELETION_RATE
    )

    if status == "awaiting_review" and approved_at is None:
        raise ValueError(
            "This snapshot is awaiting manual approval."
        )

    if approval_required and (
        status != "awaiting_review" or approved_at is None
    ):
        raise ValueError(
            "The deletion rate requires a recorded manual approval."
        )

    expected_history_count = (
        changes.new + changes.changed + changes.deleted
    )

    history_count = write_change_history(
        connection,
        run_id,
    )

    if history_count != expected_history_count:
        raise ValueError(
            "Audit history does not match the expected changes."
        )

    updated_count = update_current_policies(
        connection,
        run_id,
        snapshot_date,
    )

    expected_staged_count = (
        changes.new + changes.changed + changes.unchanged
    )

    if updated_count != expected_staged_count:
        raise ValueError(
            "Current policy updates do not match the staged snapshot."
        )

    deleted_count_applied = mark_missing_policies_deleted(
        connection,
        run_id,
    )

    if deleted_count_applied != changes.deleted:
        raise ValueError(
            "The number of deleted policies does not match the review."
        )

    active_after = connection.execute(
        """
        SELECT COUNT(*)
        FROM policy_current
        WHERE is_deleted = FALSE
        """
    ).fetchone()[0]

    if active_after != expected_staged_count:
        raise ValueError(
            "Active policy count does not match the incoming snapshot."
        )

    connection.execute(
        """
        UPDATE ingestion_runs
        SET status = 'committed',
            finished_at = NOW()
        WHERE run_id = %s
        """,
        (run_id,),
    )

    return PromotionResult(
        run_id=run_id,
        changes=changes,
        active_after=active_after,
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Promote a reviewed policy snapshot"
    )

    parser.add_argument(
        "--run-id",
        required=True,
        type=UUID,
        help="Ingestion run ID to promote",
    )

    args = parser.parse_args()

    try:
        # A successful exit commits all changes together.
        # Any exception rolls the transaction back.
        with get_connection() as connection:
            result = promote_snapshot(
                connection,
                args.run_id,
            )

    except ValueError as error:
        print(f"Promotion blocked: {error}")
        return 1

    except psycopg.Error:
        print(
            "Database promotion failed. "
            "The transaction was rolled back."
        )
        return 1

    print(f"Committed run {result.run_id}")
    print(f"New policies: {result.changes.new:,}")
    print(f"Changed policies: {result.changes.changed:,}")
    print(f"Deleted policies: {result.changes.deleted:,}")
    print(f"Unchanged policies: {result.changes.unchanged:,}")
    print(f"Active policies after promotion: {result.active_after:,}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())