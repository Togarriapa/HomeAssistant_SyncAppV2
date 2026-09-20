# Deployment finalization

Deployment finalization is the immutable decision boundary between observation
and a later promotion or rollback operation. It computes authority only: it
does not tag, promote, roll back, call Home Assistant or Supervisor, or mutate
live configuration.

## Ordered decision contract

A successful outcome requires the exact prepared deployment and every ordered
observation predicate through post-deployment assertions to be complete,
integrity-valid, temporally ordered, and successful. The resulting authority is
`promote_and_tag`.

A deterministic failure is finalized at the first failed predicate that has a
complete successful prerequisite chain. Supported terminal stages are startup
errors, affected-entity state validation, automation/script loading, and
post-deployment assertions. Evidence for predicates after that failure must be
absent. The resulting authority is `rollback`, and the exact candidate SHA is
queryably blocked. An incomplete or transiently failed chain produces no
record and grants `none` authority, so normal bounded retry/backoff remains in
control.

## Persistence and replay

Schema v22 stores one finalization row per deployment. The row binds the
candidate SHA, verified backup slug, prepared-deployment digest, target digest,
terminal evidence digest, complete chain digest, outcome, terminal stage,
bounded predicate counts, and finalization time. It contains no entity IDs,
state values, response bodies, credentials, exception details, or other live
payloads.

Every load recomputes the decision through the public integrity-validating
observation loaders. Replays are credential-free and network-free. Tampering,
candidate or backup rebinding, conflicting downstream evidence, temporal
regression, duplicate records, and corrupt prerequisite evidence all fail
closed with sanitized errors.

## Recovery integration

Retrigger and later lifecycle operations must consult the finalization
authority rather than infer an outcome from individual rows. A failed exact
candidate remains blocked and cannot become a blind retry. Any explicit
administrative retry remains subject to the existing durable work-item
administration, validation, backup, observation, and rollback safeguards; this
decision record itself is never overwritten or bypassed.
