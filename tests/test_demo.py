
"""Integration tests for reproducible PolicyOps incident demonstrations."""

import json
import os
from pathlib import Path

import psycopg
import pytest

import policyops.demo as demo_module
from policyops.demo import run_deletion_contrast


EXPECTED_REPORT_FILES = {
    "baseline_committed.json",
    "baseline_committed.md",
    "incomplete_rejected.json",
    "incomplete_rejected.md",
    "valid_drop_awaiting_review.json",
    "valid_drop_awaiting_review.md",
    "incident_comparison.md",
}


@pytest.fixture
def demo_reports(tmp_path: Path) -> Path:
    """Run the demonstration and return its generated report folder."""

    output_dir = tmp_path / "incident_reports"

    run_deletion_contrast(output_dir)

    return output_dir


def read_json_report(folder: Path, filename: str) -> dict:
    """Read a generated incident report."""

    report_path = folder / filename

    return json.loads(
        report_path.read_text(encoding="utf-8")
    )


def capture_test_database_state() -> tuple:
    """Inspect public tables and temporary schemas without writing."""

    database_url = os.getenv(
        "POLICYOPS_TEST_DATABASE_URL",
        "dbname=policyops_test",
    )

    with psycopg.connect(database_url) as connection:
        database_name = connection.execute(
            "SELECT current_database()"
        ).fetchone()[0]

        if database_name != "policyops_test":
            pytest.fail(
                "Demo tests must use policyops_test."
            )

        table_counts = connection.execute(
            """
            SELECT
                (SELECT COUNT(*) FROM public.ingestion_runs),
                (SELECT COUNT(*) FROM public.policy_staging),
                (SELECT COUNT(*) FROM public.policy_current),
                (SELECT COUNT(*) FROM public.policy_history),
                (SELECT COUNT(*) FROM public.quality_events)
            """
        ).fetchone()

        temporary_schemas = tuple(
            row[0]
            for row in connection.execute(
                """
                SELECT nspname
                FROM pg_namespace
                WHERE LEFT(nspname, LENGTH('policyops_demo_'))
                      = 'policyops_demo_'
                ORDER BY nspname
                """
            ).fetchall()
        )

    return table_counts, temporary_schemas


def test_demo_generates_correct_incident_evidence(
    demo_reports: Path,
):
    """Verify the recorded outcomes for both deletion scenarios."""

    baseline = read_json_report(
        demo_reports,
        "baseline_committed.json",
    )

    incomplete = read_json_report(
        demo_reports,
        "incomplete_rejected.json",
    )

    valid = read_json_report(
        demo_reports,
        "valid_drop_awaiting_review.json",
    )

    # The baseline establishes 1,000 current policies.
    assert baseline["processing"]["status"] == "committed"
    assert baseline["source"]["accepted_count"] == 1000
    assert baseline["audit_history"]["inserted"] == 1000

    # The incomplete delivery declares 1,000 rows but
    # contains only 650, with the original checksum.
    assert incomplete["processing"]["status"] == "rejected"
    assert incomplete["source"]["expected_row_count"] == 1000
    assert incomplete["source"]["source_count"] == 650
    assert incomplete["source"]["complete_snapshot"] is True

    failed_checks = {
        event["check_id"]
        for event in incomplete["quality_events"]
    }

    assert "CHECKSUM_MISMATCH" in failed_checks
    assert "ROW_COUNT_MISMATCH" in failed_checks

    assert incomplete["recorded_changes"]["deleted"] is None
    assert incomplete["audit_history"]["total"] == 0

    # The second delivery is a consistent 650-row snapshot,
    # but it implies 350 deletions against the baseline.
    assert valid["processing"]["status"] == "awaiting_review"
    assert valid["source"]["expected_row_count"] == 650
    assert valid["source"]["source_count"] == 650
    assert valid["source"]["accepted_count"] == 650

    assert valid["recorded_changes"] == {
        "new": 0,
        "changed": 0,
        "deleted": 350,
        "unchanged": 650,
    }

    review_events = [
        event
        for event in valid["quality_events"]
        if event["check_id"] == "DELETION_RATE_REVIEW_REQUIRED"
    ]

    assert len(review_events) == 1

    review_event = review_events[0]

    assert review_event["severity"] == "warning"
    assert review_event["observed_value"] == "35.00%"
    assert review_event["evidence"]["active_before"] == 1000
    assert review_event["evidence"]["deleted_count"] == 350
    assert review_event["evidence"]["threshold"] == "0.20"

    # No approval or business deletion occurred.
    assert valid["approval"]["approved_at"] is None
    assert valid["audit_history"]["total"] == 0


def test_demo_writes_consistent_markdown_reports(
    demo_reports: Path,
):
    """Verify the portfolio explanation matches the JSON evidence."""

    generated_files = {
        path.name
        for path in demo_reports.iterdir()
        if path.is_file()
    }

    assert generated_files == EXPECTED_REPORT_FILES

    incomplete_markdown = (
        demo_reports / "incomplete_rejected.md"
    ).read_text(encoding="utf-8")

    valid_markdown = (
        demo_reports / "valid_drop_awaiting_review.md"
    ).read_text(encoding="utf-8")

    comparison = (
        demo_reports / "incident_comparison.md"
    ).read_text(encoding="utf-8")

    assert "Snapshot rejected" in incomplete_markdown
    assert "CHECKSUM_MISMATCH" in incomplete_markdown
    assert "ROW_COUNT_MISMATCH" in incomplete_markdown

    assert "Snapshot held for manual review" in valid_markdown
    assert "DELETION_RATE_REVIEW_REQUIRED" in valid_markdown
    assert "35.00%" in valid_markdown
    assert "| Deleted | 350 |" in valid_markdown

    assert "# PolicyOps: Two 35% Drop Scenarios" in comparison
    assert "| Received policies | 650 | 650 |" in comparison
    assert "| Checksum consistent with CSV | No | Yes |" in comparison
    assert "- Active policies: 1,000" in comparison

    assert (
        "no 350-policy deletion was committed."
        in comparison
    )

    # The comparison must link to generated evidence files.
    assert "baseline_committed.md" in comparison
    assert "incomplete_rejected.md" in comparison
    assert "valid_drop_awaiting_review.md" in comparison


def test_demo_preserves_public_tables_and_cleans_up_schema(
    tmp_path: Path,
):
    """A successful demo must leave the test database unchanged."""

    before = capture_test_database_state()

    output_dir = tmp_path / "incident_reports"

    run_deletion_contrast(output_dir)

    after = capture_test_database_state()

    assert before == after

    assert (
        output_dir / "incident_comparison.md"
    ).is_file()


def test_demo_cleans_up_schema_when_report_generation_fails(
    tmp_path: Path,
    monkeypatch,
):
    """A report-writing failure must not leave demo tables behind."""

    before = capture_test_database_state()

    def fail_to_save_report(output_dir, filename, report):
        raise OSError("Simulated report-writing failure")

    monkeypatch.setattr(
        demo_module,
        "save_report",
        fail_to_save_report,
    )

    with pytest.raises(
        OSError,
        match="Simulated report-writing failure",
    ):
        run_deletion_contrast(
            tmp_path / "failed_report",
        )

    after = capture_test_database_state()

    assert before == after
