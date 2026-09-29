# Deploy-key candidate ingress adoption

SyncApp can explicitly select the verified Repo B deploy key for production
candidate observation and Fetch/Stage. This is a candidate-only adoption boundary:
Local, database, runtime and log publication, deployment promotion, rollback, and
GitHub REST repository-identity verification retain their existing transports.

## Explicit selection and prerequisites

Set `repo_b_candidate_transport` to `deploy_key`. The default remains `token` so
existing installations do not change authentication implicitly. Deploy-key mode
requires all of the following before the service starts candidate intake:

- configured `repo_b` and `github_token` values;
- the previously pinned numeric Repo B identity;
- an existing durable `main` synchronization baseline, proving that explicit
  initialization or an earlier successful publication completed;
- a complete protected key generation at
  `/data/syncapp/repo-b-deploy-key`; and
- a fresh read-only access proof for that repository, fingerprint, and generation.

The GitHub token is retained only for private/numeric repository identity
verification and the production paths not adopted by this increment. It is never
passed to deploy-key candidate reference reads or candidate fetch commands.
Missing initialization, enrollment, proof, or key state fails service activation.
There is no automatic token fallback.

## Candidate observation and Fetch/Stage

The runtime creates one immutable candidate-ingress authority containing only the
content-free access proof and protected filesystem boundaries. Periodic detection
uses the existing bounded, host-pinned SSH reference reader and accepts zero or one
exact `refs/heads/candidate` record. The Retrigger Fetch/Stage lane uses the same
authority and the existing descriptor-only SSH fetch primitive.

Each operation re-inspects the protected key and rejects fingerprint or generation
drift. Candidate SHA, repository identity, branch, journal-before-network
checkpoint, private workspaces, staging integrity, cleanup, transient retry/backoff,
and deterministic blocking remain unchanged. A completed Fetch/Stage replay remains
offline and does not require either credential.

The private key travels only through an inherited descriptor. It is not placed in
a URL, command argument, environment variable, log, exception, work identity,
database row, runtime artifact, or candidate output. The protected key and access
workspace paths are also excluded from the authority's representation.

## Recovery and rollback

Temporary Git/SSH availability failures remain retryable through the existing
bounded work scheduler. Invalid authority, changed key generation, rebound
repository/reference evidence, or candidate mismatch blocks the exact work rather
than switching transport.

To return candidate ingress to the legacy path, explicitly set
`repo_b_candidate_transport` to `token` and restart. Do not delete the protected
key as a transport-selection mechanism. Key rotation continues through its
journaled workflow; a running ingress rejects a generation change until a restart
creates a fresh proof.

This option grants no initialization, publication, promotion, rollback, Apply, or
Home Assistant mutation authority.
