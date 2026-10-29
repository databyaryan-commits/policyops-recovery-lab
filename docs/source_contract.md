
# PolicyOps Recovery Lab — Source Contract

## 1. The problem

This project simulates an insurance company receiving a daily snapshot of its policy records.

Each file is expected to represent the complete set of policies known to the source system for that day.

A missing policy does not automatically mean it should be deleted. The file itself might be incomplete, incorrectly generated, or delivered out of order.

The pipeline needs to check the evidence before changing stored policy records.

All data used in this project is synthetic.

## 2. Snapshot format

Every delivery contains three files:

- `snapshot.csv` — policy records received from the source.
- `manifest.json` — metadata describing the expected delivery.
- `scenario.json` — expected results for the generated test scenario.

The scenario file is used by tests only. The ingestion pipeline must determine changes independently.

### Required CSV columns

| Column | Description |
|---|---|
| policy_id | Unique identifier for a policy |
| customer_id | Customer associated with the policy |
| status | Current policy status |
| premium_amount | Policy premium stored as a decimal |
| effective_date | Date the policy becomes effective |
| source_updated_at | Timestamp of the source record's latest update |

### Field rules

- Policy IDs and customer IDs cannot be empty.
- Each policy ID must appear only once per snapshot.
- Allowed statuses are `active`, `cancelled`, and `expired`.
- Premium amounts must be non-negative and valid to two decimal places.
- Effective dates must use `YYYY-MM-DD`.
- Source timestamps must be valid UTC timestamps.
- Missing required values and malformed records must be rejected.

The pipeline will preserve the original CSV and store normalized values separately.

## 3. Manifest requirements

Each snapshot must have a manifest containing:

- `snapshot_date`
- `schema_version`
- `expected_row_count`
- `sha256`
- `complete_snapshot`
- `csv_file`

The checksum will be calculated from the original CSV bytes.

The pipeline must compare the actual row count and checksum against the manifest before accepting a snapshot.

A valid manifest proves that the received file matches its declared metadata. It does not prove that every business record is correct.

## 4. Detecting changes

The pipeline will compare a validated incoming snapshot with the latest committed policy state.

Four outcomes are possible:

| Change | Meaning |
|---|---|
| New | Policy exists in the incoming snapshot but not in the previous state |
| Changed | Policy exists in both snapshots, but its business values differ |
| Deleted | Previously active policy is absent from a validated complete snapshot |
| Unchanged | Policy exists in both snapshots with equivalent business values |

Business comparisons will use customer ID, status, premium amount, and effective date.

Whitespace and equivalent decimal representations should not create false changes.

The source update timestamp is retained as metadata but does not independently count as a business change.

An explicitly cancelled policy is different from a policy missing from the source file.

## 5. Deletion safety

The pipeline must never infer deletions from an incomplete or unverified snapshot.

For the initial implementation:

- Missing or invalid manifests block promotion.
- An incomplete snapshot cannot delete policies.
- Invalid rows and duplicate policy IDs block promotion.
- Deletion candidates exceeding 20% of the previously active policies require manual approval.
- Approval must include a reason and be recorded before promotion.

The 20% threshold is a project assumption for demonstrating the review process. It is not a verified insurance-industry standard.

## 6. Replay and recovery

A successfully committed snapshot should not create additional changes when loaded again.

A failed load should be retryable without duplicating history.

Policy changes and their audit history must be committed together in one database transaction.

If an error occurs during promotion, neither partial policy changes nor partial audit records should remain.

Older snapshots must not overwrite newer committed states.

A different file submitted for an already committed snapshot date will be rejected until a correction process is defined.

## 7. Initial test scenario

The first generated snapshot will contain 1,000 policies, numbered P0001 to P1000.

The second snapshot will:

- Add P1001 to P1030.
- Modify 50 retained policies.
- Remove P0001 to P0020.
- Leave 930 retained policies unchanged.

Expected comparison:

| Result | Count |
|---|---:|
| New | 30 |
| Changed | 50 |
| Deleted | 20 |
| Unchanged | 930 |

Expected active policy count after the second load: 1,010.

Deleted identities will be retained with an inactive marker, so the database may contain more historical identities than active policies.

The fixtures will use a fixed random seed of 42.

## 8. Planned failure scenarios

The project will test:

1. Duplicate policy IDs.
2. Missing values and invalid data types.
3. Incomplete delivery with a 35% row-count drop.
4. Valid complete delivery declaring a 35% reduction.
5. Loading the same successful snapshot twice.
6. A simulated crash during database promotion.
7. Receiving an older snapshot after a newer one.
8. Two concurrent attempts to promote the same snapshot.
9. Cosmetic formatting changes that should not affect business history.

These are acceptance requirements, not completed results.

## 9. Scope and limitations

This is an independent engineering project using generated policy data.

The first version will focus on local PostgreSQL ingestion, validation, change tracking, transaction safety, and audit evidence.

It will not include a dashboard, cloud deployment, or external insurance-system integration.

The system can establish whether a delivery follows its declared contract. It cannot independently confirm whether a large business change is legitimate.
