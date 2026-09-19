# Post-Apply Core activation authorization

`authorize_post_apply_activation()` is the fail-closed boundary between completed
path-level Apply and a future Home Assistant Core restart transport. It does not call
the Supervisor API. It only records that one exact non-empty deployment is eligible
for the separately implemented activation step.

## Complete evidence requirement

The gate re-verifies the producer-issued Apply authorization, Stage pre-write proof,
isolated Candidate Stage, deterministic plan, live preconditions, durable Apply intent,
and bounded progress journal. The Stage is integrity-checked again immediately before
persistence. Every operation must be present, contiguous, plan-bound, and
`mutation_verified`; the recovery classifier must independently report `complete`.

Missing progress, `mutation_started`, `blocked`, `not_applied`, `ambiguous`, corrupt
records, repository drift, plan changes, Stage changes, and mismatched evidence all fail
closed with sanitized diagnostics. An empty plan returns `no_activation_required` and
never creates restart authority.

## Durable authorization

Schema version 11 stores a content-free authorization bound to the deployment ID,
private repository identity, baseline and Candidate SHAs, Stage manifest, exact backup,
durable Apply-intent digest, canonical operations digest, operation count, and the fixed
action `restart_core`. The write is transactional and idempotent. An exact replay returns
the existing record; any attempt to rebind the deployment fails closed.

The authorization class cannot be constructed from caller-supplied scalar fields. A
future restart transport must load and revalidate the durable record and must still own
its own bounded Supervisor call, journal-before-mutation semantics, crash reconciliation,
locking, retry classification, and runtime reporting.

## Authority boundary

This capability performs no Home Assistant API call, filesystem mutation, Git mutation,
reload, restart, health observation, promotion, tagging, rejection, backup restore, or
rollback. Authorization is not deployment success. Retrigger may reconsider this gate
only with the complete freshly proven chain and must not convert incomplete, uncertain,
or blocked Apply evidence into activation authority.
