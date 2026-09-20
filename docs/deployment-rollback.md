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

Completed and blocked rollback records are terminal for automatic replay. In-progress and uncertain records must be recovered through reconciliation rather than restore replay. Retrigger integration must preserve the same state-machine rules, locking, bounded retry/backoff behavior, stale-work recovery, and deterministic-failure suppression used by the wider V2 recovery model.

In particular, the Retrigger path must never:

- issue a second restore merely because a previous request timed out;
- clear or forget the rejected candidate SHA;
- select a caller-provided backup instead of the bound verified backup;
- bypass configuration validation, backup proof, observation, or rollback authority;
- force or silently merge Git divergence.

## Runtime evidence

Rollback status exposed for troubleshooting and AI analysis must be sanitized and content-free. Useful fields are stable identifiers and state-machine metadata such as deployment/candidate identity, phase, reconciliation state, block reason, attempt count, and timestamps. Tokens, Supervisor response bodies, Home Assistant configuration content, and other secrets must never be persisted as runtime evidence.

## Verification

The task is merge-eligible only after RED-before-GREEN tests cover authorization, exact backup binding, journal-before-mutation, uncertain-outcome reconciliation, deterministic blocking, post-restore Core/Supervisor health proof, idempotent replay, Retrigger recovery/backoff, and sanitized runtime exposure. Final review requires exact-head Ruff format/lint, strict mypy, Bandit, pytest, and native amd64/aarch64 CI to pass.