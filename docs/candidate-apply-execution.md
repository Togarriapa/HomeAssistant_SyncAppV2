# Candidate Apply execution recovery

Task #355 connects durable Apply admission to the existing one-operation live
writer without turning Retrigger into an unbounded deployment worker.

## Fresh authority on every action

`candidate_apply_execute` work is keyed by the immutable deployment ID and is
created atomically with the admitted live Apply intent. Before every action the
executor reloads the completed candidate authority and freshly re-proves the
Supervisor backup, private Repo B identity and exact `main`/`candidate` heads,
Apply authorization, isolated Stage, and canonical ordered plan.

Earlier successful operations mean the original all-path baseline proof cannot be
repeated. Recovery therefore reconstructs only its exact integrity-protected
admission binding. It does not grant a filesystem write. The existing writer still
performs fresh no-follow baseline proofs both before and after journaling the one
selected operation. An uncertain journal selects read-only reconciliation instead
of blindly replaying a mutation.

## Bounded progress and completion

One executor invocation delegates exactly once to `advance_live_apply_once()`.
Verified operations and successful reconciliation are returned to `pending`
without failure backoff so a later cycle may continue. Deterministically blocked
reconciliation blocks the exact work item. A complete non-empty plan persists
post-Apply activation authorization before `candidate_restart` is enqueued; an
empty plan completes without restart authority.

The Retrigger lane recovers only stale `candidate_apply_execute` work, considers a
bounded two-row window, and advances at most one item per cycle. Transient backup,
repository, and journaled-uncertainty failures use the normal bounded retry policy.
Invalid or rebound evidence is deterministic and cannot loop automatically.

## Runtime and safety boundary

Runtime recovery inventory exposes aggregate lifecycle, attempt, readiness, and
backoff counts for `candidate_apply_execute`. It never publishes deployment IDs,
commit identities, paths, credentials, file content, or nested error text.

The execution lane does not bypass backup, repository freshness, Stage integrity,
ordered journaling, reconciliation, activation, observation, promotion, or rollback
safeguards. It schedules restart work but does not itself call the Home Assistant
or Supervisor restart API.
