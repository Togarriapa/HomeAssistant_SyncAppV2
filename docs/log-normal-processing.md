# Normal Logs Processing

The sole product specification for this capability is the initial V2 `README.md` at root commit `71d284ce447d79b044e332c9bc01ae801dc91947`.

The README defines a dedicated Home Assistant → GitHub `logs` branch and separately defines Retrigger as recovery for failed log synchronization. Normal log publication therefore has its own bounded processing boundary.

`log_sync_process.run_log_sync_process()` claims at most one eligible durable `logs` item. Before loading or publishing anything, it proves that the exact-artifact work key belongs to the configured Repo B target. Malformed or foreign work identities are deterministic failures and are blocked.

For a valid work item, the processor delegates to the existing guarded exact-artifact log publication pipeline. The artifact is reloaded and verified from protected SyncApp storage and all Git work remains in isolated snapshot/workspace roots. Transient publication failures retain the existing bounded retry behavior; deterministic failures remain blocked.

`log_sync_retrigger.run_log_sync_retrigger_pass()` remains the recovery adapter. It first recovers interrupted durable work and then delegates one bounded attempt to the normal processor. The normal processor itself never performs interrupted-work recovery or administrative retry.

This slice does not invent a log collection cadence and does not implement Git-history retention pruning. Those are separate requirements from the initial README, including its 30-day logs retention objective. It also does not deploy Candidate bytes, write the live Home Assistant configuration tree, or bypass validation, backup, Apply, observation, promotion, or rollback safeguards.
