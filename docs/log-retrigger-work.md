# Exact log artifact recovery

The initial V2 README requires interrupted log synchronization to be retriggerable without substituting different work. Log publication recovery therefore starts from the exact immutable artifact already created under the protected SyncApp work root.

`load_log_artifact()` accepts only a configured absolute `0700` artifact root and a lowercase 64-character manifest SHA-256. It derives the only admissible path as `<artifact-root>/<artifact-id>`, reads the private manifest, proves that its SHA-256 is the requested artifact ID, reconstructs file evidence from that manifest, and then runs the full `verify_log_artifact()` check over the artifact before returning it.

The loader never accepts an arbitrary artifact path from durable work state. Missing artifacts, unsafe roots or modes, malformed manifests, identifier mismatches, changed payloads, inserted files, and other integrity failures are deterministic failures. A retrigger worker blocks that exact work item rather than recollecting logs or retrying the damaged artifact forever.

## Durable work identity

Logs work uses the dedicated `logs` work kind. Its bounded work key contains a SHA-256 binding for the case-insensitive Repo B target plus the exact artifact ID. The artifact ID can be recovered from the work key only after the target binding has been proven, so the retrigger path never scans for a replacement artifact and never consumes an arbitrary filesystem path from durable state.

Enqueue reverifies the artifact before persisting work and is idempotent through the existing `StateStore` work identity. Execution reconstructs and reverifies the artifact again immediately before guarded logs synchronization.

Outcomes follow the existing durable retry policy:

- `initialized`, `published`, and `no_change` complete the work item;
- `baseline_required`, `diverged`, and `remote_missing` are deterministic blocked outcomes;
- missing, tampered, malformed, or wrong-root exact artifacts are deterministic blocked outcomes;
- guarded GitHub/network/publication errors are transient and use the existing StateStore backoff and attempt cap.

The exact staged artifact is intentionally retained after processing. `log_sync` continues to remove only its temporary snapshot and Git workspace.

## Retrigger integration

`run_log_sync_retrigger_pass()` recovers interrupted durable work and processes at most one eligible `logs` item per invocation. `run_retrigger_cycle()` invokes this bounded lane after the existing outbound lanes when the service supplies the three protected log work roots. The service derives those roots from its existing app-owned work directory; no scheduler or cron timing is changed by this implementation.

This milestone does not collect logs, rewrite history, modify Home Assistant, perform candidate deployment, create backups, apply candidate bytes, or relax the initial README's requirement that candidate validation precede backup and Apply.
