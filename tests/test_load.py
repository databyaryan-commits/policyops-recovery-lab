
"""Integration tests for snapshot registration and staging."""

import csv
import hashlib
import json
import os
from pathlib import Path

import psycopg
import pytest

from policyops.db import get_connection
from policyops.generate import CSV_COLUMNS, generate_golden
from policyops.load import stage_snapshot


@pytest.fixture
def db_connection(monkeypatch):
    """Connect only to the test database and roll back afterward."""

    test_database = os.getenv(
        "POLICYOPS_TEST_DATABASE_URL",
        "dbname=policyops_test",
    )
    monkeypatch.setenv("POLICYOPS_DATABASE_URL", test_database)

    connection = get_connection()
    database_info = connection.execute(
        "SELECT current_database()"
    ).fetchone()

    if database_info is None or database_info[0] != "policyops_test":
        connection.rollback()
        connection.close()
        pytest.fail("Loader tests must use policyops_test.")

    try:
        yield connection
    finally:
        connection.rollback()
        connection.close()


@pytest.fixture
def demo_folder(tmp_path: Path) -> Path:
    generate_golden(tmp_path, seed=42)
    return tmp_path


def issue_ids(report) -> set[str]:
    return {issue.check_id for issue in report.issues}


def shorten_day1_csv(folder: Path) -> bytes:
    csv_path = folder / "day1.csv"
    lines = csv_path.read_bytes().splitlines(keepends=True)

    assert len(lines) == 1001

    shortened_csv = b"".join(lines[:651])
    csv_path.write_bytes(shortened_csv)

    return shortened_csv


def update_day1_manifest(folder: Path, **changes) -> None:
    manifest_path = folder / "day1.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    manifest.update(changes)

    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


@pytest.mark.parametrize(
    ("snapshot", "expected_count"),
    [("day1", 1000), ("day2", 1010)],
)
def test_valid_snapshot_is_staged(
    db_connection,
    demo_folder: Path,
    snapshot: str,
    expected_count: int,
):
    run_id, report = stage_snapshot(
        db_connection,
        demo_folder / f"{snapshot}.json",
    )

    assert report.passed
    assert report.accepted_count == expected_count

    run = db_connection.execute(
        """
        SELECT status, source_count, accepted_count
        FROM ingestion_runs
        WHERE run_id = %s
        """,
        (run_id,),
    ).fetchone()

    assert run == ("validated", expected_count, expected_count)

    staged_count = db_connection.execute(
        "SELECT COUNT(*) FROM policy_staging WHERE run_id = %s",
        (run_id,),
    ).fetchone()[0]

    assert staged_count == expected_count

    # Staging must not change business state or audit history.
    current_count = db_connection.execute(
        "SELECT COUNT(*) FROM policy_current"
    ).fetchone()[0]

    history_count = db_connection.execute(
        "SELECT COUNT(*) FROM policy_history"
    ).fetchone()[0]

    assert current_count == 0
    assert history_count == 0


def test_incomplete_delivery_is_rejected_and_recorded(
    db_connection,
    demo_folder: Path,
):
    shorten_day1_csv(demo_folder)

    run_id, report = stage_snapshot(
        db_connection,
        demo_folder / "day1.json",
    )

    assert not report.passed
    assert report.source_count == 650

    problems = issue_ids(report)
    assert "CHECKSUM_MISMATCH" in problems
    assert "ROW_COUNT_MISMATCH" in problems

    status = db_connection.execute(
        "SELECT status FROM ingestion_runs WHERE run_id = %s",
        (run_id,),
    ).fetchone()[0]

    assert status == "rejected"

    recorded_checks = {
        row[0]
        for row in db_connection.execute(
            "SELECT check_id FROM quality_events WHERE run_id = %s",
            (run_id,),
        ).fetchall()
    }

    assert "CHECKSUM_MISMATCH" in recorded_checks
    assert "ROW_COUNT_MISMATCH" in recorded_checks

    staged_count = db_connection.execute(
        "SELECT COUNT(*) FROM policy_staging WHERE run_id = %s",
        (run_id,),
    ).fetchone()[0]

    assert staged_count == 0


def test_duplicate_policy_id_cannot_be_staged(
    db_connection,
    demo_folder: Path,
):
    csv_path = demo_folder / "day1.csv"

    with csv_path.open("r", encoding="utf-8", newline="") as file:
        rows = list(csv.DictReader(file))

    rows[1]["policy_id"] = rows[0]["policy_id"]

    with csv_path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(
            file,
            fieldnames=CSV_COLUMNS,
            lineterminator="\n",
        )
        writer.writeheader()
        writer.writerows(rows)

    update_day1_manifest(
        demo_folder,
        sha256=hashlib.sha256(csv_path.read_bytes()).hexdigest(),
    )

    run_id, report = stage_snapshot(
        db_connection,
        demo_folder / "day1.json",
    )

    assert not report.passed
    assert "POLICY_ID_DUPLICATE" in issue_ids(report)

    staged_count = db_connection.execute(
        "SELECT COUNT(*) FROM policy_staging WHERE run_id = %s",
        (run_id,),
    ).fetchone()[0]

    assert staged_count == 0

    recorded_check = db_connection.execute(
        """
        SELECT COUNT(*)
        FROM quality_events
        WHERE run_id = %s
          AND check_id = 'POLICY_ID_DUPLICATE'
        """,
        (run_id,),
    ).fetchone()[0]

    assert recorded_check == 1


def test_declared_drop_is_staged_but_not_promoted(
    db_connection,
    demo_folder: Path,
):
    shortened_csv = shorten_day1_csv(demo_folder)

    update_day1_manifest(
        demo_folder,
        expected_row_count=650,
        sha256=hashlib.sha256(shortened_csv).hexdigest(),
        complete_snapshot=True,
    )

    run_id, report = stage_snapshot(
        db_connection,
        demo_folder / "day1.json",
    )

    assert report.passed
    assert report.accepted_count == 650

    staged_count = db_connection.execute(
        "SELECT COUNT(*) FROM policy_staging WHERE run_id = %s",
        (run_id,),
    ).fetchone()[0]

    assert staged_count == 650

    # Passing file checks does not authorize a large deletion.
    assert db_connection.execute(
        "SELECT COUNT(*) FROM policy_current"
    ).fetchone()[0] == 0

    assert db_connection.execute(
        "SELECT COUNT(*) FROM policy_history"
    ).fetchone()[0] == 0


def test_missing_manifest_creates_a_rejected_run(
    db_connection,
    demo_folder: Path,
):
    run_id, report = stage_snapshot(
        db_connection,
        demo_folder / "missing.json",
    )

    assert not report.passed
    assert "MANIFEST_MISSING" in issue_ids(report)

    status = db_connection.execute(
        "SELECT status FROM ingestion_runs WHERE run_id = %s",
        (run_id,),
    ).fetchone()[0]

    assert status == "rejected"

    recorded_count = db_connection.execute(
        """
        SELECT COUNT(*)
        FROM quality_events
        WHERE run_id = %s
          AND check_id = 'MANIFEST_MISSING'
        """,
        (run_id,),
    ).fetchone()[0]

    assert recorded_count == 1


def test_staging_error_rolls_back_the_attempt(
    db_connection,
    demo_folder: Path,
):
    # Temporarily make all staging inserts fail.
    # The added constraint is inside the test transaction.
    db_connection.execute(
        """
        ALTER TABLE policy_staging
        ADD CONSTRAINT test_reject_staging CHECK (FALSE)
        """
    )

    with pytest.raises(psycopg.Error):
        stage_snapshot(
            db_connection,
            demo_folder / "day1.json",
        )

    # Clear the failed transaction, including its temporary DDL.
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
