# Candidate Apply admission recovery

Task #353 connects a completed prepared deployment to the existing durable live
Apply intent boundary without executing a live Home Assistant mutation.

## Durable work creation

Successful candidate backup completion persists all of the following atomically:

- exact candidate backup evidence;
- the immutable prepared deployment;
- terminal candidate orchestration state; and
- one `candidate_apply` work item keyed by the deployment ID.

Exact replay is idempotent. Conflicting deployment or work identity fails closed.
The backup worker remains terminal and never chains directly into Apply admission.

## Bounded admission

The admission executor requires the exact claimed work item and loads the completed
prepared-deployment authority from protected state. Before recording any intent it
freshly:

1. re-proves the bound Supervisor backup;
2. re-proves the private Repo B identity and exact `main` and `candidate` heads;
3. re-authorizes Apply from the exact prepared deployment;
4. re-verifies the isolated Stage;
5. rebuilds the canonical ordered Apply plan; and
6. verifies every affected live path against its trusted baseline.

Only the resulting immutable intent and successful work completion are committed,
in one StateStore transaction. The live Apply writer is not called.

## Recovery and failure policy

The Retrigger lane runs after all validation and backup lanes and processes at most
one eligible admission per cycle. It returns stale `running` work to `retry` while
holding the StateStore lock, then claims the earliest eligible item deterministically.

Network transport failures and HTTP 408, 429 and 5xx proof failures use the durable
bounded retry/backoff policy. Invalid configuration or credentials, malformed or
tampered evidence, stale repository heads, Stage drift and unsafe live-path state are
deterministic failures and are blocked. The exact blocked work is not automatically
retried unchanged.

Generated runtime inventory exposes the `candidate_apply` lifecycle, attempts,
readiness and backoff as aggregate counts. It omits the deployment ID, candidate SHA,
paths, credentials, response bodies and nested error text.

## Safety boundary

Admission is authority persistence, not mutation. It performs no live configuration
write, delete, rename or permission change; no reload or restart; no backup restore;
and no promotion, observation acceptance or rollback. The downstream live writer
must still validate the intent and its fresh producer evidence immediately before
each guarded operation.
