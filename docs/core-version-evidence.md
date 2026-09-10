# Exact Home Assistant Core version evidence

This increment is derived only from the initial V2 `README.md` at root commit `71d284ce447d79b044e332c9bc01ae801dc91947`, specifically the runtime inventory and Home Assistant validation requirements.

Before SyncApp can trust a semantic candidate validator, it must know exactly which Home Assistant Core release is running. `ha_syncapp.core_version_evidence.bind_core_version()` extracts that value only from the already-collected `RuntimeInventoryInput.manifest.core_config.version`. The Core runtime collector populates `core_config` from the read-only Home Assistant Core `/api/config` response.

The evidence accepts only an exact stable calendar release such as `2026.9.1`. Missing values, whitespace-normalized guesses, `stable`, `latest`, prerelease/dev forms, invalid months, and other ambiguous version strings fail closed. SyncApp does not derive this value from its own image version or an external release lookup.

The resulting immutable evidence contains the exact Core version plus the canonical runtime SHA-256. Verification recomputes both, so any runtime change or version change invalidates previously captured version evidence.

This is **not** Home Assistant configuration validation and cannot authorize backup or Apply. It is a prerequisite for #148: a future semantic validator must prove that the Home Assistant validator used for the isolated Candidate Stage matches this exact running Core release.

No network call, Home Assistant write, service call, reload/restart, backup, Apply, observation, promotion, rejection, or rollback is performed by this evidence layer.
