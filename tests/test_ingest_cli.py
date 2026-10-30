
"""Test automatic failure recording through the ingestion CLI."""

import os
import sys
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql

import policyops.ingest as ingest_module
import policyops.promote as promote_module
from policyops.generate import generate_golden
from policyops.report import build_run_report


@pytest.fixture
def isolated_cli_schema():
    """Build temporary PolicyOps tables in the test database."""

    database_url = os.getenv(
        "POLICYOPS_TEST_DATABASE_URL",
        "dbname=policyops_test",
    )

    schema_name = f"policyops_cli_{uuid4().hex}"

    with psycopg.connect(
        database_url,
        autocommit=True,
    ) as admin:
        database_name = admin.execute(
            "SELECT current_database()"
        ).fetchone()[0]

        if database_name != "policyops_test":
            pytest.fail(
                "CLI recovery tests must use policyops_test."
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
                    sql.SQL(
                        "DROP SCHEMA IF EXISTS {} CASCADE"
                    ).format(
                        sql.Identifier(schema_name)
                    )
                )


def open_schema_connection(
    database_url: str,
    schema_name: str,
):
    """Open a fresh connection to the temporary schema."""

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


def count_business_state(connection) -> tuple:
    """Count persisted runs, policies, history, and quality events."""

    return connection.execute(
        """
        SELECT
            (SELECT COUNT(*) FROM ingestion_runs),
            (SELECT COUNT(*) FROM policy_staging),
            (SELECT COUNT(*) FROM policy_current
             WHERE is_deleted = FALSE),
            (SELECT COUNT(*) FROM policy_history),
            (SELECT COUNT(*) FROM quality_events)
        """
    ).fetchone()


def test_cli_records_failure_then_allows_safe_retry(
    isolated_cli_schema,
    tmp_path: Path,
    monkeypatch,
    capsys,
):
    """A promotion failure must roll back before evidence is saved."""

    database_url, schema_name = isolated_cli_schema

    generate_golden(tmp_path, seed=42)

    day1_manifest = tmp_path / "day1.json"
    day2_manifest = tmp_path / "day2.json"

    # Establish the committed Day 1 baseline.
    with open_schema_connection(
        database_url,
        schema_name,
    ) as connection:
        baseline = ingest_module.ingest_snapshot(
            connection,
            day1_manifest,
        )

    assert baseline.outcome == "committed"

    # Route the CLI's original and failure-recording
    # connections into the same temporary test schema.
    def get_test_connection():
        return open_schema_connection(
            database_url,
            schema_name,
        )

    monkeypatch.setattr(
        ingest_module,
        "get_connection",
        get_test_connection,
    )

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "policyops.ingest",
            "--manifest",
            str(day2_manifest),
        ],
    )

    # Inject a failure after 100 Day 2 audit-history rows
    # have been written inside the processing transaction.
    def interrupt_promotion(
        connection,
        run_id,
        snapshot_date,
    ):
        history_written = connection.execute(
            """
            SELECT COUNT(*)
            FROM policy_history
            WHERE run_id = %s
            """,
            (run_id,),
        ).fetchone()[0]

        assert history_written == 100

        raise RuntimeError(
            "Simulated failure after audit-history write"
        )

    with monkeypatch.context() as patch:
        patch.setattr(
            promote_module,
            "update_current_policies",
            interrupt_promotion,
        )

        # The real CLI handles the exception and returns
        # an unsuccessful exit code.
        exit_code = ingest_module.main()

    assert exit_code == 1

    failure_output = capsys.readouterr().out

    assert "Ingestion processing failed" in failure_output
    assert "Failure recorded as run" in failure_output

    # The processing transaction should be gone, while
    # the separately committed failure record remains.
    with open_schema_connection(
        database_url,
        schema_name,
    ) as connection:
        counts_after_failure = count_business_state(
            connection
        )

        failed_runs = connection.execute(
            """
            SELECT run_id
            FROM ingestion_runs
            WHERE status = 'failed'
            """
        ).fetchall()

        assert len(failed_runs) == 1

        failed_report = build_run_report(
            connection,
            failed_runs[0][0],
        )

    assert counts_after_failure == (
        2,     # Committed baseline and recorded failure
        1000,  # Only baseline staging survives
        1000,  # Current policies remain unchanged
        1000,  # No Day 2 audit history survived
        1,     # Failure quality event
    )

    assert failed_report["processing"]["status"] == "failed"

    assert failed_report["processing"]["failure_reason"] == (
        "RuntimeError: "
        "Simulated failure after audit-history write"
    )

    assert failed_report["source"]["snapshot_date"] == (
        "2026-10-02"
    )

    assert failed_report["audit_history"]["total"] == 0

    assert len(failed_report["quality_events"]) == 1

    quality_event = failed_report["quality_events"][0]

    assert quality_event["check_id"] == (
        "PROCESSING_TRANSACTION_FAILED"
    )

    assert quality_event["severity"] == "error"

    # The current CLI records the overall ingestion stage.
    assert quality_event["evidence"]["stage"] == "ingestion"

    # The injected failure is now removed. Retrying the
    # same unchanged source should complete normally.
    assert ingest_module.main() == 0

    retry_output = capsys.readouterr().out

    assert "Committed run" in retry_output
    assert "New policies: 30" in retry_output
    assert "Changed policies: 50" in retry_output
    assert "Deleted policies: 20" in retry_output

    # Running it again must not duplicate committed changes.
    assert ingest_module.main() == 0

    replay_output = capsys.readouterr().out

    assert "Already committed as run" in replay_output

    with open_schema_connection(
        database_url,
        schema_name,
    ) as connection:
        final_counts = count_business_state(
            connection
        )

        statuses = connection.execute(
            """
            SELECT
                COUNT(*) FILTER (
                    WHERE status = 'failed'
                ),
                COUNT(*) FILTER (
                    WHERE status = 'committed'
                )
            FROM ingestion_runs
            """
        ).fetchone()

    assert final_counts == (
        3,     # Baseline, failed attempt, successful retry
        2010,  # Day 1 and Day 2 staging
        1010,  # Active policies after Day 2
        1100,  # Baseline history + 100 Day 2 changes
        1,     # One persisted failure event
    )

    assert statuses == (1, 2)


def test_cli_reports_when_failure_evidence_cannot_be_saved(
    isolated_cli_schema,
    tmp_path: Path,
    monkeypatch,
    capsys,
):
    """A recorder outage must not be reported as a saved incident."""

    database_url, schema_name = isolated_cli_schema

    def get_test_connection():
        return open_schema_connection(
            database_url,
            schema_name,
        )

    monkeypatch.setattr(
        ingest_module,
        "get_connection",
        get_test_connection,
    )

    # Simulate a processing error through the CLI.
    def fail_ingestion(connection, manifest_path):
        raise RuntimeError(
            "Simulated processing interruption"
        )

    monkeypatch.setattr(
        ingest_module,
        "ingest_snapshot",
        fail_ingestion,
    )

    # The separate failure recorder is also unavailable.
    def fail_to_record(
        connection,
        manifest_path,
        reason,
        *,
        stage,
    ):
        raise psycopg.OperationalError(
            "Simulated evidence recording outage"
        )

    monkeypatch.setattr(
        ingest_module,
        "record_failed_attempt",
        fail_to_record,
    )

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "policyops.ingest",
            "--manifest",
            str(tmp_path / "unavailable.json"),
        ],
    )

    exit_code = ingest_module.main()

    assert exit_code == 1

    output = capsys.readouterr().out

    assert "Ingestion processing failed" in output

    assert (
        "Failure evidence could not be saved"
        in output
    )

    assert "Failure recorded as run" not in output

    with open_schema_connection(
        database_url,
        schema_name,
    ) as connection:
        final_counts = count_business_state(
            connection
        )

    assert final_counts == (0, 0, 0, 0, 0)
