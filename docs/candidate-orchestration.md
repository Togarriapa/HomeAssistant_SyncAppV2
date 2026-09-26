# Candidate deployment orchestration authority

The initial V2 README requires a detected Repo B `candidate` commit to enter a
controlled deployment pipeline. Detection already creates one durable
`candidate` work identity per exact commit SHA. Schema v26 adds the first
production consumer-side authority: a claimed candidate can be registered and
assigned its first permitted action without executing that action.

## Authority boundary

`register_claimed_candidate` accepts only the exact `WorkItem` returned by an
atomic `claim_work_kind("candidate")` operation. In the same immediate SQLite
transaction it re-reads that row, requires the full claimed value to match,
requires status `running`, verifies the pinned Repo B target and repository ID,
and inserts an immutable initial orchestration row.

The initial state is deliberately narrow:

| Phase | Next action | Meaning |
|---|---|---|
| `detected` | `fetch_stage` | The exact trusted SHA may enter the existing read-only Fetch/Stage boundary. |
| `staged` | `analyze` | A verified, durable Stage exists for the exact candidate and may enter analysis. |
| `completed` | `none` | Reserved terminal state for a later evidence-driven transition. |
| `blocked` | `none` | Reserved deterministic terminal state for a later evidence-driven transition. |

Callers cannot supply a phase or action. This slice therefore grants no
authority to fetch, write Stage, validate configuration, create or restore a
backup, modify live Home Assistant files, restart Core, publish Git refs,
promote, or roll back a deployment.

## Durable evidence and replay

`candidate_orchestration` was introduced by schema v26 and expanded by schema v27.
Each row is bound by composite
foreign key to `work_kind=candidate` plus the exact work key/SHA, and by foreign
key to the pinned repository target. The canonical integrity digest covers the
work kind, exact SHA, schema version, target, repository ID, phase, next action,
and timestamps. Loading revalidates the digest and the current repository
binding. Missing, malformed, rebound, or tampered evidence fails closed.

Registering the same claimed work and exact repository binding returns the
existing row without changing its timestamp or action. A different binding is
rejected. The table stores no token, configuration bytes, response body, live
path, Stage path, or exception text.

## Retrigger and runtime visibility

`discover_candidate_orchestrations` returns at most 64 canonical nonterminal
records in deterministic order and performs no network or filesystem action.
It is the bounded discovery seam for a later Retrigger worker.

`candidate_orchestration_runtime_evidence` exposes only phase, next action, and
update time. Repository targets and candidate SHAs are intentionally absent.
The top-level runtime publication aggregates Fetch/Stage checkpoints into the
existing recovery document without publishing repository or candidate identity.

## Follow-up delivery

Later child tasks under user story #336 must consume the selected action and
advance state only from producer-issued evidence. They must retain one bounded
action per invocation, existing retry/backoff and stale-claim recovery,
deterministic exact-SHA blocking, and the complete validation, backup, Apply,
observation, promotion, and rollback gates. Schema v26 itself is not a shortcut
around any of those safeguards.
