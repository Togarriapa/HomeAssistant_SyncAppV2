# Automation and script load observation

The initial V2 README requires deployment observation to prove that relevant
automations and scripts load. SyncApp derives the relevant set exclusively from
the exact affected-resource target: only `automation.*` and `script.*` entity
IDs are included, so callers cannot supply unrelated resources.

This gate requires the preceding affected-entity state proof to be successful.
For a non-empty relevant set it performs one bounded, authenticated, read-only
Core WebSocket `get_states` request. Each expected entity must occur exactly
once with exact state `on` or `off`. Other states are durably classified as a
deterministic load failure. Missing or malformed entities remain an incomplete,
retryable observation because the snapshot itself could not prove the result.

Schema-v20 evidence stores prerequisite and target digests, observation time,
and expected/loaded/failed counts only. It excludes identifiers, states,
attributes, credentials, response bodies, and exception text. Empty sets and
both completed outcomes replay without credentials or network access.

The command contract follows Home Assistant Core:
<https://github.com/home-assistant/core/blob/949a484720ac3ebd2f474ce7e408a800c6c4ebdc/homeassistant/components/websocket_api/commands.py>.

This observer grants no Apply, restart, promotion, rollback, Supervisor, or Git
mutation authority and cannot bypass any earlier deployment safeguard.
