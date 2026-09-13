# Logs branch history retention

This behavior is derived only from the initial V2 root `README.md` at commit `71d284ce447d79b044e332c9bc01ae801dc91947`.

The README requires the generated Repo B `logs` branch to retain operational and diagnostic information for 30 days. Removing expired files from the current Git tree does not remove those bytes from older commits, so generated log history must not be allowed to grow without bound. Rewriting generated history is retention management; it is **not forensic secure deletion** and must never be represented as a guarantee that historical bytes are unrecoverable from GitHub, caches, replicas, backups, clones, or other storage.

## Retention policy and evidence

`ha_syncapp.log_history_retention` computes the fixed 30-day policy from explicit newest-first trusted commit evidence and an explicit timezone-aware UTC reference time. It accepts only the exact `logs` branch, retains the current head, rejects malformed/duplicate/future/out-of-order history, and produces a deterministic retained/pruned partition without hidden wall-clock state.

Before mutation, the retention path re-proves the pinned private Repo B identity, the exact `logs` head, the complete linear history and the authorized retention partition. A stale or divergent head fails closed. No-op retention completes without creating publication credentials or changing a ref.

## Replacement boundary

When pruning is required, SyncApp acquires the exact authorized `logs` history into disposable isolated staging and rebuilds only the retained commits. Commit payload and metadata are preserved except for the minimum parent rewrite required to sever pruned ancestry. The builder returns a sealed artifact bound to the staging repository, private repository identity, branch, expected head and retained history. Publication re-validates the rebuilt payload/ancestry; callers cannot authorize an arbitrary replacement SHA.

Mutation is confined to `refs/heads/logs` and uses an exact expected-head force-with-lease. `main`, `candidate`, `runtime` and `database` are outside this authority boundary. Private Git authentication is ephemeral: disposable askpass/token files are used only for the authenticated fetch/publication windows, credentials are excluded from Git argv/URLs and persisted work state, and temporary credentials/staging are removed on success and failure paths.

## Durable recovery

Logs retention uses the `logs_retention` StateStore work kind. Its deterministic identity is bound to the pinned private repository, exact original `logs` head and retention outcome. StateStore remains the locking and lifecycle authority, so duplicate discovery is idempotent, only one eligible item is claimed, interrupted running work can be recovered, and transient failures use the existing bounded retry/backoff model. Invalid evidence, malformed replacement history, stale heads and rejected updates are blocked rather than continuously retriggered.

Before publishing a replacement, SyncApp persists an immutable, non-secret compare-and-swap intent containing the expected and intended replacement heads plus the synchronization snapshot binding. After a restart, recovery distinguishes three cases:

- remote `logs` is already at the intended replacement: reconcile the synchronization baseline and complete without publishing again;
- remote `logs` is still at the original expected head: publication may safely resume through the normal authorization path;
- remote `logs` is at any other head: mark the work stale/blocked and do not rewrite it.

Normal periodic log service runs retention after collection/publication with interrupted-work recovery disabled for that routine lane. The recurring Retrigger cycle runs logs synchronization recovery, then logs-retention recovery with interrupted-work recovery enabled, before fresh log collection. This does not grant either path any candidate-deployment authority.

## Runtime evidence and safety boundary

Retention executions use the same durable work ledger exposed through bounded `analysis/recovery.json` runtime evidence. The runtime view includes validated work kind, lifecycle status, attempt count and lifecycle timestamps, but deliberately excludes work keys. Repository targets, commit identities, staging paths, credentials and exception text are not published through that evidence interface.

Logs remain strictly Home Assistant -> GitHub generated data; retention never restores logs into Home Assistant. Candidate Fetch/Stage, dependency/risk analysis, validation, backup, Apply, reload/restart, deployment observation, promotion/tagging and rollback remain separate and unchanged. The recurring Retrigger Work mechanism remains enabled and is part of the recovery design rather than a bypass around these safeguards.
