# PolicyOps Incident Report

**Run ID:** `6e262800-ad21-471e-8a08-ac9dd9efb8fe`
**Status:** awaiting_review

## Decision

Snapshot held for manual review. Business changes have not been promoted.

## Source delivery

| Field | Recorded value |
|---|---|
| Snapshot date | 2026-10-02 |
| Source file | valid_drop.csv |
| CSV SHA-256 | 12e23166ad3881e7269db7dbb6a96f99ea05331a1a23b2c013fed83bacb801c6 |
| Schema version | 1 |
| Declared complete | Yes |
| Declared row count | 650 |
| Received row count | 650 |
| Accepted row count | 650 |

## Recorded change assessment

| Change type | Count |
|---|---:|
| New | 0 |
| Changed | 0 |
| Deleted | 350 |
| Unchanged | 650 |

These are assessment counts, not proof that changes were committed.

## Audit history written

| Event type | Rows |
|---|---:|
| Insert | 0 |
| Update | 0 |
| Delete | 0 |
| **Total** | **0** |

## Quality and review evidence

### Event 3: DELETION_RATE_REVIEW_REQUIRED

- Severity: warning
- Observed value: 35.00%
- Expected rule: Deletion rates above 20% require manual approval.
- Evidence: `{"active_before": 1000, "deleted_count": 350, "deletion_rate": "0.35", "threshold": "0.20"}`

## Manual approval

- Reviewer: Not recorded
- Reason: Not recorded
- Approved at: Not recorded

## Processing record

- Started: 2026-10-30T18:19:21.613780+11:00
- Finished: Not recorded
- Failure reason: Not recorded

## Interpretation limits

- Source integrity checks establish consistency with the supplied manifest, not the business reason for missing policies.
- Recorded change counts describe the assessment made for this run. They are not necessarily committed changes.
- Audit history counts describe changes recorded for this run, not the entire current database.
