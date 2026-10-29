
"""Check snapshot manifests and source-file integrity."""

import argparse
import csv
import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path


REQUIRED_COLUMNS = {
    "policy_id",
    "customer_id",
    "status",
    "premium_amount",
    "effective_date",
    "source_updated_at",
}

REQUIRED_MANIFEST_FIELDS = {
    "snapshot_date",
    "schema_version",
    "expected_row_count",
    "sha256",
    "complete_snapshot",
    "csv_file",
}


@dataclass(frozen=True)
class ValidationIssue:
    check_id: str
    observed_value: str
    expected_rule: str
    severity: str = "error"


@dataclass
class ValidationReport:
    manifest_path: Path
    snapshot_date: str | None = None
    expected_count: int | None = None
    source_count: int | None = None
    checksum_matches: bool | None = None
    issues: list[ValidationIssue] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.issues

    def add_issue(
        self,
        check_id: str,
        observed_value: str,
        expected_rule: str,
    ) -> None:
        self.issues.append(
            ValidationIssue(
                check_id=check_id,
                observed_value=observed_value,
                expected_rule=expected_rule,
            )
        )


def validate_snapshot(manifest_path: Path) -> ValidationReport:
    """Check the declared delivery before examining policy values."""

    report = ValidationReport(manifest_path=manifest_path)

    if not manifest_path.is_file():
        report.add_issue(
            "MANIFEST_MISSING",
            manifest_path.name,
            "A readable manifest must accompany every snapshot.",
        )
        return report

    try:
        manifest = json.loads(
            manifest_path.read_text(encoding="utf-8")
        )
    except (OSError, UnicodeError, json.JSONDecodeError):
        report.add_issue(
            "MANIFEST_INVALID",
            manifest_path.name,
            "The manifest must contain valid UTF-8 JSON.",
        )
        return report

    if not isinstance(manifest, dict):
        report.add_issue(
            "MANIFEST_INVALID",
            type(manifest).__name__,
            "The manifest must be a JSON object.",
        )
        return report

    missing_fields = REQUIRED_MANIFEST_FIELDS - set(manifest)

    if missing_fields:
        report.add_issue(
            "MANIFEST_FIELDS_MISSING",
            ", ".join(sorted(missing_fields)),
            "All required manifest fields must be present.",
        )

    if manifest.get("schema_version") != "1":
        report.add_issue(
            "SCHEMA_VERSION_UNSUPPORTED",
            str(manifest.get("schema_version")),
            "The supported schema version is '1'.",
        )

    snapshot_date = manifest.get("snapshot_date")

    try:
        if not isinstance(snapshot_date, str):
            raise ValueError("Snapshot date must be text.")

        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", snapshot_date):
            raise ValueError("Incorrect date format.")

        date.fromisoformat(snapshot_date)
        report.snapshot_date = snapshot_date

    except ValueError:
        report.add_issue(
            "SNAPSHOT_DATE_INVALID",
            str(snapshot_date),
            "Snapshot dates must use YYYY-MM-DD.",
        )

    expected_count = manifest.get("expected_row_count")

    if type(expected_count) is int and expected_count >= 0:
        report.expected_count = expected_count
    else:
        report.add_issue(
            "EXPECTED_COUNT_INVALID",
            str(expected_count),
            "The expected row count must be a non-negative integer.",
        )

    if manifest.get("complete_snapshot") is not True:
        report.add_issue(
            "SNAPSHOT_INCOMPLETE",
            str(manifest.get("complete_snapshot")),
            "Deletion decisions require a complete snapshot.",
        )

    declared_checksum = manifest.get("sha256")
    checksum_is_valid = (
        isinstance(declared_checksum, str)
        and re.fullmatch(r"[0-9a-f]{64}", declared_checksum) is not None
    )

    if not checksum_is_valid:
        report.add_issue(
            "CHECKSUM_FORMAT_INVALID",
            str(declared_checksum),
            "The checksum must contain 64 lowercase hexadecimal characters.",
        )

    csv_filename = manifest.get("csv_file")

    if (
        not isinstance(csv_filename, str)
        or not csv_filename.endswith(".csv")
        or Path(csv_filename).name != csv_filename
    ):
        report.add_issue(
            "CSV_FILENAME_INVALID",
            str(csv_filename),
            "The CSV filename must be a plain .csv filename.",
        )
        return report

    csv_path = manifest_path.parent / csv_filename

    if not csv_path.is_file():
        report.add_issue(
            "CSV_MISSING",
            csv_filename,
            "The CSV referenced by the manifest must exist.",
        )
        return report

    try:
        actual_checksum = hashlib.sha256(
            csv_path.read_bytes()
        ).hexdigest()

        if checksum_is_valid:
            report.checksum_matches = (
                actual_checksum == declared_checksum
            )

            if not report.checksum_matches:
                report.add_issue(
                    "CHECKSUM_MISMATCH",
                    actual_checksum,
                    "The CSV checksum must match the manifest.",
                )

        with csv_path.open(
            "r", encoding="utf-8", newline=""
        ) as file:
            reader = csv.DictReader(file)
            columns = reader.fieldnames or []

            if (
                len(columns) != len(REQUIRED_COLUMNS)
                or set(columns) != REQUIRED_COLUMNS
            ):
                report.add_issue(
                    "CSV_COLUMNS_INVALID",
                    ", ".join(columns),
                    "The CSV must contain exactly the required columns.",
                )

            report.source_count = sum(1 for _ in reader)

    except (OSError, UnicodeError, csv.Error):
        report.add_issue(
            "CSV_UNREADABLE",
            csv_filename,
            "The source must be a readable UTF-8 CSV file.",
        )
        return report

    if (
        report.expected_count is not None
        and report.source_count != report.expected_count
    ):
        report.add_issue(
            "ROW_COUNT_MISMATCH",
            str(report.source_count),
            f"The manifest declares {report.expected_count} rows.",
        )

    return report


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Check policy snapshot manifests and source files"
    )
    parser.add_argument(
        "--manifest",
        required=True,
        type=Path,
        help="Path to the snapshot manifest",
    )

    args = parser.parse_args()
    report = validate_snapshot(args.manifest)

    if report.passed:
        print(
            f"Manifest/file checks passed: {args.manifest.name} "
            f"({report.source_count:,} rows)"
        )
        return 0

    print(f"Manifest/file checks blocked: {args.manifest.name}")

    for issue in report.issues:
        print(
            f"- {issue.check_id}: {issue.observed_value}"
        )

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
