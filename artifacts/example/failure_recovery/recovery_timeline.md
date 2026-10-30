# PolicyOps: Failure and Recovery Demonstration

This demonstration uses generated policy data and an intentionally injected Python exception.

It does not represent a production incident or an operating-system crash.

## Scenario

A valid Day 2 snapshot contains 30 new, 50 changed, 20 deleted and 930 unchanged policies.

The demonstration interrupts promotion after the transaction writes 100 audit-history rows, but before it updates current policy records.

## Recovery timeline

| Metric | Before failure | After rollback and recording | After retry |
|---|---:|---:|---:|
| Active policies | 1000 | 1000 | 1010 |
| Committed audit-history rows | 1000 | 1000 | 1100 |
| Recorded failed runs | 0 | 1 | 1 |
| Committed snapshot runs | 1 | 1 | 2 |
| Quality events | 0 | 1 | 1 |

## 1. Initial committed snapshot

- Run ID: `4d4109c2-8736-4a47-80fa-ae889a23a6ac`
- Status: committed
- Active policies: 1,000
- Initial audit-history rows: 1,000

## 2. Injected processing failure

The test deliberately raises:

`RuntimeError: Simulated failure after audit-history write`

PostgreSQL rolls back the failed promotion transaction, including the 100 uncommitted Day 2 history rows.

After rollback, active policies and committed audit-history counts remain unchanged.

## 3. Persistent failure evidence

- Failed-attempt run ID: `6f9cfd89-1248-44ac-8840-8894cd7ac0d6`
- Status: failed
- Reason: RuntimeError: Simulated failure after audit-history write
- Failed-attempt audit-history rows: 0
- Failure event: `PROCESSING_TRANSACTION_FAILED`

The failure record was saved using a separate database transaction after the original rollback.

It uses a new run ID because the failed processing transaction's run registration was rolled back.

## 4. Successful retry

- Run ID: `6200c437-684f-4ab2-983a-099095d26a8b`
- Status: committed
- New policies: 30
- Changed policies: 50
- Deleted policies: 20
- Unchanged policies: 930
- Day 2 committed audit-history rows: 100
- Active policies after retry: 1,010

## 5. Replay verification

A third ingestion of the unchanged Day 2 source was recognized as an existing committed delivery.

No additional ingestion run or business audit-history rows were created.

## Evidence files

- [Initial commit](baseline_committed.md)
- [Failed attempt](failure_recorded.md)
- [Successful retry](retry_committed.md)

Matching JSON evidence files are included alongside these reports.

## Limitations

This is a controlled exception-injection demonstration. It does not establish recovery from a hard crash, power loss or failure-recording database outage.

Run IDs and timestamps change between executions. The source seed and expected outcomes are fixed.
