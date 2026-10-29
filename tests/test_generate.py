
"""Tests for the synthetic policy snapshot generator."""

import csv
import hashlib
import json
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from policyops.generate import CSV_COLUMNS, generate_golden


BUSINESS_COLUMNS = (
    "customer_id",
    "status",
    "premium_amount",
    "effective_date",
)


@pytest.fixture
def demo_folder(tmp_path: Path) -> Path:
    folder = tmp_path / "demo"
    generate_golden(folder, seed=42)
    return folder


def read_policy_rows(folder: Path, name: str) -> list[dict[str, str]]:
    csv_path = folder / f"{name}.csv"

    with csv_path.open("r", encoding="utf-8", newline="") as file:
        reader = csv.DictReader(file)
        assert reader.fieldnames == CSV_COLUMNS
        return list(reader)


def test_manifests_match_the_generated_csv_files(demo_folder: Path):
    expected_snapshots = (
        ("day1", "2026-10-01", 1000),
        ("day2", "2026-10-02", 1010),
    )

    for name, snapshot_date, expected_count in expected_snapshots:
        manifest_path = demo_folder / f"{name}.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

        csv_path = demo_folder / manifest["csv_file"]
        actual_checksum = hashlib.sha256(csv_path.read_bytes()).hexdigest()
        rows = read_policy_rows(demo_folder, name)

        assert manifest["snapshot_date"] == snapshot_date
        assert manifest["schema_version"] == "1"
        assert manifest["complete_snapshot"] is True
        assert manifest["expected_row_count"] == expected_count
        assert len(rows) == expected_count
        assert manifest["sha256"] == actual_checksum


def test_golden_changes_match_the_expected_policy_ids(demo_folder: Path):
    day1_rows = read_policy_rows(demo_folder, "day1")
    day2_rows = read_policy_rows(demo_folder, "day2")

    before = {row["policy_id"]: row for row in day1_rows}
    after = {row["policy_id"]: row for row in day2_rows}

    assert len(before) == len(day1_rows)
    assert len(after) == len(day2_rows)

    expected_original = {
        f"P{number:04d}" for number in range(1, 1001)
    }
    expected_new = {
        f"P{number:04d}" for number in range(1001, 1031)
    }
    expected_deleted = {
        f"P{number:04d}" for number in range(1, 21)
    }
    expected_changed = {
        f"P{number:04d}" for number in range(21, 71)
    }
    expected_unchanged = {
        f"P{number:04d}" for number in range(71, 1001)
    }

    before_ids = set(before)
    after_ids = set(after)
    retained_ids = before_ids & after_ids

    new_ids = after_ids - before_ids
    deleted_ids = before_ids - after_ids

    changed_ids = {
        policy_id
        for policy_id in retained_ids
        if any(
            before[policy_id][column] != after[policy_id][column]
            for column in BUSINESS_COLUMNS
        )
    }
    unchanged_ids = retained_ids - changed_ids

    assert before_ids == expected_original
    assert new_ids == expected_new
    assert deleted_ids == expected_deleted
    assert changed_ids == expected_changed
    assert unchanged_ids == expected_unchanged
    assert len(after_ids) == 1010

    for policy_id in expected_changed:
        premium_difference = (
            Decimal(after[policy_id]["premium_amount"])
            - Decimal(before[policy_id]["premium_amount"])
        )
        assert premium_difference == Decimal("25.00")


def test_scenario_oracle_contains_the_expected_ids(demo_folder: Path):
    scenario_path = demo_folder / "scenario.json"
    scenario = json.loads(scenario_path.read_text(encoding="utf-8"))

    assert scenario["scenario"] == "golden"
    assert scenario["seed"] == 42

    expected = scenario["expected"]

    assert expected["new_ids"] == [
        f"P{number:04d}" for number in range(1001, 1031)
    ]
    assert expected["changed_ids"] == [
        f"P{number:04d}" for number in range(21, 71)
    ]
    assert expected["deleted_ids"] == [
        f"P{number:04d}" for number in range(1, 21)
    ]
    assert expected["unchanged_ids"] == [
        f"P{number:04d}" for number in range(71, 1001)
    ]
    assert expected["current_count"] == 1010


def test_generated_records_follow_the_source_contract(demo_folder: Path):
    for snapshot_name in ("day1", "day2"):
        rows = read_policy_rows(demo_folder, snapshot_name)
        policy_ids = [row["policy_id"] for row in rows]

        assert len(policy_ids) == len(set(policy_ids))

        for row in rows:
            assert set(row) == set(CSV_COLUMNS)
            assert row["policy_id"].startswith("P")
            assert row["customer_id"].startswith("C")
            assert row["status"] in {"active", "cancelled", "expired"}

            premium = Decimal(row["premium_amount"])
            assert premium >= 0
            assert premium.as_tuple().exponent == -2

            date.fromisoformat(row["effective_date"])
            timestamp = datetime.fromisoformat(
                row["source_updated_at"].replace("Z", "+00:00")
            )
            assert timestamp.utcoffset().total_seconds() == 0


def test_same_seed_produces_identical_files(tmp_path: Path):
    first_folder = tmp_path / "first"
    second_folder = tmp_path / "second"
    different_seed_folder = tmp_path / "different_seed"

    generate_golden(first_folder, seed=42)
    generate_golden(second_folder, seed=42)
    generate_golden(different_seed_folder, seed=43)

    first_files = sorted(path.name for path in first_folder.iterdir())
    second_files = sorted(path.name for path in second_folder.iterdir())

    assert first_files == second_files

    for filename in first_files:
        assert (
            (first_folder / filename).read_bytes()
            == (second_folder / filename).read_bytes()
        )

    assert (
        (first_folder / "day1.csv").read_bytes()
        != (different_seed_folder / "day1.csv").read_bytes()
    )
