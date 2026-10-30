# PolicyOps Incident Report

**Run ID:** `4d4109c2-8736-4a47-80fa-ae889a23a6ac`
**Status:** committed

## Decision

Snapshot committed. Business changes and audit events were written by the promotion process.

## Source delivery

| Field | Recorded value |
|---|---|
| Snapshot date | 2026-10-01 |
| Source file | day1.csv |
| CSV SHA-256 | cb126503af3b79cd09188a09e593ad75aafd0b3b08b9743acf44199aec2aaf3c |
| Schema version | 1 |
| Declared complete | Yes |
| Declared row count | 1000 |
| Received row count | 1000 |
| Accepted row count | 1000 |

## Recorded change assessment

| Change type | Count |
|---|---:|
| New | 1000 |
| Changed | 0 |
| Deleted | 0 |
| Unchanged | 0 |

These are assessment counts, not proof that changes were committed.

## Audit history written

| Event type | Rows |
|---|---:|
| Insert | 1000 |
| Update | 0 |
| Delete | 0 |
| **Total** | **1000** |

## Quality and review evidence

No quality events are recorded for this run.
## Manual approval

- Reviewer: Not recorded
- Reason: Not recorded
- Approved at: Not recorded

## Processing record

- Started: 2026-10-30T18:55:31.991962+11:00
- Finished: 2026-10-30T18:55:31.991962+11:00
- Failure reason: Not recorded

## Interpretation limits

- Source integrity checks establish consistency with the supplied manifest, not the business reason for missing policies.
- Recorded change counts describe the assessment made for this run. They are not necessarily committed changes.
- Audit history counts describe changes recorded for this run, not the entire current database.
