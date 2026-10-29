
"""Tests for snapshot manifest and file-integrity checks."""

import hashlib
import json
from pathlib import Path

import pytest

from policyops.generate import generate_golden
from policyops.validate import validate_snapshot


@pytest.fixture
def demo_folder(tmp_path: Path) -> Path:
    generate_golden(tmp_path, seed=42)
    return tmp_path


def issue_ids(report) -> set[str]:
    return {issue.check_id for issue in report.issues}


def update_manifest(path: Path, **changes) -> None:
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest.update(changes)

    path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def reduce_to_650_rows(csv_path: Path) -> bytes:
    lines = csv_path.read_bytes().splitlines(keepends=True)

    # Header plus the first 650 policy records.
    assert len(lines) == 1001

    shortened_csv = b"".join(lines[:651])
    csv_path.write_bytes(shortened_csv)

    return shortened_csv


@pytest.mark.parametrize(
    ("snapshot", "expected_count"),
    [("day1", 1000), ("day2", 1010)],
)
def test_valid_manifests_pass(demo_folder: Path, snapshot: str, expected_count: int):
    report = validate_snapshot(demo_folder / f"{snapshot}.json")

    assert report.passed
    assert report.source_count == expected_count
    assert report.expected_count == expected_count
    assert report.checksum_matches is True


def test_missing_manifest_is_rejected(tmp_path: Path):
    report = validate_snapshot(tmp_path / "missing.json")

    assert not report.passed
    assert "MANIFEST_MISSING" in issue_ids(report)


def test_modified_csv_fails_checksum_check(demo_folder: Path):
    csv_path = demo_folder / "day1.csv"

    original = csv_path.read_bytes()
    csv_path.write_bytes(original.replace(b"active", b"expired", 1))

    report = validate_snapshot(demo_folder / "day1.json")

    assert not report.passed
    assert "CHECKSUM_MISMATCH" in issue_ids(report)
    assert report.source_count == 1000


def test_incomplete_35_percent_drop_is_detected(demo_folder: Path):
    csv_path = demo_folder / "day1.csv"

    # Simulate a delivery containing only 650 of 1,000 rows.
    # The original manifest still declares 1,000 rows.
    reduce_to_650_rows(csv_path)

    report = validate_snapshot(demo_folder / "day1.json")
    problems = issue_ids(report)

    assert not report.passed
    assert report.source_count == 650
    assert "CHECKSUM_MISMATCH" in problems
    assert "ROW_COUNT_MISMATCH" in problems


def test_declared_35_percent_drop_passes_file_checks(demo_folder: Path):
    csv_path = demo_folder / "day1.csv"
    manifest_path = demo_folder / "day1.json"

    shortened_csv = reduce_to_650_rows(csv_path)

    update_manifest(
        manifest_path,
        expected_row_count=650,
        sha256=hashlib.sha256(shortened_csv).hexdigest(),
        complete_snapshot=True,
    )

    report = validate_snapshot(manifest_path)

    # File consistency is valid. The later deletion-review
    # gate must still decide whether promotion is allowed.
    assert report.passed
    assert report.source_count == 650
    assert report.checksum_matches is True


def test_incomplete_snapshot_flag_blocks_validation(demo_folder: Path):
    manifest_path = demo_folder / "day1.json"

    update_manifest(manifest_path, complete_snapshot=False)

    report = validate_snapshot(manifest_path)

    assert not report.passed
    assert "SNAPSHOT_INCOMPLETE" in issue_ids(report)


def test_incorrect_csv_columns_are_detected(demo_folder: Path):
    csv_path = demo_folder / "day1.csv"
    manifest_path = demo_folder / "day1.json"

    modified_csv = csv_path.read_bytes().replace(
        b"customer_id",
        b"client_id",
        1,
    )
    csv_path.write_bytes(modified_csv)

    update_manifest(
        manifest_path,
        sha256=hashlib.sha256(modified_csv).hexdigest(),
    )

    report = validate_snapshot(manifest_path)

    assert not report.passed
    assert "CSV_COLUMNS_INVALID" in issue_ids(report)


def test_unsupported_schema_version_is_rejected(demo_folder: Path):
    manifest_path = demo_folder / "day1.json"

    update_manifest(manifest_path, schema_version="99")

    report = validate_snapshot(manifest_path)

    assert not report.passed
    assert "SCHEMA_VERSION_UNSUPPORTED" in issue_ids(report)
