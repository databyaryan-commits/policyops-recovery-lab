
"""End-to-end tests for replay-safe policy ingestion."""

import csv
import os
from decimal import Decimal
from pathlib import Path

import pytest

import policyops.promote as promotion_module
from policyops.approve import approve_snapshot
from policyops.changes import ChangeCounts
from policyops.db import get_connection
from policyops.generate import generate_golden, write_snapshot
from policyops.ingest import ingest_snapshot
from policyops.promote import promote_snapshot


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
            pytest.fail("Ingestion tests must use policyops_test.")

        yield connection

    finally:
        connection.rollback()
        connection.close()


@pytest.fixture
def demo_folder(tmp_path: Path) -> Path:
    """Create independently generated synthetic input files."""

    generate_golden(tmp_path, seed=42)
    return tmp_path


def ingest_demo(connection, folder: Path, name: str):
    """Process one generated snapshot through the full pipeline."""

    return ingest_snapshot(
        connection,
        folder / f"{name}.json",
    )


def count_rows(connection, table: str) -> int:
    """Count rows in a known PolicyOps database table."""

    allowed_tables = {
        "ingestion_runs",
        "policy_staging",
        "policy_current",
        "policy_history",
        "quality_events",
    }

    if table not in allowed_tables:
        raise ValueError("Unknown PolicyOps table.")

    return connection.execute(
        f"SELECT COUNT(*) FROM {table}"
    ).fetchone()[0]


def active_policy_count(connection) -> int:
    return connection.execute(
        """
        SELECT COUNT(*)
        FROM policy_current
        WHERE is_deleted = FALSE
        """
    ).fetchone()[0]


def make_reduced_snapshot(folder: Path):
    """Create a valid 650-policy delivery dated after Day 2."""

    with (folder / "day1.csv").open(
        "r",
        encoding="utf-8",
        newline="",
    ) as file:
        rows = list(csv.DictReader(file))

    assert len(rows) == 1000

    write_snapshot(
        folder,
        "reduced",
        "2026-10-03",
        rows[:650],
    )


def truncate_csv(folder: Path, name: str):
    """Simulate an incomplete delivery with an unchanged manifest."""

    csv_path = folder / f"{name}.csv"
    lines = csv_path.read_bytes().splitlines(keepends=True)

    csv_path.write_bytes(b"".join(lines[:651]))


def test_first_delivery_commits_current_state(
    db_connection,
    demo_folder: Path,
):
    result = ingest_demo(
        db_connection,
        demo_folder,
        "day1",
    )

    assert result.outcome == "committed"
    assert result.run_id is not None
    assert result.accepted_count == 1000

    assert result.changes == ChangeCounts(
        new=1000,
        changed=0,
        deleted=0,
        unchanged=0,
    )

    assert active_policy_count(db_connection) == 1000
    assert count_rows(db_connection, "policy_history") == 1000
    assert count_rows(db_connection, "ingestion_runs") == 1

    status = db_connection.execute(
        """
        SELECT status
        FROM ingestion_runs
        WHERE run_id = %s
        """,
        (result.run_id,),
    ).fetchone()[0]

    assert status == "committed"


def test_golden_day2_changes_are_applied(
    db_connection,
    demo_folder: Path,
):
    day1 = ingest_demo(db_connection, demo_folder, "day1")
    day2 = ingest_demo(db_connection, demo_folder, "day2")

    assert day1.outcome == "committed"
    assert day2.outcome == "committed"

    assert day2.changes == ChangeCounts(
        new=30,
        changed=50,
        deleted=20,
        unchanged=930,
    )

    assert active_policy_count(db_connection) == 1010
    assert count_rows(db_connection, "policy_current") == 1030
    assert count_rows(db_connection, "policy_history") == 1100
    assert count_rows(db_connection, "ingestion_runs") == 2

    history_types = dict(
        db_connection.execute(
            """
            SELECT change_type, COUNT(*)
            FROM policy_history
            WHERE run_id = %s
            GROUP BY change_type
            """,
            (day2.run_id,),
        ).fetchall()
    )

    assert history_types == {
        "insert": 30,
        "update": 50,
        "delete": 20,
    }


def test_identical_delivery_is_a_no_op(
    db_connection,
    demo_folder: Path,
):
    first = ingest_demo(db_connection, demo_folder, "day1")

    before = (
        count_rows(db_connection, "ingestion_runs"),
        count_rows(db_connection, "policy_staging"),
        count_rows(db_connection, "policy_current"),
        count_rows(db_connection, "policy_history"),
    )

    replay = ingest_demo(db_connection, demo_folder, "day1")

    after = (
        count_rows(db_connection, "ingestion_runs"),
        count_rows(db_connection, "policy_staging"),
        count_rows(db_connection, "policy_current"),
        count_rows(db_connection, "policy_history"),
    )

    assert first.outcome == "committed"
    assert replay.outcome == "replayed"
    assert replay.run_id == first.run_id
    assert before == after

    assert before == (1, 1000, 1000, 1000)


def test_old_committed_delivery_can_be_replayed(
    db_connection,
    demo_folder: Path,
):
    day1 = ingest_demo(db_connection, demo_folder, "day1")
    day2 = ingest_demo(db_connection, demo_folder, "day2")

    replay = ingest_demo(db_connection, demo_folder, "day1")

    assert day1.outcome == "committed"
    assert day2.outcome == "committed"
    assert replay.outcome == "replayed"
    assert replay.run_id == day1.run_id

    assert active_policy_count(db_connection) == 1010
    assert count_rows(db_connection, "ingestion_runs") == 2
    assert count_rows(db_connection, "policy_history") == 1100


def test_same_date_different_content_is_blocked(
    db_connection,
    demo_folder: Path,
):
    original = ingest_demo(db_connection, demo_folder, "day1")

    with (demo_folder / "day1.csv").open(
        "r",
        encoding="utf-8",
        newline="",
    ) as file:
        rows = list(csv.DictReader(file))

    # Use valid but different content for the same snapshot date.
    rows[0]["premium_amount"] = str(
        Decimal(rows[0]["premium_amount"]) + Decimal("1.00")
    )

    write_snapshot(
        demo_folder,
        "day1_conflict",
        "2026-10-01",
        rows,
    )

    conflict = ingest_demo(
        db_connection,
        demo_folder,
        "day1_conflict",
    )

    assert original.outcome == "committed"
    assert conflict.outcome == "date_conflict"
    assert conflict.run_id == original.run_id

    assert count_rows(db_connection, "ingestion_runs") == 1
    assert count_rows(db_connection, "policy_history") == 1000
    assert active_policy_count(db_connection) == 1000


def test_older_uncommitted_snapshot_is_blocked(
    db_connection,
    demo_folder: Path,
):
    # Day 2 is the first successfully committed delivery.
    day2 = ingest_demo(db_connection, demo_folder, "day2")

    older = ingest_demo(db_connection, demo_folder, "day1")

    assert day2.outcome == "committed"
    assert older.outcome == "out_of_order"

    assert count_rows(db_connection, "ingestion_runs") == 1
    assert count_rows(db_connection, "policy_history") == 1010
    assert active_policy_count(db_connection) == 1010


def test_incomplete_delivery_is_rejected_without_changes(
    db_connection,
    demo_folder: Path,
):
    ingest_demo(db_connection, demo_folder, "day1")

    # The manifest still declares the complete Day 2 delivery.
    truncate_csv(demo_folder, "day2")

    rejected = ingest_demo(
        db_connection,
        demo_folder,
        "day2",
    )

    assert rejected.outcome == "rejected"
    assert rejected.run_id is not None

    checks = {
        row[0]
        for row in db_connection.execute(
            """
            SELECT check_id
            FROM quality_events
            WHERE run_id = %s
            """,
            (rejected.run_id,),
        ).fetchall()
    }

    assert "CHECKSUM_MISMATCH" in checks
    assert "ROW_COUNT_MISMATCH" in checks

    assert active_policy_count(db_connection) == 1000
    assert count_rows(db_connection, "policy_history") == 1000
    assert count_rows(db_connection, "ingestion_runs") == 2


def test_large_deletion_is_held_for_approval(
    db_connection,
    demo_folder: Path,
):
    ingest_demo(db_connection, demo_folder, "day1")
    make_reduced_snapshot(demo_folder)

    result = ingest_demo(
        db_connection,
        demo_folder,
        "reduced",
    )

    assert result.outcome == "awaiting_review"
    assert result.run_id is not None
    assert result.accepted_count == 650
    assert result.changes.deleted == 350

    status = db_connection.execute(
        """
        SELECT status
        FROM ingestion_runs
        WHERE run_id = %s
        """,
        (result.run_id,),
    ).fetchone()[0]

    assert status == "awaiting_review"

    assert active_policy_count(db_connection) == 1000
    assert count_rows(db_connection, "policy_history") == 1000

    review_events = db_connection.execute(
        """
        SELECT COUNT(*)
        FROM quality_events
        WHERE run_id = %s
          AND check_id = 'DELETION_RATE_REVIEW_REQUIRED'
        """,
        (result.run_id,),
    ).fetchone()[0]

    assert review_events == 1


def test_approved_delivery_can_be_promoted_and_replayed(
    db_connection,
    demo_folder: Path,
):
    ingest_demo(db_connection, demo_folder, "day1")
    make_reduced_snapshot(demo_folder)

    held = ingest_demo(
        db_connection,
        demo_folder,
        "reduced",
    )

    assert held.outcome == "awaiting_review"

    approve_snapshot(
        connection=db_connection,
        run_id=held.run_id,
        reviewer="Test Reviewer",
        reason="Confirmed the synthetic deletion list.",
    )

    promoted = promote_snapshot(
        db_connection,
        held.run_id,
    )

    assert promoted.active_after == 650
    assert promoted.changes.deleted == 350

    before_replay = count_rows(
        db_connection,
        "policy_history",
    )

    replay = ingest_demo(
        db_connection,
        demo_folder,
        "reduced",
    )

    assert replay.outcome == "replayed"
    assert replay.run_id == held.run_id

    assert active_policy_count(db_connection) == 650
    assert before_replay == 1350
    assert count_rows(db_connection, "policy_history") == 1350
    assert count_rows(db_connection, "ingestion_runs") == 2


def test_mid_promotion_failure_rolls_back_and_retry_succeeds(
    db_connection,
    demo_folder: Path,
    monkeypatch,
):
    first = ingest_demo(db_connection, demo_folder, "day1")

    assert first.outcome == "committed"

    def simulate_crash(connection, run_id):
        # The history and current-state writes have already run.
        assert count_rows(connection, "policy_history") == 1100
        assert count_rows(connection, "policy_current") == 1030

        raise RuntimeError(
            "Simulated failure during policy promotion"
        )

    with monkeypatch.context() as patch:
        patch.setattr(
            promotion_module,
            "mark_missing_policies_deleted",
            simulate_crash,
        )

        with pytest.raises(
            RuntimeError,
            match="Simulated failure",
        ):
            ingest_demo(
                db_connection,
                demo_folder,
                "day2",
            )

    # The failed Day 2 attempt must leave no partial writes.
    assert count_rows(db_connection, "ingestion_runs") == 1
    assert count_rows(db_connection, "policy_current") == 1000
    assert count_rows(db_connection, "policy_history") == 1000
    assert active_policy_count(db_connection) == 1000

    # Retry the same source after removing the injected failure.
    retry = ingest_demo(
        db_connection,
        demo_folder,
        "day2",
    )

    assert retry.outcome == "committed"
    assert retry.changes.deleted == 20

    assert count_rows(db_connection, "ingestion_runs") == 2
    assert count_rows(db_connection, "policy_history") == 1100
    assert active_policy_count(db_connection) == 1010


def test_invalid_source_cannot_bypass_validation_as_replay(
    db_connection,
    demo_folder: Path,
):
    first = ingest_demo(db_connection, demo_folder, "day1")

    assert first.outcome == "committed"

    # Corrupt the CSV while leaving its original manifest intact.
    truncate_csv(demo_folder, "day1")

    second = ingest_demo(
        db_connection,
        demo_folder,
        "day1",
    )

    assert second.outcome == "rejected"
    assert second.run_id != first.run_id

    assert count_rows(db_connection, "ingestion_runs") == 2
    assert count_rows(db_connection, "policy_history") == 1000
    assert active_policy_count(db_connection) == 1000


def test_rejected_delivery_does_not_block_valid_retry(
    db_connection,
    demo_folder: Path,
):
    ingest_demo(db_connection, demo_folder, "day1")

    truncate_csv(demo_folder, "day2")

    failed = ingest_demo(
        db_connection,
        demo_folder,
        "day2",
    )

    assert failed.outcome == "rejected"

    # Restore the original valid synthetic files.
    generate_golden(demo_folder, seed=42)

    retry = ingest_demo(
        db_connection,
        demo_folder,
        "day2",
    )

    assert retry.outcome == "committed"
    assert retry.changes == ChangeCounts(
        new=30,
        changed=50,
        deleted=20,
        unchanged=930,
    )

    assert count_rows(db_connection, "ingestion_runs") == 3
    assert count_rows(db_connection, "policy_history") == 1100
    assert active_policy_count(db_connection) == 1010
