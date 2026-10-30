
"""Integration tests for evidence-based ingestion reports."""

import csv
import json
import os
from pathlib import Path
from uuid import uuid4

import pytest

from policyops.approve import approve_snapshot
from policyops.db import get_connection
from policyops.generate import generate_golden, write_snapshot
from policyops.ingest import ingest_snapshot
from policyops.promote import promote_snapshot
from policyops.report import build_run_report, render_markdown


@pytest.fixture
def db_connection(monkeypatch):
    """Run against the test database and roll back afterward."""

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
            pytest.fail("Reporting tests must use policyops_test.")

        yield connection

    finally:
        connection.rollback()
        connection.close()


@pytest.fixture
def demo_folder(tmp_path: Path) -> Path:
    """Create the standard synthetic policy snapshots."""

    generate_golden(tmp_path, seed=42)
    return tmp_path


def ingest_demo(connection, folder: Path, name: str):
    """Process a generated snapshot through the full pipeline."""

    return ingest_snapshot(
        connection,
        folder / f"{name}.json",
    )


def create_reduced_snapshot(folder: Path) -> None:
    """Create a complete 650-policy snapshot for review."""

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


def create_incomplete_day2(folder: Path) -> None:
    """Remove rows without updating the original source manifest."""

    csv_path = folder / "day2.csv"

    lines = csv_path.read_bytes().splitlines(
        keepends=True
    )

    assert len(lines) == 1011

    # Retain the header and 650 policy rows.
    csv_path.write_bytes(b"".join(lines[:651]))


def get_table_snapshots(connection) -> dict:
    """Capture all five tables so reporting can be checked for writes."""

    tables = {
        "ingestion_runs": "run_id",
        "policy_staging": "run_id, policy_id",
        "policy_current": "policy_id",
        "policy_history": "history_id",
        "quality_events": "event_id",
    }

    snapshots = {}

    for table, ordering in tables.items():
        snapshots[table] = connection.execute(
            f"SELECT * FROM {table} ORDER BY {ordering}"
        ).fetchall()

    return snapshots


def test_committed_golden_run_reports_actual_changes(
    db_connection,
    demo_folder: Path,
):
    first = ingest_demo(
        db_connection,
        demo_folder,
        "day1",
    )

    second = ingest_demo(
        db_connection,
        demo_folder,
        "day2",
    )

    assert first.outcome == "committed"
    assert second.outcome == "committed"

    first_report = build_run_report(
        db_connection,
        first.run_id,
    )

    second_report = build_run_report(
        db_connection,
        second.run_id,
    )

    assert first_report["processing"]["status"] == "committed"
    assert first_report["audit_history"] == {
        "total": 1000,
        "inserted": 1000,
        "updated": 0,
        "deleted": 0,
    }

    assert second_report["processing"]["status"] == "committed"

    assert second_report["source"]["snapshot_date"] == "2026-10-02"
    assert second_report["source"]["expected_row_count"] == 1010
    assert second_report["source"]["source_count"] == 1010
    assert second_report["source"]["accepted_count"] == 1010
    assert second_report["source"]["complete_snapshot"] is True

    assert second_report["recorded_changes"] == {
        "new": 30,
        "changed": 50,
        "deleted": 20,
        "unchanged": 930,
    }

    assert second_report["audit_history"] == {
        "total": 100,
        "inserted": 30,
        "updated": 50,
        "deleted": 20,
    }

    assert second_report["quality_events"] == []
    assert second_report["approval"]["approved_at"] is None

    assert "committed" in second_report["decision"].lower()


def test_incomplete_delivery_report_contains_failed_checks(
    db_connection,
    demo_folder: Path,
):
    first = ingest_demo(
        db_connection,
        demo_folder,
        "day1",
    )

    assert first.outcome == "committed"

    create_incomplete_day2(demo_folder)

    rejected = ingest_demo(
        db_connection,
        demo_folder,
        "day2",
    )

    assert rejected.outcome == "rejected"

    report = build_run_report(
        db_connection,
        rejected.run_id,
    )

    assert report["processing"]["status"] == "rejected"
    assert report["source"]["expected_row_count"] == 1010
    assert report["source"]["source_count"] == 650
    assert report["source"]["complete_snapshot"] is True

    check_ids = {
        event["check_id"]
        for event in report["quality_events"]
    }

    assert "CHECKSUM_MISMATCH" in check_ids
    assert "ROW_COUNT_MISMATCH" in check_ids

    assert report["audit_history"]["total"] == 0
    assert report["recorded_changes"]["deleted"] is None

    assert "rejected" in report["decision"].lower()

    # A rejected delivery must not modify business history.
    total_history = db_connection.execute(
        "SELECT COUNT(*) FROM policy_history"
    ).fetchone()[0]

    assert total_history == 1000


def test_large_deletion_report_contains_review_evidence(
    db_connection,
    demo_folder: Path,
):
    ingest_demo(db_connection, demo_folder, "day1")
    create_reduced_snapshot(demo_folder)

    held = ingest_demo(
        db_connection,
        demo_folder,
        "reduced",
    )

    assert held.outcome == "awaiting_review"

    report = build_run_report(
        db_connection,
        held.run_id,
    )

    assert report["processing"]["status"] == "awaiting_review"

    assert report["source"]["expected_row_count"] == 650
    assert report["source"]["source_count"] == 650
    assert report["source"]["accepted_count"] == 650

    assert report["recorded_changes"] == {
        "new": 0,
        "changed": 0,
        "deleted": 350,
        "unchanged": 650,
    }

    assert report["audit_history"]["total"] == 0

    review_events = [
        event
        for event in report["quality_events"]
        if event["check_id"] == "DELETION_RATE_REVIEW_REQUIRED"
    ]

    assert len(review_events) == 1

    event = review_events[0]

    assert event["severity"] == "warning"
    assert event["observed_value"] == "35.00%"
    assert event["evidence"]["active_before"] == 1000
    assert event["evidence"]["deleted_count"] == 350
    assert event["evidence"]["threshold"] == "0.20"

    assert report["approval"]["approved_at"] is None
    assert "review" in report["decision"].lower()


def test_approved_snapshot_report_still_shows_pending_promotion(
    db_connection,
    demo_folder: Path,
):
    ingest_demo(db_connection, demo_folder, "day1")
    create_reduced_snapshot(demo_folder)

    held = ingest_demo(
        db_connection,
        demo_folder,
        "reduced",
    )

    approve_snapshot(
        connection=db_connection,
        run_id=held.run_id,
        reviewer="Test Reviewer",
        reason="Reviewed the synthetic policy removals.",
    )

    report = build_run_report(
        db_connection,
        held.run_id,
    )

    assert report["processing"]["status"] == "awaiting_review"
    assert report["approval"]["approved_by"] == "Test Reviewer"
    assert report["approval"]["approval_reason"] == (
        "Reviewed the synthetic policy removals."
    )
    assert report["approval"]["approved_at"] is not None

    assert report["audit_history"]["total"] == 0
    assert "not yet been promoted" in report["decision"]


def test_committed_approved_drop_reports_audit_history(
    db_connection,
    demo_folder: Path,
):
    ingest_demo(db_connection, demo_folder, "day1")
    create_reduced_snapshot(demo_folder)

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

    report = build_run_report(
        db_connection,
        held.run_id,
    )

    assert report["processing"]["status"] == "committed"

    assert report["recorded_changes"]["deleted"] == 350

    assert report["audit_history"] == {
        "total": 350,
        "inserted": 0,
        "updated": 0,
        "deleted": 350,
    }

    assert report["approval"]["approved_by"] == "Test Reviewer"
    assert report["approval"]["approved_at"] is not None
    assert "committed" in report["decision"].lower()


def test_report_is_valid_json(
    db_connection,
    demo_folder: Path,
):
    result = ingest_demo(
        db_connection,
        demo_folder,
        "day1",
    )

    report = build_run_report(
        db_connection,
        result.run_id,
    )

    encoded = json.dumps(
        report,
        indent=2,
        sort_keys=True,
    )

    decoded = json.loads(encoded)

    assert decoded["run_id"] == str(result.run_id)
    assert decoded["processing"]["status"] == "committed"
    assert decoded["audit_history"]["total"] == 1000

    # The report should contain portable text timestamps.
    assert isinstance(
        decoded["processing"]["started_at"],
        str,
    )

    assert isinstance(
        decoded["processing"]["finished_at"],
        str,
    )


def test_markdown_report_explains_validation_failure(
    db_connection,
    demo_folder: Path,
):
    create_incomplete_day2(demo_folder)

    rejected = ingest_demo(
        db_connection,
        demo_folder,
        "day2",
    )

    assert rejected.outcome == "rejected"

    report = build_run_report(
        db_connection,
        rejected.run_id,
    )

    markdown = render_markdown(report)

    assert markdown.startswith("# PolicyOps Incident Report")
    assert "## Source delivery" in markdown
    assert "## Quality and review evidence" in markdown
    assert "## Interpretation limits" in markdown

    assert "CHECKSUM_MISMATCH" in markdown
    assert "ROW_COUNT_MISMATCH" in markdown

    assert "| Declared row count | 1010 |" in markdown
    assert "| Received row count | 650 |" in markdown

    assert "Snapshot rejected" in markdown


def test_markdown_report_explains_deletion_review(
    db_connection,
    demo_folder: Path,
):
    ingest_demo(db_connection, demo_folder, "day1")
    create_reduced_snapshot(demo_folder)

    held = ingest_demo(
        db_connection,
        demo_folder,
        "reduced",
    )

    report = build_run_report(
        db_connection,
        held.run_id,
    )

    markdown = render_markdown(report)

    assert "DELETION_RATE_REVIEW_REQUIRED" in markdown
    assert "35.00%" in markdown
    assert "| Deleted | 350 |" in markdown

    assert "Snapshot held for manual review" in markdown
    assert "These are assessment counts" in markdown

    # Reporting a warning must not imply approval.
    assert "- Reviewer: Not recorded" in markdown


def test_unknown_run_has_no_report(db_connection):
    unknown_run_id = uuid4()

    with pytest.raises(
        ValueError,
        match="does not exist",
    ):
        build_run_report(
            db_connection,
            unknown_run_id,
        )


def test_generating_reports_does_not_modify_database(
    db_connection,
    demo_folder: Path,
):
    first = ingest_demo(
        db_connection,
        demo_folder,
        "day1",
    )

    create_reduced_snapshot(demo_folder)

    held = ingest_demo(
        db_connection,
        demo_folder,
        "reduced",
    )

    assert first.outcome == "committed"
    assert held.outcome == "awaiting_review"

    before = get_table_snapshots(db_connection)

    first_report = build_run_report(
        db_connection,
        first.run_id,
    )

    held_report = build_run_report(
        db_connection,
        held.run_id,
    )

    # Rendering both formats must also leave data unchanged.
    json.dumps(first_report)
    json.dumps(held_report)

    render_markdown(first_report)
    render_markdown(held_report)

    after = get_table_snapshots(db_connection)

    assert before == after
