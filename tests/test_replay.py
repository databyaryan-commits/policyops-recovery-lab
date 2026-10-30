
"""Database tests for snapshot identity and replay detection."""

import os
from datetime import date, datetime
from uuid import uuid4

import pytest

from policyops.db import get_connection
from policyops.replay import check_snapshot_identity


DAY_1 = date(2026, 10, 1)
DAY_2 = date(2026, 10, 2)
DAY_3 = date(2026, 10, 3)

CHECKSUM_A = "a" * 64
CHECKSUM_B = "b" * 64


@pytest.fixture
def db_connection(monkeypatch):
    """Use the test database and roll back after each test."""

    test_database = os.getenv(
        "POLICYOPS_TEST_DATABASE_URL",
        "dbname=policyops_test",
    )

    monkeypatch.setenv("POLICYOPS_DATABASE_URL", test_database)

    connection = get_connection()

    try:
        database_name = connection.execute(
            "SELECT current_database()"
        ).fetchone()[0]

        if database_name != "policyops_test":
            pytest.fail("Replay tests must use policyops_test.")

        yield connection

    finally:
        connection.rollback()
        connection.close()


def register_attempt(
    connection,
    snapshot_date: date,
    checksum: str,
    status: str = "committed",
):
    """Create a source-delivery record for a replay test."""

    run_id = uuid4()

    connection.execute(
        """
        INSERT INTO ingestion_runs (
            run_id,
            snapshot_date,
            checksum_sha256,
            complete_snapshot,
            status
        )
        VALUES (%s, %s, %s, TRUE, %s)
        """,
        (
            run_id,
            snapshot_date,
            checksum,
            status,
        ),
    )

    return run_id


def test_first_snapshot_is_new(db_connection):
    decision = check_snapshot_identity(
        db_connection,
        DAY_1,
        CHECKSUM_A,
    )

    assert decision.outcome == "new"
    assert decision.can_stage
    assert decision.existing_run_id is None
    assert decision.last_committed_date is None


def test_newer_snapshot_can_be_staged(db_connection):
    register_attempt(
        db_connection,
        DAY_1,
        CHECKSUM_A,
    )

    decision = check_snapshot_identity(
        db_connection,
        DAY_2,
        CHECKSUM_B,
    )

    assert decision.outcome == "new"
    assert decision.can_stage
    assert decision.last_committed_date == DAY_1


def test_identical_committed_snapshot_is_recognized(db_connection):
    original_run_id = register_attempt(
        db_connection,
        DAY_1,
        CHECKSUM_A,
    )

    decision = check_snapshot_identity(
        db_connection,
        DAY_1,
        CHECKSUM_A,
    )

    assert decision.outcome == "already_committed"
    assert not decision.can_stage
    assert decision.existing_run_id == original_run_id


def test_same_date_different_content_is_rejected(db_connection):
    original_run_id = register_attempt(
        db_connection,
        DAY_1,
        CHECKSUM_A,
    )

    decision = check_snapshot_identity(
        db_connection,
        DAY_1,
        CHECKSUM_B,
    )

    assert decision.outcome == "date_conflict"
    assert not decision.can_stage
    assert decision.existing_run_id == original_run_id


def test_older_uncommitted_snapshot_is_rejected(db_connection):
    register_attempt(
        db_connection,
        DAY_2,
        CHECKSUM_B,
    )

    decision = check_snapshot_identity(
        db_connection,
        DAY_1,
        CHECKSUM_A,
    )

    assert decision.outcome == "out_of_order"
    assert not decision.can_stage
    assert decision.last_committed_date == DAY_2


def test_old_successful_snapshot_is_still_a_replay(db_connection):
    original_run_id = register_attempt(
        db_connection,
        DAY_1,
        CHECKSUM_A,
    )

    register_attempt(
        db_connection,
        DAY_2,
        CHECKSUM_B,
    )

    # Day 1 is older, but its exact source was already committed.
    decision = check_snapshot_identity(
        db_connection,
        DAY_1,
        CHECKSUM_A,
    )

    assert decision.outcome == "already_committed"
    assert not decision.can_stage
    assert decision.existing_run_id == original_run_id


@pytest.mark.parametrize(
    "previous_status",
    ["failed", "rejected"],
)
def test_unsuccessful_attempt_does_not_block_retry(
    db_connection,
    previous_status: str,
):
    register_attempt(
        db_connection,
        DAY_1,
        CHECKSUM_A,
        status=previous_status,
    )

    decision = check_snapshot_identity(
        db_connection,
        DAY_1,
        CHECKSUM_A,
    )

    assert decision.outcome == "new"
    assert decision.can_stage


@pytest.mark.parametrize(
    "invalid_checksum",
    [
        "not-a-checksum",
        "A" * 64,
    ],
)
def test_invalid_checksums_are_rejected(
    db_connection,
    invalid_checksum: str,
):
    with pytest.raises(ValueError, match="checksum"):
        check_snapshot_identity(
            db_connection,
            DAY_1,
            invalid_checksum,
        )


@pytest.mark.parametrize(
    "invalid_date",
    [
        "2026-10-01",
        datetime(2026, 10, 1, 9, 0),
    ],
)
def test_invalid_date_types_are_rejected(
    db_connection,
    invalid_date,
):
    with pytest.raises(ValueError, match="date object"):
        check_snapshot_identity(
            db_connection,
            invalid_date,
            CHECKSUM_A,
        )


def test_replay_check_does_not_modify_database(db_connection):
    original_run_id = register_attempt(
        db_connection,
        DAY_1,
        CHECKSUM_A,
    )

    before = db_connection.execute(
        """
        SELECT COUNT(*)
        FROM ingestion_runs
        """
    ).fetchone()[0]

    decision = check_snapshot_identity(
        db_connection,
        DAY_1,
        CHECKSUM_A,
    )

    after = db_connection.execute(
        """
        SELECT COUNT(*)
        FROM ingestion_runs
        """
    ).fetchone()[0]

    assert decision.outcome == "already_committed"
    assert decision.existing_run_id == original_run_id
    assert before == after == 1

    # Checking an identity must not touch business state.
    current_count = db_connection.execute(
        "SELECT COUNT(*) FROM policy_current"
    ).fetchone()[0]

    history_count = db_connection.execute(
        "SELECT COUNT(*) FROM policy_history"
    ).fetchone()[0]

    assert current_count == 0
    assert history_count == 0
