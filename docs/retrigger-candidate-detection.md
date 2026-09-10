# Retrigger Candidate Detection

The sole product specification for this capability is the initial V2 `README.md` at root commit `71d284ce447d79b044e332c9bc01ae801dc91947`.

The README requires the Retrigger Work Cron Job to identify unprocessed Candidate commits while remaining a recovery mechanism rather than the primary scheduler.

After the existing Local, Recorder database, Runtime and pending Logs recovery attempts, the bounded Retrigger cycle now re-observes the exact Repo B `candidate` branch through the existing repository-ID-bound candidate detector. If the branch is absent, detection succeeds with no work. If present, only the immutable trusted head SHA is durably enqueued as `candidate` work.

Candidate detection is idempotent. Re-observing the same SHA does not create duplicate work and does not rearm an identical candidate that has already reached `succeeded` or deterministic `blocked` state. A different trusted SHA receives its own durable work identity.

Repository verification and transport failures fail the Retrigger cycle closed with sanitized diagnostics. GitHub credentials are not persisted in candidate work identities or surfaced through the cycle error.

This boundary performs detection and durable enqueue only. It does not Fetch or Stage Candidate bytes and it does not perform integrity validation, dependency analysis, risk classification, Home Assistant semantic validation, backup, Apply, reload/restart, observation, promotion or rollback. Those later phases remain separate safety gates from the initial README.

Fresh log collection remains after recovery and Candidate detection so newly collected diagnostics cannot displace already pending recovery work.
