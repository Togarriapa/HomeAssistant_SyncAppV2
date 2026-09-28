# Bounded Retrigger cycle

The initial V2 README requires interrupted and retryable work to be recoverable by a periodic Retrigger mechanism. `run_retrigger_cycle()` is the scheduler-neutral execution primitive for the guarded work lanes that currently exist in V2.

One cycle runs, in deterministic order:

1. one bounded Local -> Repo B `main` recovery pass;
2. one bounded Recorder -> Repo B `database` recovery pass;
3. optional Recorder retention recovery;
4. one bounded generated runtime -> Repo B `runtime` recovery pass;
5. optional logs synchronization and retention recovery;
6. one bounded deployment-rollback recovery pass;
7. one bounded candidate Fetch/Stage recovery pass;
8. if Fetch/Stage performed no action, one bounded candidate integrity-analysis pass;
9. if earlier candidate lanes performed no action, one bounded dependency-analysis pass;
10. under the same condition, one bounded risk-classification pass;
11. under the same condition, one bounded static-validation pass;
12. under the same condition, one bounded semantic-validation pass;
13. under the same condition, one bounded candidate backup/reconciliation pass;
14. if every earlier candidate lane performed no action, one bounded candidate Apply
    admission pass;
15. if admission performed no action, one bounded admitted candidate Apply execution
    or reconciliation pass;
16. if every earlier candidate lane performed no action, one bounded authorized Core
    restart execution or reconciliation pass;
17. if every earlier candidate lane performed no action, one bounded post-restart Core
    health-window action;
18. if every earlier candidate lane performed no action, one bounded Supervisor-health
    observation;
19. trusted candidate detection;
20. optional fresh Supervisor log collection.

Each lane preserves its own durable success, deterministic block, and bounded transient retry semantics. The cycle requires the live Home Assistant root, Recorder database path, isolated staging/workspace roots for every lane, Repo B target, GitHub credential, and optional Home Assistant Core API credential as explicit inputs. It does not discover paths or credentials.

The GitHub credential is used by Repo B publication and exact rollback-baseline proof. The separate Supervisor credential is forwarded only to guarded runtime/log collection and rollback boundaries. The rollback pass performs no credential or network access when no rollback is pending. When work exists, it accepts only the persisted exact backup and executes at most one restore, reconciliation or observation action.

The candidate Fetch/Stage pass accepts only exact `detected/fetch_stage` authority,
executes at most one action, and runs before fresh detection. Successful staging
enables `analyze` for a later cycle; it never chains into another candidate action.
The analysis pass accepts only exact `staged/analyze` authority, re-fetches the
same candidate into an isolated transient workspace, checkpoints canonical
change/integrity evidence, and enables `analyze_dependencies` for a later cycle.
Completed analysis replay is credential- and network-free. The conditional lane
ordering enforces at most one candidate action per cycle. The backup pass accepts
only exact `semantically_validated/prepare_backup` authority. It journals before
mutation, reconciles interrupted/uncertain requests by exact persisted request
identity, never creates a blind replacement backup, and cannot authorize Apply.

The candidate Apply admission pass accepts only one eligible `candidate_apply` work
item produced atomically with the completed prepared deployment. It recovers stale
running admission work, applies the work ledger's bounded retry/backoff rules, and
re-proves the exact Supervisor backup, Repo B `main` and `candidate` heads, Apply
authorization, isolated Stage, canonical Apply plan and affected live-path
preconditions. Only then does it atomically persist the immutable live Apply intent
and complete the work item. Deterministic drift is blocked; transport and bounded
retryable HTTP failures remain retryable. This pass never invokes the live Apply
writer and never mutates Home Assistant configuration.

The following execution pass accepts only the atomically created
`candidate_apply_execute` successor. It freshly re-proves backup, repository, Apply,
Stage and plan authority, then delegates exactly once to the recovery-aware Apply
controller. A cycle can perform at most one live operation or one read-only
reconciliation. Partial success is deferred as normal pending work; deterministic
blocks are not retried unchanged. Completion persists activation authority before
any restart successor is enqueued. The lane never calls a restart API itself.

The candidate restart pass accepts only the exact `candidate_restart` successor and
the integrity-protected activation authorization persisted by completed Apply. It
recovers interrupted ledger claims, processes at most one restart item, and delegates
the mutation exclusively to the journal-before-POST restart transport. A fresh exact
Supervisor acknowledgement atomically completes restart work and creates the pending
`candidate_observe` successor. An already acknowledged restart replays to the same
transition without credentials or network access.

A journal left at `request_started` is an uncertain mutation outcome, not transient
retry authority. Retrigger blocks that exact restart item without issuing another
request; malformed, tampered, or rebound authority is blocked in the same fail-closed
way. No observation work is created from a blocked outcome. This keeps the one-action
candidate ordering intact and prevents automatic backoff from repeating a Core
restart whose result is unknown.

The Core observation pass accepts only the exact `candidate_observe` successor created
after acknowledged restart. It re-proves activation and restart binding before every
action, records one initial exact Core API proof, persists the configured deadline, and
returns the work to the ledger without sleeping. The deadline is used as
`next_attempt_at`, so pre-deadline cycles neither claim the item nor resolve a credential.
At or after the deadline, one fresh exact Core proof is required. Completion atomically
creates `candidate_observe_supervisor` before the Core observation work succeeds.
Transport unavailability receives bounded retry/backoff; corrupt, rebound, or malformed
authority is blocked unchanged.

The Supervisor observation pass accepts only `candidate_observe_supervisor` work
created atomically after the completed Core window. It re-proves activation, restart,
and Core-window authority, performs at most one bounded authenticated read, and accepts
only exact `healthy: true` plus `supported: true` evidence. Success atomically creates
`candidate_observe_integrations` before completing Supervisor work. Completed evidence
replays without credentials or network access. Transport/unhealthy results back off;
invalid credentials and corrupt or rebound evidence are blocked.

The integration observation pass accepts only `candidate_observe_integrations` work
created atomically after the exact Supervisor proof. It re-proves the complete
activation, restart, Core-window, and Supervisor chain before opening one bounded
authenticated Core WebSocket session. Every enabled config entry must report exact
state `loaded`; explicitly disabled entries are counted but excluded. Success atomically
creates `candidate_observe_startup_errors` before integration work completes. Completed
evidence replays without credentials or network access. Session or initialization
unavailability receives bounded backoff; invalid inputs and corrupt authority are
blocked.

If an earlier lane cannot complete its bounded pass safely, the cycle fails closed before starting later lanes. A Local-sync failure prevents both database and runtime work. A database failure prevents runtime work. Returned errors are sanitized rather than forwarding nested exception text or credentials.

## Cron-invocable same-owner trigger

The service owns `StateStore` for its entire process lifetime and keeps its exclusive filesystem lock. A cron process must therefore **not** open the SQLite state independently or bypass that lock. Instead, the running service exposes a Unix-domain socket at `/data/syncapp/retrigger.sock` (relative to the configured data root). The socket lives inside the existing owner-only `0700` state directory and is itself forced to `0600`.

A cron invocation uses the explicit one-shot CLI mode:

```text
python -m ha_syncapp --retrigger-once \
  --home-assistant-root /homeassistant \
  --recorder-database /homeassistant/home-assistant_v2.db
```

The one-shot client sends only a bounded protocol version, command name, Home Assistant root and Recorder database path. It does **not** send Repo B, the GitHub credential or `SUPERVISOR_TOKEN`. The state-owning service loads and retains those credentials, re-verifies the durably bound private Repo B identity immediately before each requested cycle, and forwards `SUPERVISOR_TOKEN` only to the runtime lane.

All staging, snapshot and Git workspace roots are derived beneath the protected SyncApp data tree and are rejected if that tree overlaps the supplied Home Assistant source tree. The IPC protocol uses bounded newline-delimited JSON, rejects duplicate keys, control-character/relative paths and oversized messages, and returns only a fixed sanitized completion/failure shape.

This is the **cron-invocable primitive**, not the cron scheduler or cadence. A later packaging increment can install the periodic scheduler only after this one-shot boundary is verified on HAOS.

Candidate detection, Fetch/Stage and logs handling use their own guarded primitives;
generic `logs` work is not claimed by another lane and candidate work is claimed only
for its persisted next action. The cycle never restores Recorder data or runs Git in
the live Home Assistant tree. A Supervisor backup restore is reachable only from the
exact immutable failed-deployment authority and journal-before-mutation rollback
state machine documented in `deployment-rollback.md`.
