# Runtime inventory artifact

The initial V2 README defines Repo B's `runtime` branch as a generated, AI-friendly representation of the currently running Home Assistant system. It is strictly Home Assistant -> GitHub evidence and is **not a deployment source**.

`build_runtime_inventory()` is the transport-neutral staging boundary for that branch. Callers provide already-collected datasets explicitly. The builder does not call Home Assistant, Supervisor, GitHub, or hardware APIs and does not discover credentials or paths.

The artifact emits the README-defined JSON layout for `manifest.json`, `homeassistant/`, `supervisor/`, `hardware/`, `analysis/`, and optional `deployments/<commit-sha>.json` records. Known datasets omitted by a collector are represented explicitly with empty list/object defaults so an AI can distinguish a generated file from a missing file. Unknown dataset keys and unsafe deployment record names are rejected rather than becoming filesystem paths.

Every JSON document is validated and serialized canonically with stable key ordering. Non-string object keys, non-JSON values, non-finite numbers, excessive nesting, and unsafe deployment SHAs fail closed. Each staged file receives SHA-256 and size evidence; a SHA-256 artifact identifier binds the ordered complete file set.

`verify_runtime_inventory()` must be called immediately before a later Git publication primitive consumes the artifact. It rejects identifier/evidence tampering, modified/inserted/deleted files, symlinks, hardlinks, and special-file substitutions. Staging is isolated from the live Home Assistant tree, and incomplete newly-created artifacts are removed when construction or initial verification fails.

This milestone does **not** collect runtime data, publish the `runtime` branch, schedule synchronization, process remote candidates, or mutate Home Assistant. API collection, topology derivation, publication, durable Retrigger work, and retention are later guarded milestones built on this evidence boundary.
