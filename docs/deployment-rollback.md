# Crash-safe deployment rollback

Task: #331

## Purpose

A candidate that reaches immutable failed deployment finalization must be recovered to the exact verified pre-deployment Home Assistant backup. Rollback is a recovery path, not an alternate deployment or arbitrary backup-restore API.

The rollback implementation preserves the initial V2 README controlled-candidate model: isolated validation and backup evidence remain authoritative, failed candidate SHAs remain blocked, and recovery cannot promote/tag Git refs, clear rejection state, or bypass validation and observation safeguards.

## Authority boundary

Rollback authorization is derived from the persisted prepared deployment and immutable failed deployment finalization. The implementation re-proves the bound private Repo B identity, exact baseline SHA, candidate SHA, and exact restorable backup evidence before durable rollback intent can be created.

Callers cannot select a different backup slug or use rollback to restore an unrelated backup. Repository divergence, invalid backup evidence, invalid finalization authority, and deterministic restore rejection fail closed.

## Journal-before-mutation

The rollback state machine persists intent before the Supervisor restore mutation. Restore attempts carry durable attempt state and bounded identifiers so a process crash, Home Assistant restart, Raspberry Pi reboot, network failure, or timeout does not erase whether mutation may have started.

An uncertain or acknowledged restore is never blindly repeated. Recovery first performs read-only Supervisor job reconciliation. Ambiguous evidence remains blocked. A restore is advanced to observation only when authoritative Supervisor evidence proves that the exact restore job completed.

## Post-restore proof

A reconciled restore is not complete merely because the Supervisor job finished. Completion re-proves that Repo B `main` is still the exact baseline bound into the rollback authority and performs bounded Home Assistant Core and Supervisor health probes. Only then may the durable rollback record transition to `completed`.

Transient health or transport unavailability is retryable; it does not authorize another restore. Repository divergence is deterministic and blocks completion.

## Idempotency and retrigger requirements

Completed and blocked rollback records are terminal for automatic replay. In-progress and uncertain records are recovered through read-only reconciliation rather than restore replay. Each bounded Retrigger cycle discovers at most 32 records, creates one deterministic `deployment_rollback` work identity per deployment, recovers only matching stale running work, and claims at most one eligible item.

The work ledger is deliberately not marked successful while the domain remains at `restore_acknowledged`, `reconciliation_required`, `in_progress`, `restored`, or another nonterminal phase. Those outcomes enter bounded retry/backoff so a later invocation can reconcile or observe them. Only `completed` succeeds. Durable `blocked` or `ambiguous` outcomes, corrupt recovery authority, invalid proofs, wrong backup evidence, invalid credentials/configuration and other deterministic failures block the work item. Network/proof unavailability, uncertain acknowledgement, reconciliation transport failure and temporarily unavailable post-restore health evidence remain bounded transient retries.

When no rollback record is pending, the pass is a credential-free, network-free no-op. A pending item requires both GitHub and Supervisor credentials. The top-level cycle runs the rollback lane before fresh candidate detection, so recovery of an already failed deployment cannot be displaced by new candidate intake.

In particular, the Retrigger path must never:

- issue a second restore merely because a previous request timed out;
- clear or forget the rejected candidate SHA;
- select a caller-provided backup instead of the bound verified backup;
- bypass configuration validation, backup proof, observation, or rollback authority;
- force or silently merge Git divergence.

## Runtime evidence

Rollback status exposed for troubleshooting and AI analysis is sanitized and content-free. `analysis/recovery.json` includes fixed aggregate counts for phase, reconciliation state and block reason, plus total/maximum domain attempts and the latest update time. The generic recovery ledger independently exposes `deployment_rollback` pending/running/retry/blocked/succeeded counts and bounded backoff metadata.

The runtime query never selects deployment IDs, repository targets, candidate or baseline SHAs, backup slugs, job UUIDs, tokens, private paths, response bodies or exception text. Collection is read-only, bounded to 4,096 rows and fails closed on malformed, oversized or future-dated evidence.

## Crash windows and replay classification

| Durable state | Automatic action | Work classification |
| --- | --- | --- |
| `planned` | Re-prove exact repository and backup, journal, then request one restore | Retry only pre-mutation transport unavailability; invalid authority/proof blocks |
| `restore_started` / `uncertain` | Read-only Supervisor inventory reconciliation | Never repeat restore; transport failure retries, ambiguity blocks |
| `restore_acknowledged` | Read the exact acknowledged job UUID | In-progress retries; malformed, missing, failed or conflicting evidence blocks |
| `observing` + `restored` | Re-prove baseline and collect bounded Core/Supervisor health | Temporary health failure retries; repository divergence blocks |
| `completed` | No mutation | Terminal success |
| `blocked` | No mutation | Terminal until explicit administrative handling |

The failed candidate remains rejected throughout every state. Rollback cannot clear rejection, manufacture deployment success, promote/tag the candidate, or move/force/merge Repo B refs.

## Verification

RED-before-GREEN tests cover authorization, exact backup binding, journal-before-mutation, uncertain-outcome reconciliation, deterministic blocking, post-restore Core/Supervisor health proof, idempotent replay, Retrigger recovery/backoff, cycle ordering and sanitized runtime exposure. Merge still requires exact reviewed-head Ruff format/lint, strict mypy, Bandit, full pytest, and native amd64/aarch64 CI.
