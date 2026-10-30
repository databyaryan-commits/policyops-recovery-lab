# PolicyOps: Two 35% Drop Scenarios

This demonstration uses generated insurance-policy data.
It does not represent a real insurer or a production incident.

## Starting state

- Baseline run: `df94ea00-dfc9-415b-af9c-bb2b7cb72fdb` (committed)
- Initial active policies: 1,000

## Comparison

| Evidence | Incomplete delivery | Valid deletion delivery |
|---|---|---|
| Received policies | 650 | 650 |
| Declared row count | 1000 | 650 |
| Complete flag in manifest | True | True |
| Checksum consistent with CSV | No | Yes |
| Pipeline status | rejected | awaiting_review |
| Assessed deletion candidates | Not assessed | 350 |
| Audit events for the run | 0 | 0 |

## Incomplete delivery

Run ID: `4b5b0189-ec3c-4a3d-965d-6db806fa6c27`

Recorded failed checks:

- `CHECKSUM_MISMATCH`
- `ROW_COUNT_MISMATCH`

The delivery failed validation. Its missing rows cannot be treated as confirmed business deletions.

## Valid but suspicious delivery

Run ID: `6e262800-ad21-471e-8a08-ac9dd9efb8fe`

Recorded checks requiring review:

- `DELETION_RATE_REVIEW_REQUIRED`

The source passed file-integrity checks, but the 35% reduction exceeded the illustrative 20% manual-review threshold.

No approval was granted in this demonstration, and no 350-policy deletion was committed.

## Final database state

- Active policies: 1,000
- The incomplete delivery was rejected.
- The valid reduced snapshot remains awaiting review.
- Neither incident created business audit-history changes.

## Inspect the evidence

- [Baseline report](baseline_committed.md)
- [Incomplete delivery report](incomplete_rejected.md)
- [Valid deletion review report](valid_drop_awaiting_review.md)

Matching JSON reports are provided alongside these files.

## What this demonstration cannot establish

A matching source manifest does not establish why 350 policies are missing. It only establishes consistency with the declared file contract.

The approval threshold is an example configuration, not an industry-standard insurance rule.

Run IDs and timestamps change between executions. The source seed and expected business outcomes are fixed.
