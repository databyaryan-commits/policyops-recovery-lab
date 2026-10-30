
"""Integration tests for persistent failure evidence and safe retries."""

import json
import os
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql

import policyops.promote as promote_module
from policyops.failure import record_failed_attempt
from policyops.generate import generate_golden
from policyops.ingest import ingest_snapshot
from policyops.report import build_run_report


@pytest.fixture
def isolated_failure_schema():
    """Create disposable PolicyOps tables in the test database."""

    database_url = os.getenv(
        "POLICYOPS_TEST_DATABASE_URL",
        "dbname=policyops_test",
    )

    schema_name = f"policyops_failure_{uuid4().hex}"

    with psycopg.connect(
        database_url,
        autocommit=True,
    ) as admin:
        database_name = admin.execute(
            "SELECT current_database()"
        ).fetchone()[0]

        if database_name != "policyops_test":
            pytest.fail(
                "Failure tests must use policyops_test."
            )

        schema_created = False

        try:
            admin.execute(
                sql.SQL("CREATE SCHEMA {}").format(
                    sql.Identifier(schema_name)
                )
            )
            schema_created = True

            admin.execute(
                sql.SQL("SET search_path TO {}").format(
                    sql.Identifier(schema_name)
                )
            )

            schema_file = (
                Path(__file__).resolve().parents[1]
                / "sql"
                / "schema.sql"
            )

            admin.execute(
                schema_file.read_text(encoding="utf-8")
            )

            yield database_url, schema_name

        finally:
            if schema_created:
                admin.execute(
                    sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(
                        sql.Identifier(schema_name)
                    )
                )


def open_test_connection(database_url: str, schema_name: str):
    """Open a separate transaction against the temporary schema."""

    connection = psycopg.connect(database_url)

    try:
        connection.execute(
            sql.SQL("SET search_path TO {}").format(
                sql.Identifier(schema_name)
            )
        )
    except Exception:
        connection.close()
        raise

    return connection


def test_failure_is_recorded_after_rollback_and_retry_succeeds(
    isolated_failure_schema,
    tmp_path: Path,
    monkeypatch,
):
    """Rollback, record failure, retry, then replay without duplication."""

    database_url, schema_name = isolated_failure_schema

    generate_golden(tmp_path, seed=42)

    day1_manifest = tmp_path / "day1.json"
    day2_manifest = tmp_path / "day2.json"

    # Establish a committed baseline of 1,000 policies.
    with open_test_connection(
        database_url,
        schema_name,
    ) as connection:
        baseline = ingest_snapshot(
            connection,
            day1_manifest,
        )

    assert baseline.outcome == "committed"

    # Inject a failure after promotion has inserted audit
    # history, but before current policies are updated.
    def fail_after_history(connection, run_id, snapshot_date):
        written_history = connection.execute(
            """
            SELECT COUNT(*)
            FROM policy_history
            WHERE run_id = %s
            """,
            (run_id,),
        ).fetchone()[0]

        assert written_history == 100

        raise RuntimeError(
            "Simulated failure after audit-history write"
        )

    with monkeypatch.context() as patch:
        patch.setattr(
            promote_module,
            "update_current_policies",
            fail_after_history,
        )

        with pytest.raises(
            RuntimeError,
            match="Simulated failure after audit-history write",
        ):
            with open_test_connection(
                database_url,
                schema_name,
            ) as connection:
                ingest_snapshot(
                    connection,
                    day2_manifest,
                )

    # The failed processing transaction must disappear
    # completely, including its uncommitted history rows.
    with open_test_connection(
        database_url,
        schema_name,
    ) as connection:
        counts = connection.execute(
            """
            SELECT
                (SELECT COUNT(*) FROM ingestion_runs),
                (SELECT COUNT(*) FROM policy_staging),
                (SELECT COUNT(*) FROM policy_current),
                (SELECT COUNT(*) FROM policy_history),
                (SELECT COUNT(*) FROM quality_events)
            """
        ).fetchone()

    assert counts == (1, 1000, 1000, 1000, 0)

    # A fresh transaction writes the incident evidence
    # after the failed transaction has rolled back.
    with open_test_connection(
        database_url,
        schema_name,
    ) as connection:
        failed_run_id = record_failed_attempt(
            connection,
            day2_manifest,
            "Simulated failure after audit-history write",
            stage="promotion",
        )

    with open_test_connection(
        database_url,
        schema_name,
    ) as connection:
        report = build_run_report(
            connection,
            failed_run_id,
        )

        failed_run_history = connection.execute(
            """
            SELECT COUNT(*)
            FROM policy_history
            WHERE run_id = %s
            """,
            (failed_run_id,),
        ).fetchone()[0]

    assert report["processing"]["status"] == "failed"
    assert report["processing"]["failure_reason"] == (
        "Simulated failure after audit-history write"
    )

    assert report["source"]["snapshot_date"] == "2026-10-02"
    assert report["audit_history"]["total"] == 0
    assert failed_run_history == 0

    assert len(report["quality_events"]) == 1
    assert report["quality_events"][0]["check_id"] == (
        "PROCESSING_TRANSACTION_FAILED"
    )
    assert report["quality_events"][0]["severity"] == "error"
    assert report["quality_events"][0]["evidence"]["stage"] == (
        "promotion"
    )

    # Retry the unchanged valid source. The failed attempt
    # must not block a new, successful promotion.
    with open_test_connection(
        database_url,
        schema_name,
    ) as connection:
        retry = ingest_snapshot(
            connection,
            day2_manifest,
        )

    assert retry.outcome == "committed"
    assert retry.changes.new == 30
    assert retry.changes.changed == 50
    assert retry.changes.deleted == 20
    assert retry.changes.unchanged == 930

    # Running it again should be an ordinary replay.
    with open_test_connection(
        database_url,
        schema_name,
    ) as connection:
        replay = ingest_snapshot(
            connection,
            day2_manifest,
        )

    assert replay.outcome == "replayed"
    assert replay.run_id == retry.run_id

    with open_test_connection(
        database_url,
        schema_name,
    ) as connection:
        final_counts = connection.execute(
            """
            SELECT
                (SELECT COUNT(*) FROM ingestion_runs),
                (SELECT COUNT(*) FROM ingestion_runs
                 WHERE status = 'failed'),
                (SELECT COUNT(*) FROM ingestion_runs
                 WHERE status = 'committed'),
                (SELECT COUNT(*) FROM policy_current
                 WHERE is_deleted = FALSE),
                (SELECT COUNT(*) FROM policy_history),
                (SELECT COUNT(*) FROM quality_events)
            """
        ).fetchone()

        retry_history = connection.execute(
            """
            SELECT COUNT(*)
            FROM policy_history
            WHERE run_id = %s
            """,
            (retry.run_id,),
        ).fetchone()[0]

    assert final_counts == (
        3,     # Baseline, failed attempt, successful retry
        1,     # Persisted failed attempt
        2,     # Day 1 and Day 2 committed
        1010,  # Active policies after Day 2
        1100,  # 1,000 baseline + 100 Day 2 changes
        1,     # Failure quality event
    )

    assert retry_history == 100


def test_failure_record_sanitizes_untrusted_manifest(
    isolated_failure_schema,
    tmp_path: Path,
):
    """Bad source metadata must not prevent recording an incident."""

    database_url, schema_name = isolated_failure_schema

    manifest_path = tmp_path / "invalid.json"

    manifest_path.write_text(
        json.dumps(
            {
                "snapshot_date": "not-a-date",
                "csv_file": "../private.csv",
                "sha256": "invalid-checksum",
                "schema_version": 15,
                "complete_snapshot": "yes",
                "expected_row_count": -350,
            }
        ),
        encoding="utf-8",
    )

    with open_test_connection(
        database_url,
        schema_name,
    ) as connection:
        run_id = record_failed_attempt(
            connection,
            manifest_path,
            "Could not process source metadata",
            stage="ingestion",
        )

    with open_test_connection(
        database_url,
        schema_name,
    ) as connection:
        report = build_run_report(
            connection,
            run_id,
        )

        business_counts = connection.execute(
            """
            SELECT
                (SELECT COUNT(*) FROM policy_staging),
                (SELECT COUNT(*) FROM policy_current),
                (SELECT COUNT(*) FROM policy_history)
            """
        ).fetchone()

    assert report["processing"]["status"] == "failed"
    assert report["source"]["snapshot_date"] is None
    assert report["source"]["source_file"] is None
    assert report["source"]["checksum_sha256"] is None
    assert report["source"]["schema_version"] is None
    assert report["source"]["complete_snapshot"] is None
    assert report["source"]["expected_row_count"] is None

    assert report["quality_events"][0]["evidence"]["stage"] == (
        "ingestion"
    )

    assert business_counts == (0, 0, 0)


def test_failure_record_and_event_are_atomic(
    isolated_failure_schema,
    tmp_path: Path,
):
    """If evidence insertion fails, its parent run also rolls back."""

    database_url, schema_name = isolated_failure_schema

    manifest_path = tmp_path / "missing.json"

    # Make this temporary schema reject the event, so we
    # can test rollback of the preceding ingestion_runs insert.
    with open_test_connection(
        database_url,
        schema_name,
    ) as connection:
        connection.execute(
            """
            ALTER TABLE quality_events
            ADD CONSTRAINT reject_test_failure_event
            CHECK (check_id <> 'PROCESSING_TRANSACTION_FAILED')
            """
        )

    with pytest.raises(psycopg.errors.CheckViolation):
        with open_test_connection(
            database_url,
            schema_name,
        ) as connection:
            record_failed_attempt(
                connection,
                manifest_path,
                "Simulated evidence storage failure",
            )

    with open_test_connection(
        database_url,
        schema_name,
    ) as connection:
        remaining = connection.execute(
            """
            SELECT
                (SELECT COUNT(*) FROM ingestion_runs),
                (SELECT COUNT(*) FROM quality_events)
            """
        ).fetchone()

    assert remaining == (0, 0)
