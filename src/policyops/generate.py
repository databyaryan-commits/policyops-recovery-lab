
"""Generate repeatable policy snapshots for local testing."""

import argparse
import csv
import hashlib
import json
import random
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path


CSV_COLUMNS = [
    "policy_id",
    "customer_id",
    "status",
    "premium_amount",
    "effective_date",
    "source_updated_at",
]

DAY_1 = "2026-10-01"
DAY_2 = "2026-10-02"
DAY_1_TIMESTAMP = "2026-10-01T09:00:00Z"
DAY_2_TIMESTAMP = "2026-10-02T09:00:00Z"


def policy_id(number: int) -> str:
    return f"P{number:04d}"


def make_policy(
    number: int,
    rng: random.Random,
    updated_at: str,
) -> dict[str, str]:
    premium = Decimal(rng.randint(40_000, 250_000)) / 100
    effective_date = date(2025, 1, 1) + timedelta(
        days=rng.randint(0, 364)
    )

    return {
        "policy_id": policy_id(number),
        "customer_id": f"C{number:04d}",
        "status": "active",
        "premium_amount": f"{premium:.2f}",
        "effective_date": effective_date.isoformat(),
        "source_updated_at": updated_at,
    }


def write_json(path: Path, content: dict) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as file:
        json.dump(content, file, indent=2, sort_keys=True)
        file.write("\n")


def write_snapshot(
    folder: Path,
    name: str,
    snapshot_date: str,
    rows: list[dict[str, str]],
) -> None:
    csv_path = folder / f"{name}.csv"

    with csv_path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(
            file,
            fieldnames=CSV_COLUMNS,
            lineterminator="\n",
        )
        writer.writeheader()
        writer.writerows(rows)

    manifest = {
        "snapshot_date": snapshot_date,
        "schema_version": "1",
        "expected_row_count": len(rows),
        "sha256": hashlib.sha256(
            csv_path.read_bytes()
        ).hexdigest(),
        "complete_snapshot": True,
        "csv_file": csv_path.name,
    }

    write_json(folder / f"{name}.json", manifest)


def generate_golden(folder: Path, seed: int) -> None:
    rng = random.Random(seed)

    day1 = [
        make_policy(number, rng, DAY_1_TIMESTAMP)
        for number in range(1, 1001)
    ]

    # Fixed groups make the expected changes easy to audit.
    deleted_ids = {
        policy_id(number) for number in range(1, 21)
    }
    changed_ids = {
        policy_id(number) for number in range(21, 71)
    }
    new_ids = {
        policy_id(number) for number in range(1001, 1031)
    }

    # Copy the retained records so day1 remains unchanged.
    day2 = [
        row.copy()
        for row in day1
        if row["policy_id"] not in deleted_ids
    ]

    day2_by_id = {
        row["policy_id"]: row for row in day2
    }

    # Change the premium of exactly 50 retained policies.
    for changed_id in changed_ids:
        row = day2_by_id[changed_id]

        new_premium = (
            Decimal(row["premium_amount"]) + Decimal("25.00")
        )

        row["premium_amount"] = f"{new_premium:.2f}"
        row["source_updated_at"] = DAY_2_TIMESTAMP

    # Add 30 new policies to the second snapshot.
    day2.extend(
        make_policy(number, rng, DAY_2_TIMESTAMP)
        for number in range(1001, 1031)
    )

    day2.sort(key=lambda row: row["policy_id"])

    unchanged_ids = (
        {row["policy_id"] for row in day1}
        - deleted_ids
        - changed_ids
    )

    folder.mkdir(parents=True, exist_ok=True)

    write_snapshot(folder, "day1", DAY_1, day1)
    write_snapshot(folder, "day2", DAY_2, day2)

    # The test oracle is separate from the ingestion logic.
    # The eventual loader must never use this file to decide changes.
    scenario = {
        "scenario": "golden",
        "seed": seed,
        "expected": {
            "new_ids": sorted(new_ids),
            "changed_ids": sorted(changed_ids),
            "deleted_ids": sorted(deleted_ids),
            "unchanged_ids": sorted(unchanged_ids),
            "current_count": len(day2),
        },
    }

    write_json(folder / "scenario.json", scenario)

    print(f"Generated golden snapshots in {folder}")
    print(f"Day 1: {len(day1):,} policies")
    print(f"Day 2: {len(day2):,} policies")
    print(
        f"Expected changes: {len(new_ids)} new, "
        f"{len(changed_ids)} changed, "
        f"{len(deleted_ids)} deleted, "
        f"{len(unchanged_ids)} unchanged"
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate synthetic policy snapshots"
    )
    parser.add_argument(
        "--scenario",
        choices=["golden"],
        default="golden",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=42,
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("data/demo"),
    )

    args = parser.parse_args()

    generate_golden(args.output_dir, args.seed)


if __name__ == "__main__":
    main()
