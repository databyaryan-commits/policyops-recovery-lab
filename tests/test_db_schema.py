
"""Integration tests for PostgreSQL schema constraints."""

import os
from datetime import date, datetime, timezone
from decimal import Decimal
from uuid import uuid4

import psycopg
import pytest
from psycopg.types.json import Jsonb

from policyops.db import get_connection


@pytest.fixture
def db_connection(monkeypatch):
    """Use the test database and roll back everything afterward."""

    test_database = os.getenv(
        "POLICYOPS_TEST_DATABASE_URL",
        "dbname=policyops_test",
    )
    monkeypatch.setenv("POLICYOPS_DATABASE_URL", test_database)

    connection = get_connection()
    database_info = connection.execute(
        "SELECT current_database()"
    ).fetchone()

    # Never run database-write tests against the main lab database.
    if database_info is None or database_info[0] != "policyops_test":
        connection.rollback()
        connection.close()
        pytest.fail("Database tests must use policyops_test.")

    try:
        yield connection
    finally:
        connection.rollback()
        connection.close()


def register_run(
    connection,
    *,
    status: str = "registered",
    checksum: str = "a" * 64,
):
    run_id = uuid4()

    connection.execute(
        """
        INSERT INTO ingestion_runs (
            run_id,
            snapshot_date,
            source_file,
            checksum_sha256,
            schema_version,
            complete_snapshot,
            expected_row_count,
            status
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            run_id,
            date(2026, 10, 1),
            "day1.csv",
            checksum,
            "1",
            True,
            1000,
            status,
        ),
    )

    return run_id


def insert_staged_policy(
    connection,
    run_id,
    *,
    policy_id: str = "P0001",
    status: str = "active",
    premium: Decimal = Decimal("100.00"),
):
    connection.execute(
        """
        INSERT INTO policy_staging (
            run_id,
            policy_id,
            customer_id,
            status,
            premium_amount,
            effective_date,
            source_updated_at
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        """,
        (
            run_id,
            policy_id,
            "C0001",
            status,
            premium,
            date(2025, 1, 1),
            datetime(2026, 10, 1, 9, tzinfo=timezone.utc),
        ),
    )


def test_ingestion_run_can_be_registered(db_connection):
    run_id = register_run(db_connection)

    result = db_connection.execute(
        "SELECT status FROM ingestion_runs WHERE run_id = %s",
        (run_id,),
    ).fetchone()

    assert result is not None
    assert result[0] == "registered"


def test_registered_run_can_be_rolled_back(db_connection):
    run_id = register_run(db_connection)

    db_connection.rollback()

    result = db_connection.execute(
        "SELECT COUNT(*) FROM ingestion_runs WHERE run_id = %s",
        (run_id,),
    ).fetchone()

    assert result[0] == 0


def test_duplicate_staging_policy_is_rejected(db_connection):
    run_id = register_run(db_connection)

    insert_staged_policy(db_connection, run_id)

    with pytest.raises(psycopg.errors.UniqueViolation):
        insert_staged_policy(db_connection, run_id)


def test_invalid_staging_status_is_rejected(db_connection):
    run_id = register_run(db_connection)

    with pytest.raises(psycopg.errors.CheckViolation):
        insert_staged_policy(
            db_connection,
            run_id,
            status="suspended",
        )


def test_negative_staging_premium_is_rejected(db_connection):
    run_id = register_run(db_connection)

    with pytest.raises(psycopg.errors.CheckViolation):
        insert_staged_policy(
            db_connection,
            run_id,
            premium=Decimal("-1.00"),
        )


def test_history_cannot_duplicate_a_policy_change(db_connection):
    run_id = register_run(db_connection)

    statement = """
        INSERT INTO policy_history (
            run_id,
            policy_id,
            change_type,
            new_values
        )
        VALUES (%s, %s, 'insert', %s)
    """

    values = (
        run_id,
        "P0001",
        Jsonb({"status": "active"}),
    )

    db_connection.execute(statement, values)

    with pytest.raises(psycopg.errors.UniqueViolation):
        db_connection.execute(statement, values)


def test_only_one_snapshot_can_commit_per_date(db_connection):
    register_run(
        db_connection,
        status="committed",
        checksum="a" * 64,
    )

    with pytest.raises(psycopg.errors.UniqueViolation):
        register_run(
            db_connection,
            status="committed",
            checksum="b" * 64,
        )


def test_quality_event_requires_an_existing_run(db_connection):
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        db_connection.execute(
            """
            INSERT INTO quality_events (
                run_id,
                check_id,
                severity,
                expected_rule
            )
            VALUES (%s, %s, %s, %s)
            """,
            (
                uuid4(),
                "POLICY_ID_DUPLICATE",
                "error",
                "Policy IDs must be unique.",
            ),
        )


def test_approval_requires_a_reason(db_connection):
    with pytest.raises(psycopg.errors.CheckViolation):
        db_connection.execute(
            """
            INSERT INTO ingestion_runs (
                run_id,
                status,
                approved_by,
                approved_at
            )
            VALUES (%s, 'registered', %s, NOW())
            """,
            (uuid4(), "test_reviewer"),
        )
