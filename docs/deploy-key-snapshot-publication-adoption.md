# Deploy-key ordinary snapshot publication adoption

SyncApp can explicitly select its verified Repo B deploy key for routine,
non-force snapshot publication. The adopted lanes are Local configuration,
Recorder database snapshots, runtime inventory, and collected log artifacts.

## Selection and prerequisites

Set `repo_b_publication_transport` to `deploy_key`. The default remains `token`,
so an existing installation does not change authentication implicitly. Before
starting publication services, deploy-key mode requires:

- configured `repo_b` and `github_token` options;
- the pinned numeric Repo B identity;
- an existing durable `main` synchronization baseline, proving explicit Repo B
  initialization completed;
- a complete protected key generation at
  `/data/syncapp/repo-b-deploy-key`; and
- a fresh, read-only access proof bound to that repository identity, key
  fingerprint, and generation.

The GitHub token remains necessary for private/numeric repository identity
verification and for production paths not adopted by this increment. It is not
passed to deploy-key branch observation, baseline fetch, or non-force snapshot
pushes. Missing initialization, enrollment, proof, or protected key state fails
service activation. There is no automatic token fallback.

## Publication boundary

One immutable authority observes exact branch refs with host-pinned SSH, anchors
an existing baseline commit into an isolated workspace through an inherited
private-key descriptor, and publishes the already-authorized immutable
`PublicationIntent` with a non-force refspec. Fresh exact ref evidence is checked
before and after the push. The resulting baseline is persisted only after the
published commit is confirmed.

Repository identity pinning, branch routing, snapshot identity, baseline
comparison, workspace containment, Git configuration restrictions, locking,
bounded retry/backoff, and deterministic divergence blocking are unchanged.
Private key bytes and protected paths do not enter Git URLs, arguments,
environment variables, logs, exceptions, work identities, database rows, or
runtime artifacts.

## Deliberately unchanged paths

Candidate ingress retains its independent `repo_b_candidate_transport` option.
History-retention rewrites, candidate promotion, candidate rollback, deployment
rollback, and GitHub REST repository identity verification retain their existing
transports. Retention rewrites use exact lease/destructive semantics and require a
separate reviewed deploy-key adapter; they are not silently routed through the
ordinary non-force writer.

To roll back this adoption, explicitly set
`repo_b_publication_transport: token` and restart. Do not delete the protected key
as a transport-selection mechanism. Key rotation remains journaled; a running
authority rejects fingerprint or generation drift until restart obtains a fresh
proof.

No option in this increment grants Apply, promotion, rollback, initialization, or
live Home Assistant mutation authority.
