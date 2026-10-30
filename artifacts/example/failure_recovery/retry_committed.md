# PolicyOps Incident Report

**Run ID:** `6200c437-684f-4ab2-983a-099095d26a8b`
**Status:** committed

## Decision

Snapshot committed. Business changes and audit events were written by the promotion process.

## Source delivery

| Field | Recorded value |
|---|---|
| Snapshot date | 2026-10-02 |
| Source file | day2.csv |
| CSV SHA-256 | 18c9a3df95e9ba73735e4516bb0a531b4ea654d54c6168f5daba2e5d76e7c09a |
| Schema version | 1 |
| Declared complete | Yes |
| Declared row count | 1010 |
| Received row count | 1010 |
| Accepted row count | 1010 |

## Recorded change assessment

| Change type | Count |
|---|---:|
| New | 30 |
| Changed | 50 |
| Deleted | 20 |
| Unchanged | 930 |

These are assessment counts, not proof that changes were committed.

## Audit history written

| Event type | Rows |
|---|---:|
| Insert | 30 |
| Update | 50 |
| Delete | 20 |
| **Total** | **100** |

## Quality and review evidence

No quality events are recorded for this run.
## Manual approval

- Reviewer: Not recorded
- Reason: Not recorded
- Approved at: Not recorded

## Processing record

- Started: 2026-10-30T18:55:32.099129+11:00
- Finished: 2026-10-30T18:55:32.099129+11:00
- Failure reason: Not recorded

## Interpretation limits

- Source integrity checks establish consistency with the supplied manifest, not the business reason for missing policies.
- Recorded change counts describe the assessment made for this run. They are not necessarily committed changes.
- Audit history counts describe changes recorded for this run, not the entire current database.
