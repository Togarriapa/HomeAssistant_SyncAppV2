# Durable candidate static-validation checkpoint

Schema v31 turns the existing read-only candidate configuration validator into a
crash-safe pipeline gate. A planned record is persisted before validation and is
integrity-bound to the exact candidate orchestration plus Fetch/Stage, integrity,
dependency, and risk checkpoints.

The executor re-verifies the entire evidence chain and canonical validation result.
Success atomically advances only to `static_validated/validate_semantics`; it grants
no backup, Apply, restart, promotion, or rollback authority. A deterministic invalid
result atomically blocks the exact candidate work identity. Changed candidates receive
new identities, while an unchanged invalid SHA is not continuously retriggered.

Completed valid and invalid outcomes replay without credentials, transport, network
access, or mutation. Missing, duplicate, stale, malformed, rebound, or tampered
evidence fails closed. Transaction-time compare-and-swap checks prevent a stale worker
from finalizing over newer authority.

Retrigger discovers bounded eligible work, recovers interrupted claims, and executes
at most one static-validation action per cycle. Runtime evidence is deliberately
content-free: only checkpoint phases, valid/invalid counts, aggregate invalid and
unvalidated path totals, and the latest timestamp are published.
