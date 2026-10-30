
"""Generate reproducible PolicyOps incident demonstrations."""

import argparse
import csv
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

import psycopg
from psycopg import sql

from policyops.generate import generate_golden, write_snapshot
from policyops.ingest import ingest_snapshot
from policyops.report import build_run_report, render_markdown


def count_active_policies(connection: psycopg.Connection) -> int:
    """Count policies still present in the current snapshot."""

    return connection.execute(
        """
        SELECT COUNT(*)
        FROM policy_current
        WHERE is_deleted = FALSE
        """
    ).fetchone()[0]


def prepare_incident_sources(folder: Path) -> None:
    """Build two 650-row deliveries with different source evidence."""

    with (folder / "day1.csv").open(
        "r",
        encoding="utf-8",
        newline="",
    ) as file:
        original_rows = list(csv.DictReader(file))

    if len(original_rows) != 1000:
        raise ValueError("The demo requires 1,000 starting policies.")

    # Case 1: the manifest describes 1,000 rows, but the
    # delivered CSV has been truncated to 650 policy records.
    write_snapshot(
        folder,
        "incomplete_drop",
        "2026-10-02",
        original_rows,
    )

    incomplete_csv = folder / "incomplete_drop.csv"

    original_lines = incomplete_csv.read_bytes().splitlines(
        keepends=True
    )

    incomplete_csv.write_bytes(
        b"".join(original_lines[:651])
    )

    # Case 2: the source explicitly declares a complete
    # 650-row snapshot, and its checksum matches the file.
    write_snapshot(
        folder,
        "valid_drop",
        "2026-10-02",
        original_rows[:650],
    )


def save_report(
    output_dir: Path,
    filename: str,
    report: dict,
) -> None:
    """Save JSON and Markdown from the same database evidence."""

    json_path = output_dir / f"{filename}.json"
    markdown_path = output_dir / f"{filename}.md"

    json_path.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    markdown_path.write_text(
        render_markdown(report),
        encoding="utf-8",
    )


def render_comparison(
    baseline: dict,
    incomplete: dict,
    valid: dict,
    active_after: int,
) -> str:
    """Summarize why two similar drops have different outcomes."""

    incomplete_checks = sorted(
        {
            event["check_id"]
            for event in incomplete["quality_events"]
        }
    )

    valid_checks = sorted(
        {
            event["check_id"]
            for event in valid["quality_events"]
        }
    )

    lines = [
        "# PolicyOps: Two 35% Drop Scenarios",
        "",
        "This demonstration uses generated insurance-policy data.",
        "It does not represent a real insurer or a production incident.",
        "",
        "## Starting state",
        "",
        (
            f"- Baseline run: `{baseline['run_id']}` "
            f"({baseline['processing']['status']})"
        ),
        "- Initial active policies: 1,000",
        "",
        "## Comparison",
        "",
        "| Evidence | Incomplete delivery | Valid deletion delivery |",
        "|---|---|---|",
        "| Received policies | 650 | 650 |",
        (
            "| Declared row count | "
            f"{incomplete['source']['expected_row_count']} | "
            f"{valid['source']['expected_row_count']} |"
        ),
        (
            "| Complete flag in manifest | "
            f"{incomplete['source']['complete_snapshot']} | "
            f"{valid['source']['complete_snapshot']} |"
        ),
        "| Checksum consistent with CSV | No | Yes |",
        (
            "| Pipeline status | "
            f"{incomplete['processing']['status']} | "
            f"{valid['processing']['status']} |"
        ),
        (
            "| Assessed deletion candidates | Not assessed | "
            f"{valid['recorded_changes']['deleted']} |"
        ),
        (
            "| Audit events for the run | "
            f"{incomplete['audit_history']['total']} | "
            f"{valid['audit_history']['total']} |"
        ),
        "",
        "## Incomplete delivery",
        "",
        f"Run ID: `{incomplete['run_id']}`",
        "",
        "Recorded failed checks:",
        "",
    ]

    for check_id in incomplete_checks:
        lines.append(f"- `{check_id}`")

    lines.extend(
        [
            "",
            (
                "The delivery failed validation. Its missing rows "
                "cannot be treated as confirmed business deletions."
            ),
            "",
            "## Valid but suspicious delivery",
            "",
            f"Run ID: `{valid['run_id']}`",
            "",
            "Recorded checks requiring review:",
            "",
        ]
    )

    for check_id in valid_checks:
        lines.append(f"- `{check_id}`")

    lines.extend(
        [
            "",
            (
                "The source passed file-integrity checks, but the "
                "35% reduction exceeded the illustrative 20% "
                "manual-review threshold."
            ),
            "",
            (
                "No approval was granted in this demonstration, "
                "and no 350-policy deletion was committed."
            ),
            "",
            "## Final database state",
            "",
            f"- Active policies: {active_after:,}",
            "- The incomplete delivery was rejected.",
            "- The valid reduced snapshot remains awaiting review.",
            "- Neither incident created business audit-history changes.",
            "",
            "## Inspect the evidence",
            "",
            "- [Baseline report](baseline_committed.md)",
            "- [Incomplete delivery report](incomplete_rejected.md)",
            "- [Valid deletion review report](valid_drop_awaiting_review.md)",
            "",
            "Matching JSON reports are provided alongside these files.",
            "",
            "## What this demonstration cannot establish",
            "",
            (
                "A matching source manifest does not establish why "
                "350 policies are missing. It only establishes "
                "consistency with the declared file contract."
            ),
            "",
            (
                "The approval threshold is an example configuration, "
                "not an industry-standard insurance rule."
            ),
            "",
            (
                "Run IDs and timestamps change between executions. "
                "The source seed and expected business outcomes are fixed."
            ),
            "",
        ]
    )

    return "\n".join(lines)


def run_deletion_contrast(output_dir: Path) -> None:
    """Run both incidents in a disposable PostgreSQL schema."""

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

    # Only the dedicated test database is permitted. The demo
    # never connects to the normal policyops_lab database.
    with psycopg.connect(
        database_url,
        autocommit=True,
    ) as admin:
        database_name = admin.execute(
            "SELECT current_database()"
        ).fetchone()[0]

        if database_name != "policyops_test":
            raise ValueError(
                "The demonstration must use policyops_test."
            )

        schema_name = f"policyops_demo_{uuid4().hex}"
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
                prefix="policyops_incident_"
            ) as temporary_folder:
                source_folder = Path(temporary_folder)

                generate_golden(source_folder, seed=42)
                prepare_incident_sources(source_folder)

                with psycopg.connect(
                    database_url
                ) as connection:
                    connection.execute(
                        sql.SQL("SET search_path TO {}").format(
                            sql.Identifier(schema_name)
                        )
                    )

                    baseline = ingest_snapshot(
                        connection,
                        source_folder / "day1.json",
                    )

                    if baseline.outcome != "committed":
                        raise RuntimeError(
                            "Failed to establish the demo baseline."
                        )

                    if count_active_policies(connection) != 1000:
                        raise RuntimeError(
                            "The baseline must have 1,000 active policies."
                        )

                    incomplete = ingest_snapshot(
                        connection,
                        source_folder / "incomplete_drop.json",
                    )

                    if incomplete.outcome != "rejected":
                        raise RuntimeError(
                            "The incomplete delivery was not rejected."
                        )

                    if count_active_policies(connection) != 1000:
                        raise RuntimeError(
                            "The rejected delivery changed current policies."
                        )

                    valid = ingest_snapshot(
                        connection,
                        source_folder / "valid_drop.json",
                    )

                    if valid.outcome != "awaiting_review":
                        raise RuntimeError(
                            "The valid 35% drop was not held for review."
                        )

                    if (
                        valid.changes is None
                        or valid.changes.deleted != 350
                    ):
                        raise RuntimeError(
                            "Expected exactly 350 deletion candidates."
                        )

                    active_after = count_active_policies(
                        connection
                    )

                    if active_after != 1000:
                        raise RuntimeError(
                            "An unapproved delivery changed current policies."
                        )

                    baseline_report = build_run_report(
                        connection,
                        baseline.run_id,
                    )

                    incomplete_report = build_run_report(
                        connection,
                        incomplete.run_id,
                    )

                    valid_report = build_run_report(
                        connection,
                        valid.run_id,
                    )

                # The source and report queries above have completed.
                # Only generated files will be retained.
                output_dir.mkdir(parents=True, exist_ok=True)

                save_report(
                    output_dir,
                    "baseline_committed",
                    baseline_report,
                )

                save_report(
                    output_dir,
                    "incomplete_rejected",
                    incomplete_report,
                )

                save_report(
                    output_dir,
                    "valid_drop_awaiting_review",
                    valid_report,
                )

                comparison = render_comparison(
                    baseline_report,
                    incomplete_report,
                    valid_report,
                    active_after,
                )

                (output_dir / "incident_comparison.md").write_text(
                    comparison,
                    encoding="utf-8",
                )

                print()
                print("PolicyOps deletion-contrast demonstration")
                print("-----------------------------------------")
                print("Incomplete delivery: rejected")
                print("Valid 35% deletion: awaiting_review")
                print(f"Active policies after both cases: {active_after:,}")
                print(f"Reports saved to: {output_dir}")
                print("No manual approval or deletion was applied.")

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
            "Generate PolicyOps incident evidence "
            "using an isolated test database schema"
        )
    )

    parser.add_argument(
        "--scenario",
        choices=["deletion-contrast"],
        required=True,
        help="Incident scenario to demonstrate",
    )

    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path(
            "artifacts/example/deletion_contrast"
        ),
        help="Directory for generated incident reports",
    )

    args = parser.parse_args()

    try:
        run_deletion_contrast(args.output_dir)

    except (
        ValueError,
        RuntimeError,
        FileNotFoundError,
        OSError,
        psycopg.Error,
    ) as error:
        print(f"Demonstration failed: {error}")
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
