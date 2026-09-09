# Bounded runtime Retrigger pass

This increment is derived only from the initial V2 `README.md` at root commit
`71d284ce447d79b044e332c9bc01ae801dc91947`, especially its generated `runtime` branch and
Retrigger/recovery requirements.

One pass is scheduler-neutral and deliberately bounded. It recovers interrupted durable work,
claims at most one `runtime` item through the kind-scoped state API, and does nothing further when
no runtime work is eligible.

## Execution order

For a valid claimed runtime item the pass performs these steps in order:

1. Verify the durable work key still belongs to the explicit Repo B target.
2. Collect the already-approved read-only Home Assistant Core datasets (`config`, `states`, and
   `services`) through the fixed Supervisor Core API proxy.
3. Pass the resulting `RuntimeInventoryInput` to the durable runtime publication adapter.
4. Let that adapter run the existing isolated runtime artifact/snapshot/Git publication transaction
   and persist success, deterministic block, or bounded retry state.

Home Assistant collection occurs only after a valid runtime work claim. An idle pass therefore does
not call Home Assistant APIs. A target mismatch is permanently blocked before collection or Repo B
publication.

The Home Assistant Core credential and GitHub credential are separate inputs. A Core collection
failure is converted into the existing transient retry/backoff state, and neither credential nor the
raw collector exception is returned in result evidence.

## Safety boundary

This pass processes no more than one runtime item and cannot consume candidate, database,
local-sync, or log work. It does not issue Home Assistant write requests, process `candidate`, create
deployment backups, restart Home Assistant, observe a deployment, or roll back a deployment. Git
remains confined to the existing isolated runtime publication workspace.

This slice is intentionally not wired into the broader Retrigger coordinator. The next increment may
add the runtime lane to that bounded coordinator after this pass is independently green, while
preserving deterministic lane ordering and failure isolation.
