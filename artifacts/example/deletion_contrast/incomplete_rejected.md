# PolicyOps Incident Report

**Run ID:** `4b5b0189-ec3c-4a3d-965d-6db806fa6c27`
**Status:** rejected

## Decision

Snapshot rejected during validation. Business changes were not promoted.

## Source delivery

| Field | Recorded value |
|---|---|
| Snapshot date | 2026-10-02 |
| Source file | incomplete_drop.csv |
| CSV SHA-256 | cb126503af3b79cd09188a09e593ad75aafd0b3b08b9743acf44199aec2aaf3c |
| Schema version | 1 |
| Declared complete | Yes |
| Declared row count | 1000 |
| Received row count | 650 |
| Accepted row count | 650 |

## Recorded change assessment

| Change type | Count |
|---|---:|
| New | Not recorded |
| Changed | Not recorded |
| Deleted | Not recorded |
| Unchanged | Not recorded |

These are assessment counts, not proof that changes were committed.

## Audit history written

| Event type | Rows |
|---|---:|
| Insert | 0 |
| Update | 0 |
| Delete | 0 |
| **Total** | **0** |

## Quality and review evidence

### Event 1: CHECKSUM_MISMATCH

- Severity: error
- Observed value: 12e23166ad3881e7269db7dbb6a96f99ea05331a1a23b2c013fed83bacb801c6
- Expected rule: The CSV checksum must match the manifest.
- Evidence: `{"manifest": "incomplete_drop.json"}`

### Event 2: ROW_COUNT_MISMATCH

- Severity: error
- Observed value: 650
- Expected rule: The manifest declares 1000 rows.
- Evidence: `{"manifest": "incomplete_drop.json"}`

## Manual approval

- Reviewer: Not recorded
- Reason: Not recorded
- Approved at: Not recorded

## Processing record

- Started: 2026-10-30T18:19:21.613780+11:00
- Finished: 2026-10-30T18:19:21.613780+11:00
- Failure reason: 2 validation check(s) failed

## Interpretation limits

- Source integrity checks establish consistency with the supplied manifest, not the business reason for missing policies.
- Recorded change counts describe the assessment made for this run. They are not necessarily committed changes.
- Audit history counts describe changes recorded for this run, not the entire current database.
