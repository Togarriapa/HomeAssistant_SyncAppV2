# Candidate dependency checkpoint and recovery

This increment implements step 4 of the controlled candidate workflow in the
initial V2 README: analyze dependencies only after the exact candidate and its
isolated Stage have passed integrity verification. It grants no validation,
backup, Apply, restart, promotion, or rollback authority.

## Evidence chain

Schema v29 adds `candidate_dependency_checkpoint`. Each row is bound to the
exact candidate SHA, pinned repository identity, orchestration digest,
Fetch/Stage checkpoint digest, integrity checkpoint digest, baseline SHA, and
Stage manifest digest. A canonical SHA-256 record digest covers all fields.

The executor durably inserts `planned` before making any Home Assistant Core
request. It then collects the existing bounded read-only REST/WebSocket runtime
bundle and invokes the existing conservative dependency analyzer against the
exact verified Stage. Completion stores canonical runtime and dependency JSON
with strict size, duplicate-key, type, binding, and internal-consistency checks.
This exact runtime snapshot is required because downstream impact and risk
proofs must not silently combine dependency evidence with a different live
state.

The checkpoint advances atomically with orchestration from
`integrity_verified/analyze_dependencies` to
`dependencies_analyzed/classify_risk`. It cannot authorize risk classification
unless both updates commit. The risk transition remains a separate guarded
increment.

## Replay and failure semantics

A completed checkpoint reconstructs and reverifies its runtime and dependency
objects without a credential or network request. Replays also reverify the
durable Stage and integrity/change evidence. Missing, malformed, oversized,
tampered, stale, or rebound evidence fails closed and is deterministic.

Core transport or credential unavailability is transient and stays eligible
for the existing bounded backoff. Invalid Stage, runtime, dependency, or
checkpoint evidence is deterministic and blocks the exact unchanged candidate
through the existing candidate work state. A changed SHA remains a different
work identity.

## Retrigger and observability

The dependency Retrigger lane recovers only interrupted candidate work whose
exact next action is `analyze_dependencies`, atomically claims at most one
eligible item, and defers it after successful completion so a later invocation
may consider the next gate. The top-level cycle runs this lane only when neither
Fetch/Stage nor integrity analysis performed an action, preserving one
candidate action per cycle.

Runtime status exposes only bounded aggregate phase counts, reference counts,
and the latest update time. It excludes repository names, SHAs, paths,
candidate content, runtime payloads, tokens, and raw errors.

## Security review notes

- Candidate content is never executed and templates/includes are not resolved.
- Stage reads retain the existing no-symlink, manifest-bound verification.
- Runtime collection is read-only and bounded by the existing Core collectors.
- SQLite completion and orchestration advancement are one immediate
  transaction with compare-and-swap digests.
- Completed replay performs no external I/O.
- Tests use only fakes and isolated temporary directories; no live Home
  Assistant, Supervisor, backup, deployment, or Git-ref mutation occurs.
