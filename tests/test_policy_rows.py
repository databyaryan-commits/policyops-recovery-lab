
"""Check policy-level validation and normalization rules."""

import csv
import hashlib
import json
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from policyops.generate import CSV_COLUMNS, generate_golden
from policyops.validate import validate_snapshot


@pytest.fixture
def demo_folder(tmp_path: Path) -> Path:
    generate_golden(tmp_path, seed=42)
    return tmp_path


def read_rows(folder: Path) -> list[dict[str, str]]:
    with (folder / "day1.csv").open(
        "r", encoding="utf-8", newline=""
    ) as file:
        return list(csv.DictReader(file))


def update_checksum(folder: Path) -> None:
    csv_path = folder / "day1.csv"
    manifest_path = folder / "day1.json"

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["sha256"] = hashlib.sha256(
        csv_path.read_bytes()
    ).hexdigest()

    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def save_rows(folder: Path, rows: list[dict[str, str]]) -> None:
    csv_path = folder / "day1.csv"

    with csv_path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(
            file,
            fieldnames=CSV_COLUMNS,
            lineterminator="\n",
        )
        writer.writeheader()
        writer.writerows(rows)

    update_checksum(folder)


def check_ids(report) -> set[str]:
    return {issue.check_id for issue in report.issues}


def test_duplicate_policy_id_blocks_snapshot(demo_folder: Path):
    rows = read_rows(demo_folder)

    # The second record repeats the first ID, with extra whitespace.
    rows[1]["policy_id"] = f" {rows[0]['policy_id']} "
    save_rows(demo_folder, rows)

    report = validate_snapshot(demo_folder / "day1.json")

    assert not report.passed
    assert "POLICY_ID_DUPLICATE" in check_ids(report)
    assert report.accepted_count == 999
    assert report.checksum_matches is True


def test_missing_policy_id_is_rejected(demo_folder: Path):
    rows = read_rows(demo_folder)
    rows[0]["policy_id"] = ""
    save_rows(demo_folder, rows)

    report = validate_snapshot(demo_folder / "day1.json")

    assert not report.passed
    assert "POLICY_ID_MISSING" in check_ids(report)


def test_explicit_null_customer_id_is_rejected(demo_folder: Path):
    rows = read_rows(demo_folder)
    rows[0]["customer_id"] = " NULL "
    save_rows(demo_folder, rows)

    report = validate_snapshot(demo_folder / "day1.json")

    assert not report.passed
    assert "CUSTOMER_ID_MISSING" in check_ids(report)


def test_unsupported_policy_status_is_rejected(demo_folder: Path):
    rows = read_rows(demo_folder)
    rows[0]["status"] = "suspended"
    save_rows(demo_folder, rows)

    report = validate_snapshot(demo_folder / "day1.json")

    assert not report.passed
    assert "STATUS_INVALID" in check_ids(report)


@pytest.mark.parametrize(
    "invalid_premium",
    [
        "-10.00",
        "not_a_number",
        "10.005",
        "10000000000.00",
    ],
)
def test_invalid_premiums_are_rejected(
    demo_folder: Path,
    invalid_premium: str,
):
    rows = read_rows(demo_folder)
    rows[0]["premium_amount"] = invalid_premium
    save_rows(demo_folder, rows)

    report = validate_snapshot(demo_folder / "day1.json")

    assert not report.passed
    assert "PREMIUM_INVALID" in check_ids(report)
    assert report.checksum_matches is True


def test_invalid_calendar_date_is_rejected(demo_folder: Path):
    rows = read_rows(demo_folder)
    rows[0]["effective_date"] = "2025-02-30"
    save_rows(demo_folder, rows)

    report = validate_snapshot(demo_folder / "day1.json")

    assert not report.passed
    assert "EFFECTIVE_DATE_INVALID" in check_ids(report)


@pytest.mark.parametrize(
    "invalid_timestamp",
    [
        "2026-10-01T09:00:00",
        "2026-10-01T09:00:00+05:30",
    ],
)
def test_invalid_utc_timestamps_are_rejected(
    demo_folder: Path,
    invalid_timestamp: str,
):
    rows = read_rows(demo_folder)
    rows[0]["source_updated_at"] = invalid_timestamp
    save_rows(demo_folder, rows)

    report = validate_snapshot(demo_folder / "day1.json")

    assert not report.passed
    assert "SOURCE_TIMESTAMP_INVALID" in check_ids(report)


def test_cosmetic_differences_are_normalized(demo_folder: Path):
    rows = read_rows(demo_folder)

    rows[0]["policy_id"] = " P0001 "
    rows[0]["customer_id"] = " C0001 "
    rows[0]["status"] = " Active "
    rows[0]["premium_amount"] = "120.0"
    rows[0]["source_updated_at"] = "2026-10-01T09:00:00+00:00"

    save_rows(demo_folder, rows)

    report = validate_snapshot(demo_folder / "day1.json")

    assert report.passed
    assert report.accepted_count == 1000

    normalized = report.records[0]

    assert normalized.policy_id == "P0001"
    assert normalized.customer_id == "C0001"
    assert normalized.status == "active"
    assert normalized.premium_amount == Decimal("120.00")
    assert normalized.source_updated_at.utcoffset() == timedelta(0)


def test_extra_csv_value_is_rejected(demo_folder: Path):
    csv_path = demo_folder / "day1.csv"
    lines = csv_path.read_text(encoding="utf-8").splitlines()

    # Add a seventh value to the first data row.
    lines[1] += ",unexpected"

    csv_path.write_text(
        "\n".join(lines) + "\n",
        encoding="utf-8",
    )
    update_checksum(demo_folder)

    report = validate_snapshot(demo_folder / "day1.json")

    assert not report.passed
    assert "CSV_ROW_SHAPE_INVALID" in check_ids(report)
    assert report.source_count == 1000
    assert report.checksum_matches is True
