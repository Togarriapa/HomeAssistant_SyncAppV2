# Prepared candidate deployment state

Task [#208](https://github.com/Togarriapa/HomeAssistant_SyncAppV2/issues/208) derives from the initial V2 root `README.md` at `71d284ce447d79b044e332c9bc01ae801dc91947`: record a recoverable backup against the exact candidate before Apply, and retain an auditable deployment identity.

`StateStore.record_prepared_deployment(deployment_id, evidence, prepared_at=...)` records a canonical UUID plus an internally validated `CandidateBackupEvidence` produced by the backup gate. `StateStore.prepared_deployment(deployment_id)` retrieves the exact immutable `PreparedDeployment`, or `None` for an absent ID, after reopening the protected store. The record exposes `deployment_id`, `evidence`, and the original UTC `prepared_at` timestamp.

## Persistence and identity

Schema v5 adds `prepared_deployment` through an explicit v4→v5 transaction. New installations create the same table. Earlier supported schemas migrate sequentially without resetting installation identity, boot/interruption state, work records, repository identity pins or synchronization baselines. Unexpected existing tables, unsupported schemas and failed migrations are not adopted or silently repaired.

The table stores these bindings in dedicated columns:

| Binding | Meaning |
| --- | --- |
| Deployment UUID | Unique preparation identity supplied by orchestration |
| Repository target and numeric ID | Exact previously pinned private repository identity |
| Baseline and candidate SHAs | Distinct complete commit identities of the same Git hash format |
| Stage manifest SHA-256 | Exact staged configuration evidence |
| Runtime SHA-256 | Runtime context used by validation and risk analysis |
| Risk level | Low, medium, high or critical classification |
| Core version | Exact stable Home Assistant release used by validation |
| Backup slug | Verified Supervisor backup identifier |
| Preparation timestamp | Original preparation time normalized to UTC |
| Record SHA-256 | Canonical checksum over every preceding field |

The lifetime process lock and SQLite `BEGIN IMMEDIATE` transaction protect insertion. A deployment UUID cannot be rebound. A unique `(repository_id, candidate_sha)` key also prevents a second UUID or repository alias from associating the same candidate with different evidence. Exact replay returns the existing record and preserves its original timestamp, even when the caller supplies a later time. A new candidate may receive a new deployment identity. There is deliberately no update, replace, delete or automatic repair API.

Reads validate every field, the canonical checksum and the current protected repository pin. Malformed or changed persisted values fail closed with fixed `StateError` messages. Failed insertion leaves no partial record. The checksum detects accidental corruption; it is not authentication against an actor who controls protected app storage and can rewrite both the fields and checksum.

## Deployment and recovery boundary

This is an in-process persistence API. Callers must pass the successful output of `create_candidate_backup`; a dataclass or its checksum alone cannot prove that Supervisor was contacted. The API validates evidence shape and repository consistency and preserves the association without claiming to repeat semantic validation or verify the backup remotely.

The durable record supplies evidence for later orchestration and diagnostics. It does not copy candidate bytes, call Supervisor, enqueue deployment work, alter blocked work, reload/restart Home Assistant, observe health, promote/tag a commit or restore a backup. It changes neither service scheduling nor the external Retrigger cronjob. Runtime publication of these records and interrupted deployment execution remain future integration work.

Before Apply becomes reachable, orchestration must independently re-prove the current private repository, baseline, staged bytes, semantic validation, runtime/version evidence and backup recoverability, and establish observation and rollback safeguards. Reopening a prepared record must never skip those gates. There is no administrative supersession mechanism for a prepared candidate in this slice.

## Validation evidence — 2026-09-10

- RED: the restart/replay test failed with `AttributeError` because `StateStore.prepared_deployment` did not exist.
- GREEN: 105 focused tests passed across preparation, backup, lifecycle, work and baseline suites. Coverage includes restart persistence, exact replay, every evidence-field conflict, malformed typed/untyped inputs, candidate/alias rebinding, corrupt disk records, changed repository pins, v1–v4 migrations, migration collision preservation, and atomic/sanitized insertion failure.
- The backup integration test uses the existing isolated candidate/semantic fixture and simulated Supervisor transport, then records the returned backup evidence and reads it after restarting StateStore. It checks that the transport token is absent from SQLite. This is not physical Home Assistant testing.
- The initial full local run passed 952 tests; 20 socket/service tests failed with `PermissionError: Operation not permitted` under this runtime's socket restrictions. Three additional focused tests passed afterward. Full CI and native amd64/aarch64 container validation remain mandatory before merge.
- Ruff, formatting, mypy and Bandit must pass on the submitted code. No privilege, container packaging or live Home Assistant interface change is required.
