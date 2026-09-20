# Changed-resource availability observation

The initial V2 README requires deployment observation to prove that changed
resources are available after Home Assistant restarts. SyncApp represents this
gate with the exact affected entity set derived from reverified candidate
dependency, impact and risk evidence.

The observer runs only after the same deployment has durable, clear startup-log
evidence. It authenticates to the Core WebSocket API and issues one bounded,
read-only `get_states` command. Every expected entity identifier must occur
exactly once in the returned state-machine snapshot. This gate checks presence
only; state quality (`unknown` or `unavailable`) is the next, separate README
gate. An empty affected set completes without a credential or network request.

Durable schema-v18 evidence contains the deployment binding, prerequisite and
target digests, observation time, and aggregate expected/available counts. It
does not store entity identifiers, states, attributes, credentials, response
bodies, or exception text. Successful replay is network- and credential-free.
Changed candidate bindings, malformed or duplicate states, missing resources,
oversized messages, tampering, and persistence failures fail closed.

The implementation follows Home Assistant Core's authenticated WebSocket
`get_states` contract:
<https://github.com/home-assistant/core/blob/949a484720ac3ebd2f474ce7e408a800c6c4ebdc/homeassistant/components/websocket_api/commands.py>.

This observation grants no Apply, restart, promotion, rollback, Supervisor, or
Git mutation authority and does not bypass any earlier validation or backup
safeguard.
