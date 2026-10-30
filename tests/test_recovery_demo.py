
"""Integration tests for the reproducible failure-recovery demo."""

import json
import os
from pathlib import Path

import psycopg
import pytest

import policyops.recovery_demo as recovery_module
from policyops.recovery_demo import run_recovery_demo


EXPECTED_FILES = {
    "baseline_committed.json",
    "baseline_committed.md",
    "failure_recorded.json",
    "failure_recorded.md",
    "retry_committed.json",
    "retry_committed.md",
    "recovery_timeline.md",
}


@pytest.fixture
def recovery_reports(tmp_path: Path) -> Path:
    """Run the recovery scenario and return its evidence folder."""

    output_dir = tmp_path / "recovery_reports"

    run_recovery_demo(output_dir)

    return output_dir


def read_report(folder: Path, filename: str) -> dict:
    """Read a generated JSON report."""

    return json.loads(
        (folder / filename).read_text(encoding="utf-8")
    )


def capture_test_database_state() -> tuple:
    """Inspect public tables and temporary recovery schemas."""

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
                "Recovery tests must use policyops_test."
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
                WHERE LEFT(
                    nspname,
                    LENGTH('policyops_recovery_')
                ) = 'policyops_recovery_'
                ORDER BY nspname
                """
            ).fetchall()
        )

    return table_counts, temporary_schemas


def test_recovery_demo_generates_correct_database_evidence(
    recovery_reports: Path,
):
    """Check committed, failed, and recovered ingestion reports."""

    baseline = read_report(
        recovery_reports,
        "baseline_committed.json",
    )

    failed = read_report(
        recovery_reports,
        "failure_recorded.json",
    )

    recovered = read_report(
        recovery_reports,
        "retry_committed.json",
    )

    # Day 1 establishes the starting state.
    assert baseline["processing"]["status"] == "committed"
    assert baseline["source"]["accepted_count"] == 1000

    assert baseline["audit_history"] == {
        "total": 1000,
        "inserted": 1000,
        "updated": 0,
        "deleted": 0,
    }

    # The simulated Day 2 failure must be recorded after
    # rollback, with no committed business history.
    assert failed["processing"]["status"] == "failed"

    assert failed["source"]["snapshot_date"] == "2026-10-02"
    assert failed["source"]["expected_row_count"] == 1010

    assert failed["processing"]["failure_reason"] == (
        "RuntimeError: "
        "Simulated failure after audit-history write"
    )

    assert failed["audit_history"]["total"] == 0

    assert len(failed["quality_events"]) == 1

    failure_event = failed["quality_events"][0]

    assert failure_event["check_id"] == (
        "PROCESSING_TRANSACTION_FAILED"
    )

    assert failure_event["severity"] == "error"
    assert failure_event["evidence"]["stage"] == "ingestion"

    # A fresh attempt commits the full Day 2 changes.
    assert recovered["processing"]["status"] == "committed"

    assert recovered["source"]["snapshot_date"] == "2026-10-02"
    assert recovered["source"]["accepted_count"] == 1010

    assert recovered["recorded_changes"] == {
        "new": 30,
        "changed": 50,
        "deleted": 20,
        "unchanged": 930,
    }

    assert recovered["audit_history"] == {
        "total": 100,
        "inserted": 30,
        "updated": 50,
        "deleted": 20,
    }

    # The failed attempt and successful retry are different
    # ingestion runs, even though they use the same delivery.
    assert failed["run_id"] != recovered["run_id"]


def test_recovery_timeline_matches_generated_reports(
    recovery_reports: Path,
):
    """Check the human-readable timeline and evidence links."""

    generated_files = {
        path.name
        for path in recovery_reports.iterdir()
        if path.is_file()
    }

    assert generated_files == EXPECTED_FILES

    timeline = (
        recovery_reports / "recovery_timeline.md"
    ).read_text(encoding="utf-8")

    failed_markdown = (
        recovery_reports / "failure_recorded.md"
    ).read_text(encoding="utf-8")

    retry_markdown = (
        recovery_reports / "retry_committed.md"
    ).read_text(encoding="utf-8")

    assert (
        "# PolicyOps: Failure and Recovery Demonstration"
        in timeline
    )

    assert (
        "| Active policies | 1000 | 1000 | 1010 |"
        in timeline
    )

    assert (
        "| Committed audit-history rows | 1000 | 1000 | 1100 |"
        in timeline
    )

    assert (
        "| Recorded failed runs | 0 | 1 | 1 |"
        in timeline
    )

    assert "## 2. Injected processing failure" in timeline
    assert "## 3. Persistent failure evidence" in timeline
    assert "## 4. Successful retry" in timeline
    assert "## 5. Replay verification" in timeline

    assert "baseline_committed.md" in timeline
    assert "failure_recorded.md" in timeline
    assert "retry_committed.md" in timeline

    assert "PROCESSING_TRANSACTION_FAILED" in failed_markdown
    assert "Simulated failure after audit-history write" in (
        failed_markdown
    )

    assert "| New | 30 |" in retry_markdown
    assert "| Changed | 50 |" in retry_markdown
    assert "| Deleted | 20 |" in retry_markdown

    # The documentation must not claim a real machine crash.
    assert "operating-system crash" in timeline


def test_recovery_demo_preserves_database_and_cleans_up(
    tmp_path: Path,
):
    """A successful demo must leave no changes in public tables."""

    before = capture_test_database_state()

    output_dir = tmp_path / "recovery_reports"

    run_recovery_demo(output_dir)

    after = capture_test_database_state()

    assert before == after
    assert after[1] == ()

    assert (
        output_dir / "recovery_timeline.md"
    ).is_file()


def test_recovery_demo_cleans_up_when_report_writing_fails(
    tmp_path: Path,
    monkeypatch,
):
    """A failure while saving reports must not leave demo tables."""

    before = capture_test_database_state()

    def fail_to_save_report(output_dir, filename, report):
        raise OSError("Simulated recovery report-writing failure")

    monkeypatch.setattr(
        recovery_module,
        "save_report",
        fail_to_save_report,
    )

    with pytest.raises(
        OSError,
        match="Simulated recovery report-writing failure",
    ):
        run_recovery_demo(
            tmp_path / "failed_recovery_reports"
        )

    after = capture_test_database_state()

    assert before == after
    assert after[1] == ()
