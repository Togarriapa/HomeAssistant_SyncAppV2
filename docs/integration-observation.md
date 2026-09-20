# Integration initialization observation

SyncApp treats integration initialization as a separate, read-only deployment
observation gate. It runs only after the exact deployment has a completed Core
health window and a valid Supervisor health proof.

The observer opens one bounded, authenticated Core WebSocket session and sends
only `config_entries/get`. Every entry whose `disabled_by` value is `null` must
report the exact state `loaded`. Explicitly disabled entries are counted but do
not need to be loaded. Duplicate identities, missing fields, malformed protocol
messages, unsuccessful responses, unsupported values, oversized messages, and
transport failures all fail closed.

Home Assistant Core registers `config_entries/get` as a read-only WebSocket
command and returns each config entry's API fragment, including `entry_id`,
`state`, and `disabled_by`. The implementation is pinned for review to
[Home Assistant Core commit `949a484`](https://github.com/home-assistant/core/blob/949a484720ac3ebd2f474ce7e408a800c6c4ebdc/homeassistant/components/config/config_entries.py#L657-L676).

## Durable authority and privacy

Schema v16 stores only the deployment identifier, the exact Supervisor-health
record digest, observation time, total entry count, disabled entry count, and
an integrity digest. It never stores entry identifiers, domains, titles,
failure reasons, response bodies, access tokens, or exception text.

The record is written only after the complete response has passed validation.
Inside the same immediate transaction, SyncApp re-proves the Supervisor-health
binding and temporal ordering. A valid completed record is replayed without a
credential or network session. Corrupt, rebound, or temporally impossible
evidence fails closed and cannot authorize a later deployment gate.

This gate is observational only. It cannot enable an integration, retry setup,
change configuration, restart Core, or mutate a Git repository.
