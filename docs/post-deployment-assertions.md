# Post-deployment assertions

The final ordered deployment-observation predicate in the initial V2 README is
implemented as a deliberately bounded, read-only MVP assertion gate.

## Authority and “where possible”

Schema version 1 supports one assertion kind: `entity_available`. The canonical
assertion plan is derived exclusively from every entity ID in the exact,
already-authorized affected-resource target. Callers cannot add identifiers,
URLs, commands, templates, service calls, code, or other assertion kinds.
Explicit candidate-declared assertions are not supported in this increment and
therefore cannot be ignored or used to expand observation authority.

“Where possible” means that an affected-resource target with no entities has an
applicable assertion set of zero. That case is persisted as an explicit,
successful zero-assertion result without credentials or network access. It is
not represented as fabricated evidence.

## Evaluation and failure classification

The evaluator requires the exact successful automation/script load proof for
the same deployment and target. A non-empty plan performs one bounded,
authenticated, read-only Core WebSocket `get_states` request. Each derived
entity must occur exactly once with a non-empty state other than `unknown` or
`unavailable`.

Missing, duplicate, malformed, unknown, or unavailable results are durable
deterministic assertion failures and are not retried unchanged. Authentication,
transport, timeout, response-envelope, response-size, and storage failures leave
the gate incomplete and retryable under the existing work backoff policy.

## Persistence and safety

Schema-v21 persistence contains only prerequisite, target, and canonical
assertion-set digests; observation time; and declared, derived, evaluated,
passed, failed, and skipped counts. Entity IDs, state values, attributes,
credentials, response bodies, and exception details are never stored.

Completed pass and deterministic-failure outcomes replay without credentials or
network access and reject deployment, target, prerequisite, or assertion-set
rebinding. This observer grants no Apply, restart, promotion, tagging, rollback,
Supervisor, Home Assistant, backup, or Git mutation authority.
