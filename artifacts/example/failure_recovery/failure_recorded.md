# PolicyOps Incident Report

**Run ID:** `6f9cfd89-1248-44ac-8840-8894cd7ac0d6`
**Status:** failed

## Decision

A processing failure was recorded. Inspect the failure reason and quality events.

## Source delivery

| Field | Recorded value |
|---|---|
| Snapshot date | 2026-10-02 |
| Source file | day2.csv |
| CSV SHA-256 | 18c9a3df95e9ba73735e4516bb0a531b4ea654d54c6168f5daba2e5d76e7c09a |
| Schema version | 1 |
| Declared complete | Yes |
| Declared row count | 1010 |
| Received row count | Not recorded |
| Accepted row count | Not recorded |

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

### Event 1: PROCESSING_TRANSACTION_FAILED

- Severity: error
- Observed value: RuntimeError: Simulated failure after audit-history write
- Expected rule: Failed processing must not leave partial business changes or audit history.
- Evidence: `{"manifest": "day2.json", "stage": "ingestion"}`

## Manual approval

- Reviewer: Not recorded
- Reason: Not recorded
- Approved at: Not recorded

## Processing record

- Started: 2026-10-30T18:55:32.094469+11:00
- Finished: 2026-10-30T18:55:32.094469+11:00
- Failure reason: RuntimeError: Simulated failure after audit-history write

## Interpretation limits

- Source integrity checks establish consistency with the supplied manifest, not the business reason for missing policies.
- Recorded change counts describe the assessment made for this run. They are not necessarily committed changes.
- Audit history counts describe changes recorded for this run, not the entire current database.
