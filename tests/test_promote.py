
"""Integration tests for transactional policy promotion."""

import csv
import os
from pathlib import Path

import pytest

import policyops.promote as promotion_module
from policyops.approve import approve_snapshot
from policyops.changes import ChangeCounts
from policyops.db import get_connection
from policyops.generate import generate_golden, write_snapshot
from policyops.load import stage_snapshot
from policyops.promote import promote_snapshot
from policyops.safety import evaluate_deletion_risk


@pytest.fixture
def db_connection(monkeypatch):
    """Keep promotion tests isolated from the main database."""

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
            pytest.fail(
                "Promotion tests must use policyops_test."
            )

        yield connection

    finally:
        connection.rollback()
        connection.close()


@pytest.fixture
def demo_folder(tmp_path: Path) -> Path:
    """Create fresh synthetic data for each test."""

    generate_golden(tmp_path, seed=42)
    return tmp_path


def stage_and_assess(connection, manifest_path: Path):
    """Stage a valid snapshot and calculate its deletion risk."""

    run_id, report = stage_snapshot(
        connection,
        manifest_path,
    )

    assert report.passed

    decision = evaluate_deletion_risk(
        connection,
        run_id,
    )

    return run_id, decision


def promote_day1(connection, demo_folder: Path):
    """Create a committed starting state with 1,000 policies."""

    run_id, decision = stage_and_assess(
        connection,
        demo_folder / "day1.json",
    )

    assert not decision.needs_review

    result = promote_snapshot(connection, run_id)

    assert result.active_after == 1000

    return run_id


def stage_reduced_snapshot(
    connection,
    demo_folder: Path,
    retained_count: int = 650,
):
    """Create a valid smaller snapshot with matching metadata."""

    with (demo_folder / "day1.csv").open(
        "r",
        encoding="utf-8",
        newline="",
    ) as file:
        rows = list(csv.DictReader(file))

    assert len(rows) == 1000

    write_snapshot(
        demo_folder,
        "reduced",
        "2026-10-03",
        rows[:retained_count],
    )

    return stage_and_assess(
        connection,
        demo_folder / "reduced.json",
    )


def count_rows(connection, table: str) -> int:
    """Count rows in one of the project's known tables."""

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


def test_first_snapshot_creates_current_policies_and_history(
    db_connection,
    demo_folder: Path,
):
    run_id = promote_day1(db_connection, demo_folder)

    assert count_rows(db_connection, "policy_current") == 1000
    assert count_rows(db_connection, "policy_history") == 1000

    status = db_connection.execute(
        """
        SELECT status
        FROM ingestion_runs
        WHERE run_id = %s
        """,
        (run_id,),
    ).fetchone()[0]

    assert status == "committed"

    history_types = db_connection.execute(
        """
        SELECT change_type, COUNT(*)
        FROM policy_history
        WHERE run_id = %s
        GROUP BY change_type
        """,
        (run_id,),
    ).fetchall()

    assert history_types == [("insert", 1000)]


def test_golden_day2_promotion_matches_expected_changes(
    db_connection,
    demo_folder: Path,
):
    promote_day1(db_connection, demo_folder)

    run_id, decision = stage_and_assess(
        db_connection,
        demo_folder / "day2.json",
    )

    assert not decision.needs_review

    result = promote_snapshot(db_connection, run_id)

    assert result.changes == ChangeCounts(
        new=30,
        changed=50,
        deleted=20,
        unchanged=930,
    )

    assert result.active_after == 1010

    # All policy identities remain stored, including deleted ones.
    assert count_rows(db_connection, "policy_current") == 1030

    active_count = db_connection.execute(
        """
        SELECT COUNT(*)
        FROM policy_current
        WHERE is_deleted = FALSE
        """
    ).fetchone()[0]

    deleted_count = db_connection.execute(
        """
        SELECT COUNT(*)
        FROM policy_current
        WHERE is_deleted = TRUE
        """
    ).fetchone()[0]

    assert active_count == 1010
    assert deleted_count == 20

    history_types = dict(
        db_connection.execute(
            """
            SELECT change_type, COUNT(*)
            FROM policy_history
            WHERE run_id = %s
            GROUP BY change_type
            """,
            (run_id,),
        ).fetchall()
    )

    assert history_types == {
        "insert": 30,
        "update": 50,
        "delete": 20,
    }

    # Day 1 creates 1,000 events; Day 2 adds 100.
    assert count_rows(db_connection, "policy_history") == 1100

    examples = db_connection.execute(
        """
        SELECT policy_id, change_type
        FROM policy_history
        WHERE run_id = %s
          AND policy_id IN ('P0001', 'P0021', 'P1001')
        ORDER BY policy_id
        """,
        (run_id,),
    ).fetchall()

    assert examples == [
        ("P0001", "delete"),
        ("P0021", "update"),
        ("P1001", "insert"),
    ]


def test_promotion_requires_a_safety_assessment(
    db_connection,
    demo_folder: Path,
):
    run_id, report = stage_snapshot(
        db_connection,
        demo_folder / "day1.json",
    )

    assert report.passed

    with pytest.raises(
        ValueError,
        match="deletion safety assessment",
    ):
        promote_snapshot(db_connection, run_id)

    assert count_rows(db_connection, "policy_current") == 0
    assert count_rows(db_connection, "policy_history") == 0


def test_large_deletion_cannot_promote_without_approval(
    db_connection,
    demo_folder: Path,
):
    promote_day1(db_connection, demo_folder)

    run_id, decision = stage_reduced_snapshot(
        db_connection,
        demo_folder,
    )

    assert decision.needs_review
    assert decision.changes.deleted == 350

    with pytest.raises(
        ValueError,
        match="awaiting manual approval",
    ):
        promote_snapshot(db_connection, run_id)

    status = db_connection.execute(
        """
        SELECT status
        FROM ingestion_runs
        WHERE run_id = %s
        """,
        (run_id,),
    ).fetchone()[0]

    assert status == "awaiting_review"
    assert count_rows(db_connection, "policy_current") == 1000
    assert count_rows(db_connection, "policy_history") == 1000


def test_approved_large_deletion_can_be_promoted(
    db_connection,
    demo_folder: Path,
):
    promote_day1(db_connection, demo_folder)

    run_id, decision = stage_reduced_snapshot(
        db_connection,
        demo_folder,
    )

    assert decision.needs_review

    approve_snapshot(
        connection=db_connection,
        run_id=run_id,
        reviewer="Test Reviewer",
        reason="Reviewed the synthetic policy removal list.",
    )

    result = promote_snapshot(db_connection, run_id)

    assert result.changes.deleted == 350
    assert result.active_after == 650

    deleted_count = db_connection.execute(
        """
        SELECT COUNT(*)
        FROM policy_current
        WHERE is_deleted = TRUE
        """
    ).fetchone()[0]

    assert deleted_count == 350

    # Deleted identities remain available for audit.
    assert count_rows(db_connection, "policy_current") == 1000
    assert count_rows(db_connection, "policy_history") == 1350

    status = db_connection.execute(
        """
        SELECT status
        FROM ingestion_runs
        WHERE run_id = %s
        """,
        (run_id,),
    ).fetchone()[0]

    assert status == "committed"


def test_same_run_cannot_be_promoted_twice(
    db_connection,
    demo_folder: Path,
):
    run_id = promote_day1(db_connection, demo_folder)

    with pytest.raises(ValueError, match="status"):
        promote_snapshot(db_connection, run_id)

    assert count_rows(db_connection, "policy_current") == 1000
    assert count_rows(db_connection, "policy_history") == 1000


def test_older_snapshot_cannot_replace_newer_state(
    db_connection,
    demo_folder: Path,
):
    promote_day1(db_connection, demo_folder)

    day2_run_id, _ = stage_and_assess(
        db_connection,
        demo_folder / "day2.json",
    )

    promote_snapshot(db_connection, day2_run_id)

    # Attempt to process Day 1 again after Day 2 has committed.
    older_run_id, _ = stage_and_assess(
        db_connection,
        demo_folder / "day1.json",
    )

    with pytest.raises(ValueError, match="newer"):
        promote_snapshot(db_connection, older_run_id)

    active_count = db_connection.execute(
        """
        SELECT COUNT(*)
        FROM policy_current
        WHERE is_deleted = FALSE
        """
    ).fetchone()[0]

    assert active_count == 1010
    assert count_rows(db_connection, "policy_history") == 1100


def test_changed_staging_data_blocks_promotion(
    db_connection,
    demo_folder: Path,
):
    promote_day1(db_connection, demo_folder)

    run_id, decision = stage_and_assess(
        db_connection,
        demo_folder / "day2.json",
    )

    assert not decision.needs_review

    # Change a policy after the safety assessment.
    # P0071 was originally unchanged.
    db_connection.execute(
        """
        UPDATE policy_staging
        SET premium_amount = premium_amount + 1
        WHERE run_id = %s
          AND policy_id = 'P0071'
        """,
        (run_id,),
    )

    with pytest.raises(ValueError, match="recorded safety assessment"):
        promote_snapshot(db_connection, run_id)

    assert count_rows(db_connection, "policy_current") == 1000
    assert count_rows(db_connection, "policy_history") == 1000


def test_simulated_crash_rolls_back_and_retry_succeeds(
    db_connection,
    demo_folder: Path,
    monkeypatch,
):
    promote_day1(db_connection, demo_folder)

    run_id, decision = stage_and_assess(
        db_connection,
        demo_folder / "day2.json",
    )

    assert not decision.needs_review

    def simulate_crash(connection, current_run_id):
        raise RuntimeError(
            "Simulated failure after current policy updates"
        )

    # The surrounding transaction creates a savepoint in our
    # rollback-isolated test connection. The injected exception
    # must undo the promotion writes made after that savepoint.
    with monkeypatch.context() as patch:
        patch.setattr(
            promotion_module,
            "mark_missing_policies_deleted",
            simulate_crash,
        )

        with pytest.raises(RuntimeError, match="Simulated failure"):
            with db_connection.transaction():
                promote_snapshot(db_connection, run_id)

    # Day 1 must still be the current committed business state.
    assert count_rows(db_connection, "policy_current") == 1000
    assert count_rows(db_connection, "policy_history") == 1000

    run_status = db_connection.execute(
        """
        SELECT status
        FROM ingestion_runs
        WHERE run_id = %s
        """,
        (run_id,),
    ).fetchone()[0]

    assert run_status == "validated"

    # Retry the original staged run after the simulated failure.
    with db_connection.transaction():
        result = promote_snapshot(db_connection, run_id)

    assert result.active_after == 1010
    assert count_rows(db_connection, "policy_history") == 1100

    final_status = db_connection.execute(
        """
        SELECT status
        FROM ingestion_runs
        WHERE run_id = %s
        """,
        (run_id,),
    ).fetchone()[0]

    assert final_status == "committed"
