# Deploy-key rollback repository-proof adoption

Rollback safety can use the repository-scoped deploy key independently of
candidate ingress, ordinary publication, promotion, and retention. Set:

```yaml
repo_b_rollback_transport: deploy_key
```

The default remains `token`. SyncApp fails closed during authority activation
unless Repo B has a pinned private numeric identity, a durable initialized `main`
baseline, an enrolled protected private key, and a fresh successful access proof
for the same repository and key generation. The GitHub token remains required for
startup REST identity verification, but it is not passed to rollback Git commands
in deploy-key mode.

## Authority boundary

The immutable authority binds the exact access proof generation, repository
identity, protected key directory, and isolated private read workspace. Each read
revalidates that key generation and obtains a canonical reference snapshot through
the pinned GitHub SSH host key. Rollback accepts exactly one valid
`refs/heads/main` value and rejects missing, duplicate, reordered, rebound, or
malformed evidence.

The same authority instance is used for:

- initial failed-candidate rollback authorization;
- planned restore preflight re-proof;
- post-restore observation completion;
- stale-work recovery and controlled retry.

Token and deploy-key repository authorities cannot be mixed, and deploy-key mode
has no token fallback. Once rollback authorization or completion is durable, its
existing replay path remains network-free and does not require either credential.

## Home Assistant boundary

This option grants no Home Assistant authority. Exact backup proof, restore
execution, restore-job reconciliation, and Core/Supervisor health probes continue
to use the Supervisor token and retain their journal-before-mutation, observation,
and ambiguity safeguards. The deploy-key rollback authority performs no Git write,
force push, history rewrite, backup restore, or deployment mutation.

Retention history rewrites remain independently token-backed. A later retention
transport increment must preserve its destructive-operation safeguards and is not
enabled by this option.

## Failures and recovery

Invalid configuration, repository/key rebinding, malformed reference evidence,
and baseline divergence are deterministic failures and block the affected durable
work. SSH, DNS, GitHub, timeout, and other transport unavailability is classified
as transient and follows the existing bounded backoff. Errors and authority
representations expose neither token values nor private-key paths or bytes.

To return only rollback repository proof to the compatibility path, set
`repo_b_rollback_transport: token` and restart. Do not delete or rotate protected
key material as part of transport rollback; key lifecycle changes use the separate
journaled rotation procedure.
