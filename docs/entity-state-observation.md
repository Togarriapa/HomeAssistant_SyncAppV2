# Affected-entity state observation

The initial V2 README requires deployment observation to prove that affected
entities have valid states where applicable. This gate follows the exact
changed-resource availability proof and uses the same integrity-bound affected
entity target.

For a non-empty target, SyncApp makes one bounded authenticated, read-only Core
WebSocket `get_states` request. Every expected entity must still occur exactly
once and its state must be a non-empty string other than Home Assistant's exact
`unknown` or `unavailable` states. States of unrelated entities do not affect
the result. An empty target completes without credentials or network access.

Schema-v19 evidence stores only the prerequisite and target digests,
observation time, and expected/valid/invalid counts. Entity identifiers, state
values, attributes, credentials, response bodies, and exception text are never
persisted. Both valid and deterministic invalid-state outcomes replay without
network access so an unchanged deployment cannot enter a retry loop. Transport,
protocol, size, and persistence failures remain incomplete and fail closed.

The command contract follows Home Assistant Core:
<https://github.com/home-assistant/core/blob/949a484720ac3ebd2f474ce7e408a800c6c4ebdc/homeassistant/components/websocket_api/commands.py>.

This observer grants no Apply, restart, promotion, rollback, Supervisor, or Git
mutation authority and cannot bypass earlier validation, backup, or observation
gates.
