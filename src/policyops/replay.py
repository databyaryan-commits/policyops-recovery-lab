
"""Identify repeated, conflicting, and out-of-order policy snapshots."""

import re
from dataclasses import dataclass
from datetime import date
from typing import Literal
from uuid import UUID

import psycopg


ReplayOutcome = Literal[
    "new",
    "already_committed",
    "date_conflict",
    "out_of_order",
]


@dataclass(frozen=True)
class ReplayDecision:
    outcome: ReplayOutcome
    snapshot_date: date
    existing_run_id: UUID | None = None
    last_committed_date: date | None = None

    @property
    def can_stage(self) -> bool:
        return self.outcome == "new"


def check_snapshot_identity(
    connection: psycopg.Connection,
    snapshot_date: date,
    checksum_sha256: str,
) -> ReplayDecision:
    """Check a validated source identity against committed runs.

    A snapshot is identified by its date and original CSV checksum.
    Only committed runs count as completed deliveries.
    """

    if type(snapshot_date) is not date:
        raise ValueError("Snapshot date must be a date object.")

    if not (
        isinstance(checksum_sha256, str)
        and re.fullmatch(r"[0-9a-f]{64}", checksum_sha256)
    ):
        raise ValueError(
            "Snapshot checksum must be a 64-character SHA-256 value."
        )

    # Check this date first, even if newer snapshots exist.
    # This lets us recognize an old successful delivery as a
    # replay rather than treating it as a new out-of-order load.
    existing = connection.execute(
        """
        SELECT run_id, checksum_sha256
        FROM ingestion_runs
        WHERE snapshot_date = %s
          AND status = 'committed'
        """,
        (snapshot_date,),
    ).fetchone()

    if existing is not None:
        existing_run_id, existing_checksum = existing

        if existing_checksum == checksum_sha256:
            return ReplayDecision(
                outcome="already_committed",
                snapshot_date=snapshot_date,
                existing_run_id=existing_run_id,
            )

        # Different content on an already committed date
        # requires an explicit correction process.
        return ReplayDecision(
            outcome="date_conflict",
            snapshot_date=snapshot_date,
            existing_run_id=existing_run_id,
        )

    latest = connection.execute(
        """
        SELECT MAX(snapshot_date)
        FROM ingestion_runs
        WHERE status = 'committed'
        """
    ).fetchone()

    latest_date = latest[0] if latest is not None else None

    if latest_date is not None and snapshot_date < latest_date:
        return ReplayDecision(
            outcome="out_of_order",
            snapshot_date=snapshot_date,
            last_committed_date=latest_date,
        )

    # No matching committed snapshot or ordering conflict.
    # Failed and rejected attempts do not prevent a retry.
    return ReplayDecision(
        outcome="new",
        snapshot_date=snapshot_date,
        last_committed_date=latest_date,
    )
