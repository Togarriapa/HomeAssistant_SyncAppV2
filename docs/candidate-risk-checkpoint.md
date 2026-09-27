# Durable candidate impact and risk checkpoint

This schema-v30 increment implements the next controlled candidate gate from the
initial V2 README: impact expansion and deployment-risk classification after
dependency analysis and before static/configuration validation.

The executor consumes only the canonical runtime and dependency evidence in the
completed schema-v29 checkpoint. It does not recollect runtime inventory or
contact Home Assistant. Before classification it persists a `planned` record
bound to the exact candidate orchestration and dependency checkpoint digests.
Impact and risk are then deterministically recomputed and verified before their
canonical JSON, risk level, affected-entity count and integrity digest are
committed atomically with the `risk_classified/validate` orchestration successor.

Completed replay loads and verifies the exact persisted chain without
credentials, network traffic, external transport or mutation. Missing,
duplicate, corrupt, stale or rebound evidence fails closed. Retrigger can claim
at most one eligible `classify_risk` action per cycle; deterministic evidence
failures block the exact candidate rather than creating a retry loop.

Runtime diagnostics expose only aggregate planned/completed counts, risk-level
counts, affected-entity totals and the latest timestamp. Repository identity,
candidate SHA, paths, reasons, configuration contents and credentials remain
excluded.

This checkpoint grants no deployment authority. Static and exact-version Home
Assistant validation remain the next required gate, followed by recoverable
backup, Apply, activation, observation, finalization and promotion or rollback.
No live Home Assistant or Supervisor state is modified by this capability.
