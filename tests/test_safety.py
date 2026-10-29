"""Integration tests for the policy deletion safety gate."""

import csv
import os
from datetime import date
from decimal import Decimal
from pathlib import Path

import psycopg
import pytest

from policyops.changes import ChangeCounts
from policyops.db import get_connection
from policyops.generate import generate_golden, write_snapshot
from policyops.load import stage_snapshot
from policyops.safety import evaluate_deletion_risk


@pytest.fixture
def db_connection(monkeypatch):
    """Use the dedicated test database and roll back after each test."""

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
        pytest.fail("Safety tests must use policyops_test.")

    try:
        yield connection
    finally:
        connection.rollback()
        connection.close()


@pytest.fixture
def demo_folder(tmp_path: Path) -> Path:
    generate_golden(tmp_path, seed=42)
    return tmp_path


def prepare_current_policies(db_connection, demo_folder: Path):
    """Copy the validated Day 1 snapshot into current state."""

    run_id, report = stage_snapshot(
        db_connection,
        demo_folder / "day1.json",
    )

    assert report.passed
    assert report.accepted_count == 1000

    # The real promotion process is not implemented yet.
    # This creates the starting state needed for safety tests.
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
        (date(2026, 10, 1), run_id),
    )

    return run_id


def stage_smaller_snapshot(
    db_connection,
    demo_folder: Path,
    retained_count: int,
):
    """Make a valid snapshot containing fewer than 1,000 policies."""

    csv_path = demo_folder / "day1.csv"

    with csv_path.open(
        "r",
        encoding="utf-8",
        newline="",
    ) as file:
        original_rows = list(csv.DictReader(file))

    assert len(original_rows) == 1000

    retained_rows = original_rows[:retained_count]

    # write_snapshot creates both the CSV and its matching
    # manifest, including the correct checksum and row count.
    write_snapshot(
        demo_folder,
        "reduced",
        "2026-10-03",
        retained_rows,
    )

    run_id, report = stage_snapshot(
        db_connection,
        demo_folder / "reduced.json",
    )

    assert report.passed
    assert report.accepted_count == retained_count

    return run_id


def test_first_snapshot_does_not_need_deletion_review(
    db_connection,
    demo_folder: Path,
):
    run_id, report = stage_snapshot(
        db_connection,
        demo_folder / "day1.json",
    )

    assert report.passed

    decision = evaluate_deletion_risk(
        db_connection,
        run_id,
    )

    assert decision.active_before == 0
    assert decision.deletion_rate == Decimal("0")
    assert not decision.needs_review

    assert decision.changes == ChangeCounts(
        new=1000,
        changed=0,
        deleted=0,
        unchanged=0,
    )


def test_normal_day2_changes_pass_the_safety_gate(
    db_connection,
    demo_folder: Path,
):
    prepare_current_policies(db_connection, demo_folder)

    run_id, report = stage_snapshot(
        db_connection,
        demo_folder / "day2.json",
    )

    assert report.passed

    decision = evaluate_deletion_risk(
        db_connection,
        run_id,
    )

    assert decision.active_before == 1000
    assert decision.deletion_rate == Decimal("0.02")
    assert not decision.needs_review

    assert decision.changes == ChangeCounts(
        new=30,
        changed=50,
        deleted=20,
        unchanged=930,
    )

    stored_run = db_connection.execute(
        """
        SELECT
            status,
            new_count,
            changed_count,
            deleted_count,
            unchanged_count
        FROM ingestion_runs
        WHERE run_id = %s
        """,
        (run_id,),
    ).fetchone()

    assert stored_run == (
        "validated",
        30,
        50,
        20,
        930,
    )


def test_exactly_20_percent_deletions_pass_the_gate(
    db_connection,
    demo_folder: Path,
):
    prepare_current_policies(db_connection, demo_folder)

    run_id = stage_smaller_snapshot(
        db_connection,
        demo_folder,
        retained_count=800,
    )

    decision = evaluate_deletion_risk(
        db_connection,
        run_id,
    )

    assert decision.changes.deleted == 200
    assert decision.deletion_rate == Decimal("0.20")
    assert not decision.needs_review

    status = db_connection.execute(
        """
        SELECT status
        FROM ingestion_runs
        WHERE run_id = %s
        """,
        (run_id,),
    ).fetchone()[0]

    assert status == "validated"


def test_35_percent_drop_requires_review(
    db_connection,
    demo_folder: Path,
):
    prepare_current_policies(db_connection, demo_folder)

    run_id = stage_smaller_snapshot(
        db_connection,
        demo_folder,
        retained_count=650,
    )

    decision = evaluate_deletion_risk(
        db_connection,
        run_id,
    )

    assert decision.active_before == 1000
    assert decision.deletion_rate == Decimal("0.35")
    assert decision.needs_review

    assert decision.changes == ChangeCounts(
        new=0,
        changed=0,
        deleted=350,
        unchanged=650,
    )

    stored_run = db_connection.execute(
        """
        SELECT
            status,
            new_count,
            changed_count,
            deleted_count,
            unchanged_count
        FROM ingestion_runs
        WHERE run_id = %s
        """,
        (run_id,),
    ).fetchone()

    assert stored_run == (
        "awaiting_review",
        0,
        0,
        350,
        650,
    )

    warning = db_connection.execute(
        """
        SELECT
            check_id,
            severity,
            observed_value,
            evidence
        FROM quality_events
        WHERE run_id = %s
        """,
        (run_id,),
    ).fetchone()

    assert warning is not None
    assert warning[0] == "DELETION_RATE_REVIEW_REQUIRED"
    assert warning[1] == "warning"
    assert warning[2] == "35.00%"

    evidence = warning[3]

    assert evidence["active_before"] == 1000
    assert evidence["deleted_count"] == 350
    assert evidence["deletion_rate"] == "0.35"
    assert evidence["threshold"] == "0.20"

    # The safety check must not change current policies
    # or create business history records.
    current_count = db_connection.execute(
        "SELECT COUNT(*) FROM policy_current"
    ).fetchone()[0]

    history_count = db_connection.execute(
        "SELECT COUNT(*) FROM policy_history"
    ).fetchone()[0]

    assert current_count == 1000
    assert history_count == 0


def test_deletion_rate_just_above_20_percent_requires_review(
    db_connection,
    demo_folder: Path,
):
    prepare_current_policies(db_connection, demo_folder)

    run_id = stage_smaller_snapshot(
        db_connection,
        demo_folder,
        retained_count=799,
    )

    decision = evaluate_deletion_risk(
        db_connection,
        run_id,
    )

    assert decision.changes.deleted == 201
    assert decision.deletion_rate == Decimal("0.201")
    assert decision.needs_review


def test_rejected_snapshot_cannot_pass_safety_review(
    db_connection,
    demo_folder: Path,
):
    run_id, report = stage_snapshot(
        db_connection,
        demo_folder / "missing.json",
    )

    assert not report.passed

    with pytest.raises(ValueError, match="status"):
        evaluate_deletion_risk(
            db_connection,
            run_id,
        )


def test_review_cannot_be_recorded_twice(
    db_connection,
    demo_folder: Path,
):
    prepare_current_policies(db_connection, demo_folder)

    run_id = stage_smaller_snapshot(
        db_connection,
        demo_folder,
        retained_count=650,
    )

    first_decision = evaluate_deletion_risk(
        db_connection,
        run_id,
    )

    assert first_decision.needs_review

    with pytest.raises(ValueError, match="status"):
        evaluate_deletion_risk(
            db_connection,
            run_id,
        )

    warning_count = db_connection.execute(
        """
        SELECT COUNT(*)
        FROM quality_events
        WHERE run_id = %s
          AND check_id = 'DELETION_RATE_REVIEW_REQUIRED'
        """,
        (run_id,),
    ).fetchone()[0]

    assert warning_count == 1


def test_failed_warning_insert_rolls_back_review(
    db_connection,
    demo_folder: Path,
):
    prepare_current_policies(db_connection, demo_folder)

    run_id = stage_smaller_snapshot(
        db_connection,
        demo_folder,
        retained_count=650,
    )

    # Force the quality-event insert to fail.
    # This constraint is temporary because the test rolls back.
    db_connection.execute(
        """
        ALTER TABLE quality_events
        ADD CONSTRAINT test_reject_safety_warning CHECK (FALSE)
        """
    )

    with pytest.raises(psycopg.Error):
        evaluate_deletion_risk(
            db_connection,
            run_id,
        )

    # Undo the whole failed transaction, including its test data.
    db_connection.rollback()

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