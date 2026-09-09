# Guarded runtime publication

The initial V2 README defines Repo B's `runtime` branch as generated Home Assistant -> GitHub evidence. It is never a deployment source.

`synchronize_runtime_inventory()` publishes one explicit `RuntimeInventoryInput` through the same isolated trust and publication boundaries used by the existing V2 outbound lanes. Each cycle creates a unique temporary runtime staging root, builds and reverifies the deterministic runtime artifact, converts only that verified artifact into the existing immutable snapshot boundary, and materializes Git in a separate workspace.

The Repo B repository identity must already be pinned. A pre-existing `runtime` branch without a durable synchronization baseline is blocked rather than adopted implicitly. Once a baseline exists, a missing or remotely diverged branch is also blocked. An absent branch may be initialized only when no baseline exists. Authorized writes use the existing ordinary non-force push, post-push branch verification, and durable baseline completion primitives.

Successful cycles report whether the branch was initialized, published, or unchanged together with runtime artifact, snapshot, commit, and baseline evidence. Runtime artifact, generic snapshot, and Git workspace staging are removed after the cycle regardless of success or failure.

This milestone does not collect Home Assistant or Supervisor APIs, schedule runtime work, process the `candidate` branch, deploy runtime data, restart Home Assistant, or mutate the live Home Assistant configuration tree.
