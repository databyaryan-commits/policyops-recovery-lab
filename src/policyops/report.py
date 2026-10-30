
"""Read recorded ingestion evidence and produce incident reports."""

import argparse
import json
from uuid import UUID

import psycopg
from psycopg.rows import dict_row

from policyops.db import get_connection


def as_text(value):
    """Convert dates and other database values for JSON output."""

    if value is None:
        return None

    if hasattr(value, "isoformat"):
        return value.isoformat()

    return value


def describe_decision(status: str, approved_at) -> str:
    """Explain the recorded state without guessing the source cause."""

    if status == "committed":
        return (
            "Snapshot committed. Business changes and audit "
            "events were written by the promotion process."
        )

    if status == "awaiting_review":
        if approved_at is not None:
            return (
                "Manual approval is recorded, but the snapshot "
                "has not yet been promoted."
            )

        return (
            "Snapshot held for manual review. "
            "Business changes have not been promoted."
        )

    if status == "rejected":
        return (
            "Snapshot rejected during validation. "
            "Business changes were not promoted."
        )

    if status == "validated":
        return (
            "Snapshot validated and staged, but not committed."
        )

    if status == "failed":
        return (
            "A processing failure was recorded. "
            "Inspect the failure reason and quality events."
        )

    if status == "replayed":
        return (
            "Run marked as a replay. "
            "Inspect the recorded source identity."
        )

    return "Run registered; no final processing decision recorded."


def build_run_report(
    connection: psycopg.Connection,
    run_id: UUID,
) -> dict:
    """Build a read-only report from one ingestion run."""

    with connection.cursor(row_factory=dict_row) as cursor:
        cursor.execute(
            """
            SELECT
                run_id,
                snapshot_date,
                source_file,
                checksum_sha256,
                schema_version,
                complete_snapshot,
                expected_row_count,
                source_count,
                accepted_count,
                status,
                new_count,
                changed_count,
                deleted_count,
                unchanged_count,
                started_at,
                finished_at,
                failure_reason,
                approved_by,
                approval_reason,
                approved_at
            FROM ingestion_runs
            WHERE run_id = %s
            """,
            (run_id,),
        )

        run = cursor.fetchone()

        if run is None:
            raise ValueError(
                "The requested ingestion run does not exist."
            )

        cursor.execute(
            """
            SELECT
                event_id,
                check_id,
                severity,
                observed_value,
                expected_rule,
                evidence,
                created_at
            FROM quality_events
            WHERE run_id = %s
            ORDER BY event_id
            """,
            (run_id,),
        )

        quality_rows = cursor.fetchall()

        cursor.execute(
            """
            SELECT
                COUNT(*) AS total,
                COUNT(*) FILTER (
                    WHERE change_type = 'insert'
                ) AS inserted,
                COUNT(*) FILTER (
                    WHERE change_type = 'update'
                ) AS updated,
                COUNT(*) FILTER (
                    WHERE change_type = 'delete'
                ) AS deleted
            FROM policy_history
            WHERE run_id = %s
            """,
            (run_id,),
        )

        history = cursor.fetchone()

    quality_events = [
        {
            "event_id": row["event_id"],
            "check_id": row["check_id"],
            "severity": row["severity"],
            "observed_value": row["observed_value"],
            "expected_rule": row["expected_rule"],
            "evidence": row["evidence"],
            "created_at": as_text(row["created_at"]),
        }
        for row in quality_rows
    ]

    return {
        "run_id": str(run["run_id"]),
        "source": {
            "snapshot_date": as_text(run["snapshot_date"]),
            "source_file": run["source_file"],
            "checksum_sha256": run["checksum_sha256"],
            "schema_version": run["schema_version"],
            "complete_snapshot": run["complete_snapshot"],
            "expected_row_count": run["expected_row_count"],
            "source_count": run["source_count"],
            "accepted_count": run["accepted_count"],
        },
        "processing": {
            "status": run["status"],
            "started_at": as_text(run["started_at"]),
            "finished_at": as_text(run["finished_at"]),
            "failure_reason": run["failure_reason"],
        },
        "recorded_changes": {
            "new": run["new_count"],
            "changed": run["changed_count"],
            "deleted": run["deleted_count"],
            "unchanged": run["unchanged_count"],
        },
        "approval": {
            "approved_by": run["approved_by"],
            "approval_reason": run["approval_reason"],
            "approved_at": as_text(run["approved_at"]),
        },
        "audit_history": {
            "total": history["total"],
            "inserted": history["inserted"],
            "updated": history["updated"],
            "deleted": history["deleted"],
        },
        "quality_events": quality_events,
        "decision": describe_decision(
            run["status"],
            run["approved_at"],
        ),
        "limitations": [
            (
                "Source integrity checks establish consistency "
                "with the supplied manifest, not the business "
                "reason for missing policies."
            ),
            (
                "Recorded change counts describe the assessment "
                "made for this run. They are not necessarily "
                "committed changes."
            ),
            (
                "Audit history counts describe changes recorded "
                "for this run, not the entire current database."
            ),
        ],
    }


def markdown_value(value) -> str:
    """Safely format one value for a Markdown report."""

    if value is None:
        return "Not recorded"

    if isinstance(value, bool):
        return "Yes" if value else "No"

    return (
        str(value)
        .replace("|", "\\|")
        .replace("\r", " ")
        .replace("\n", " ")
    )


def render_markdown(report: dict) -> str:
    """Turn recorded database evidence into a readable report."""

    source = report["source"]
    processing = report["processing"]
    changes = report["recorded_changes"]
    approval = report["approval"]
    history = report["audit_history"]

    lines = [
        "# PolicyOps Incident Report",
        "",
        f"**Run ID:** `{report['run_id']}`",
        f"**Status:** {processing['status']}",
        "",
        "## Decision",
        "",
        report["decision"],
        "",
        "## Source delivery",
        "",
        "| Field | Recorded value |",
        "|---|---|",
    ]

    source_labels = {
        "snapshot_date": "Snapshot date",
        "source_file": "Source file",
        "checksum_sha256": "CSV SHA-256",
        "schema_version": "Schema version",
        "complete_snapshot": "Declared complete",
        "expected_row_count": "Declared row count",
        "source_count": "Received row count",
        "accepted_count": "Accepted row count",
    }

    for key, label in source_labels.items():
        lines.append(
            f"| {label} | {markdown_value(source[key])} |"
        )

    lines.extend(
        [
            "",
            "## Recorded change assessment",
            "",
            "| Change type | Count |",
            "|---|---:|",
        ]
    )

    for key in ("new", "changed", "deleted", "unchanged"):
        lines.append(
            f"| {key.capitalize()} | "
            f"{markdown_value(changes[key])} |"
        )

    lines.extend(
        [
            "",
            "These are assessment counts, not proof that "
            "changes were committed.",
            "",
            "## Audit history written",
            "",
            "| Event type | Rows |",
            "|---|---:|",
            f"| Insert | {history['inserted']} |",
            f"| Update | {history['updated']} |",
            f"| Delete | {history['deleted']} |",
            f"| **Total** | **{history['total']}** |",
            "",
            "## Quality and review evidence",
            "",
        ]
    )

    if not report["quality_events"]:
        lines.append(
            "No quality events are recorded for this run."
        )
    else:
        for event in report["quality_events"]:
            lines.extend(
                [
                    (
                        f"### Event {event['event_id']}: "
                        f"{markdown_value(event['check_id'])}"
                    ),
                    "",
                    f"- Severity: {markdown_value(event['severity'])}",
                    (
                        "- Observed value: "
                        f"{markdown_value(event['observed_value'])}"
                    ),
                    (
                        "- Expected rule: "
                        f"{markdown_value(event['expected_rule'])}"
                    ),
                ]
            )

            if event["evidence"] is not None:
                evidence_text = json.dumps(
                    event["evidence"],
                    sort_keys=True,
                    default=str,
                )

                lines.append(
                    f"- Evidence: `{markdown_value(evidence_text)}`"
                )

            lines.append("")

    lines.extend(
        [
            "## Manual approval",
            "",
            (
                "- Reviewer: "
                f"{markdown_value(approval['approved_by'])}"
            ),
            (
                "- Reason: "
                f"{markdown_value(approval['approval_reason'])}"
            ),
            (
                "- Approved at: "
                f"{markdown_value(approval['approved_at'])}"
            ),
            "",
            "## Processing record",
            "",
            (
                "- Started: "
                f"{markdown_value(processing['started_at'])}"
            ),
            (
                "- Finished: "
                f"{markdown_value(processing['finished_at'])}"
            ),
            (
                "- Failure reason: "
                f"{markdown_value(processing['failure_reason'])}"
            ),
            "",
            "## Interpretation limits",
            "",
        ]
    )

    for limitation in report["limitations"]:
        lines.append(f"- {limitation}")

    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Read an ingestion run and print its incident report"
    )

    parser.add_argument(
        "--run-id",
        required=True,
        type=UUID,
        help="ID of the ingestion run to report",
    )

    parser.add_argument(
        "--format",
        choices=("json", "markdown"),
        default="markdown",
        help="Report output format (default: markdown)",
    )

    args = parser.parse_args()

    try:
        with get_connection() as connection:
            report = build_run_report(
                connection,
                args.run_id,
            )

    except ValueError as error:
        print(f"Report unavailable: {error}")
        return 1

    except psycopg.Error:
        print("Database error. The report could not be generated.")
        return 1

    if args.format == "json":
        print(json.dumps(report, indent=2))
    else:
        print(render_markdown(report), end="")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
