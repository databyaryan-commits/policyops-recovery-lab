
"""Demonstrate transactional rollback, failure evidence, and safe retry."""

import argparse
import os
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from uuid import uuid4

import psycopg
from psycopg import sql

import policyops.ingest as ingest_module
import policyops.promote as promote_module
from policyops.demo import save_report
from policyops.generate import generate_golden
from policyops.report import build_run_report


def open_demo_connection(database_url: str, schema_name: str):
    """Connect to the disposable PolicyOps schema."""

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


def read_state(connection) -> dict:
    """Count recorded runs, active policies, and audit evidence."""

    row = connection.execute(
        """
        SELECT
            (SELECT COUNT(*) FROM ingestion_runs),
            (SELECT COUNT(*) FROM ingestion_runs
             WHERE status = 'failed'),
            (SELECT COUNT(*) FROM ingestion_runs
             WHERE status = 'committed'),
            (SELECT COUNT(*) FROM policy_staging),
            (SELECT COUNT(*) FROM policy_current
             WHERE is_deleted = FALSE),
            (SELECT COUNT(*) FROM policy_history),
            (SELECT COUNT(*) FROM quality_events)
        """
    ).fetchone()

    fields = (
        "runs",
        "failed_runs",
        "committed_runs",
        "staged",
        "active_policies",
        "audit_history",
        "quality_events",
    )

    return dict(zip(fields, row))


def render_recovery_timeline(
    baseline: dict,
    failed: dict,
    recovered: dict,
    before_failure: dict,
    after_failure: dict,
    after_retry: dict,
) -> str:
    """Summarize the observed database states and recovery process."""

    lines = [
        "# PolicyOps: Failure and Recovery Demonstration",
        "",
        "This demonstration uses generated policy data and an "
        "intentionally injected Python exception.",
        "",
        "It does not represent a production incident or an "
        "operating-system crash.",
        "",
        "## Scenario",
        "",
        "A valid Day 2 snapshot contains 30 new, 50 changed, "
        "20 deleted and 930 unchanged policies.",
        "",
        "The demonstration interrupts promotion after the "
        "transaction writes 100 audit-history rows, but before "
        "it updates current policy records.",
        "",
        "## Recovery timeline",
        "",
        "| Metric | Before failure | After rollback and recording | After retry |",
        "|---|---:|---:|---:|",
    ]

    labels = (
        ("active_policies", "Active policies"),
        ("audit_history", "Committed audit-history rows"),
        ("failed_runs", "Recorded failed runs"),
        ("committed_runs", "Committed snapshot runs"),
        ("quality_events", "Quality events"),
    )

    for key, label in labels:
        lines.append(
            f"| {label} | "
            f"{before_failure[key]} | "
            f"{after_failure[key]} | "
            f"{after_retry[key]} |"
        )

    lines.extend(
        [
            "",
            "## 1. Initial committed snapshot",
            "",
            f"- Run ID: `{baseline['run_id']}`",
            "- Status: committed",
            "- Active policies: 1,000",
            "- Initial audit-history rows: 1,000",
            "",
            "## 2. Injected processing failure",
            "",
            "The test deliberately raises:",
            "",
            "`RuntimeError: Simulated failure after audit-history write`",
            "",
            "PostgreSQL rolls back the failed promotion transaction, "
            "including the 100 uncommitted Day 2 history rows.",
            "",
            "After rollback, active policies and committed "
            "audit-history counts remain unchanged.",
            "",
            "## 3. Persistent failure evidence",
            "",
            f"- Failed-attempt run ID: `{failed['run_id']}`",
            f"- Status: {failed['processing']['status']}",
            (
                "- Reason: "
                f"{failed['processing']['failure_reason']}"
            ),
            "- Failed-attempt audit-history rows: 0",
            "- Failure event: `PROCESSING_TRANSACTION_FAILED`",
            "",
            "The failure record was saved using a separate "
            "database transaction after the original rollback.",
            "",
            "It uses a new run ID because the failed processing "
            "transaction's run registration was rolled back.",
            "",
            "## 4. Successful retry",
            "",
            f"- Run ID: `{recovered['run_id']}`",
            f"- Status: {recovered['processing']['status']}",
            "- New policies: 30",
            "- Changed policies: 50",
            "- Deleted policies: 20",
            "- Unchanged policies: 930",
            "- Day 2 committed audit-history rows: 100",
            "- Active policies after retry: 1,010",
            "",
            "## 5. Replay verification",
            "",
            "A third ingestion of the unchanged Day 2 source "
            "was recognized as an existing committed delivery.",
            "",
            "No additional ingestion run or business "
            "audit-history rows were created.",
            "",
            "## Evidence files",
            "",
            "- [Initial commit](baseline_committed.md)",
            "- [Failed attempt](failure_recorded.md)",
            "- [Successful retry](retry_committed.md)",
            "",
            "Matching JSON evidence files are included alongside "
            "these reports.",
            "",
            "## Limitations",
            "",
            "This is a controlled exception-injection demonstration. "
            "It does not establish recovery from a hard crash, "
            "power loss or failure-recording database outage.",
            "",
            "Run IDs and timestamps change between executions. "
            "The source seed and expected outcomes are fixed.",
            "",
        ]
    )

    return "\n".join(lines)


def run_recovery_demo(output_dir: Path) -> None:
    """Run a rollback and retry scenario in an isolated test schema."""

    database_url = os.getenv(
        "POLICYOPS_TEST_DATABASE_URL",
        "dbname=policyops_test",
    )

    schema_file = (
        Path(__file__).resolve().parents[2]
        / "sql"
        / "schema.sql"
    )

    if not schema_file.is_file():
        raise FileNotFoundError(
            f"Cannot find the PolicyOps schema: {schema_file}"
        )

    with psycopg.connect(
        database_url,
        autocommit=True,
    ) as admin:
        database_name = admin.execute(
            "SELECT current_database()"
        ).fetchone()[0]

        if database_name != "policyops_test":
            raise ValueError(
                "Recovery demonstration must use policyops_test."
            )

        schema_name = f"policyops_recovery_{uuid4().hex}"
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

            admin.execute(
                schema_file.read_text(encoding="utf-8")
            )

            with TemporaryDirectory(
                prefix="policyops_recovery_"
            ) as temporary_folder:
                source_folder = Path(temporary_folder)

                generate_golden(source_folder, seed=42)

                day1_manifest = source_folder / "day1.json"
                day2_manifest = source_folder / "day2.json"

                def get_demo_connection():
                    return open_demo_connection(
                        database_url,
                        schema_name,
                    )

                # Commit a valid Day 1 snapshot first.
                with get_demo_connection() as connection:
                    baseline_result = ingest_module.ingest_snapshot(
                        connection,
                        day1_manifest,
                    )

                if baseline_result.outcome != "committed":
                    raise RuntimeError(
                        "Could not establish the committed baseline."
                    )

                with get_demo_connection() as connection:
                    before_failure = read_state(connection)

                if (
                    before_failure["active_policies"] != 1000
                    or before_failure["audit_history"] != 1000
                ):
                    raise RuntimeError(
                        "Unexpected baseline policy state."
                    )

                # The same CLI is used for the failed attempt,
                # successful retry, and subsequent replay.
                cli_arguments = [
                    "policyops.ingest",
                    "--manifest",
                    str(day2_manifest),
                ]

                def interrupt_after_history(
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

                    if history_written != 100:
                        raise RuntimeError(
                            "Unexpected history count before injection."
                        )

                    raise RuntimeError(
                        "Simulated failure after audit-history write"
                    )

                with (
                    patch.object(
                        ingest_module,
                        "get_connection",
                        side_effect=get_demo_connection,
                    ),
                    patch.object(sys, "argv", cli_arguments),
                ):
                    # This exception occurs after 100 history
                    # rows are written but before their commit.
                    with patch.object(
                        promote_module,
                        "update_current_policies",
                        side_effect=interrupt_after_history,
                    ):
                        failed_exit = ingest_module.main()

                    if failed_exit != 1:
                        raise RuntimeError(
                            "The injected failure was not detected."
                        )

                    with get_demo_connection() as connection:
                        after_failure = read_state(connection)

                        failed_runs = connection.execute(
                            """
                            SELECT run_id
                            FROM ingestion_runs
                            WHERE status = 'failed'
                            """
                        ).fetchall()

                        if len(failed_runs) != 1:
                            raise RuntimeError(
                                "Expected one persistent failure record."
                            )

                        failed_report = build_run_report(
                            connection,
                            failed_runs[0][0],
                        )

                    # The failed transaction must not have
                    # committed any Day 2 business changes.
                    if after_failure != {
                        "runs": 2,
                        "failed_runs": 1,
                        "committed_runs": 1,
                        "staged": 1000,
                        "active_policies": 1000,
                        "audit_history": 1000,
                        "quality_events": 1,
                    }:
                        raise RuntimeError(
                            "Failure rollback did not preserve baseline state."
                        )

                    if (
                        failed_report["audit_history"]["total"] != 0
                        or failed_report["processing"]["status"] != "failed"
                        or not any(
                            event["check_id"]
                            == "PROCESSING_TRANSACTION_FAILED"
                            for event in failed_report["quality_events"]
                        )
                    ):
                        raise RuntimeError(
                            "Persistent failure evidence is incomplete."
                        )

                    # Remove the injected failure and retry.
                    if ingest_module.main() != 0:
                        raise RuntimeError(
                            "The unchanged Day 2 retry failed."
                        )

                    with get_demo_connection() as connection:
                        after_retry = read_state(connection)

                        retry_runs = connection.execute(
                            """
                            SELECT run_id
                            FROM ingestion_runs
                            WHERE status = 'committed'
                              AND snapshot_date = %s
                            """,
                            ("2026-10-02",),
                        ).fetchall()

                        if len(retry_runs) != 1:
                            raise RuntimeError(
                                "Expected one committed Day 2 run."
                            )

                        retry_report = build_run_report(
                            connection,
                            retry_runs[0][0],
                        )

                        baseline_report = build_run_report(
                            connection,
                            baseline_result.run_id,
                        )

                    if after_retry != {
                        "runs": 3,
                        "failed_runs": 1,
                        "committed_runs": 2,
                        "staged": 2010,
                        "active_policies": 1010,
                        "audit_history": 1100,
                        "quality_events": 1,
                    }:
                        raise RuntimeError(
                            "Unexpected state after the successful retry."
                        )

                    if retry_report["audit_history"] != {
                        "total": 100,
                        "inserted": 30,
                        "updated": 50,
                        "deleted": 20,
                    }:
                        raise RuntimeError(
                            "Committed Day 2 audit history is incorrect."
                        )

                    # The committed retry must become a no-op
                    # when the same source is ingested again.
                    if ingest_module.main() != 0:
                        raise RuntimeError(
                            "The committed snapshot could not be replayed."
                        )

                    with get_demo_connection() as connection:
                        after_replay = read_state(connection)

                    if after_replay != after_retry:
                        raise RuntimeError(
                            "Replay created unexpected database changes."
                        )

                # All PostgreSQL connections above are closed.
                # Persist only reports derived from their evidence.
                output_dir.mkdir(parents=True, exist_ok=True)

                save_report(
                    output_dir,
                    "baseline_committed",
                    baseline_report,
                )

                save_report(
                    output_dir,
                    "failure_recorded",
                    failed_report,
                )

                save_report(
                    output_dir,
                    "retry_committed",
                    retry_report,
                )

                timeline = render_recovery_timeline(
                    baseline_report,
                    failed_report,
                    retry_report,
                    before_failure,
                    after_failure,
                    after_retry,
                )

                (output_dir / "recovery_timeline.md").write_text(
                    timeline,
                    encoding="utf-8",
                )

                print()
                print("PolicyOps recovery demonstration")
                print("--------------------------------")
                print("Injected processing failure: detected")
                print("Uncommitted Day 2 history: rolled back")
                print("Failure evidence: recorded")
                print("Day 2 retry: committed")
                print("Committed snapshot replay: no-op")
                print(
                    "Active policies after recovery: "
                    f"{after_retry['active_policies']:,}"
                )
                print(f"Reports saved to: {output_dir}")

        finally:
            if schema_created:
                admin.execute(
                    sql.SQL(
                        "DROP SCHEMA IF EXISTS {} CASCADE"
                    ).format(
                        sql.Identifier(schema_name)
                    )
                )


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Demonstrate failure rollback, persistent incident "
            "evidence, successful retry, and replay protection"
        )
    )

    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path(
            "artifacts/example/failure_recovery"
        ),
        help="Directory for generated recovery evidence",
    )

    args = parser.parse_args()

    try:
        run_recovery_demo(args.output_dir)

    except (
        ValueError,
        RuntimeError,
        FileNotFoundError,
        OSError,
        psycopg.Error,
    ) as error:
        print(f"Recovery demonstration failed: {error}")
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
