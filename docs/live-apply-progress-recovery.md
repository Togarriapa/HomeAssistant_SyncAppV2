# Live Apply progress recovery

This document describes the protected, non-authoritative progress journal introduced for the controlled candidate deployment path.

The journal exists to make a future live Apply writer crash-safe. It does **not** grant permission to mutate Home Assistant configuration and it does not replace validation, backup, observation, promotion, or rollback controls.

## Authority boundary

A durable progress row is recovery evidence only. Neither a persisted live Apply intent nor a progress row is sufficient write authority.

A future writer must still obtain fresh producer-issued evidence for the exact candidate and exact deterministic Apply plan, re-check the affected live path immediately before mutation, persist the `mutation_started` boundary first, perform the mutation, and verify the post-mutation result before recording `mutation_verified`.

This component performs no `/homeassistant` write, delete, rename, chmod, directory creation, Supervisor mutation, reload or restart, backup restoration, Git promotion, observation acceptance, or rollback action.

## Durable binding chain

Progress is bound to the existing protected deployment chain:

1. Prepared deployment and recoverable backup evidence.
2. Durable live Apply intent.
3. Exact deterministic `LiveApplyPlan` identity and operations digest.
4. Ordered operation index and SHA-256 of the exact relative operation path.
5. Monotonic progress phase.

The durable store re-proves the supplied plan against the persisted intent and re-proves the operation index/path against that plan before recording a transition. A caller-constructed progress object cannot substitute a different path while retaining otherwise plausible hashes.

The journal stores only identifiers, hashes, phase and timestamp. Candidate file bytes, Home Assistant secrets, GitHub credentials and tokens are not stored in progress records.

## Lifecycle

The persisted lifecycle is intentionally small and monotonic:

| Durable state | Meaning | Permitted next state |
| --- | --- | --- |
| no row | Operation has not crossed the mutation boundary | `mutation_started` |
| `mutation_started` | Mutation may have begun; result is uncertain until explicitly reconciled | `mutation_verified` or `blocked` |
| `mutation_verified` | The operation's post-mutation result was explicitly verified | exact replay only |
| `blocked` | Operation is terminally blocked for this recorded attempt | exact replay only |

Exact replay of the same state is idempotent. Skipped operation indexes, conflicting operation identities, terminal-state regression, reordered operation histories and an unverified predecessor fail closed.

## Restart and Retrigger Work classification

`discover_live_apply_recovery()` combines the durable intent, a fresh exact Apply plan and the ordered progress journal to classify the only safe orchestration action. The classification is non-authoritative; it does not execute that action.

| Observed journal state | Recovery classification | Required behavior |
| --- | --- | --- |
| No progress and operations remain | `start_next` | Fresh producer evidence is still required; journal the operation before mutation. |
| Latest operation is `mutation_started` | `reconcile_uncertain` | Do **not** blindly re-apply. Re-prove/reconcile the live result and then explicitly mark verified or blocked. |
| Latest operation is `mutation_verified` and operations remain | `start_next` | The next contiguous operation may enter the normal fresh-evidence path. |
| Latest operation is `blocked` | `blocked` | Stop automatic Apply progression. |
| Every planned operation is `mutation_verified` | `complete` | The path-level Apply journal is complete only. This is **not** deployment success. |

`complete` does not authorize promotion to `main`. Reload/restart decisions, Home Assistant health recovery, the observation window, affected-resource validation, deployment result recording, promotion/tagging, and rollback remain later controlled-deployment stages.

The Retrigger Work mechanism must therefore treat a recovered `mutation_started` entry as an uncertainty boundary rather than a retry instruction. Process restart, Home Assistant restart, Raspberry Pi reboot, timeout, or network failure can never convert uncertainty into success automatically.

## Crash safety and integrity

Progress persistence uses the protected `StateStore`, an explicit schema migration and an immediate SQLite transaction. Rows contain a deterministic record digest and are revalidated when loaded or discovered.

Recovery fails closed when it detects, among other conditions:

- corrupt or tampered progress data;
- a missing or altered durable live Apply intent;
- repository identity or prepared-deployment drift detected by the durable intent layer;
- a different baseline, candidate or Stage manifest;
- a changed deterministic operation set;
- a mismatched operation index/path;
- non-contiguous or contradictory progress history;
- progress history longer than the supplied Apply plan.

Diagnostics are intentionally sanitized and do not echo persisted sensitive values.

## Downstream writer contract

An eventual filesystem writer may consume this journal only after this recovery boundary is proven. For each operation it must preserve this ordering:

1. Reconstruct and freshly validate the controlled deployment evidence chain.
2. Re-prove the exact deterministic operation.
3. Re-check the live path precondition immediately before mutation.
4. Durably record `mutation_started`.
5. Mutate exactly the authorized path.
6. Verify the resulting live state.
7. Durably transition to `mutation_verified`, or `blocked` when deterministic reconciliation fails.
8. Continue only after a verified predecessor.

A crash between steps 4 and 7 intentionally leaves `mutation_started`, forcing reconciliation on restart instead of duplicate mutation.

## Retrigger Work

The recurring Retrigger Work recovery mechanism remains required and unchanged by this component. Its future deployment-recovery consumer must use the deterministic classifications above while continuing to respect configuration validation, backup, observation and rollback safeguards. Permanent or deterministic failures must remain blocked rather than entering infinite retry loops.
