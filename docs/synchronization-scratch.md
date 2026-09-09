# Protected synchronization scratch storage

The initial V2 README requires snapshots and all Git operations to occur in
isolated staging rather than in the live Home Assistant configuration directory.
V2 therefore reserves two transient roots below its protected app-owned state:

- `/data/syncapp/snapshots`
- `/data/syncapp/workspaces`

Both are owner-only directories. The preparation path opens the app-state root
and scratch roots with no-follow directory descriptors, validates ownership and
type, and normalizes the scratch permissions to `0700`. A symlink or ownership
mismatch fails closed.

Stale cleanup is deliberately narrow. It traverses only the two dedicated roots
and removes only recognized transient directories ending in `.tmp` with the
snapshot/workspace prefixes used by V2. It does not delete the scratch roots,
`state.sqlite3`, `instance.lock`, credentials, or unrelated state. Cleanup walks
with directory descriptors, refuses symlinks and special files, and checks
ownership before deletion.

These roots are only storage boundaries. This increment does not schedule a
synchronization, execute Git, write to `/homeassistant`, process a remote
`candidate`, create a Home Assistant backup, restart Home Assistant, observe a
deployment, or roll back. Those behaviors remain separately gated by the initial
V2 README's safety model.
