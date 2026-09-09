# Durable runtime publication work

This increment is derived only from the initial V2 `README.md` at root commit
`71d284ce447d79b044e332c9bc01ae801dc91947`, especially the `runtime` branch and
Retrigger/recovery requirements.

The runtime work adapter makes a generated runtime publication recoverable through the existing
durable work state machine without changing the guarded publication transaction itself.

## Work identity and isolation

A `runtime` work item is scoped to one explicit Repo B target and the fixed `runtime` lane. Repeated
enqueue requests for the same target are idempotent while eligible work already exists. Runtime
claiming uses `StateStore.claim_work_kind("runtime")`, so it cannot consume candidate, database,
local-sync or log work.

Execution accepts an already-collected `RuntimeInventoryInput` plus explicit isolated staging and
Git workspace roots, the trusted Repo B target, and its credential. It delegates publication to
`synchronize_runtime_inventory`; Git remains confined to the existing isolated workspace boundary.

## Durable outcome mapping

- `initialized`, `published`, and `no_change` complete the durable item;
- `baseline_required`, `diverged`, and `remote_missing` block it permanently because retrying the
  same evidence must not overwrite an unexpected remote state;
- a guarded runtime synchronization exception enters the existing bounded retry/backoff state
  machine and is returned without the raw exception or credential.

The adapter validates the claimed work kind, status, attempt evidence, target identity, and runtime
inventory type before publication can run.

## Deliberately not included

This slice does not collect Home Assistant APIs, schedule periodic work, consume `candidate`,
mutate Home Assistant, create deployment backups, restart Home Assistant, observe a deployment, or
perform rollback. It also does not add the runtime lane to the bounded Retrigger coordinator yet.

The next incremental step may combine the already-merged read-only Core collector with this durable
adapter in a single bounded runtime Retrigger pass. That pass must remain kind-scoped and must be
green before it can be added to the broader Retrigger cycle.
