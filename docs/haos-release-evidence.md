# Physical HAOS release evidence

This runbook defines the version 1 evidence document for the two physical release
gates tracked by [issue #212](https://github.com/Togarriapa/HomeAssistant_SyncAppV2/issues/212)
and [issue #251](https://github.com/Togarriapa/HomeAssistant_SyncAppV2/issues/251).
It is intentionally limited to Home Assistant OS on a Raspberry Pi 5 (`aarch64`).

The checker validates document shape, canonical identities, safety and integrity.
It cannot prove that an operator performed a check or that a reported result is
true. A passing document therefore does **not** close either physical gate by
itself. Preserve the original observation, have another person review how it was
collected, and attach the content-free checker summary to the applicable issues.

## Safety boundary

Use a dedicated test Home Assistant installation. Do not run these release checks
against a production home. Never put credentials, GitHub tokens, private repository
names, raw filesystem paths, raw API bodies, configuration content, logs or free-form
diagnostics in the evidence document. The contract rejects unknown fields so those
items cannot accidentally become durable release evidence.

The document may contain only the exact fields below. Use a new UUIDv4 for every
observation and record the exact UTC completion time. Record `false` for a failed
check; do not change the observation to make it pass.

```json
{
  "schema_version": 1,
  "observation_id": "123e4567-e89b-42d3-a456-426614174000",
  "observed_at": "2026-09-29T00:30:00Z",
  "build": {
    "syncapp_commit": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    "image_sha256": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
  },
  "platform": {
    "architecture": "aarch64",
    "hardware": "raspberry_pi_5",
    "haos_version": "17.0",
    "supervisor_version": "2026.09.2",
    "core_version": "2026.9.3"
  },
  "security": {
    "parent_apparmor_label": "local_homeassistant_syncapp_v2 (enforce)",
    "validator_apparmor_label": "local_homeassistant_syncapp_v2//validator (enforce)",
    "protection_mode_enabled": true,
    "homeassistant_mount_read_only": true,
    "undocumented_privileges_absent": true
  },
  "lifecycle": {
    "app_discoverable": true,
    "app_installed": true,
    "state_persisted_after_restart": true,
    "state_persisted_after_reboot": true,
    "retrigger_resumed_after_reboot": true,
    "single_state_owner_after_reboot": true,
    "sigterm_shutdown_clean": true,
    "invalid_options_rejected": true,
    "public_repository_rejected": true,
    "mismatched_repository_rejected": true
  },
  "validator": {
    "valid_candidate_accepted": true,
    "invalid_candidate_rejected": true,
    "warning_candidate_rejected": true,
    "live_config_read_denied": true,
    "durable_data_read_denied": true,
    "unrelated_tmp_read_denied": true,
    "ipv4_socket_denied": true,
    "ipv6_socket_denied": true,
    "alternate_executable_denied": true,
    "profile_enforced_after_restart": true,
    "missing_profile_failed_closed": true
  }
}
```

## Observation map

| Evidence field | Physical observation | Gate |
| --- | --- | --- |
| `build.*` | Resolve the exact installed SyncApp Git commit and built image digest before starting. | Both |
| `platform.*` | Read the installed HAOS, Supervisor and Core versions; confirm Raspberry Pi 5 and `aarch64`. | Both |
| `security.parent_apparmor_label` | Read the SyncApp process AppArmor label and confirm `(enforce)`. | #251 |
| `security.validator_apparmor_label` | Read the semantic-validator child label and confirm it is the exact parent profile plus `//validator (enforce)`. | #212 |
| `security.protection_mode_enabled` | Confirm Supervisor protection mode is enabled. | #251 |
| `security.homeassistant_mount_read_only` | Attempt a disposable write through the App's `/homeassistant` mount and observe denial. Never target a real configuration file. | #251 |
| `security.undocumented_privileges_absent` | Compare the installed App configuration with the committed manifest and confirm no extra privilege, device or host-network override. | #251 |
| `lifecycle.app_discoverable` | Add the repository and confirm the App appears in the Home Assistant store. | #251 |
| `lifecycle.app_installed` | Install and start the exact build successfully on the test host. | #251 |
| `lifecycle.state_persisted_after_restart` | Restart the App and confirm the same installation identity with an incremented boot count. | #251 |
| `lifecycle.state_persisted_after_reboot` | Reboot the host and confirm the same installation identity and retained durable state. | #251 |
| `lifecycle.retrigger_resumed_after_reboot` | After reboot, confirm bounded Retrigger scheduling resumes at the configured interval. | #251 |
| `lifecycle.single_state_owner_after_reboot` | Confirm only the supervised service owns the durable state lock after reboot. | #251 |
| `lifecycle.sigterm_shutdown_clean` | Stop the App and confirm the sanitized `service_stopped` event and clean persisted run state. | #251 |
| `lifecycle.invalid_options_rejected` | Supply a synthetic invalid option and confirm startup fails before state ownership. | #251 |
| `lifecycle.public_repository_rejected` | Configure a disposable public repository and confirm the trust boundary rejects it. | #251 |
| `lifecycle.mismatched_repository_rejected` | Use a disposable repository whose pinned identity does not match and confirm rejection. | #251 |
| `validator.valid_candidate_accepted` | Submit a synthetic valid candidate and observe successful isolated semantic validation. | #212 |
| `validator.invalid_candidate_rejected` | Submit a synthetic semantic error and observe rejection. | #212 |
| `validator.warning_candidate_rejected` | Submit a synthetic warning-only candidate and observe fail-closed rejection. | #212 |
| `validator.live_config_read_denied` | From the validator child, attempt to read the live configuration and observe AppArmor denial. | #212 |
| `validator.durable_data_read_denied` | From the validator child, attempt to read SyncApp durable data and observe denial. | #212 |
| `validator.unrelated_tmp_read_denied` | From the validator child, attempt to read an unrelated temporary file and observe denial. | #212 |
| `validator.ipv4_socket_denied` | Attempt IPv4 network access from the validator child and observe denial. | #212 |
| `validator.ipv6_socket_denied` | Attempt IPv6 network access from the validator child and observe denial. | #212 |
| `validator.alternate_executable_denied` | Attempt an executable outside the authorized validator path and observe denial. | #212 |
| `validator.profile_enforced_after_restart` | Restart the App, rerun validation, and confirm the same enforced child label. | #212 |
| `validator.missing_profile_failed_closed` | On a disposable test installation, make the child profile unavailable and confirm validation refuses to start. Restore the committed profile before continuing. | #212 |

Use only synthetic candidates and repositories. A denied probe is successful only
when the attempted operation itself fails and the expected enforced profile is
still active. A warning in a log without a failed operation is not sufficient.

## Validate and retain the result

Run the checker from a clean checkout of the exact SyncApp commit named by the
document:

```sh
PYTHONPATH=syncapp/src python -m ha_syncapp.haos_release_evidence evidence.json
```

The checker is read-only and emits a content-free JSON summary. Exit status `0`
means all 26 checks are `true`; status `1` means the document is well formed but at
least one observed check is `false`; status `2` means the file is unreadable,
malformed, unsafe, oversized or does not match this schema. A status `1` document
is valid failure evidence and must not be rewritten as a passing observation.

Store the original JSON in the approved private evidence location. Post only the
summary, exact SyncApp commit, reviewer conclusion and applicable issue links to
the public tracker. The `evidence_sha256` is computed from canonical JSON, so
formatting and object-key order do not change it. Any substantive field change
does. Re-run the complete physical procedure for a new build; do not carry a prior
observation forward.
