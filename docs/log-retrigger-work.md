# Exact log artifact recovery

The initial V2 README requires interrupted log synchronization to be retriggerable without substituting different work. Log publication recovery therefore starts from the exact immutable artifact already created under the protected SyncApp work root.

`load_log_artifact()` accepts only a configured absolute `0700` artifact root and a lowercase 64-character manifest SHA-256. It derives the only admissible path as `<artifact-root>/<artifact-id>`, reads the private manifest, proves that its SHA-256 is the requested artifact ID, reconstructs file evidence from that manifest, and then runs the full `verify_log_artifact()` check over the artifact before returning it.

The loader never accepts an arbitrary artifact path from durable work state. Missing artifacts, unsafe roots or modes, malformed manifests, identifier mismatches, changed payloads, inserted files, and other integrity failures are deterministic failures. A retrigger worker must block that exact work item rather than recollecting logs or retrying the damaged artifact forever.

This primitive does not collect logs, invoke Git, publish a branch, modify Home Assistant, perform candidate validation/deployment, or change scheduler configuration. Temporary Git and snapshot layers remain the responsibility of the guarded logs publication transaction.
