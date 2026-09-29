# Repo B initialization authority

SyncApp now has an explicit, durable authorization boundary for initializing a
private Repo B. This boundary records policy authority only. It performs no Git,
GitHub, Home Assistant, Supervisor, network, filesystem-publication, deployment,
promotion or rollback mutation.

## Exact request and proof

`authorize_repo_b_initialization()` accepts one canonical UUIDv4 request bound to
an exact repository target and positive GitHub repository ID. A previously unseen
request also requires:

- the exact pinned repository binding;
- a deploy-key access proof and canonical reference snapshot bound to the same
  target, repository ID, public-key fingerprint and generation UUID;
- matching reference count and observation digest;
- a canonical observation no more than five minutes old and no more than thirty
  seconds ahead of the authorization clock.

The snapshot is reconstructed as canonical `git ls-remote` evidence before its
digest is accepted. An empty repository therefore requires the exact SHA-256 of
an empty response. Malformed, duplicate, unsorted, rebound or stale evidence
fails closed before the journal changes.

## Atomic authorization and deterministic blocks

Schema v36 stores the request, non-secret key-generation binding, content-free
observation digest, lifecycle state and integrity digest in one transaction. The
transaction rechecks repository identity, existing synchronization baselines and
active initialization authority under `BEGIN IMMEDIATE`.

Only a repository with zero observed refs and no synchronization baseline becomes
`authorized`. The following deterministic conditions become terminal `blocked`
receipts and are never returned by authorized-work discovery:

- `repository_not_empty`;
- `already_initialized`;
- `active_request` for a competing authority on the same target.

A partial unique index permits at most one active authorization per target. An
exact request replay returns the integrity-checked journal record without remote
evidence, credentials, network access or mutation. Reusing the request UUID for
another repository, key generation or observation fails closed.

## Recovery and runtime visibility

`discover_authorized_repo_b_initializations()` returns at most sixteen
integrity-checked authorized records in deterministic order. This is the bounded
handoff for a later idempotent initialization executor and Retrigger lane; this
increment does not execute or schedule that work.

Runtime inventory exposes only aggregate initialization phase counts, fixed block
reason counts and the latest recorded timestamp. The runtime query never selects
request UUIDs, repository targets or IDs, key fingerprints, generation UUIDs,
observation digests or record digests. Malformed, oversized or future-dated
runtime evidence fails closed.

The current production token-backed synchronization and promotion selection is
unchanged. A later executor and transport-adoption task must consume this authority
without bypassing validation, backup, Apply observation, promotion, rollback,
locking, idempotency or controlled retry/backoff safeguards. Missing authority
must never cause implicit initialization or silent transport fallback.
