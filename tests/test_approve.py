"""Integration tests for manual policy snapshot approval."""

import csv
import os
from datetime import date
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest

from policyops.approve import approve_snapshot
from policyops.db import get_connection
from policyops.generate import generate_golden, write_snapshot
from policyops.load import stage_snapshot
from policyops.safety import evaluate_deletion_risk


@pytest.fixture
def db_connection(monkeypatch):
    """Use the test database and roll back changes after each test."""

    test_database = os.getenv(
        "POLICYOPS_TEST_DATABASE_URL",
        "dbname=policyops_test",
    )

    monkeypatch.setenv("POLICYOPS_DATABASE_URL", test_database)

    connection = get_connection()

    database_name = connection.execute(
        "SELECT current_database()"
    ).fetchone()[0]

    if database_name != "policyops_test":
        connection.rollback()
        connection.close()
        pytest.fail("Approval tests must use policyops_test.")

    try:
        yield connection
    finally:
        connection.rollback()
        connection.close()


@pytest.fixture
def demo_folder(tmp_path: Path) -> Path:
    generate_golden(tmp_path, seed=42)
    return tmp_path


def prepare_flagged_run(db_connection, demo_folder: Path):
    """Create a valid delivery that removes 35% of current policies."""

    day1_run_id, day1_report = stage_snapshot(
        db_connection,
        demo_folder / "day1.json",
    )

    assert day1_report.passed

    # Set up current state directly until real promotion is available.
    db_connection.execute(
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
        """,
        (date(2026, 10, 1), day1_run_id),
    )

    with (demo_folder / "day1.csv").open(
        "r",
        encoding="utf-8",
        newline="",
    ) as file:
        rows = list(csv.DictReader(file))

    assert len(rows) == 1000

    # Keep 650 policies and supply a matching manifest.
    write_snapshot(
        demo_folder,
        "reduced",
        "2026-10-03",
        rows[:650],
    )

    run_id, report = stage_snapshot(
        db_connection,
        demo_folder / "reduced.json",
    )

    assert report.passed

    decision = evaluate_deletion_risk(
        db_connection,
        run_id,
    )

    assert decision.needs_review
    assert decision.changes.deleted == 350

    return run_id


def test_approval_records_reviewer_reason_and_timestamp(
    db_connection,
    demo_folder: Path,
):
    run_id = prepare_flagged_run(db_connection, demo_folder)

    approval = approve_snapshot(
        connection=db_connection,
        run_id=run_id,
        reviewer="  Aryan Nair  ",
        reason="  Confirmed the removal list after manual review.  ",
    )

    assert approval.run_id == run_id
    assert approval.reviewer == "Aryan Nair"
    assert approval.reason == (
        "Confirmed the removal list after manual review."
    )
    assert approval.approved_at is not None
    assert approval.approved_at.tzinfo is not None

    stored_approval = db_connection.execute(
        """
        SELECT
            status,
            approved_by,
            approval_reason,
            approved_at
        FROM ingestion_runs
        WHERE run_id = %s
        """,
        (run_id,),
    ).fetchone()

    assert stored_approval[0] == "awaiting_review"
    assert stored_approval[1] == "Aryan Nair"
    assert stored_approval[2] == approval.reason
    assert stored_approval[3] == approval.approved_at

    # Recording approval must not apply any policy changes.
    current_count = db_connection.execute(
        "SELECT COUNT(*) FROM policy_current"
    ).fetchone()[0]

    staged_count = db_connection.execute(
        """
        SELECT COUNT(*)
        FROM policy_staging
        WHERE run_id = %s
        """,
        (run_id,),
    ).fetchone()[0]

    history_count = db_connection.execute(
        "SELECT COUNT(*) FROM policy_history"
    ).fetchone()[0]

    assert current_count == 1000
    assert staged_count == 650
    assert history_count == 0


def test_normal_validated_run_cannot_be_approved(
    db_connection,
    demo_folder: Path,
):
    run_id, report = stage_snapshot(
        db_connection,
        demo_folder / "day1.json",
    )

    assert report.passed

    with pytest.raises(ValueError, match="status"):
        approve_snapshot(
            connection=db_connection,
            run_id=run_id,
            reviewer="Aryan Nair",
            reason="Manual approval requested.",
        )


def test_rejected_run_cannot_be_approved(
    db_connection,
    demo_folder: Path,
):
    run_id, report = stage_snapshot(
        db_connection,
        demo_folder / "missing.json",
    )

    assert not report.passed

    with pytest.raises(ValueError, match="status"):
        approve_snapshot(
            connection=db_connection,
            run_id=run_id,
            reviewer="Aryan Nair",
            reason="Manual approval requested.",
        )


def test_unknown_run_cannot_be_approved(db_connection):
    with pytest.raises(ValueError, match="does not exist"):
        approve_snapshot(
            connection=db_connection,
            run_id=uuid4(),
            reviewer="Aryan Nair",
            reason="Manual approval requested.",
        )


def test_same_run_cannot_be_approved_twice(
    db_connection,
    demo_folder: Path,
):
    run_id = prepare_flagged_run(db_connection, demo_folder)

    first_approval = approve_snapshot(
        connection=db_connection,
        run_id=run_id,
        reviewer="Aryan Nair",
        reason="Reviewed the missing policies.",
    )

    with pytest.raises(ValueError, match="already been approved"):
        approve_snapshot(
            connection=db_connection,
            run_id=run_id,
            reviewer="Another Reviewer",
            reason="Second approval attempt.",
        )

    stored_approval = db_connection.execute(
        """
        SELECT approved_by, approval_reason, approved_at
        FROM ingestion_runs
        WHERE run_id = %s
        """,
        (run_id,),
    ).fetchone()

    assert stored_approval == (
        "Aryan Nair",
        "Reviewed the missing policies.",
        first_approval.approved_at,
    )


@pytest.mark.parametrize(
    "reviewer",
    ["", "   ", None],
)
def test_missing_reviewer_is_rejected(
    db_connection,
    demo_folder: Path,
    reviewer,
):
    run_id = prepare_flagged_run(db_connection, demo_folder)

    with pytest.raises(ValueError, match="reviewer name"):
        approve_snapshot(
            connection=db_connection,
            run_id=run_id,
            reviewer=reviewer,
            reason="Reviewed the source delivery.",
        )


@pytest.mark.parametrize(
    "reason",
    ["", "   ", None],
)
def test_missing_reason_is_rejected(
    db_connection,
    demo_folder: Path,
    reason,
):
    run_id = prepare_flagged_run(db_connection, demo_folder)

    with pytest.raises(ValueError, match="approval reason"):
        approve_snapshot(
            connection=db_connection,
            run_id=run_id,
            reviewer="Aryan Nair",
            reason=reason,
        )


def test_failed_approval_rolls_back_transaction(
    db_connection,
    demo_folder: Path,
):
    run_id = prepare_flagged_run(db_connection, demo_folder)

    # This temporary constraint makes the approval UPDATE fail.
    # It is removed when the transaction is rolled back.
    db_connection.execute(
        """
        ALTER TABLE ingestion_runs
        ADD CONSTRAINT test_block_approval
        CHECK (approved_at IS NULL)
        """
    )

    with pytest.raises(psycopg.Error):
        approve_snapshot(
            connection=db_connection,
            run_id=run_id,
            reviewer="Aryan Nair",
            reason="Reviewed and approved the source delivery.",
        )

    db_connection.rollback()

    # All setup data was part of the same transaction.
    # Nothing from the failed attempt should remain.
    assert db_connection.execute(
        "SELECT COUNT(*) FROM ingestion_runs"
    ).fetchone()[0] == 0

    assert db_connection.execute(
        "SELECT COUNT(*) FROM policy_staging"
    ).fetchone()[0] == 0

    assert db_connection.execute(
        "SELECT COUNT(*) FROM policy_current"
    ).fetchone()[0] == 0

    assert db_connection.execute(
        "SELECT COUNT(*) FROM policy_history"
    ).fetchone()[0] == 0

    assert db_connection.execute(
        "SELECT COUNT(*) FROM quality_events"
    ).fetchone()[0] == 0