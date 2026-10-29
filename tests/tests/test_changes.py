"""Database tests for policy change detection."""

import os
from datetime import date
from pathlib import Path
from uuid import uuid4

import pytest

from policyops.changes import ChangeCounts, compare_snapshot
from policyops.db import get_connection
from policyops.generate import generate_golden
from policyops.load import stage_snapshot


@pytest.fixture
def db_connection(monkeypatch):
    """Use the test database and undo all changes after each test."""

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
        pytest.fail("These tests must use policyops_test.")

    try:
        yield connection
    finally:
        connection.rollback()
        connection.close()


@pytest.fixture
def demo_folder(tmp_path: Path) -> Path:
    """Create fresh synthetic snapshots for each test."""

    generate_golden(tmp_path, seed=42)
    return tmp_path


def prepare_day1_current(db_connection, demo_folder: Path):
    """Set up the Day 1 state that Day 2 will be compared against."""

    run_id, report = stage_snapshot(
        db_connection,
        demo_folder / "day1.json",
    )

    assert report.passed
    assert report.accepted_count == 1000

    # Promotion has not been implemented yet, so the tests
    # copy Day 1 into current state directly.
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


def stage_day2(db_connection, demo_folder: Path):
    """Stage Day 2 and return its ingestion run ID."""

    run_id, report = stage_snapshot(
        db_connection,
        demo_folder / "day2.json",
    )

    assert report.passed
    assert report.accepted_count == 1010

    return run_id


def test_first_snapshot_contains_only_new_policies(
    db_connection,
    demo_folder: Path,
):
    run_id, report = stage_snapshot(
        db_connection,
        demo_folder / "day1.json",
    )

    assert report.passed

    counts = compare_snapshot(db_connection, run_id)

    assert counts == ChangeCounts(
        new=1000,
        changed=0,
        deleted=0,
        unchanged=0,
    )


def test_golden_day2_changes_match_expected_counts(
    db_connection,
    demo_folder: Path,
):
    prepare_day1_current(db_connection, demo_folder)

    run_id = stage_day2(db_connection, demo_folder)

    counts = compare_snapshot(db_connection, run_id)

    assert counts == ChangeCounts(
        new=30,
        changed=50,
        deleted=20,
        unchanged=930,
    )

    # Comparing snapshots must not change current state.
    current_count = db_connection.execute(
        "SELECT COUNT(*) FROM policy_current"
    ).fetchone()[0]

    history_count = db_connection.execute(
        "SELECT COUNT(*) FROM policy_history"
    ).fetchone()[0]

    assert current_count == 1000
    assert history_count == 0


def test_timestamp_change_alone_is_not_a_business_change(
    db_connection,
    demo_folder: Path,
):
    prepare_day1_current(db_connection, demo_folder)

    run_id = stage_day2(db_connection, demo_folder)

    # P0071 is unchanged in the golden scenario.
    db_connection.execute(
        """
        UPDATE policy_staging
        SET source_updated_at = source_updated_at + INTERVAL '1 hour'
        WHERE run_id = %s
          AND policy_id = 'P0071'
        """,
        (run_id,),
    )

    counts = compare_snapshot(db_connection, run_id)

    assert counts == ChangeCounts(
        new=30,
        changed=50,
        deleted=20,
        unchanged=930,
    )


def test_business_field_changes_are_detected(
    db_connection,
    demo_folder: Path,
):
    prepare_day1_current(db_connection, demo_folder)

    run_id = stage_day2(db_connection, demo_folder)

    # These three policies are normally unchanged.
    db_connection.execute(
        """
        UPDATE policy_staging
        SET status = 'cancelled'
        WHERE run_id = %s
          AND policy_id = 'P0071'
        """,
        (run_id,),
    )

    db_connection.execute(
        """
        UPDATE policy_staging
        SET customer_id = 'C9999'
        WHERE run_id = %s
          AND policy_id = 'P0072'
        """,
        (run_id,),
    )

    db_connection.execute(
        """
        UPDATE policy_staging
        SET effective_date = effective_date + 1
        WHERE run_id = %s
          AND policy_id = 'P0073'
        """,
        (run_id,),
    )

    counts = compare_snapshot(db_connection, run_id)

    assert counts == ChangeCounts(
        new=30,
        changed=53,
        deleted=20,
        unchanged=927,
    )


def test_already_deleted_policy_is_not_deleted_again(
    db_connection,
    demo_folder: Path,
):
    prepare_day1_current(db_connection, demo_folder)

    # P0001 is missing from Day 2, but has already been deleted.
    db_connection.execute(
        """
        UPDATE policy_current
        SET is_deleted = TRUE
        WHERE policy_id = 'P0001'
        """
    )

    run_id = stage_day2(db_connection, demo_folder)

    counts = compare_snapshot(db_connection, run_id)

    assert counts == ChangeCounts(
        new=30,
        changed=50,
        deleted=19,
        unchanged=930,
    )


def test_reappearing_policy_is_counted_as_new(
    db_connection,
    demo_folder: Path,
):
    prepare_day1_current(db_connection, demo_folder)

    # P0071 exists in Day 2 but is marked deleted in current state.
    db_connection.execute(
        """
        UPDATE policy_current
        SET is_deleted = TRUE
        WHERE policy_id = 'P0071'
        """
    )

    run_id = stage_day2(db_connection, demo_folder)

    counts = compare_snapshot(db_connection, run_id)

    assert counts == ChangeCounts(
        new=31,
        changed=50,
        deleted=20,
        unchanged=929,
    )


def test_rejected_snapshot_cannot_be_compared(
    db_connection,
    demo_folder: Path,
):
    run_id, report = stage_snapshot(
        db_connection,
        demo_folder / "missing.json",
    )

    assert not report.passed

    with pytest.raises(ValueError, match="status"):
        compare_snapshot(db_connection, run_id)


def test_missing_staged_row_blocks_comparison(
    db_connection,
    demo_folder: Path,
):
    run_id, report = stage_snapshot(
        db_connection,
        demo_folder / "day1.json",
    )

    assert report.passed

    # Simulate a staging table that no longer matches
    # the row count recorded during validation.
    db_connection.execute(
        """
        DELETE FROM policy_staging
        WHERE run_id = %s
          AND policy_id = 'P0001'
        """,
        (run_id,),
    )

    with pytest.raises(ValueError, match="staged row count"):
        compare_snapshot(db_connection, run_id)


def test_unknown_run_cannot_be_compared(db_connection):
    with pytest.raises(ValueError, match="does not exist"):
        compare_snapshot(db_connection, uuid4())