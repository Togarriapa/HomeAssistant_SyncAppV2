# Interrupted live Apply reconciliation

Live Apply reconciliation is a read-only recovery boundary for an operation whose durable
progress stopped at `mutation_started`. It does not grant filesystem mutation, retry,
reload, restart, promotion, backup restoration, or rollback authority.

## Durable evidence

Immediately before a live mutation, the writer records a content-free mutation guard bound
to the exact durable intent, ordered operation, verified Home Assistant root identity, and
every existing parent-directory identity. The guard contains device/inode numbers and hashes,
but no candidate bytes, clear-text operation paths, credentials, or Home Assistant secrets.

If guard persistence fails, the writer blocks the operation before touching the target.
Schema version 10 adds the guard and reconciliation tables through a transactional migration.

## Reconciliation outcomes

The reconciler consumes the complete producer-issued authorization, verified Stage, plan,
precondition, intent, progress, and mutation-guard chain. It opens the guarded root and parents
without following symlinks and compares the live leaf's Git object identity and mode with the
operation contract.

- `applied`: the exact candidate state is live. Progress advances to `mutation_verified`.
- `not_applied`: the exact trusted baseline state remains. The explicit outcome is persisted
  and progress becomes terminal `blocked`; a later policy must explicitly authorize any retry.
- `ambiguous`: identities changed, the leaf is unsafe, or neither exact state matches. The
  explicit outcome is persisted and progress becomes terminal `blocked`.

Exact outcome replay is idempotent. Conflicting outcomes, changed evidence, corrupt durable
records, root/parent replacement, and regressive progress transitions fail closed with
sanitized diagnostics.

## Retrigger boundary

Retrigger discovery may identify `mutation_started` as `reconcile_uncertain` and call this
read-only proof. It must never translate uncertainty or `not_applied` into blind writer
authority. The recurring Retrigger Work schedule remains enabled and unchanged.
