# Deploy-key candidate promotion adoption

Candidate promotion can use the repository-scoped deploy key independently of
candidate ingress and ordinary snapshot publication. Set:

```yaml
repo_b_promotion_transport: deploy_key
```

The default remains `token`. SyncApp fails closed during authority activation
unless Repo B has a pinned private repository identity, a durable initialized
`main` baseline, an enrolled protected private key, and a fresh successful access
proof for the same repository and key generation. The GitHub token remains
required for REST repository identity verification; it is not passed to promotion
Git commands in deploy-key mode.

## Promotion boundary

The authority is immutable and binds the access proof, repository identity,
protected key directory, private access workspace, private promotion workspace,
and `/homeassistant` root. It delegates to the previously reviewed promotion
transport, which:

- observes the exact `candidate`, `main`, and known-good tag references;
- fetches and verifies the exact candidate commit through the deploy key;
- publishes only missing `main` and tag references in one atomic, non-force push;
- re-observes the references before and after publication;
- removes its temporary candidate workspace after every outcome.

Normal Retrigger execution, stale-running recovery, controlled retry, and
post-interruption reconciliation receive the same authority instance. A completed
durable promotion still replays without network access. Token and deploy-key
promotion credentials cannot be mixed, and deploy-key mode has no token fallback.

## Failure and non-goals

Reference divergence, invalid authority/evidence, configuration faults, and
candidate validation failures are deterministic and block the durable promotion
work. Network, GitHub, or bounded Git transport unavailability remains transient
and follows the existing backoff policy. Error messages and authority
representations omit token and protected path material.

This option does not change candidate rollback or recovery authorization,
retention history rewrites, candidate ingress, ordinary snapshot publication, or
REST identity verification. Those lanes keep their independently selected
transports and safeguards.

## Verification

The adoption contract covers proof-bound read/publish delegation, secret-safe
failure classification, production authority construction only after durable
initialization, tokenless promotion execution, stale-work forwarding, and an
orchestration boundary proving rollback still receives its existing GitHub and
Home Assistant credentials.
