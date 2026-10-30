
"""Concurrent ingestion tests using isolated PostgreSQL schemas."""

import csv
import os
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from pathlib import Path
from threading import Barrier
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql

from policyops.db import get_connection
from policyops.generate import generate_golden, write_snapshot
from policyops.ingest import ingest_snapshot


@pytest.fixture
def isolated_schema(monkeypatch):
    """Create disposable tables without touching normal test data."""

    test_database = os.getenv(
        "POLICYOPS_TEST_DATABASE_URL",
        "dbname=policyops_test",
    )

    admin = psycopg.connect(test_database, autocommit=True)
    schema_name = None

    try:
        database_name = admin.execute(
            "SELECT current_database()"
        ).fetchone()[0]

        if database_name != "policyops_test":
            pytest.fail(
                "Concurrency tests must use policyops_test."
            )

        monkeypatch.setenv(
            "POLICYOPS_DATABASE_URL",
            test_database,
        )

        schema_name = f"policyops_concurrent_{uuid4().hex}"

        admin.execute(
            sql.SQL("CREATE SCHEMA {}").format(
                sql.Identifier(schema_name)
            )
        )

        admin.execute(
            sql.SQL("SET search_path TO {}").format(
                sql.Identifier(schema_name)
            )
        )

        # Build the same five tables as the main project,
        # but inside our temporary schema.
        schema_file = (
            Path(__file__).resolve().parents[1]
            / "sql"
            / "schema.sql"
        )

        admin.execute(
            schema_file.read_text(encoding="utf-8")
        )

        table_count = admin.execute(
            """
            SELECT COUNT(*)
            FROM information_schema.tables
            WHERE table_schema = %s
              AND table_type = 'BASE TABLE'
            """,
            (schema_name,),
        ).fetchone()[0]

        assert table_count == 5

        yield schema_name

    finally:
        # Cleanup runs even if a concurrency assertion fails.
        if schema_name is not None:
            admin.execute(
                sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(
                    sql.Identifier(schema_name)
                )
            )

        admin.close()


@pytest.fixture
def demo_folder(tmp_path: Path) -> Path:
    """Generate synthetic policies for concurrent deliveries."""

    generate_golden(tmp_path, seed=42)
    return tmp_path


def ingest_in_connection(
    schema_name: str,
    manifest_path: Path,
    start_barrier: Barrier,
):
    """Process one delivery using its own database connection."""

    with get_connection() as connection:
        connection.execute(
            sql.SQL("SET search_path TO {}").format(
                sql.Identifier(schema_name)
            )
        )

        # Prevent a blocked test from waiting indefinitely.
        connection.execute(
            "SET LOCAL statement_timeout = '15s'"
        )

        # Both workers reach this point before either starts
        # the ingestion operation.
        start_barrier.wait(timeout=10)

        result = ingest_snapshot(
            connection,
            manifest_path,
        )

    # Exiting the connection context commits a successful
    # transaction or rolls it back if an error occurred.
    return result


def run_simultaneously(
    schema_name: str,
    first_manifest: Path,
    second_manifest: Path,
):
    """Start two independent ingestion transactions."""

    start_barrier = Barrier(2)

    with ThreadPoolExecutor(max_workers=2) as executor:
        first_future = executor.submit(
            ingest_in_connection,
            schema_name,
            first_manifest,
            start_barrier,
        )

        second_future = executor.submit(
            ingest_in_connection,
            schema_name,
            second_manifest,
            start_barrier,
        )

        return (
            first_future.result(timeout=25),
            second_future.result(timeout=25),
        )


def read_database_counts(schema_name: str) -> dict:
    """Inspect only the disposable concurrency-test tables."""

    with get_connection() as connection:
        connection.execute(
            sql.SQL("SET search_path TO {}").format(
                sql.Identifier(schema_name)
            )
        )

        result = connection.execute(
            """
            SELECT
                (SELECT COUNT(*) FROM ingestion_runs),
                (SELECT COUNT(*) FROM policy_staging),
                (SELECT COUNT(*) FROM policy_current),
                (SELECT COUNT(*) FROM policy_history),
                (SELECT COUNT(*) FROM quality_events)
            """
        ).fetchone()

        committed_count = connection.execute(
            """
            SELECT COUNT(*)
            FROM ingestion_runs
            WHERE status = 'committed'
            """
        ).fetchone()[0]

    return {
        "runs": result[0],
        "staged": result[1],
        "current": result[2],
        "history": result[3],
        "quality_events": result[4],
        "committed": committed_count,
    }


def test_simultaneous_identical_deliveries_commit_once(
    isolated_schema: str,
    demo_folder: Path,
):
    """Two copies of one delivery must not duplicate history."""

    manifest_path = demo_folder / "day1.json"

    first, second = run_simultaneously(
        isolated_schema,
        manifest_path,
        manifest_path,
    )

    outcomes = Counter(
        [first.outcome, second.outcome]
    )

    assert outcomes == {
        "committed": 1,
        "replayed": 1,
    }

    # The replay must refer to the actual committed run.
    assert first.run_id == second.run_id

    counts = read_database_counts(isolated_schema)

    assert counts == {
        "runs": 1,
        "staged": 1000,
        "current": 1000,
        "history": 1000,
        "quality_events": 0,
        "committed": 1,
    }


def test_simultaneous_conflicting_deliveries_commit_once(
    isolated_schema: str,
    demo_folder: Path,
):
    """Different content on one date cannot both be committed."""

    with (demo_folder / "day1.csv").open(
        "r",
        encoding="utf-8",
        newline="",
    ) as file:
        rows = list(csv.DictReader(file))

    assert len(rows) == 1000

    # Make one valid business change while keeping the date.
    original_premium = Decimal(
        rows[0]["premium_amount"]
    )

    rows[0]["premium_amount"] = (
        f"{original_premium + Decimal('1.00'):.2f}"
    )

    write_snapshot(
        demo_folder,
        "day1_conflict",
        "2026-10-01",
        rows,
    )

    first, second = run_simultaneously(
        isolated_schema,
        demo_folder / "day1.json",
        demo_folder / "day1_conflict.json",
    )

    outcomes = Counter(
        [first.outcome, second.outcome]
    )

    assert outcomes == {
        "committed": 1,
        "date_conflict": 1,
    }

    # The rejected conflict identifies the successful run.
    assert first.run_id == second.run_id

    counts = read_database_counts(isolated_schema)

    assert counts == {
        "runs": 1,
        "staged": 1000,
        "current": 1000,
        "history": 1000,
        "quality_events": 0,
        "committed": 1,
    }
