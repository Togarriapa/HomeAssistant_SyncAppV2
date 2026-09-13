# Epic 1 — recurring Retrigger recovery scheduler

Parent: Epic #1.

Specification source: initial V2 root README at `71d284ce447d79b044e332c9bc01ae801dc91947`.

This increment closes the missing activation gap between the existing durable Retrigger work state machine / bounded recovery lanes and an actually recurring recovery mechanism.

Acceptance criteria:

- configurable bounded recovery interval with no disable switch;
- scheduler remains recovery-only and separate from normal synchronization producers;
- one StateStore-owning service thread executes recovery, preventing overlapping recovery cycles;
- elapsed intervals coalesce instead of creating catch-up queues;
- existing durable retry/backoff and blocked deterministic failure semantics remain authoritative;
- service shutdown disarms scheduling before state ownership is released;
- candidate work cannot bypass validation, backup, controlled Apply, observation or rollback safeguards;
- tests cover cadence boundaries, coalescing, shutdown, invalid configuration and service activation;
- full CI must be green before merge.
