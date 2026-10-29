
"""Validate policy snapshot files and normalize their records."""

import argparse
import csv
import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
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

ALLOWED_STATUSES = {"active", "cancelled", "expired"}
MONEY_PRECISION = Decimal("0.01")
MAX_PREMIUM = Decimal("9999999999.99")
NULL_VALUES = {"", "null", "none"}


@dataclass(frozen=True)
class ValidationIssue:
    check_id: str
    observed_value: str
    expected_rule: str
    severity: str = "error"


@dataclass(frozen=True)
class PolicyRecord:
    policy_id: str
    customer_id: str
    status: str
    premium_amount: Decimal
    effective_date: date
    source_updated_at: datetime


@dataclass
class ValidationReport:
    manifest_path: Path
    snapshot_date: str | None = None
    expected_count: int | None = None
    source_count: int | None = None
    checksum_matches: bool | None = None
    issues: list[ValidationIssue] = field(default_factory=list)
    records: list[PolicyRecord] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.issues

    @property
    def accepted_count(self) -> int:
        return len(self.records)

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


def clean_text(row: dict, column: str) -> str | None:
    """Trim text and treat explicit null markers as missing."""

    value = row.get(column)

    if not isinstance(value, str):
        return None

    cleaned = value.strip()

    if cleaned.lower() in NULL_VALUES:
        return None

    return cleaned


def normalize_policy_row(
    row: dict,
    row_number: int,
    report: ValidationReport,
    seen_policy_ids: set[str],
) -> PolicyRecord | None:
    """Normalize one row and report any invalid business values."""

    issue_count_before = len(report.issues)

    # Extra CSV values appear under a None key. Missing values
    # appear as None in the corresponding column.
    if None in row or any(value is None for value in row.values()):
        report.add_issue(
            "CSV_ROW_SHAPE_INVALID",
            f"row {row_number}",
            "Every row must contain exactly six values.",
        )
        return None

    policy_id = clean_text(row, "policy_id")

    if policy_id is None:
        report.add_issue(
            "POLICY_ID_MISSING",
            f"row {row_number}",
            "Every policy must have a non-empty ID.",
        )
    elif policy_id in seen_policy_ids:
        report.add_issue(
            "POLICY_ID_DUPLICATE",
            f"row {row_number}",
            "Policy IDs must be unique within a snapshot.",
        )
    else:
        seen_policy_ids.add(policy_id)

    customer_id = clean_text(row, "customer_id")

    if customer_id is None:
        report.add_issue(
            "CUSTOMER_ID_MISSING",
            f"row {row_number}",
            "Every policy must have a customer ID.",
        )

    status_text = clean_text(row, "status")
    status = status_text.lower() if status_text else None

    if status not in ALLOWED_STATUSES:
        report.add_issue(
            "STATUS_INVALID",
            f"row {row_number}",
            "Status must be active, cancelled, or expired.",
        )

    premium = None
    premium_text = clean_text(row, "premium_amount")

    try:
        if premium_text is None:
            raise ValueError("Missing premium.")

        premium = Decimal(premium_text)

        if (
            not premium.is_finite()
            or premium < 0
            or premium > MAX_PREMIUM
            or premium != premium.quantize(MONEY_PRECISION)
        ):
            raise ValueError("Invalid premium precision or range.")

        premium = premium.quantize(MONEY_PRECISION)

    except (InvalidOperation, ValueError):
        report.add_issue(
            "PREMIUM_INVALID",
            f"row {row_number}",
            "Premium must be non-negative with at most two decimal places.",
        )

    effective_date = None
    effective_date_text = clean_text(row, "effective_date")

    try:
        if (
            effective_date_text is None
            or not re.fullmatch(
                r"\d{4}-\d{2}-\d{2}",
                effective_date_text,
            )
        ):
            raise ValueError("Invalid date format.")

        effective_date = date.fromisoformat(effective_date_text)

    except ValueError:
        report.add_issue(
            "EFFECTIVE_DATE_INVALID",
            f"row {row_number}",
            "Effective dates must be valid YYYY-MM-DD dates.",
        )

    source_updated_at = None
    timestamp_text = clean_text(row, "source_updated_at")

    try:
        if timestamp_text is None or "T" not in timestamp_text:
            raise ValueError("Missing or malformed timestamp.")

        parsed_timestamp = datetime.fromisoformat(
            timestamp_text.replace("Z", "+00:00")
        )

        if (
            parsed_timestamp.tzinfo is None
            or parsed_timestamp.utcoffset() != timedelta(0)
        ):
            raise ValueError("Timestamp must be in UTC.")

        source_updated_at = parsed_timestamp.astimezone(timezone.utc)

    except ValueError:
        report.add_issue(
            "SOURCE_TIMESTAMP_INVALID",
            f"row {row_number}",
            "Source timestamps must include a valid UTC timezone.",
        )

    if len(report.issues) > issue_count_before:
        return None

    return PolicyRecord(
        policy_id=policy_id,
        customer_id=customer_id,
        status=status,
        premium_amount=premium,
        effective_date=effective_date,
        source_updated_at=source_updated_at,
    )


def validate_snapshot(manifest_path: Path) -> ValidationReport:
    """Check the delivery contract and normalize its policy rows."""

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

            columns_valid = (
                len(columns) == len(REQUIRED_COLUMNS)
                and set(columns) == REQUIRED_COLUMNS
            )

            if not columns_valid:
                report.add_issue(
                    "CSV_COLUMNS_INVALID",
                    ", ".join(columns),
                    "The CSV must contain exactly the required columns.",
                )

            report.source_count = 0
            seen_policy_ids = set()

            for row_number, row in enumerate(reader, start=2):
                report.source_count += 1

                # Avoid interpreting rows against an invalid header.
                if not columns_valid:
                    continue

                record = normalize_policy_row(
                    row,
                    row_number,
                    report,
                    seen_policy_ids,
                )

                if record is not None:
                    report.records.append(record)

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
        description="Validate policy snapshot files and records"
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
            f"Snapshot validation passed: {args.manifest.name} "
            f"({report.accepted_count:,} accepted rows)"
        )
        return 0

    print(f"Snapshot validation blocked: {args.manifest.name}")

    for issue in report.issues:
        print(f"- {issue.check_id}: {issue.observed_value}")

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
