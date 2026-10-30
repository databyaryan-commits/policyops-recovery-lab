
# PolicyOps Recovery Lab

A Python and PostgreSQL data-engineering project exploring how to safely ingest daily full snapshots when incomplete deliveries, unexpected deletions, duplicate submissions and processing failures could corrupt downstream data.

Built independently using generated insurance-policy records. No real customer or insurer data is used.

## The problem

A full snapshot contains the policies believed to exist on a particular date. When a policy disappears from the next snapshot, a naive pipeline might interpret that absence as a deletion.

But two different situations can look identical:

- An incomplete delivery contains only 650 of 1,000 expected policies.
- A valid complete snapshot contains 650 policies, implying 350 deletions.

The first should fail source-integrity checks. The second may be valid, but warrants review before hundreds of records are removed.

PolicyOps Recovery Lab separates source validation, change detection, human approval and database promotion so these decisions can be audited.

## How it works

1. **Validate the delivery:** Check the manifest, schema, row count, checksum, completeness declaration and policy records.
2. **Check snapshot identity:** Detect committed replays, conflicting same-date files and out-of-order deliveries.
3. **Stage validated records:** Keep incoming policies separate from the current business state.
4. **Assess changes:** Compare staged records with active policies to identify new, changed, deleted and unchanged records.
5. **Apply a safety gate:** Require manual approval when the deletion rate exceeds the illustrative 20% threshold.
6. **Promote transactionally:** Write policy changes and audit history in the same PostgreSQL transaction.
7. **Preserve failure evidence:** For handled ingestion errors, record a failed attempt in a separate transaction after rollback.
8. **Support safe retries:** Previously committed snapshots are treated as replays rather than applied twice.

## Technology

- Python 3.11–3.14
- PostgreSQL 17
- psycopg 3
- pytest
- GitHub Actions
- JSON and Markdown evidence reports

## Reproducible test scenario

The generated golden dataset uses a fixed seed and two full snapshots.

| Metric | Day 1 / Day 2 |
|---|---:|
| Day 1 policies | 1,000 |
| Day 2 policies | 1,010 |
| New policies | 30 |
| Changed policies | 50 |
| Deleted policies | 20 |
| Unchanged policies | 930 |

The generator supplies an independent expected-result file for testing. The ingestion pipeline does not use that file to make business decisions.

## Safety demonstration: Two 35% drops

Both example deliveries contain 650 policy records, but their evidence differs.

| Check | Incomplete delivery | Valid reduced snapshot |
|---|---|---|
| Received records | 650 | 650 |
| Declared records | 1,000 | 650 |
| Checksum matches | No | Yes |
| Validation outcome | Rejected | Passed |
| Deletion candidates | Not assessed | 350 |
| Final decision | Rejected | Awaiting review |
| Deletions committed | 0 | 0 |

The valid snapshot is held because 350 of the initial 1,000 active policies would be deleted, exceeding the example 20% threshold.

[Read the deletion incident comparison](artifacts/example/deletion_contrast/incident_comparison.md)

## Failure and recovery demonstration

A second demonstration injects a Python exception during Day 2 promotion, after the transaction has written 100 audit-history rows but before updating current policies.

PostgreSQL rolls back the uncommitted writes. A separate transaction then saves the failure record.

The unchanged Day 2 snapshot is retried and committed successfully. Submitting it again produces a replay instead of duplicate history.

| Metric | Before failure | After rollback and recording | After retry |
|---|---:|---:|---:|
| Active policies | 1,000 | 1,000 | 1,010 |
| Committed audit rows | 1,000 | 1,000 | 1,100 |
| Recorded failed attempts | 0 | 1 | 1 |

[Read the recovery timeline](artifacts/example/failure_recovery/recovery_timeline.md)

This is controlled exception-injection testing, not a hard-crash or power-failure simulation.

## Database design

Five PostgreSQL tables separate operational state from audit evidence.

| Table | Purpose |
|---|---|
| `ingestion_runs` | Source identity, status, counts, approvals and failure details |
| `policy_staging` | Validated records for individual ingestion attempts |
| `policy_current` | Latest policy state, including soft-deletion flags |
| `policy_history` | Committed insert, update and delete events with before/after evidence |
| `quality_events` | Validation failures, deletion warnings and recorded processing failures |

The schema is defined in [`sql/schema.sql`](sql/schema.sql).

## Local setup

Requirements:

- Python 3.11–3.14
- PostgreSQL 17, running locally
- `psql` and `createdb`, or equivalent PostgreSQL administration tools

Clone the repository and install the package:

```bash
git clone https://github.com/databyaryan-commits/policyops-recovery-lab.git
cd policyops-recovery-lab

python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
```

Create two separate databases:

```bash
createdb policyops_lab
createdb policyops_test

psql -d policyops_lab -f sql/schema.sql
psql -d policyops_test -f sql/schema.sql
```

These commands assume your local PostgreSQL user has permission to create databases and connect using local authentication. If not, configure an appropriate PostgreSQL role and connection string first.

Set the database connection variables:

```bash
export POLICYOPS_DATABASE_URL="dbname=policyops_lab"
export POLICYOPS_TEST_DATABASE_URL="dbname=policyops_test"
```

Verify the main database:

```bash
python -m policyops.db
```

The connection check should find five PolicyOps tables.

On macOS with Homebrew, you may need to use the full path to PostgreSQL commands, such as `/opt/homebrew/opt/postgresql@17/bin/psql`.

## Run the tests

```bash
python -m pytest -q
```

The suite includes source validation, change comparison, approval gates, transactional promotion, replay detection, concurrency, failure recovery, incident reporting and database isolation.

At the latest verified milestone:

- **131 automated tests passed locally.**
- GitHub Actions succeeded on Ubuntu with Python 3.11 and Python 3.14, both using PostgreSQL 17.

[View GitHub Actions](https://github.com/databyaryan-commits/policyops-recovery-lab/actions)

## Generate example incident reports

Both demonstrations use a disposable schema inside `policyops_test`. They do not modify `policyops_lab`.

To reproduce the deletion-safety comparison:

```bash
python -m policyops.demo \
  --scenario deletion-contrast \
  --output-dir artifacts/generated/deletion_contrast
```

To reproduce the rollback and recovery sequence:

```bash
python -m policyops.recovery_demo \
  --output-dir artifacts/generated/failure_recovery
```

The generated outputs contain Markdown reports and matching JSON evidence.

The demonstrations delete their temporary schemas when finished. Run IDs and timestamps change each time, but the scenario seed and expected results remain fixed.

## Try the ingestion pipeline

Generate example snapshots:

```bash
python -m policyops.generate \
  --scenario golden \
  --seed 42 \
  --output-dir data/generated
```

Validate the first delivery:

```bash
python -m policyops.validate \
  --manifest data/generated/day1.json
```

Ingest Day 1 and Day 2:

```bash
python -m policyops.ingest \
  --manifest data/generated/day1.json

python -m policyops.ingest \
  --manifest data/generated/day2.json
```

Run the Day 2 command again to observe replay detection.

These ingestion commands use `POLICYOPS_DATABASE_URL` and write to the configured database. Use a dedicated local development database, not a production database.

For snapshots held for review, the system provides separate approval and promotion commands:

```bash
python -m policyops.approve \
  --run-id <RUN_ID> \
  --reviewer "Demo Reviewer" \
  --reason "Reviewed the synthetic deletion candidates"

python -m policyops.promote --run-id <RUN_ID>
```

Replace `<RUN_ID>` with the run identifier printed by the ingestion command. These steps should be used only after the proposed deletions have been reviewed.

For an existing run, generate a report with:

```bash
python -m policyops.report \
  --run-id <RUN_ID> \
  --format markdown
```

The run must still exist in the configured database. The committed example reports remain available in the repository even after their demonstration schemas are removed.

## Important limitations

- All policy records are synthetic.
- The 20% deletion-review threshold is illustrative, not an insurance industry standard.
- A matching manifest and checksum establish source consistency, not the business reason for a deletion.
- Concurrency tests cover selected simultaneous-ingestion scenarios, not every possible workload.
- Failure recovery tests inject controlled exceptions; they do not establish recovery from hardware failures or abrupt machine termination.
- Failure evidence is recorded separately after handled errors. If the database is unavailable during failure recording, that evidence may not be persisted.
- Cross-environment test success does not guarantee identical behaviour on every operating system or configuration.
- This project is a portfolio demonstration, not a production insurance platform.

## What this project demonstrates

The emphasis is on operational correctness rather than simply loading files quickly:

- Data quality and source-integrity validation
- Transactional database updates and audit trails
- Decision gates for potentially destructive changes
- Safe retries and idempotent committed replays
- Concurrent-ingestion protection
- Failure recording and evidence-based incident analysis
- Automated integration testing and CI verification

The central lesson is that data ingestion is not only about transforming records. It is also about deciding when the evidence is sufficient to safely change business state.
