"""Compare an incoming policy snapshot with the current policy records."""

from dataclasses import dataclass
from uuid import UUID

import psycopg


@dataclass(frozen=True)
class ChangeCounts:
    new: int
    changed: int
    deleted: int
    unchanged: int


def compare_snapshot(
    connection: psycopg.Connection,
    run_id: UUID,
) -> ChangeCounts:
    """Count policy changes without updating the database."""

    run = connection.execute(
        """
        SELECT status, accepted_count, complete_snapshot
        FROM ingestion_runs
        WHERE run_id = %s
        """,
        (run_id,),
    ).fetchone()

    if run is None:
        raise ValueError("The requested ingestion run does not exist.")

    status, accepted_count, complete_snapshot = run

    if status not in {"validated", "awaiting_review"}:
        raise ValueError(
            f"Cannot compare a run with status '{status}'."
        )

    if complete_snapshot is not True:
        raise ValueError(
            "Change detection requires a complete policy snapshot."
        )

    staged_count = connection.execute(
        """
        SELECT COUNT(*)
        FROM policy_staging
        WHERE run_id = %s
        """,
        (run_id,),
    ).fetchone()[0]

    if staged_count != accepted_count:
        raise ValueError(
            "The staged row count does not match the validated count."
        )

    # Compare business fields only. A newer source timestamp
    # does not necessarily mean the policy itself has changed.
    counts = connection.execute(
        """
        WITH incoming AS (
            SELECT
                policy_id,
                customer_id,
                status,
                premium_amount,
                effective_date
            FROM policy_staging
            WHERE run_id = %s
        ),
        classified AS (
            SELECT
                CASE
                    WHEN incoming.policy_id IS NULL
                        THEN 'deleted'

                    WHEN current_policy.policy_id IS NULL
                         OR current_policy.is_deleted = TRUE
                        THEN 'new'

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
                        THEN 'changed'

                    ELSE 'unchanged'
                END AS change_type

            FROM incoming

            FULL OUTER JOIN policy_current AS current_policy
                ON incoming.policy_id = current_policy.policy_id

            -- Previously deleted policies that are still absent
            -- should not be counted as deleted again.
            WHERE incoming.policy_id IS NOT NULL
               OR current_policy.is_deleted = FALSE
        )
        SELECT
            COUNT(*) FILTER (
                WHERE change_type = 'new'
            ) AS new_count,

            COUNT(*) FILTER (
                WHERE change_type = 'changed'
            ) AS changed_count,

            COUNT(*) FILTER (
                WHERE change_type = 'deleted'
            ) AS deleted_count,

            COUNT(*) FILTER (
                WHERE change_type = 'unchanged'
            ) AS unchanged_count

        FROM classified
        """,
        (run_id,),
    ).fetchone()

    return ChangeCounts(
        new=counts[0],
        changed=counts[1],
        deleted=counts[2],
        unchanged=counts[3],
    )