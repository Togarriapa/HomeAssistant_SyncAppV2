# Epic 1 — recurring Retrigger recovery scheduler

Parent: Epic #1.

Specification source: initial V2 root README at `71d284ce447d79b044e332c9bc01ae801dc91947`.

This increment closes the missing activation gap between the existing durable Retrigger work state machine / bounded recovery lanes and an actually recurring recovery mechanism.

Acceptance criteria:

- configurable bounded recovery interval with no disable switch;
- scheduler remains recovery-only and separate from normal synchronization producers;
- a cadence-only launcher never opens StateStore and dispatches through the existing protected same-owner IPC boundary;
- the child service remains the sole StateStore/process-lock owner and executes each bounded recovery cycle synchronously, preventing overlapping state mutation;
- elapsed intervals coalesce instead of creating catch-up queues;
- scheduler setup fails closed when Repo B is configured, so the App cannot silently run with required automatic recovery disabled;
- existing durable retry/backoff and blocked deterministic failure semantics remain authoritative;
- optional Recorder configuration skips only the database lane rather than disabling recovery for other lanes;
- service shutdown disarms scheduling and terminates the state-owning child cleanly;
- candidate work cannot bypass validation, backup, controlled Apply, observation or rollback safeguards;
- tests cover cadence boundaries, coalescing, dispatch failures, shutdown, invalid configuration, optional Recorder recovery and launcher activation;
- full CI must be green on the exact reviewed head before merge.
