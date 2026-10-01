# Deploy-key retention history adoption

Generated `logs` and Recorder `database` history rewrites can use the repository-
scoped deploy key independently of candidate ingress, ordinary publication,
promotion, and rollback. Set:

```yaml
repo_b_retention_transport: deploy_key
```

The default remains `token`. Activation fails closed unless Repo B has a pinned
private numeric identity, a durable initialized `main` baseline, an enrolled
protected private key, and a fresh access proof for that same repository and key
generation. The GitHub token remains necessary at startup to verify private Repo B
identity because deploy keys do not authenticate GitHub REST; it is not passed to
retention Git commands after deploy-key authority selection.

## History evidence and staging

The immutable authority binds the access proof, repository identity, key
fingerprint and generation, pinned GitHub host key, protected key directory, and
private `0700` work roots. Each retention pass obtains the exact `logs` or
`database` branch head through bounded `ls-remote`, fetches that advertised ref
through a private-key descriptor, and reads commit timestamps and parent links from
the isolated repository.

Evidence is bounded by the existing per-branch limits and must be a complete,
newest-first linear history ending at a root commit. Malformed identities, merge
history, broken parent links, future or out-of-order timestamps, oversized history,
repository/key rebinding, and a head that moves during acquisition fail closed.
Private key bytes never enter a URL, environment value, durable work identity,
artifact, log, or exception.

## Replacement boundary

The existing deterministic retention planners, immutable replacement
authorizations, sealed builder artifacts, durable work identities, locks, retry
budgets, and replacement intents remain authoritative. A no-op plan creates no
publication session and changes no ref.

When pruning is required, SyncApp rebuilds only the authorized retained commits in
isolated staging. Immediately before mutation it re-observes the exact branch head.
The only writable ref is the authorized generated branch and the push contains an
exact `--force-with-lease=<ref>:<expected-head>`. After publication, SyncApp
re-observes the replacement head before updating the synchronization baseline and
completing durable work. A stale or rejected write blocks rather than looping.
Temporary SSH, DNS, timeout, or process failures enter the existing bounded
retry/backoff path.

## Recovery and safety

Normal database/log services and the Retrigger recovery cycle receive the same
immutable authority instance. Interrupted replacement reconciliation first checks
the durable intent and exact remote head: an already-published replacement updates
the matching baseline and completes idempotently; unrelated head movement is
blocked as stale. Token and deploy-key authorities cannot be supplied together,
and deploy-key mode never falls back to token Git transport.

Retention rewrites only generated Repo B history. They do not restore logs or
Recorder data into Home Assistant and grant no candidate validation, backup,
Apply, reload/restart, deployment observation, promotion, rollback, or Supervisor
authority. History retention is not forensic secure deletion: old bytes may remain
in GitHub caches, replicas, backups, or prior clones. The recurring Retrigger work
mechanism remains enabled and unchanged.
